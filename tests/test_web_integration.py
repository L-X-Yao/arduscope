"""
Integration tests for the web_app.py analysis pipeline.

Uses webtest.TestApp to exercise Bottle routes without a live server.
"""

import json
import os
import re
import sys
import tempfile
import time

import pytest

# Ensure project root is importable
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

from webtest import TestApp

from tests.conftest import LOG_QUADPLANE_FLIGHT
from lib.i18n import set_locale, t


@pytest.fixture
def app():
    """Create a test app with a temp report dir and isolated state."""
    import web_app

    original_report_dir = web_app.REPORT_DIR
    original_history_file = web_app.HISTORY_FILE
    original_history = web_app.HISTORY[:]
    original_jobs = dict(web_app.JOBS)

    with tempfile.TemporaryDirectory() as tmpdir:
        web_app.REPORT_DIR = tmpdir
        web_app.HISTORY_FILE = os.path.join(tmpdir, "history.json")
        web_app.HISTORY = []
        web_app.JOBS = {}

        yield TestApp(web_app.app)

        # Restore original state
        web_app.REPORT_DIR = original_report_dir
        web_app.HISTORY_FILE = original_history_file
        web_app.HISTORY = original_history
        web_app.JOBS = original_jobs

    # Reset locale to zh so other tests are not affected
    set_locale("zh")


# ---- Basic route tests ----

def test_index_returns_html(app):
    """GET / returns 200 with valid HTML containing ArduScope."""
    resp = app.get("/")
    assert resp.status_int == 200
    body = resp.text
    assert body.strip().startswith("<!DOCTYPE html>")
    assert "ArduScope" in body


def test_upload_missing_filename(app):
    """POST /api/upload without filename query param returns 400."""
    resp = app.post(
        "/api/upload",
        params=b"\x00" * 10,
        content_type="application/octet-stream",
        expect_errors=True,
    )
    assert resp.status_int == 400
    data = resp.json
    assert "error" in data


def test_upload_non_bin_file(app):
    """POST /api/upload with a non-.BIN filename returns 400."""
    resp = app.post(
        "/api/upload?filename=test.txt",
        params=b"\x00" * 10,
        content_type="application/octet-stream",
        expect_errors=True,
    )
    assert resp.status_int == 400
    data = resp.json
    assert "error" in data


def test_upload_empty_body(app):
    """POST /api/upload with an empty body returns 400."""
    resp = app.post(
        "/api/upload?filename=test.BIN",
        params=b"",
        content_type="application/octet-stream",
        expect_errors=True,
    )
    assert resp.status_int == 400
    data = resp.json
    assert "error" in data


def test_language_switch(app):
    """POST /api/set-lang switches the UI language visible on GET /."""
    # Switch to English
    resp = app.post_json("/api/set-lang", {"lang": "en"})
    assert resp.status_int == 200
    assert resp.json["ok"] is True

    resp_en = app.get("/")
    assert resp_en.status_int == 200
    # English subtitle should appear
    assert "Upload ArduPilot" in resp_en.text or 'lang="en"' in resp_en.text

    # Switch back to Chinese
    resp = app.post_json("/api/set-lang", {"lang": "zh"})
    assert resp.status_int == 200

    resp_zh = app.get("/")
    assert resp_zh.status_int == 200
    assert 'lang="zh-CN"' in resp_zh.text


def test_jobs_endpoint(app):
    """GET /api/jobs returns 200 with a JSON list."""
    resp = app.get("/api/jobs")
    assert resp.status_int == 200
    data = resp.json
    assert "jobs" in data
    assert isinstance(data["jobs"], list)


def test_history_endpoint(app):
    """GET /api/history returns 200 with a JSON list."""
    resp = app.get("/api/history")
    assert resp.status_int == 200
    data = resp.json
    assert "history" in data
    assert isinstance(data["history"], list)


def test_compare_needs_two_jobs(app):
    """POST /api/compare with fewer than 2 done jobs returns 400."""
    # No jobs at all
    resp = app.post_json(
        "/api/compare",
        {"job_ids": ["nonexistent"]},
        expect_errors=True,
    )
    assert resp.status_int == 400
    data = resp.json
    assert "error" in data

    # Only one fake "done" job
    import web_app
    web_app.JOBS["fake1"] = {
        "id": "fake1",
        "filename": "test.BIN",
        "status": "done",
        "metrics": {"vibe_x": 1.0},
        "params": {},
    }
    resp = app.post_json(
        "/api/compare",
        {"job_ids": ["fake1"]},
        expect_errors=True,
    )
    assert resp.status_int == 400
    assert "error" in resp.json


def test_dark_mode_toggle(app):
    """Page includes dark mode toggle and CSS variables."""
    resp = app.get("/")
    body = resp.text
    assert "data-theme" in body
    assert "toggleTheme" in body
    assert "--bg" in body
    assert "prefers-color-scheme" in body


def test_toast_notifications(app):
    """Page includes toast notification system instead of alert()."""
    resp = app.get("/")
    body = resp.text
    assert "toast-container" in body
    assert "showToast" in body


def test_compare_history_endpoint(app):
    """POST /api/compare-history with unknown entries returns error."""
    resp = app.post_json(
        "/api/compare-history",
        {"ids": ["nope", "nada"]},
        expect_errors=True,
    )
    assert resp.status_int == 400
    assert "error" in resp.json


def _history_entry(name, vibe, flight_time=None):
    metrics = {"max_vibe": vibe}
    if flight_time is not None:
        metrics["flight_time"] = flight_time
    return {"id": name, "timestamp": "2026-07-01 10:00:00", "filename": name,
            "vehicle_info": None, "health_score": 80, "metrics": metrics,
            "analyzer_results": []}


def _compare_rows(app, url):
    import web_app
    path = os.path.join(web_app.REPORT_DIR, os.path.basename(url))
    with open(path, encoding="utf-8") as f:
        return re.findall(r'class="trend-row (trend-\w+)"', f.read())


def test_compare_history_in_page_order_reads_oldest_first(app):
    """The page lists newest first and posts in that order."""
    import web_app
    web_app.HISTORY[:] = [_history_entry("old.BIN", 40.0, 1_780_000_000.0),
                          _history_entry("new.BIN", 20.0, 1_780_090_000.0)]
    resp = app.post_json("/api/compare-history",
                         {"ids": ["new.BIN", "old.BIN"]})
    assert _compare_rows(app, resp.json["url"]) == ["trend-improving"]


def test_compare_history_without_gps_time_uses_archive_order(app):
    import web_app
    web_app.HISTORY[:] = [_history_entry("old.BIN", 40.0),
                          _history_entry("new.BIN", 20.0)]
    resp = app.post_json("/api/compare-history",
                         {"ids": ["new.BIN", "old.BIN"]})
    assert _compare_rows(app, resp.json["url"]) == ["trend-improving"]


def _compared_names(url):
    import web_app
    path = os.path.join(web_app.REPORT_DIR, os.path.basename(url))
    with open(path, encoding="utf-8") as f:
        report = f.read()
    table = report[report.index('<table class="comparison-table">'):]
    return re.findall(r"<th>(flight_\d+\.BIN)</th>", table)


def test_compare_history_survives_the_100_entry_trim(app):
    """An open page must compare what it shows, after the list has moved."""
    import web_app
    web_app.HISTORY[:] = [_history_entry(f"flight_{i}.BIN", float(i))
                          for i in range(100)]
    chosen = [web_app.HISTORY[40]["id"], web_app.HISTORY[50]["id"]]
    web_app._append_history({"id": "next", "filename": "flight_100.BIN",
                             "metrics": {"max_vibe": 1.0}})
    assert web_app.HISTORY[0]["filename"] == "flight_1.BIN"  # precondition
    resp = app.post_json("/api/compare-history", {"ids": chosen})
    assert _compared_names(resp.json["url"]) == ["flight_40.BIN",
                                                 "flight_50.BIN"]


def test_compare_history_entry_gone_from_the_list_is_an_error(app):
    import web_app
    web_app.HISTORY[:] = [_history_entry(f"flight_{i}.BIN", float(i))
                          for i in range(100)]
    chosen = [web_app.HISTORY[0]["id"], web_app.HISTORY[1]["id"]]
    for i in range(100, 102):
        web_app._append_history({"id": str(i), "filename": f"flight_{i}.BIN",
                                 "metrics": {"max_vibe": 1.0}})
    resp = app.post_json("/api/compare-history", {"ids": chosen},
                         expect_errors=True)
    assert resp.status_int == 400


def test_history_entries_get_stable_ids(app):
    import web_app
    web_app._append_history({"id": "job1", "filename": "a.BIN",
                             "metrics": {}})
    assert web_app.HISTORY[0]["id"] == "job1"


def test_old_history_entries_gain_ids_on_load(app):
    """A file written before entries had ids must stay comparable."""
    import web_app
    with open(web_app.HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump([{"filename": "a.BIN", "metrics": {"max_vibe": 9.0}},
                   {"filename": "b.BIN", "metrics": {"max_vibe": 8.0}}], f)
    web_app._load_history()
    ids = [e["id"] for e in web_app.HISTORY]
    assert len(set(ids)) == 2 and all(ids)
    assert app.post_json("/api/compare-history", {"ids": ids}).status_int == 200


def test_page_selects_history_by_id_not_position(app):
    page = app.get("/").text
    assert "data-hist-id" in page
    assert "data-hist-idx" not in page


def test_compare_history_does_not_claim_identical_parameters(app):
    """History keeps no parameters, so it has nothing to compare."""
    import web_app
    web_app.HISTORY[:] = [_history_entry("a.BIN", 40.0),
                          _history_entry("b.BIN", 20.0)]
    resp = app.post_json("/api/compare-history",
                         {"ids": ["a.BIN", "b.BIN"]})
    path = os.path.join(web_app.REPORT_DIR,
                        os.path.basename(resp.json["url"]))
    with open(path, encoding="utf-8") as f:
        report = f.read()
    assert t("multi.param_no_diff") not in report
    assert t("multi.param_unknown") in report


def _reload_history():
    import web_app
    web_app.HISTORY[:] = []
    web_app._load_history()
    return [e["filename"] for e in web_app.HISTORY]


def test_interrupted_history_write_keeps_the_old_file(app, monkeypatch):
    """Dying mid-write used to leave a truncated file that loads as empty."""
    import web_app
    web_app.HISTORY[:] = [_history_entry("a.BIN", 10.0)]
    web_app._save_history()
    assert _reload_history() == ["a.BIN"]            # precondition

    def die_halfway(_obj, f, **_kwargs):
        f.write('[{"filename": "b.B')
        raise OSError("disk full")

    web_app.HISTORY.append(_history_entry("b.BIN", 20.0))
    monkeypatch.setattr(web_app.json, "dump", die_halfway)
    web_app._save_history()
    monkeypatch.undo()

    assert _reload_history() == ["a.BIN"]
    leftovers = [n for n in os.listdir(web_app.REPORT_DIR)
                 if n.endswith(".tmp")]
    assert leftovers == []


def test_unserializable_entry_does_not_break_the_caller(app):
    """A finished job must not turn into an error over its history entry."""
    import web_app
    web_app.HISTORY[:] = [_history_entry("a.BIN", 10.0)]
    web_app._save_history()
    web_app._append_history({"filename": "b.BIN", "metrics": {"x": object()}})
    assert _reload_history() == ["a.BIN"]


def test_saved_history_replaces_the_previous_one(app):
    import web_app
    web_app.HISTORY[:] = [_history_entry("a.BIN", 10.0)]
    web_app._save_history()
    web_app.HISTORY.append(_history_entry("b.BIN", 20.0))
    web_app._save_history()
    assert _reload_history() == ["a.BIN", "b.BIN"]


@pytest.mark.parametrize("content", ['[{"filename": "a.B', '{"not": "a list"}'],
                         ids=["truncated", "wrong-shape"])
def test_unreadable_history_file_is_kept_aside(app, content):
    """The next save must not destroy the only copy of what it held."""
    import web_app
    with open(web_app.HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    assert _reload_history() == []
    web_app._append_history({"filename": "new.BIN", "metrics": {}})
    with open(web_app.HISTORY_FILE + ".corrupt", encoding="utf-8") as f:
        assert f.read() == content
    assert _reload_history() == ["new.BIN"]


def test_unreadable_history_that_cannot_be_moved_does_not_stop_startup(
        app, monkeypatch):
    import web_app
    with open(web_app.HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write('[{"filename": "a.B')

    def refuse(*_args):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(web_app.os, "replace", refuse)
    web_app._load_history()
    assert web_app.HISTORY == []


def _start_server(monkeypatch, *args):
    """Run web_app.main() up to the point it would start serving."""
    import web_app
    monkeypatch.setattr(sys, "argv", ["web_app.py", "--no-open", *args])
    monkeypatch.setattr(web_app, "_run_dev_server", lambda _args: None)
    web_app.main()
    return web_app.REPORT_DIR


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private home directory, and web_app globals restored afterwards."""
    import web_app
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for name in ("REPORT_DIR", "HISTORY_FILE", "_UI_LOCALE"):
        monkeypatch.setattr(web_app, name, getattr(web_app, name))
    monkeypatch.setattr(web_app, "HISTORY", [])
    yield tmp_path
    set_locale("zh")


def test_default_report_dir_survives_a_restart(home, monkeypatch):
    """The archive and history live here, so a restart must find them."""
    import web_app
    first = _start_server(monkeypatch)
    web_app._append_history({"filename": "a.BIN", "metrics": {}})
    web_app.HISTORY[:] = []
    second = _start_server(monkeypatch)
    assert first == second
    assert first == str(home / ".arduscope" / "reports")
    assert [e["filename"] for e in web_app.HISTORY] == ["a.BIN"]


def test_default_report_dir_does_not_depend_on_the_working_dir(
        home, monkeypatch):
    first = _start_server(monkeypatch)
    monkeypatch.chdir(home)
    assert _start_server(monkeypatch) == first


def test_explicit_report_dir_is_used(home, monkeypatch):
    chosen = home / "chosen"
    assert _start_server(monkeypatch, "--report-dir", str(chosen)) == str(chosen)
    assert chosen.is_dir()


@pytest.fixture
def no_worker(monkeypatch):
    """Uploads only: no analysis thread outliving the test's directory."""
    import web_app
    monkeypatch.setattr(web_app, "_run_analysis", lambda *a, **k: None)


@pytest.mark.parametrize("name", ["第一架次.BIN", "飞行 2.BIN", "日志.bin"])
def test_batch_upload_accepts_and_keeps_real_filenames(app, no_worker, name):
    """Most of the fleet's logs have Chinese names."""
    resp = app.post("/api/upload-batch",
                    upload_files=[("files", name, b"\xa3\x95" * 40)])
    assert resp.json["errors"] == []
    assert [j["filename"] for j in resp.json["jobs"]] == [name]


def test_batch_upload_cannot_escape_the_report_dir(app, no_worker):
    import web_app
    resp = app.post("/api/upload-batch",
                    upload_files=[("files", "..\\..\\evil.BIN", b"x" * 40)])
    assert [j["filename"] for j in resp.json["jobs"]] == ["evil.BIN"]
    saved = [n for n in os.listdir(web_app.REPORT_DIR) if "evil" in n]
    assert len(saved) == 1 and saved[0].endswith("_evil.BIN")


def test_batch_upload_still_rejects_other_files(app, no_worker):
    resp = app.post("/api/upload-batch",
                    upload_files=[("files", "notes.txt", b"x" * 40)])
    assert resp.json["jobs"] == []
    assert len(resp.json["errors"]) == 1


def _full_job_table():
    import web_app
    web_app.JOBS.clear()
    for i in range(web_app.MAX_JOBS):
        web_app.JOBS[f"done{i}"] = {"id": f"done{i}", "status": "done",
                                    "start_time": float(i)}


@pytest.mark.parametrize("upload", [
    lambda app: app.post("/api/upload-batch",
                         upload_files=[("files", "a.BIN", b"x" * 40)]),
    lambda app: app.post("/api/upload?filename=a.BIN", params=b"x" * 40,
                         content_type="application/octet-stream"),
], ids=["batch", "single"])
def test_uploads_keep_the_job_table_within_its_limit(app, no_worker, upload):
    import web_app
    _full_job_table()
    for _ in range(5):
        upload(app)
    assert len(web_app.JOBS) == web_app.MAX_JOBS
    assert "done0" not in web_app.JOBS               # oldest went first
    assert f"done{web_app.MAX_JOBS - 1}" in web_app.JOBS


def test_unfinished_jobs_are_never_evicted(app, no_worker):
    import web_app
    _full_job_table()
    web_app.JOBS["done0"]["status"] = "running"
    app.post("/api/upload-batch", upload_files=[("files", "a.BIN", b"x" * 40)])
    assert "done0" in web_app.JOBS
    assert "done1" not in web_app.JOBS


def test_compare_jobs_ignores_upload_order(app):
    """Parallel uploads finish in any order; the flights did not."""
    import web_app
    web_app.JOBS.update({
        "new": {"status": "done", "filename": "new.BIN",
                "metrics": {"max_vibe": 20.0,
                            "flight_time": 1_780_090_000.0}},
        "old": {"status": "done", "filename": "old.BIN",
                "metrics": {"max_vibe": 40.0,
                            "flight_time": 1_780_000_000.0}},
    })
    resp = app.post_json("/api/compare", {"job_ids": ["new", "old"]})
    assert _compare_rows(app, resp.json["url"]) == ["trend-improving"]


@pytest.mark.skipif(
    not os.path.isfile(LOG_QUADPLANE_FLIGHT),
    reason="Reference log not found",
)
def test_upload_and_analyze(app):
    """Upload a real .BIN file, poll until done, and verify the report."""
    filename = os.path.basename(LOG_QUADPLANE_FLIGHT)
    with open(LOG_QUADPLANE_FLIGHT, "rb") as f:
        log_bytes = f.read()

    resp = app.post(
        f"/api/upload?filename={filename}",
        params=log_bytes,
        content_type="application/octet-stream",
    )
    assert resp.status_int == 200
    job_id = resp.json["job_id"]
    assert job_id

    # Poll /api/jobs until the job is done (max 120s for large logs)
    deadline = time.time() + 120
    status = None
    result_url = None
    while time.time() < deadline:
        jobs_resp = app.get("/api/jobs")
        jobs = jobs_resp.json["jobs"]
        job = next((j for j in jobs if j["id"] == job_id), None)
        assert job is not None, "Job disappeared from jobs list"
        status = job["status"]
        if status == "done":
            result_url = job.get("result_url")
            break
        if status == "error":
            pytest.fail(f"Analysis failed: {job.get('message')}")
        time.sleep(1)

    assert status == "done", f"Analysis did not complete within timeout (last status: {status})"
    assert result_url is not None

    # Fetch the generated report
    report_resp = app.get(result_url)
    assert report_resp.status_int == 200
    assert "<!DOCTYPE html>" in report_resp.text or "<html" in report_resp.text

    # Verify history was updated
    hist_resp = app.get("/api/history")
    history = hist_resp.json["history"]
    assert len(history) >= 1
    assert history[-1]["filename"] == filename


class TestNonFiniteValues:
    """A NaN from a log must not take the job or history list down."""

    @staticmethod
    def _strict(body: bytes) -> dict:
        def reject(token):
            raise AssertionError(f"non-JSON token {token} in response")

        return json.loads(body, parse_constant=reject)

    def test_job_list(self, app):
        import web_app
        with web_app._JOBS_LOCK:
            web_app.JOBS["nan-job"] = {
                "id": "nan-job", "status": "done",
                "params": {"X": float("nan"), "Y": float("inf"), "Z": 1.5},
            }
        try:
            jobs = self._strict(app.get("/api/jobs").body)["jobs"]
            job = next(j for j in jobs if j["id"] == "nan-job")
            assert job["params"] == {"X": None, "Y": None, "Z": 1.5}
            one = self._strict(app.get("/api/jobs/nan-job").body)
            assert one["params"]["X"] is None
        finally:
            with web_app._JOBS_LOCK:
                del web_app.JOBS["nan-job"]

    def test_history(self, app):
        import web_app
        with web_app._HISTORY_LOCK:
            web_app.HISTORY.append({"metrics": {"max_vibe": float("nan")}})
        try:
            history = self._strict(app.get("/api/history").body)["history"]
            assert history[-1]["metrics"]["max_vibe"] is None
        finally:
            with web_app._HISTORY_LOCK:
                web_app.HISTORY.pop()

