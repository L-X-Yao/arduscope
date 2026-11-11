"""Only a full standard analysis may stand for a flight in the archive.

A flight is archived once, by file hash, so whatever run gets there first
is what the airframe's history shows for good.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

import batch_report
import binlog
import web_app
from analyzers import discover_analyzers
from lib import fleet_history
from lib.i18n import t
from lib.pipeline import run_analysis
from lib.templates import get_template

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def log_path(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("bin") / "copter.bin"
    return binlog.flight_log("copter").save(path)


def _rows(db: str) -> list:
    if not os.path.isfile(db):
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT health_score FROM flights").fetchall()
    finally:
        conn.close()


class TestRecordGate:
    def test_full_run_is_archived(self, tmp_path, log_path):
        db = str(tmp_path / "fleet.db")
        serial, count = fleet_history.record(
            db, run_analysis(log_path), log_path)
        assert serial == "001A002B-3030510C-31323334"
        assert count == 1

    @pytest.mark.parametrize("subset", [
        {"only": {"summary"}}, {"skip": {"vibration"}},
    ], ids=["only", "skip"])
    def test_subset_of_analyzers_is_not_archived(
            self, tmp_path, log_path, subset):
        db = str(tmp_path / "fleet.db")
        result = run_analysis(log_path, **subset)
        with pytest.raises(fleet_history.NotArchived):
            fleet_history.record(db, result, log_path)
        assert _rows(db) == []

    def test_template_with_its_own_thresholds_is_not_archived(
            self, tmp_path, log_path):
        template = get_template("copter-factory")
        assert template.thresholds                   # precondition
        db = str(tmp_path / "fleet.db")
        result = run_analysis(log_path, template=template)
        with pytest.raises(fleet_history.NotArchived):
            fleet_history.record(db, result, log_path)
        assert _rows(db) == []

    def test_threshold_override_is_not_archived(self, tmp_path, log_path):
        overrides = tmp_path / "thresholds.json"
        overrides.write_text(json.dumps({"vibe_warn": 1.0}))
        db = str(tmp_path / "fleet.db")
        result = run_analysis(log_path, thresholds_path=str(overrides))
        assert result.thresholds.vibe_warn == 1.0    # precondition
        with pytest.raises(fleet_history.NotArchived):
            fleet_history.record(db, result, log_path)
        assert _rows(db) == []

    def test_analyzer_that_does_not_apply_does_not_make_it_partial(
            self, tmp_path, log_path):
        result = run_analysis(log_path)
        registered = {key for key, _ in discover_analyzers()}
        assert registered - set(result.results_by_key)   # e.g. VTOL on copter
        db = str(tmp_path / "fleet.db")
        _, count = fleet_history.record(db, result, log_path)
        assert count == 1


class TestFlightReportCli:
    def _run(self, *args, cwd):
        return subprocess.run(
            [sys.executable, os.path.join(_ROOT, "flight_report.py"), *args,
             "--no-open", "--lang", "en"],
            cwd=cwd, capture_output=True, text=True, timeout=120,
            encoding="utf-8")

    def test_partial_run_leaves_room_for_the_full_score(
            self, tmp_path, log_path):
        first = self._run("--only", "summary", "-o",
                          str(tmp_path / "partial.html"), log_path,
                          cwd=tmp_path)
        assert first.returncode == 0, first.stderr
        db = str(tmp_path / "fleet.db")
        assert _rows(db) == []
        second = self._run("-o", str(tmp_path / "full.html"), log_path,
                           cwd=tmp_path)
        assert second.returncode == 0, second.stderr
        assert len(_rows(db)) == 1


@pytest.fixture
def unusable_archive(tmp_path) -> str:
    """A report directory whose fleet.db cannot be opened, even as root."""
    out = tmp_path / "out"
    (out / "fleet.db").mkdir(parents=True)
    return str(out)


class TestArchiveFailureIsVisible:
    """The report still comes out, but nobody may think the flight is stored."""

    def test_batch_says_so(self, unusable_archive, log_path):
        result = batch_report.analyze_one(
            log_path, unusable_archive, no_ai=True, no_map=True)
        assert result["error"] is None
        assert result["report_path"] is not None
        assert result["archive_warning"]

    def test_batch_cli_prints_it(self, tmp_path, unusable_archive, log_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        shutil.copy(log_path, logs / "a.bin")
        done = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "batch_report.py"),
             str(logs), "-o", unusable_archive, "--no-open", "--no-map",
             "--no-ai", "--lang", "en"],
            capture_output=True, text=True, timeout=120, encoding="utf-8")
        assert done.returncode == 0, done.stderr
        assert "Not archived" in done.stderr

    def test_healthy_archive_stays_quiet(self, tmp_path, log_path):
        result = batch_report.analyze_one(
            log_path, str(tmp_path), no_ai=True, no_map=True)
        assert result["archive_warning"] is None
        assert len(_rows(str(tmp_path / "fleet.db"))) == 1

    def test_web_job_carries_it(self, unusable_archive, log_path,
                                monkeypatch, capsys):
        monkeypatch.setattr(web_app, "REPORT_DIR", unusable_archive)
        monkeypatch.setattr(web_app, "HISTORY_FILE", "")
        upload = os.path.join(unusable_archive, "upload_x_a.bin")
        shutil.copy(log_path, upload)
        monkeypatch.setitem(web_app.JOBS, "x", {"id": "x", "start_time": 0.0})
        web_app._run_analysis("x", upload, "a.bin")
        job = web_app.JOBS["x"]
        assert job["status"] == "done"
        assert job["archive_warning"]
        assert job["archive_warning"] in capsys.readouterr().err

    def test_web_page_shows_it(self):
        assert "job.archive_warning" in web_app._build_index_html()

    def test_partial_web_run_says_why(self, tmp_path, log_path, monkeypatch):
        monkeypatch.setattr(web_app, "REPORT_DIR", str(tmp_path))
        monkeypatch.setattr(web_app, "HISTORY_FILE", "")
        upload = str(tmp_path / "upload_y_a.bin")
        shutil.copy(log_path, upload)
        monkeypatch.setitem(web_app.JOBS, "y", {"id": "y", "start_time": 0.0})
        web_app._run_analysis("y", upload, "a.bin", "copter-factory")
        assert t("fleet.partial_run") in web_app.JOBS["y"]["archive_warning"]


class TestArchivedLogName:
    def test_web_flight_keeps_the_name_it_was_uploaded_under(
            self, tmp_path, log_path, monkeypatch):
        monkeypatch.setattr(web_app, "REPORT_DIR", str(tmp_path))
        monkeypatch.setattr(web_app, "HISTORY_FILE", "")
        upload = str(tmp_path / "upload_1a2b3c4d_log_88.bin")
        shutil.copy(log_path, upload)
        monkeypatch.setitem(web_app.JOBS, "z", {"id": "z", "start_time": 0.0})
        web_app._run_analysis("z", upload, "log_88.bin")
        conn = sqlite3.connect(str(tmp_path / "fleet.db"))
        names = conn.execute("SELECT log_name FROM flights").fetchall()
        conn.close()
        assert names == [("log_88.bin",)]

    def test_cli_paths_still_use_the_file_name(self, tmp_path, log_path):
        db = str(tmp_path / "fleet.db")
        fleet_history.record(db, run_analysis(log_path), log_path)
        conn = sqlite3.connect(db)
        names = conn.execute("SELECT log_name FROM flights").fetchall()
        conn.close()
        assert names == [(os.path.basename(log_path),)]
