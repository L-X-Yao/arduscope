"""Uploads and analyses stay inside named limits."""

from __future__ import annotations

import io
import json
import os
import threading
import time

import bottle
import pytest
from webtest import TestApp

import web_app
from lib.i18n import set_locale
from tests.binlog import flight_log


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(web_app, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(web_app, "HISTORY", [])
    monkeypatch.setattr(web_app, "JOBS", {})
    yield tmp_path
    set_locale("zh")


@pytest.fixture
def client(report_dir):
    return TestApp(web_app.app)


def _bind_upload(body, length, query="filename=x.BIN"):
    """Bind a request whose body and Content-Length may disagree."""
    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": "/api/upload",
        "QUERY_STRING": query, "CONTENT_TYPE": "application/octet-stream",
        "wsgi.input": body, "wsgi.errors": io.StringIO(),
        "wsgi.url_scheme": "http",
    }
    if length is not None:
        environ["CONTENT_LENGTH"] = str(length)
    bottle.request.bind(environ)
    bottle.response.bind()
    return environ


class _MustNotBeRead(io.BytesIO):
    def read(self, *args):
        raise AssertionError("the body was read")


class TestUploadSize:
    """The size was checked while copying, after bottle had already spooled
    the whole body to a temp file: any size could fill the disk."""

    def test_oversize_is_refused_before_reading(self, report_dir, monkeypatch):
        monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 1000)
        _bind_upload(_MustNotBeRead(), 1001)

        web_app.upload()

        assert bottle.response.status_code == 413
        assert list(report_dir.iterdir()) == []
        assert web_app.JOBS == {}

    def test_limit_itself_is_accepted(self, report_dir, monkeypatch):
        monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 1000)
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)
        _bind_upload(io.BytesIO(b"x" * 1000), 1000)

        reply = json.loads(web_app.upload())

        assert reply["job_id"] in web_app.JOBS

    def test_body_goes_straight_to_its_file(self, report_dir, monkeypatch):
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)
        environ = _bind_upload(io.BytesIO(b"x" * 500), 500)

        web_app.upload()

        assert "bottle.request.body" not in environ

    def test_truncated_body_is_dropped(self, report_dir):
        """The client went away. Half a log is not a log to analyze."""
        _bind_upload(io.BytesIO(b"x" * 10), 100)

        web_app.upload()

        assert bottle.response.status_code == 400
        assert list(report_dir.iterdir()) == []
        assert web_app.JOBS == {}

    def test_unknown_length_is_refused(self, report_dir):
        _bind_upload(_MustNotBeRead(), None)

        web_app.upload()

        assert bottle.response.status_code == 411
        assert list(report_dir.iterdir()) == []


class TestBatchUploadSize:
    """upload_batch saved each file first and compared sizes afterwards."""

    def test_oversize_batch_is_refused_before_parsing(self, client, report_dir,
                                                      monkeypatch):
        monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 100)

        resp = client.post("/api/upload-batch",
                           upload_files=[("files", "x.BIN", b"x" * 500)],
                           expect_errors=True)

        assert resp.status_int == 413
        assert "error" in resp.json
        assert list(report_dir.iterdir()) == []
        assert web_app.JOBS == {}

    def test_batch_within_the_limit_still_runs(self, client, report_dir,
                                               monkeypatch):
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)

        resp = client.post("/api/upload-batch", upload_files=[
            ("files", "a.BIN", b"x" * 50), ("files", "b.BIN", b"y" * 50)])

        assert len(resp.json["jobs"]) == 2


def test_waitress_refuses_oversize_bodies_itself(monkeypatch):
    """Waitress buffers a request before the app sees it."""
    waitress = pytest.importorskip("waitress")
    seen = {}
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: seen.update(kw))
    args = type("Args", (), dict(host="127.0.0.1", port=1, workers=1,
                                 no_open=True))

    web_app._run_waitress(args)

    assert seen["max_request_body_size"] == web_app.MAX_UPLOAD_BYTES


@pytest.mark.fast
def test_index_page_checks_size_before_sending():
    """A browser still sending the body sees the early refusal as a network
    error, so the page has to catch it first."""
    page = web_app._build_index_html()

    assert f"const MAX_UPLOAD_BYTES = {web_app.MAX_UPLOAD_BYTES};" in page


class TestPendingCap:
    """Every upload held a thread and its file until its turn came, with no
    limit on how many could wait."""

    @pytest.fixture
    def gate(self, monkeypatch):
        """Analyses stay pending until the test sets this event."""
        release = threading.Event()
        slots = threading.BoundedSemaphore(2)
        monkeypatch.setattr(web_app, "_run_analysis",
                            lambda *args: release.wait(10))
        monkeypatch.setattr(web_app, "_PENDING_SLOTS", slots, raising=False)
        yield release
        release.set()
        # Workers release into web_app._PENDING_SLOTS, so let them finish
        # before monkeypatch puts the real semaphore back.
        for _ in range(500):
            if slots._value == 2:
                break
            time.sleep(0.01)

    def _upload(self, client, body=b"data", **kwargs):
        return client.post("/api/upload?filename=x.BIN", body,
                           content_type="application/octet-stream", **kwargs)

    def test_upload_past_the_cap_is_refused(self, client, gate, report_dir):
        self._upload(client)
        self._upload(client)

        resp = self._upload(client, expect_errors=True)

        assert resp.status_int == 429
        assert "error" in resp.json
        assert len(list(report_dir.glob("upload_*"))) == 2

    def test_finished_analysis_frees_its_slot(self, client, gate):
        self._upload(client)
        self._upload(client)
        gate.set()

        for _ in range(200):
            if self._upload(client, expect_errors=True).status_int == 200:
                return
            time.sleep(0.01)
        pytest.fail("no slot came back")

    def test_failed_upload_keeps_no_slot(self, client, gate):
        for _ in range(3):
            resp = self._upload(client, body=b"", expect_errors=True)
            assert resp.status_int == 400

        assert self._upload(client).status_int == 200

    def test_batch_counts_every_file(self, client, gate, report_dir):
        resp = client.post("/api/upload-batch", upload_files=[
            ("files", f"{name}.BIN", b"x") for name in "abc"])

        assert len(resp.json["jobs"]) == 2
        assert len(resp.json["errors"]) == 1
        assert len(list(report_dir.glob("upload_*"))) == 2


class TestReportRetention:
    """Reports are about 6 MB each, a 4 KB upload makes one, and none was
    ever deleted."""

    def _write(self, directory, name, age_s):
        path = directory / name
        path.write_bytes(b"x")
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))

    def test_oldest_generated_reports_go_first(self, report_dir, monkeypatch):
        monkeypatch.setattr(web_app, "MAX_KEPT_REPORTS", 3, raising=False)
        self._write(report_dir, "00000001_a_report.html", 500)
        self._write(report_dir, "compare_00000002.html", 400)
        self._write(report_dir, "00000003_b_report.html", 300)
        self._write(report_dir, "compare_00000004.html", 200)
        self._write(report_dir, "00000005_c_report.html", 100)

        web_app._prune_reports()

        assert sorted(p.name for p in report_dir.iterdir()) == [
            "00000003_b_report.html", "00000005_c_report.html",
            "compare_00000004.html"]

    def test_only_generated_reports_are_candidates(self, report_dir,
                                                   monkeypatch):
        monkeypatch.setattr(web_app, "MAX_KEPT_REPORTS", 1, raising=False)
        keep = ["fleet.db", "history.json", "upload_00000001_a.BIN",
                "flight_report.html", "notes.html"]
        for name in keep:
            self._write(report_dir, name, 1000)
        self._write(report_dir, "00000003_b_report.html", 1)

        web_app._prune_reports()

        assert sorted(p.name for p in report_dir.iterdir()) == sorted(
            keep + ["00000003_b_report.html"])

    def test_reports_are_kept_unless_asked_otherwise(self, report_dir):
        assert web_app.MAX_KEPT_REPORTS == 0
        report = report_dir / "00000001_a_report.html"
        report.write_text("x")
        web_app._prune_reports()
        assert report.exists()

    def test_orphaned_uploads_from_a_killed_server_are_swept(
            self, report_dir):
        old = report_dir / "upload_dead_x.BIN"
        fresh = report_dir / "upload_live_y.BIN"
        report = report_dir / "00000001_a_report.html"
        old.write_bytes(b"old")
        fresh.write_bytes(b"new")
        report.write_text("x")
        stale = time.time() - web_app.ORPHAN_UPLOAD_AGE_S - 60
        os.utime(old, (stale, stale))

        web_app._sweep_orphaned_uploads()

        assert not old.exists()
        assert fresh.exists()
        assert report.exists()

    def test_compare_report_counts(self, client, report_dir, monkeypatch):
        calls = []
        monkeypatch.setattr(web_app, "_prune_reports",
                            lambda: calls.append(1), raising=False)
        monkeypatch.setattr(web_app, "HISTORY",
                            [{"id": f"id{i}", "filename": f"{i}.BIN",
                              "metrics": {"max_vibe": 1.0}}
                             for i in range(2)])

        client.post_json("/api/compare-history", {"ids": ["id0", "id1"]})

        assert calls == [1]

    def test_analysis_counts(self, client, report_dir, monkeypatch):
        calls = []
        monkeypatch.setattr(web_app, "_prune_reports",
                            lambda: calls.append(1), raising=False)

        job = client.post("/api/upload?filename=x.BIN",
                          bytes(flight_log(duration_s=6.0).data),
                          content_type="application/octet-stream").json
        for _ in range(1000):
            state = web_app.JOBS[job["job_id"]]["status"]
            if state in ("done", "error"):
                break
            time.sleep(0.02)

        assert state == "done"
        assert calls == [1]


class TestMemoryBudget:
    """Three at a time says nothing about memory: three logs at the upload
    limit need several times what three small ones do."""

    def _start(self, budget, cost, finished):
        def run():
            budget.acquire(cost)
            finished.append(cost)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def _wait_for(self, finished, count):
        for _ in range(300):
            if len(finished) >= count:
                return
            time.sleep(0.01)
        pytest.fail(f"only {finished} got through")

    @pytest.mark.fast
    def test_analyses_that_fit_run_together(self):
        budget = web_app._MemoryBudget(10)
        budget.acquire(4)

        budget.acquire(6)       # would block the test if it did not fit

        budget.release(4)
        budget.release(6)

    @pytest.mark.fast
    def test_one_that_does_not_fit_waits_for_memory(self):
        budget = web_app._MemoryBudget(10)
        budget.acquire(8)
        finished = []

        self._start(budget, 8, finished)
        time.sleep(0.05)
        assert finished == []

        budget.release(8)
        self._wait_for(finished, 1)

    @pytest.mark.fast
    def test_small_logs_do_not_jump_the_queue(self):
        """Otherwise a steady stream of small logs starves a big one."""
        budget = web_app._MemoryBudget(10)
        budget.acquire(8)
        finished = []

        self._start(budget, 8, finished)
        time.sleep(0.05)
        self._start(budget, 1, finished)    # fits, but arrived second
        time.sleep(0.05)
        assert finished == []

        budget.release(8)
        self._wait_for(finished, 2)
        assert finished == [8, 1]

    @pytest.mark.fast
    def test_log_over_the_budget_still_runs_alone(self):
        budget = web_app._MemoryBudget(10)

        budget.acquire(50)

        finished = []
        self._start(budget, 1, finished)
        time.sleep(0.05)
        assert finished == []
        budget.release(50)
        self._wait_for(finished, 1)

    # Peak of a waitress server after four analyses of the same log, one
    # after the other, on 2026-11-11. The first analysis in a new process
    # peaks lower: 2.95 GB for the last of these. Until every analysis read
    # its log once, that log peaked at 8.93 GB.
    STEADY_PEAKS = [
        (5_128_192, 230_000_000),
        (49_127_424, 430_000_000),
        (87_621_632, 490_000_000),
        (115_900_416, 630_000_000),
        (150_929_408, 520_000_000),
        (228_712_448, 970_000_000),
        (254_967_808, 760_000_000),
        (662_925_312, 3_300_000_000),
        (813_002_752, 4_130_000_000),
    ]

    @pytest.mark.fast
    @pytest.mark.parametrize("log_bytes, measured_peak", STEADY_PEAKS)
    def test_estimate_covers_measured_peaks(self, log_bytes, measured_peak):
        assert measured_peak <= web_app._estimated_peak_bytes(log_bytes)

    @pytest.mark.fast
    def test_estimate_is_not_loose_on_the_heaviest_log(self):
        log_bytes, measured_peak = max(self.STEADY_PEAKS)

        assert web_app._estimated_peak_bytes(log_bytes) < measured_peak * 1.5

    @pytest.mark.fast
    def test_log_at_the_upload_limit_fits_the_budget(self):
        """The reason the limit is where it is: over the budget a log
        runs alone, with nothing to say the machine can hold it."""
        at_limit = web_app._estimated_peak_bytes(web_app.MAX_UPLOAD_BYTES)

        assert at_limit <= web_app.ANALYSIS_MEMORY_BUDGET_BYTES

    @pytest.mark.fast
    def test_largest_corpus_log_is_under_the_limit(self):
        largest = max(size for size, _peak in self.STEADY_PEAKS)

        assert largest == 813_002_752  # precondition
        assert largest < web_app.MAX_UPLOAD_BYTES

    def test_worker_budgets_by_file_size(self, report_dir, monkeypatch):
        running, peak, lock = [0], [0], threading.Lock()
        together = []

        def fake_analysis(*args, **kwargs):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            if together:
                # Waits for the others, so being allowed to run in parallel
                # does not depend on how fast the threads happen to start.
                together[0].wait(timeout=5)
            else:
                time.sleep(0.05)
            with lock:
                running[0] -= 1
            raise RuntimeError("stop here")

        def run_all(sizes):
            threads = []
            for i, size in enumerate(sizes):
                path = report_dir / f"upload_{i}.BIN"
                path.write_bytes(b"x" * size)
                web_app.JOBS[str(i)] = {"id": str(i), "status": "queued"}
                threads.append(threading.Thread(
                    target=web_app._run_analysis,
                    args=(str(i), str(path), f"{i}.BIN")))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

        monkeypatch.setattr(web_app, "run_analysis", fake_analysis)
        monkeypatch.setattr(web_app, "ANALYSIS_BASE_BYTES", 0)
        monkeypatch.setattr(web_app, "ANALYSIS_PEAK_PER_LOG_BYTE", 1)
        monkeypatch.setattr(web_app, "_ANALYSIS_BUDGET",
                            web_app._MemoryBudget(1000))

        run_all([600, 600, 600])
        assert peak[0] == 1

        peak[0] = 0
        together.append(threading.Barrier(3))
        run_all([300, 300, 300])
        assert peak[0] == 3


class TestUploadCleanup:
    def test_upload_that_cannot_be_deleted_is_reported(self, report_dir,
                                                       monkeypatch, capsys):
        """Windows refuses to delete a file still open, and the silence
        kept the leftovers out of sight until it was run there."""
        def in_use(path):
            raise PermissionError(13, "in use", path)

        upload = report_dir / "upload_x_a.BIN"
        upload.write_bytes(b"not a log")
        monkeypatch.setitem(web_app.JOBS, "x", {"id": "x", "start_time": 0.0})
        monkeypatch.setattr(web_app.os, "remove", in_use)

        web_app._run_analysis("x", str(upload), "a.BIN")

        assert web_app.JOBS["x"]["status"] == "error"
        assert "upload_x_a.BIN" in capsys.readouterr().err
