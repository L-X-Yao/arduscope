"""Parser and pipeline on synthetic .BIN files.

Real logs are not in the repository, so these are the only tests that
reach pymavlink, LogReader and run_analysis in CI.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import textwrap

import numpy as np
import pytest

import binlog
from analyzers.notch_tuning import NotchTuningAnalyzer
from lib import log_reader as log_reader_module
from lib.log_reader import LogReader
from lib.pipeline import run_analysis
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def copter_log(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("bin") / "copter.bin"
    return binlog.flight_log("copter").save(path)


@pytest.fixture(scope="module")
def quadplane_log(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("bin") / "quadplane.bin"
    return binlog.flight_log("quadplane").save(path)


class TestParsing:
    def test_metadata(self, copter_log):
        reader = LogReader(copter_log)
        assert reader.vehicle_type == "ArduCopter"
        assert reader.firmware_version == "V4.6.3"
        assert reader.params["MOT_SPIN_MIN"] == pytest.approx(0.15)
        assert [e.event_id for e in reader.events] == [10, 11]
        assert reader.mode_list[0].mode_number == 5
        assert reader.get_flight_duration() == pytest.approx(40.0, abs=0.1)
        assert reader.get_armed_intervals()

    def test_preloaded_values(self, copter_log):
        reader = LogReader(copter_log, preload_types=["ATT", "BAT"])
        att = reader.get_messages_as_arrays("ATT", ["Yaw", "DesYaw"])
        assert att["Yaw"].dtype == np.float64
        assert np.nanmean(att["DesYaw"]) == pytest.approx(90.0)
        assert "ATT" in reader._fully_cached

    def test_query_without_preload_reads_the_file(self, copter_log):
        reader = LogReader(copter_log)
        vibe = reader.get_messages_as_arrays("VIBE", ["VibeX"])
        assert np.nanmean(vibe["VibeX"]) == pytest.approx(5.0)

    def test_missing_field_is_nan(self, copter_log):
        reader = LogReader(copter_log, preload_types=["VIBE"])
        data = reader.get_messages_as_arrays("VIBE", ["NoSuchField"])
        assert len(data["NoSuchField"]) > 0
        assert np.all(np.isnan(data["NoSuchField"]))

    def test_field_alias(self, copter_log):
        reader = LogReader(copter_log, preload_types=["BAT"])
        data = reader.get_messages_as_arrays("BAT", ["Rem"])
        assert np.nanmax(data["Rem"]) == 90

    def test_preload_all_matches_prescan_preload(self, copter_log):
        prescan = LogReader(copter_log, preload_types=["IMU", "XKF4"])
        later = LogReader(copter_log)
        later.preload_all(["IMU", "XKF4"])
        for msg in ("IMU", "XKF4"):
            for field, values in prescan._msg_cache[msg].items():
                np.testing.assert_array_equal(
                    values, later._msg_cache[msg][field])

    def test_non_numeric_field_is_nan_not_an_error(self, copter_log):
        reader = LogReader(copter_log, preload_types=["MCU"])
        reader.preload_all(["MCU"])
        assert np.nanmean(
            reader.get_messages_as_arrays("MCU", ["MVolt"])["MVolt"]
        ) == pytest.approx(3.3)


class TestBatchSamples:
    def test_collected_in_the_first_pass(self, copter_log):
        reader = LogReader(copter_log)
        assert len(reader.isb_batches) == 6
        first = reader.isb_batches[0]
        assert first["type"] == 1
        assert first["smp_rate"] == pytest.approx(1000.0)
        assert len(first["x"]) == first["smp_cnt"] == 512

    def test_notch_finds_the_tone_without_rereading_the_log(
            self, copter_log, monkeypatch):
        """With the messages it declares loaded, as the pipeline loads
        them, the spectrum needs nothing else from the file."""
        reader = LogReader(copter_log, preload_types=(
            NotchTuningAnalyzer.required_messages
            + NotchTuningAnalyzer.optional_messages))

        def forbid(*args, **kwargs):
            raise AssertionError("second pass over the log")

        monkeypatch.setattr(log_reader_module.mavutil,
                            "mavlink_connection", forbid)
        vehicle = detect_vehicle(reader)
        thresholds = Thresholds.from_params(
            reader.params, reader.canonical_vehicle_type)
        analyzer = NotchTuningAnalyzer(reader, thresholds, vehicle)
        analyzer._armed_intervals = reader.get_armed_intervals()
        peak, _, _ = analyzer._fft_from_batch()
        assert peak == pytest.approx(120.0, abs=3.0)


class TestHandles:
    @pytest.fixture
    def opened(self, monkeypatch):
        seen = []
        real = log_reader_module.mavutil.mavlink_connection

        def spy(*args, **kwargs):
            seen.append(real(*args, **kwargs))
            return seen[-1]

        monkeypatch.setattr(log_reader_module.mavutil,
                            "mavlink_connection", spy)
        return seen

    def test_every_read_path_closes_the_file(self, copter_log, opened):
        reader = LogReader(copter_log)
        reader.get_messages_as_arrays("VIBE", ["VibeX"])
        reader.preload_all(["IMU"])
        assert len(opened) >= 3
        assert all(m.filehandle.closed for m in opened)

    def test_mode_fallback_closes_the_file(self, tmp_path, opened):
        log = binlog.BinLog()
        log.write("MSG", 1000, "ArduCopter V4.6.3 (abc12345)")
        log.write("ATT", 2000, 0, 0, 0, 0, 0, 0, 0)
        LogReader(log.save(tmp_path / "no_mode.bin"))
        assert len(opened) == 2
        assert all(m.filehandle.closed for m in opened)

    def test_closed_on_error_during_the_scan(self, copter_log, opened,
                                             monkeypatch):
        def boom(self, *args, **kwargs):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(LogReader, "_scan", boom)
        with pytest.raises(RuntimeError):
            LogReader(copter_log)
        assert opened and opened[0].filehandle.closed


class TestPymavlinkKeepsNoReader:
    """mavutil keeps the last connection in a module global. For a log that
    is its index, an offset for every message, and it stayed until the next
    log's index had been built beside it: a web server peaked at 5.2 GB on
    its third 813 MB log, against 3.5 GB on its first."""

    def test_reader_is_let_go_when_the_read_ends(self, copter_log):
        mavutil = log_reader_module.mavutil
        with log_reader_module._open_log(copter_log) as mlog:
            assert mavutil.mavfile_global is mlog  # precondition

        assert mavutil.mavfile_global is None

    def test_no_reader_is_left_after_a_log_is_loaded(self, copter_log):
        reader = LogReader(copter_log)
        assert log_reader_module.mavutil.mavfile_global is None

        reader.get_messages_as_arrays("ATT", ["Roll"])

        assert log_reader_module.mavutil.mavfile_global is None

    def test_a_connection_that_is_not_ours_is_left_alone(self, copter_log,
                                                         monkeypatch):
        other = object()
        with log_reader_module._open_log(copter_log):
            monkeypatch.setattr(log_reader_module.mavutil, "mavfile_global",
                                other)

        assert log_reader_module.mavutil.mavfile_global is other


class TestDiskCache:
    @pytest.fixture
    def cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(log_reader_module, "_CACHE_DIR", str(tmp_path))
        return tmp_path

    def test_round_trip_skips_pymavlink(self, copter_log, cache_dir,
                                        monkeypatch):
        cold = LogReader(copter_log, disk_cache=True)
        assert list(cache_dir.glob("*.pkl"))

        def forbid(*args, **kwargs):
            raise AssertionError("cache miss")

        monkeypatch.setattr(log_reader_module.mavutil,
                            "mavlink_connection", forbid)
        warm = LogReader(copter_log, disk_cache=True)
        assert warm.params == cold.params
        assert warm.isb_batches.keys() == cold.isb_batches.keys()
        assert warm._fully_cached == cold._fully_cached
        np.testing.assert_array_equal(
            warm.get_messages_as_arrays("ATT", ["Roll"])["Roll"],
            cold.get_messages_as_arrays("ATT", ["Roll"])["Roll"])

    def test_cold_build_caches_every_data_type_in_one_pass(
            self, copter_log, cache_dir):
        reader = LogReader(copter_log, disk_cache=True)
        for msg in ("ATT", "IMU", "XKF4", "RCOU", "PM"):
            assert msg in reader._fully_cached

    def test_key_depends_on_the_cache_version(self, copter_log, cache_dir,
                                              monkeypatch):
        reader = LogReader(copter_log, disk_cache=True)
        before = reader._cache_key()
        monkeypatch.setattr(log_reader_module, "_CACHE_VERSION", 999)
        assert reader._cache_key() != before


def _whole_records(log: binlog.BinLog, count: int = 48) -> None:
    """Padding that is log data. Zeros would do for pymavlink, but the
    reader refuses a file of mostly zeros before pymavlink meets it."""
    start = len(log._defs)
    for i in range(start, start + count):
        log.declare(f"P{i:03d}", "Q", "TimeUS")


def _malformed_fmt_log(path) -> str:
    """A log whose second record is an FMT with an impossible length."""
    log = binlog.BinLog()
    log.data += b"\xa3\x95\x80" + struct.pack(
        "<BB4s16s64s", 200, 2, b"BAD\0", b"Q", b"TimeUS")
    _whole_records(log)
    return log.save(path)


def test_malformed_fmt_does_not_end_the_process(tmp_path):
    """pymavlink's compiled indexer calls exit() on this input."""
    path = _malformed_fmt_log(tmp_path / "bad.bin")
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_ROOT!r})
        from lib.log_reader import LogReader
        try:
            LogReader({path!r})
        except Exception:
            pass
        print("still running")
    """)
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=60)
    assert "still running" in done.stdout, done.stdout + done.stderr


class TestNotADataFlashLog:
    """pymavlink picks its reader by extension, and anything but .bin or
    .log got its telemetry log reader, which makes packets of any bytes. An
    HTML report read as a flight with a health score of 90."""

    @pytest.fixture(params=[
        ("report.html", b"<html><body>" + b"flight " * 2000 + b"</body></html>"),
        ("report.param", b"INS_HNTCH_ENABLE,1\nINS_HNTCH_MODE,3\n"),
        ("notes.txt", b"hover test, third flight\n" * 40),
        ("flight.tlog", bytes(range(256)) * 40),
        ("noextension", b"\xa3\x95\x80" + bytes(200)),
    ], ids=lambda case: case[0])
    def stray(self, request, tmp_path):
        name, content = request.param
        path = tmp_path / name
        path.write_bytes(content)
        return str(path)

    def test_reader_refuses_it(self, stray):
        with pytest.raises(ValueError, match="Not a DataFlash log"):
            LogReader(stray)

    def test_pipeline_gives_no_verdict(self, stray):
        with pytest.raises(ValueError, match="Not a DataFlash log"):
            run_analysis(stray)

    @pytest.mark.skipif(not os.path.isdir("/proc/self/fd"),
                        reason="needs /proc")
    def test_refusal_lets_go_of_the_file(self, stray):
        before = _open_paths()
        with pytest.raises(ValueError):
            LogReader(stray)
        assert [p for p in _open_paths() if p not in before] == []

    def test_real_log_under_another_name_says_what_is_wrong(self, copter_log,
                                                            tmp_path):
        """The extension is how pymavlink tells: the message names it."""
        renamed = tmp_path / "flight.dat"
        renamed.write_bytes(open(copter_log, "rb").read())

        with pytest.raises(ValueError, match=r"\.BIN or \.log"):
            LogReader(str(renamed))


def _open_paths() -> list[str]:
    paths = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            paths.append(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            pass
    return paths


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_failed_open_lets_go_of_the_file(tmp_path):
    """Windows cannot delete a file still open, so this upload stayed behind.

    pytest.raises keeps the traceback, and with it the failed reader, alive
    while the test looks, so a garbage collection cannot hide the leak.
    """
    log = binlog.BinLog()
    log.data += b"\xa3\x95\x80" + struct.pack(
        "<BB4s16s64s", 200, 12, b"BAD\0", b"\\", b"TimeUS")
    _whole_records(log)
    path = log.save(tmp_path / "bad.bin")

    with pytest.raises(Exception, match="Unsupported format") as failure:
        LogReader(path)

    assert failure.traceback
    assert path not in _open_paths()


@pytest.mark.parametrize("fixture", ["copter_log", "quadplane_log"])
def test_pipeline_runs_clean_end_to_end(fixture, request):
    pipeline = run_analysis(request.getfixturevalue(fixture))
    assert len(pipeline.results) >= 25
    assert not [r.title for r in pipeline.results
                if "error" in r.title.lower() or "错误" in r.title]
    assert pipeline.gps_track is not None
    assert pipeline.health_score >= 90
    # A trace with a time axis longer than its values cost the whole report.
    assert not [(r.registry_key, trace.name)
                for r in pipeline.results for fig in r.figures or []
                for trace in fig.data
                if getattr(trace, "x", None) is not None
                and getattr(trace, "y", None) is not None
                and len(trace.x) != len(trace.y)]
