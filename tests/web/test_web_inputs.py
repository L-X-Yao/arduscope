"""What a request says about itself is checked before it is used."""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from urllib.parse import quote

import bottle
import pytest
from webtest import TestApp

import web_app
from lib.i18n import set_locale
from lib.templates import get_template


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


@pytest.fixture
def slots(monkeypatch):
    """Two pending slots, checked to be free again when the test ends."""
    semaphore = threading.BoundedSemaphore(2)
    monkeypatch.setattr(web_app, "_PENDING_SLOTS", semaphore)
    yield semaphore
    _settle(semaphore)


def _settle(semaphore, free: int = 2) -> None:
    """Let workers that already ran give their slots back."""
    for _ in range(500):
        if semaphore._value == free:
            return
        time.sleep(0.01)


def _upload(client, name: str, **kwargs):
    return client.post("/api/upload?filename=" + quote(name, safe=""), b"data",
                       content_type="application/octet-stream", **kwargs)


def _bind(path: str, body, length: int, query: str = "") -> None:
    """Bind a request whose body must not be touched."""
    bottle.request.bind({
        "REQUEST_METHOD": "POST", "PATH_INFO": path, "QUERY_STRING": query,
        "CONTENT_TYPE": "application/octet-stream",
        "CONTENT_LENGTH": str(length), "wsgi.input": body,
        "wsgi.errors": io.StringIO(), "wsgi.url_scheme": "http",
    })
    bottle.response.bind()


class _MustNotBeRead(io.BytesIO):
    def read(self, *args):
        raise AssertionError("the body was read")


class TestUploadName:
    """The name comes from the request and goes into two file names."""

    @pytest.mark.parametrize("sent, stored", [
        ("a\x00b.BIN", "a_b.BIN"),
        ("flight:1.BIN", "flight_1.BIN"),
        ('a<b>|?*".BIN', "a_b_____.BIN"),
        ("a\r\nb.BIN", "a__b.BIN"),
        ("..\\..\\x.BIN", "x.BIN"),
        ("../../x.BIN", "x.BIN"),
        ("C:\\logs\\7.BIN", "7.BIN"),
        ("中文 名称.BIN", "中文 名称.BIN"),
    ])
    def test_name_is_made_safe_for_a_file(self, client, report_dir, slots,
                                          monkeypatch, sent, stored):
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)

        resp = _upload(client, sent)

        assert resp.status_int == 200
        job = web_app.JOBS[resp.json["job_id"]]
        assert job["filename"] == stored
        assert (report_dir / f"upload_{job['id']}_{stored}").is_file()

    def test_batch_names_are_cleaned_the_same_way(self, client, report_dir,
                                                  slots, monkeypatch):
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)

        resp = client.post("/api/upload-batch", upload_files=[
            ("files", "dir\\flight:1.BIN", b"x")])

        assert [job["filename"] for job in resp.json["jobs"]] == ["flight_1.BIN"]
        assert [p.name[len("upload_12345678_"):]
                for p in report_dir.glob("upload_*")] == ["flight_1.BIN"]

    def test_long_name_still_fits_a_file_name(self, client, report_dir, slots,
                                              monkeypatch):
        """80 Chinese characters are 240 bytes, and the report name adds 21
        to the 255 a file name can hold."""
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)
        sent = "测" * 120 + ".BIN"
        assert len(sent.encode()) > 255  # precondition

        resp = _upload(client, sent)

        assert resp.status_int == 200
        stored = web_app.JOBS[resp.json["job_id"]]["filename"]
        assert stored.startswith("测") and stored.endswith(".BIN")
        assert len(list(report_dir.glob("upload_*"))) == 1
        report = f"12345678_{os.path.splitext(stored)[0]}_report.html"
        assert len(report.encode()) <= 255
        assert len(f"upload_12345678_{stored}".encode()) <= 255

    def test_cleaned_name_must_still_be_a_log(self, client, report_dir):
        resp = _upload(client, "report.BIN/", expect_errors=True)

        assert resp.status_int == 400
        assert list(report_dir.iterdir()) == []


class TestPendingSlot:
    """A slot that leaks is gone until the server restarts, and after 64
    of them every upload is refused."""

    def test_odd_names_keep_no_slot(self, client, slots, monkeypatch):
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)

        for _ in range(5):
            assert _upload(client, "a\x00.BIN").status_int == 200
            _settle(slots)

        assert slots._value == 2

    def test_failure_while_saving_frees_the_slot(self, client, slots,
                                                 monkeypatch):
        def broken(path):
            raise ValueError("embedded null byte")
        monkeypatch.setattr(web_app, "_receive_body", broken)

        for _ in range(3):
            resp = _upload(client, "a.BIN", expect_errors=True)
            assert resp.status_int == 500

        assert slots._value == 2

    def test_thread_that_cannot_start_leaves_nothing(self, client, report_dir,
                                                     slots, monkeypatch):
        def refuse(self):
            raise RuntimeError("can't start new thread")
        monkeypatch.setattr(threading.Thread, "start", refuse)

        resp = _upload(client, "a.BIN", expect_errors=True)

        assert resp.status_int == 500
        assert slots._value == 2
        assert web_app.JOBS == {}
        assert list(report_dir.iterdir()) == []

    def test_batch_thread_that_cannot_start_leaves_nothing(
            self, client, report_dir, slots, monkeypatch):
        def refuse(self):
            raise RuntimeError("can't start new thread")
        monkeypatch.setattr(threading.Thread, "start", refuse)

        resp = client.post("/api/upload-batch", upload_files=[
            ("files", "a.BIN", b"x"), ("files", "b.BIN", b"x")])

        assert resp.json["jobs"] == []
        assert [e["filename"] for e in resp.json["errors"]] == ["a.BIN", "b.BIN"]
        assert slots._value == 2
        assert web_app.JOBS == {}
        assert list(report_dir.iterdir()) == []

    def test_discard_survives_a_name_the_system_refuses(self):
        web_app._discard("upload_\x00.BIN")


class TestTemplateName:
    """A name that matched nothing ran the default thresholds, with the
    caller none the wiser."""

    def test_unknown_template_is_refused_before_the_body(self, report_dir):
        assert get_template("nope") is None  # precondition
        _bind("/api/upload", _MustNotBeRead(), 10,
              query="filename=x.BIN&template=nope")

        reply = web_app.upload()

        assert bottle.response.status_code == 400
        assert "nope" in json.loads(reply)["error"]
        assert list(report_dir.iterdir()) == []
        assert web_app.JOBS == {}

    def test_known_template_reaches_the_analysis(self, client, slots,
                                                 monkeypatch):
        seen = []
        monkeypatch.setattr(web_app, "_run_analysis",
                            lambda *args: seen.append(args))

        resp = client.post(
            "/api/upload?filename=x.BIN&template=crash-diagnosis", b"data",
            content_type="application/octet-stream")
        _settle(slots)

        assert resp.status_int == 200
        assert [args[3] for args in seen] == ["crash-diagnosis"]

    def test_every_template_the_page_offers_exists(self, report_dir):
        offered = re.findall(r'<option value="([^"]+)">',
                             web_app._build_index_html())

        assert len(offered) >= 5  # precondition
        assert [name for name in offered if get_template(name) is None] == []


class TestJsonBody:
    """Each of these was an uncaught exception and a 500."""

    @pytest.mark.parametrize("path", [
        "/api/compare", "/api/compare-history", "/api/set-lang"])
    @pytest.mark.parametrize("body", [
        "[1, 2]", "3", "null", '"text"', "{", "", "\xff\xfe"])
    def test_body_that_is_not_an_object_is_refused(self, client, path, body):
        resp = client.post(path, body.encode("latin-1"),
                           content_type="application/json",
                           expect_errors=True)

        assert resp.status_int == 400
        assert "error" in resp.json

    @pytest.mark.parametrize("body", [
        '{"job_ids": 5}', '{"job_ids": null}', '{"job_ids": {"a": 1}}',
        '{"job_ids": [[1], [2]]}', '{"job_ids": ["x", 5, null, {}]}'])
    def test_job_ids_of_the_wrong_type_select_nothing(self, client, body):
        resp = client.post("/api/compare", body,
                           content_type="application/json",
                           expect_errors=True)

        assert resp.status_int == 400
        assert "error" in resp.json

    def test_deep_nesting_is_refused(self, client):
        body = '{"a":' * 5000 + "1" + "}" * 5000
        assert len(body) <= web_app.MAX_JSON_BYTES  # precondition

        resp = client.post("/api/compare", body,
                           content_type="application/json",
                           expect_errors=True)

        assert resp.status_int == 400

    @pytest.mark.parametrize("route", [
        web_app.compare, web_app.compare_history, web_app.set_lang])
    def test_large_body_is_refused_unread(self, report_dir, route):
        _bind("/api/compare", _MustNotBeRead(), web_app.MAX_JSON_BYTES + 1)

        reply = route()

        assert bottle.response.status_code == 400
        assert "error" in json.loads(reply)

    def test_language_of_the_wrong_type_falls_back(self, client):
        resp = client.post_json("/api/set-lang", {"lang": ["en"]})

        assert resp.json == {"ok": True, "lang": "zh"}
