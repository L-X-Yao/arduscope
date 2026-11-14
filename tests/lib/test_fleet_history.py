"""
Fast tests for the fleet history archive (lib/fleet_history.py).
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
from types import SimpleNamespace

import numpy as np
import pytest

import fleet_report
from analyzers import discover_analyzers
from lib import fleet_history
from lib.i18n import t
from lib.log_reader import TextMessage
from lib.thresholds import Thresholds
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

BANNER = "Acme-H743 001A002B 3030510C 31323334"
SERIAL = "001A002B-3030510C-31323334"


def _reader(with_banner=True, gwk=2366.0):
    n = 100
    ts = np.linspace(0.0, 100.0, n)
    reader = SyntheticReader(messages={
        "GPS": {"_timestamp": ts,
                "GWk": np.full(n, gwk), "GMS": np.full(n, 0.0)},
        "VIBE": {"_timestamp": ts, "IMU": np.zeros(n),
                 "VibeX": np.full(n, 12.5), "VibeY": np.full(n, 8.0),
                 "VibeZ": np.full(n, 9.0)},
        "BAT": {"_timestamp": ts, "Inst": np.zeros(n),
                "Volt": np.linspace(25.2, 22.8, n)},
        "RATE": {"_timestamp": ts,
                 "ROut": np.full(n, 0.05), "POut": np.full(n, 0.04),
                 "YOut": np.full(n, 0.03)},
    })
    if with_banner:
        reader.text_messages = [
            TextMessage(timestamp=0.5, text="ArduPlane V4.6.3 (f5790b76)"),
            TextMessage(timestamp=0.6, text=BANNER),
        ]
    return reader


def _pipeline(reader, score=87):
    """A full standard run, which is what record() accepts."""
    return SimpleNamespace(
        reader=reader, results=[], health_score=score, vehicle_info=None,
        thresholds=Thresholds.from_params(
            reader.params, reader.canonical_vehicle_type),
        results_by_key={key: None for key, _ in discover_analyzers()},
    )


def _logfile(tmp_path, name="a.BIN", payload=b"log-bytes-1"):
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


class TestExtraction:

    def test_identity_from_banner(self):
        serial, board = fleet_history.extract_identity(_reader())
        assert serial == SERIAL
        assert board == "Acme-H743"

    def test_identity_absent(self):
        serial, board = fleet_history.extract_identity(
            _reader(with_banner=False))
        assert serial is None and board is None

    def test_flight_date_gps_epoch(self):
        # GWk=0, GMS=0 is exactly the GPS epoch.
        r = _reader(gwk=0.0)
        r._messages["GPS"]["GWk"] = np.zeros(100)
        # precondition: all-zero week is treated as "no fix" -> ""
        assert fleet_history.extract_flight_date(r) == ""
        r2 = _reader(gwk=1.0)
        assert fleet_history.extract_flight_date(r2) == "1980-01-13 00:00"

    def test_metrics_content(self):
        m = fleet_history.extract_metrics(_pipeline(_reader()))
        assert m["vibe_max_x"] == 12.5
        assert m["batt_v_min"] == 22.8
        assert m["rate_rms_r"] == pytest.approx(0.05, abs=1e-3)


class TestBatteryMinimum:
    """The fleet's real logs end with the rail decaying after power-off."""

    def _reader(self, armed):
        n = 400
        ts = np.linspace(0.0, 100.0, n)
        volt = np.where(ts < 90.0, np.linspace(50.0, 46.0, n), 17.0)
        reader = SyntheticReader(messages={
            "BAT": {"_timestamp": ts, "Inst": np.zeros(n), "Volt": volt},
        })
        reader._armed = armed
        return reader

    def test_power_off_decay_is_not_the_battery(self):
        reader = self._reader([(5.0, 85.0)])
        assert reader._messages["BAT"]["Volt"].min() == 17.0  # precondition
        metrics = fleet_history.extract_metrics(_pipeline(reader))
        assert metrics["batt_v_min"] == pytest.approx(46.6, abs=0.1)
        assert metrics["batt_v_start"] == pytest.approx(50.0)

    def test_no_armed_window_gives_no_minimum(self):
        metrics = fleet_history.extract_metrics(_pipeline(self._reader([])))
        assert metrics["batt_v_start"] == pytest.approx(50.0)
        assert metrics.get("batt_v_min") is None


def _archive(db, tmp_path, name, date, score=80):
    """Archive one flight on a chosen GPS date ('' for no GPS fix)."""
    reader = _reader()
    if date:
        when = datetime.datetime.strptime(date, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc)
        weeks = (when - fleet_history._GPS_EPOCH).total_seconds() / 604800
        reader._messages["GPS"]["GWk"] = np.full(100, float(int(weeks)))
        reader._messages["GPS"]["GMS"] = np.full(
            100, (weeks - int(weeks)) * 604800e3)
    else:
        reader._messages["GPS"]["GWk"] = np.zeros(100)
    fleet_history.record(db, _pipeline(reader, score=score),
                         _logfile(tmp_path, name, name.encode()))


class TestUndatedFlights:
    """A flight with no GPS fix has no date, but it did happen."""

    def test_newest_undated_flight_reaches_its_own_card(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        for day in range(1, 26):
            _archive(db, tmp_path, f"d{day}.BIN", f"2026-06-{day:02d}")
        _archive(db, tmp_path, "indoor.BIN", "")
        rows = fleet_history.query_history(db, SERIAL)
        assert len(rows) == 20                       # precondition: the cap
        assert rows[-1]["log_name"] == "indoor.BIN"

    def test_latest_score_is_the_latest_flight(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        _archive(db, tmp_path, "dated.BIN", "2026-06-01", score=90)
        _archive(db, tmp_path, "indoor.BIN", "", score=11)
        assert fleet_history.list_airframes(db)[0]["last_score"] == 11

    def test_undated_flight_stays_where_it_was_archived(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        _archive(db, tmp_path, "a.BIN", "2026-06-01")
        _archive(db, tmp_path, "indoor.BIN", "")
        _archive(db, tmp_path, "b.BIN", "2026-06-05")
        names = [r["log_name"]
                 for r in fleet_history.query_history(db, SERIAL)]
        assert names == ["a.BIN", "indoor.BIN", "b.BIN"]

    def test_dated_flights_still_sort_by_date(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        _archive(db, tmp_path, "late.BIN", "2026-06-05")
        _archive(db, tmp_path, "early.BIN", "2026-06-01")
        names = [r["log_name"]
                 for r in fleet_history.query_history(db, SERIAL)]
        assert names == ["early.BIN", "late.BIN"]

    def test_undated_flights_of_another_airframe_do_not_count(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        _archive(db, tmp_path, "indoor.BIN", "")
        other = _reader()
        other.text_messages = [TextMessage(
            timestamp=0.6, text="Acme-H743 002C003D 3030510E 35363738")]
        fleet_history.record(db, _pipeline(other),
                             _logfile(tmp_path, "other.BIN", b"other"))
        _archive(db, tmp_path, "dated.BIN", "2026-06-05")
        names = [r["log_name"]
                 for r in fleet_history.query_history(db, SERIAL)]
        assert names == ["indoor.BIN", "dated.BIN"]


OTHER_SERIAL = "002C003D-3030510E-35363738"


def _two_airframes(tmp_path):
    """SERIAL lists first: it has the later flight."""
    db = str(tmp_path / "fleet.db")
    _archive(db, tmp_path, "a.BIN", "2026-06-05")
    other = _reader()
    other.text_messages = [TextMessage(
        timestamp=0.6, text="Acme-H743 002C003D 3030510E 35363738")]
    fleet_history.record(db, _pipeline(other),
                         _logfile(tmp_path, "b.BIN", b"b"))
    assert fleet_history.list_airframes(db)[0]["serial"] == SERIAL
    return db


class TestAliases:
    def test_alias_may_not_be_another_serial(self, tmp_path):
        db = _two_airframes(tmp_path)
        with pytest.raises(fleet_history.AliasTaken):
            fleet_history.set_alias(db, SERIAL, OTHER_SERIAL)
        assert fleet_history.resolve_serial(db, OTHER_SERIAL) == OTHER_SERIAL

    def test_alias_may_not_be_another_airframes_alias(self, tmp_path):
        db = _two_airframes(tmp_path)
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        with pytest.raises(fleet_history.AliasTaken):
            fleet_history.set_alias(db, OTHER_SERIAL, "3号机")
        assert fleet_history.resolve_serial(db, "3号机") == SERIAL

    def test_airframe_can_keep_or_clear_its_own_alias(self, tmp_path):
        db = _two_airframes(tmp_path)
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        assert fleet_history.set_alias(db, SERIAL, "")
        assert fleet_history.set_alias(db, OTHER_SERIAL, "")

    def _force_alias(self, db, serial, alias):
        """An alias already in a database from before the checks."""
        conn = sqlite3.connect(db)
        with conn:
            conn.execute("UPDATE airframes SET alias=? WHERE serial=?",
                         (alias, serial))
        conn.close()

    def test_real_serial_wins_over_an_old_colliding_alias(self, tmp_path):
        db = _two_airframes(tmp_path)
        self._force_alias(db, SERIAL, OTHER_SERIAL)
        assert fleet_history.resolve_serial(db, OTHER_SERIAL) == OTHER_SERIAL

    def test_old_duplicate_alias_is_not_guessed(self, tmp_path):
        db = _two_airframes(tmp_path)
        self._force_alias(db, SERIAL, "3号机")
        self._force_alias(db, OTHER_SERIAL, "3号机")
        assert fleet_history.resolve_serial(db, "3号机") is None

    def test_cli_states_the_conflict(self, tmp_path, monkeypatch):
        db = _two_airframes(tmp_path)
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        monkeypatch.setattr(sys, "argv", [
            "fleet_report.py", "--db", db,
            "--set-alias", OTHER_SERIAL, "3号机"])
        with pytest.raises(SystemExit) as stopped:
            fleet_report.main()
        assert str(stopped.value) == t("fleet.alias_taken", alias="3号机",
                                       serial=SERIAL)

    def test_alias_and_prefix_still_resolve(self, tmp_path):
        db = _two_airframes(tmp_path)
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        assert fleet_history.resolve_serial(db, "3号机") == SERIAL
        assert fleet_history.resolve_serial(db, "002C") == OTHER_SERIAL


def _rows_on(dates):
    return [{"log_name": f"{i}.bin", "flight_date": date, "duration_s": 60.0,
             "health_score": 90 - i, "fw_version": "V4.6.3",
             "metrics": {"vibe_max_x": 5.0 + i}}
            for i, date in enumerate(dates)]


class TestTrendFigure:
    """Some boards log several short flights inside one minute."""

    def test_flights_in_one_minute_keep_their_own_points(self):
        dates = ["2026-07-14 08:49"] * 4
        figure = fleet_history._trend_figure(_rows_on(dates))
        for trace in figure.data:
            assert len(set(trace.x)) == 4
        assert list(figure.layout.xaxis.ticktext) == dates

    def test_undated_flight_is_labelled_by_its_log(self):
        figure = fleet_history._trend_figure(_rows_on(["", "2026-07-14 08:49"]))
        assert list(figure.layout.xaxis.ticktext) == ["0.bin",
                                                      "2026-07-14 08:49"]

    def test_hover_names_the_log(self):
        figure = fleet_history._trend_figure(_rows_on(["2026-07-14 08:49"] * 2))
        assert list(figure.data[0].text) == ["0.bin", "1.bin"]


class TestTrendPage:
    def test_log_names_are_escaped(self, tmp_path):
        rows = _rows_on(["2026-07-14 08:49"] * 2)
        rows[0]["log_name"] = "<img src=x onerror=alert(1)>.BIN"
        page = tmp_path / "trend.html"
        fleet_report._render_html(rows, SERIAL, str(page))
        html = page.read_text(encoding="utf-8")
        details = html[html.rindex("<ul>"):]
        assert "<img" not in details
        assert "&lt;img" in details


class TestRecordAndQuery:

    def test_record_dedup_by_sha1(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        log = _logfile(tmp_path)
        serial, n1 = fleet_history.record(db, _pipeline(_reader()), log)
        assert serial == SERIAL and n1 == 1
        _, n2 = fleet_history.record(db, _pipeline(_reader()), log)
        assert n2 == 1                      # same file -> no new row
        log2 = _logfile(tmp_path, "b.BIN", b"log-bytes-2")
        _, n3 = fleet_history.record(db, _pipeline(_reader()), log2)
        assert n3 == 2

    def test_unknown_serial_still_archived(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        log = _logfile(tmp_path)
        serial, n = fleet_history.record(
            db, _pipeline(_reader(with_banner=False)), log)
        assert serial == fleet_history.UNKNOWN_SERIAL
        assert n == 1

    def test_history_result_thresholds(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        log1 = _logfile(tmp_path, "a.BIN", b"one")
        fleet_history.record(db, _pipeline(_reader(), score=90), log1)
        rows = fleet_history.query_history(db, SERIAL)
        assert fleet_history.build_history_result(rows, SERIAL) is None
        log2 = _logfile(tmp_path, "b.BIN", b"two")
        fleet_history.record(db, _pipeline(_reader(), score=80), log2)
        rows = fleet_history.query_history(db, SERIAL)
        assert len(rows) == 2               # precondition
        result = fleet_history.build_history_result(rows, SERIAL)
        assert result is not None
        assert result.status == "info"
        assert result.action_level == "ok"  # never a verdict
        assert len(result.details) == 2
        assert result.figures

    def test_alias_and_listing(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        fleet_history.record(db, _pipeline(_reader()),
                             _logfile(tmp_path))
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        frames = fleet_history.list_airframes(db)
        assert frames[0]["serial"] == SERIAL
        assert frames[0]["alias"] == "3号机"
        assert frames[0]["n_flights"] == 1
        assert not fleet_history.set_alias(db, "NO-SUCH", "x")

    def test_listing_reports_latest_score(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        fleet_history.record(db, _pipeline(_reader(), score=90),
                             _logfile(tmp_path, "a.BIN", b"one"))
        fleet_history.record(db, _pipeline(_reader(), score=80),
                             _logfile(tmp_path, "b.BIN", b"two"))
        frames = fleet_history.list_airframes(db)
        assert len(frames) == 1             # precondition: same airframe
        assert frames[0]["last_score"] == 80

    def test_resolve_serial(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        fleet_history.record(db, _pipeline(_reader()),
                             _logfile(tmp_path))
        fleet_history.set_alias(db, SERIAL, "3号机")
        assert fleet_history.resolve_serial(db, SERIAL) == SERIAL
        assert fleet_history.resolve_serial(db, "001A002B") == SERIAL
        assert fleet_history.resolve_serial(db, "3号机") == SERIAL
        assert fleet_history.resolve_serial(db, "no-such") is None


class TestBackfill:
    """backfill(): walk sources, analyze new logs, skip archived, isolate
    failures."""

    def test_records_walks_and_isolates_failures(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        logs = tmp_path / "logs"
        (logs / "sub").mkdir(parents=True)
        _logfile(logs, "a.BIN", b"one")
        _logfile(logs / "sub", "b.bin", b"two")     # lowercase ext + subdir
        _logfile(logs, "bad.BIN", b"broken")
        (logs / "notes.txt").write_text("not a log")

        def analyze(path):
            if "bad" in path:
                raise ValueError("boom")
            return _pipeline(_reader())

        events = []
        stats = fleet_history.backfill(
            db, [str(logs)], analyze=analyze,
            progress=lambda i, n, p, ev, d: events.append(ev))
        assert stats == {"total": 3, "recorded": 2,
                         "skipped": 0, "failed": 1}
        assert events.count("record") == 2 and events.count("fail") == 1
        assert len(fleet_history.query_history(db, SERIAL)) == 2

    def test_rerun_skips_archived_without_reanalysis(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        logs = tmp_path / "logs"
        logs.mkdir()
        _logfile(logs, "a.BIN", b"one")
        _logfile(logs, "b.BIN", b"two")
        fleet_history.backfill(db, [str(logs)],
                               analyze=lambda p: _pipeline(_reader()))
        # precondition: both archived
        assert len(fleet_history.query_history(db, SERIAL)) == 2

        analyzed = []

        def analyze(path):
            analyzed.append(path)
            return _pipeline(_reader())

        stats = fleet_history.backfill(db, [str(logs)], analyze=analyze)
        assert stats == {"total": 2, "recorded": 0,
                         "skipped": 2, "failed": 0}
        assert analyzed == []               # dedup happened BEFORE analysis

    def test_accepts_single_file_source(self, tmp_path):
        db = str(tmp_path / "fleet.db")
        log = _logfile(tmp_path, "solo.BIN", b"solo")
        stats = fleet_history.backfill(
            db, [log], analyze=lambda p: _pipeline(_reader()))
        assert stats["recorded"] == 1 and stats["total"] == 1
