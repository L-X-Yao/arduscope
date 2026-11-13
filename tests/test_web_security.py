"""The local web UI answers only to itself.

Threat model: the server binds 127.0.0.1, but any web page the user visits
can aim requests at it, and can repoint its own DNS name at 127.0.0.1
(rebinding), which also defeats the browser's same-origin rule.
"""

from __future__ import annotations

import json
import re
import sys
import time

import pytest
from webtest import TestApp

import web_app
from lib.html_report import HtmlReportGenerator
from lib.i18n import set_locale
from lib.results import AnalysisResult
from lib.vehicle_detect import VehicleInfo


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(web_app, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(web_app, "HISTORY", [])
    monkeypatch.setattr(web_app, "JOBS", {})
    yield TestApp(web_app.app)
    set_locale("zh")


def _host(value: str) -> dict:
    return {"HTTP_HOST": value}


def _from(origin: str, host: str = "127.0.0.1:8080") -> dict:
    return {"HTTP_HOST": host, "HTTP_ORIGIN": origin}


class TestHostHeader:
    """DNS rebinding: the page's own name resolves to us, so the browser
    treats us as same-origin. Only the Host header gives it away."""

    @pytest.mark.parametrize("host", ["evil.example", "evil.example:8080",
                                      "127.0.0.1.evil.example", "", "[::1"])
    @pytest.mark.parametrize("path", ["/", "/api/jobs", "/api/history",
                                      "/fleet", "/reports/x_report.html"])
    def test_foreign_host_is_refused(self, client, host, path):
        resp = client.get(path, extra_environ=_host(host), expect_errors=True)
        assert resp.status_int == 403

    @pytest.mark.parametrize("host", [
        "127.0.0.1:8080", "localhost:8080", "LOCALHOST:8080", "[::1]:8080",
        "127.0.0.1", "localhost"])
    def test_loopback_names_are_served(self, client, host):
        assert client.get("/", extra_environ=_host(host)).status_int == 200

    def test_explicit_address_keeps_working(self, client, monkeypatch):
        monkeypatch.setattr(web_app, "_ALLOWED_HOST_NAMES", set())
        monkeypatch.setattr(web_app, "_ACCEPT_ANY_IP", False)
        web_app._configure_allowed_hosts("192.168.1.10", [])

        ok = client.get("/", extra_environ=_host("192.168.1.10:8080"))
        other = client.get("/", extra_environ=_host("192.168.1.11:8080"),
                           expect_errors=True)
        evil = client.get("/", extra_environ=_host("evil.example:8080"),
                          expect_errors=True)

        assert ok.status_int == 200
        assert other.status_int == 403
        assert evil.status_int == 403

    def test_wildcard_bind_accepts_addresses_not_foreign_names(
            self, client, monkeypatch):
        """An IP literal cannot be rebound, so 0.0.0.0 can stay reachable
        by any address of the machine while names stay refused."""
        monkeypatch.setattr(web_app, "_ALLOWED_HOST_NAMES", set())
        monkeypatch.setattr(web_app, "_ACCEPT_ANY_IP", False)
        monkeypatch.setattr(web_app.socket, "gethostname", lambda: "GCS-Laptop")
        monkeypatch.setattr(web_app.socket, "getfqdn",
                            lambda: "gcs-laptop.lan")
        web_app._configure_allowed_hosts("0.0.0.0", ["fleet.example"])

        for good in ("10.0.0.7:8080", "gcs-laptop:8080", "gcs-laptop.lan:8080",
                     "fleet.example", "localhost:8080"):
            assert client.get("/", extra_environ=_host(good)).status_int == 200
        evil = client.get("/", extra_environ=_host("evil.example:8080"),
                          expect_errors=True)
        assert evil.status_int == 403


class TestOriginHeader:
    """CSRF: a foreign page can POST here without reading the response."""

    def _jobs(self):
        return dict(web_app.JOBS)

    @pytest.mark.parametrize("origin", [
        "https://evil.example", "http://127.0.0.1:3000", "http://localhost:8080",
        "null", "http://["])
    def test_foreign_upload_is_refused(self, client, origin):
        resp = client.post("/api/upload?filename=x.BIN", b"\xa3\x95" * 8,
                           content_type="text/plain",
                           extra_environ=_from(origin), expect_errors=True)

        assert resp.status_int == 403
        assert "error" in resp.json
        assert self._jobs() == {}

    @pytest.mark.parametrize("method, path", [
        ("post", "/api/compare"), ("post", "/api/compare-history"),
        ("post", "/api/set-lang"), ("post", "/api/upload-batch"),
        ("post", "/fleet/abc/alias"), ("delete", "/api/history")])
    def test_every_state_changing_route_is_covered(self, client, method, path):
        env = _from("https://evil.example")
        send = getattr(client, method)
        resp = (send(path, extra_environ=env, expect_errors=True)
                if method == "delete"
                else send(path, b"{}", content_type="text/plain",
                          extra_environ=env, expect_errors=True))
        assert resp.status_int == 403

    def test_foreign_cors_read_is_refused(self, client):
        resp = client.get("/api/jobs", extra_environ=_from("https://evil.example"),
                          expect_errors=True)
        assert resp.status_int == 403

    def test_same_origin_post_works(self, client):
        resp = client.post_json(
            "/api/compare-history", {"indices": []},
            extra_environ=_from("http://127.0.0.1:8080"), expect_errors=True)
        assert resp.status_int == 400       # reached the handler

    def test_host_case_does_not_matter(self, client):
        resp = client.post_json(
            "/api/compare-history", {"indices": []},
            extra_environ=_from("http://LOCALHOST:8080", "localhost:8080"),
            expect_errors=True)
        assert resp.status_int == 400

    def test_settings_origin_accepts_ipv6_loopback(self, monkeypatch):
        class Request:
            headers = {"Origin": "http://[::1]:8080",
                       "Host": "[::1]:8080"}
        monkeypatch.setattr(web_app, "request", Request())
        assert web_app._settings_same_origin()

    def test_client_without_origin_works(self, client, monkeypatch):
        """curl and scripts send no Origin, and CSRF needs a browser.

        The analysis is stubbed: a real one outlives this test, and once
        the fixture puts REPORT_DIR back to "" it writes the report and
        fleet.db into the working directory.
        """
        monkeypatch.setattr(web_app, "_run_analysis", lambda *args: None)

        resp = client.post("/api/upload?filename=x.BIN", b"data",
                           content_type="application/octet-stream")

        assert "job_id" in json.loads(resp.text)


class TestReportsDirectory:
    """/reports shares its directory with uploads, the fleet database and
    the history file. static_file labels what it does not recognise as
    text/html, so a script stored in fleet.db ran in this origin."""

    PRIVATE = ["history.json", "fleet.db", "fleet.db-wal", "fleet.db-shm",
               "upload_abcd1234_customer.BIN", "notes.txt"]

    @pytest.fixture(autouse=True)
    def _files(self, tmp_path):
        for name in self.PRIVATE + ["abcd1234_x_report.html",
                                    "compare_abcd1234.html"]:
            (tmp_path / name).write_bytes(b"<html>content</html>")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.html").write_bytes(b"<html>deep</html>")

    @pytest.mark.parametrize("name", PRIVATE)
    def test_private_files_are_not_served(self, client, name):
        resp = client.get(f"/reports/{name}", expect_errors=True)
        assert resp.status_int == 404

    @pytest.mark.parametrize("name", ["abcd1234_x_report.html",
                                      "compare_abcd1234.html"])
    def test_report_pages_are_served(self, client, name):
        resp = client.get(f"/reports/{name}")
        assert resp.status_int == 200
        assert resp.content_type == "text/html"

    @pytest.mark.parametrize("path", ["sub/deep.html", "..%2fx.html",
                                      "sub%2fdeep.html"])
    def test_only_the_top_level_is_served(self, client, path):
        resp = client.get(f"/reports/{path}", expect_errors=True)
        assert resp.status_int == 404


class TestResponseHeaders:
    def _assert_protected(self, resp):
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers[
            "Content-Security-Policy"]

    @pytest.mark.parametrize("path, status", [
        ("/", 200), ("/fleet", 200), ("/api/jobs", 200), ("/api/history", 200),
        ("/no/such/page", 404), ("/reports/missing_report.html", 404)])
    def test_every_answer_is_protected(self, client, path, status):
        resp = client.get(path, expect_errors=True)
        assert resp.status_int == status
        self._assert_protected(resp)

    def test_refused_request_is_protected(self, client):
        resp = client.get("/", extra_environ=_host("evil.example"),
                          expect_errors=True)
        assert resp.status_int == 403
        self._assert_protected(resp)

    def test_server_error_is_protected(self, client, monkeypatch):
        def boom():
            raise RuntimeError("broken")

        monkeypatch.setattr(web_app, "_build_index_html", boom)

        resp = client.get("/", expect_errors=True)

        assert resp.status_int == 500
        self._assert_protected(resp)

    def test_report_runs_in_a_sandbox(self, client, tmp_path):
        """A report holds text from a log. In a sandbox it has no access to
        this origin, so a script that got past escaping reaches nothing."""
        (tmp_path / "abcd1234_x_report.html").write_bytes(b"<html></html>")

        resp = client.get("/reports/abcd1234_x_report.html")

        policy = resp.headers["Content-Security-Policy"]
        self._assert_protected(resp)
        assert "sandbox allow-scripts" in policy
        assert "allow-same-origin" not in policy
        assert "connect-src" not in policy      # default-src 'none' covers it
        assert "default-src 'none'" in policy

    def test_app_pages_can_only_talk_to_themselves(self, client):
        policy = client.get("/").headers["Content-Security-Policy"]
        assert "default-src 'none'" in policy
        assert "connect-src 'self'" in policy
        assert "sandbox" not in policy


class TestNetworkBindWarning:
    """There is no login, so a bind beyond loopback must not pass quietly."""

    def _start(self, monkeypatch, tmp_path, *argv):
        started = []
        monkeypatch.setattr(web_app, "_run_dev_server", started.append)
        monkeypatch.setattr(sys, "argv", [
            "web_app.py", "--no-open", "--lang", "en",
            "--report-dir", str(tmp_path), *argv])
        # main() sets all of these, so give monkeypatch something to restore.
        for name, value in (("_ALLOWED_HOST_NAMES", set()),
                            ("_ACCEPT_ANY_IP", False), ("_UI_LOCALE", "zh"),
                            ("REPORT_DIR", ""), ("HISTORY_FILE", ""),
                            ("HISTORY", [])):
            monkeypatch.setattr(web_app, name, value)
        web_app.main()
        set_locale("zh")
        assert len(started) == 1

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::",
                                      "gcs-laptop"])
    def test_network_bind_warns(self, host, monkeypatch, tmp_path, capsys):
        self._start(monkeypatch, tmp_path, "--host", host)

        err = capsys.readouterr().err
        assert "WARNING" in err and host in err and "no login" in err

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_bind_stays_quiet(self, host, monkeypatch, tmp_path,
                                       capsys):
        self._start(monkeypatch, tmp_path, "--host", host)

        assert "WARNING" not in capsys.readouterr().err


class TestReportCopyButton:
    @pytest.mark.fast
    def test_copy_survives_the_sandbox(self):
        """The sandbox rejects the async clipboard API, so the button needs
        a fallback for the rejected promise, not only for a thrown error."""
        html = HtmlReportGenerator(
            VehicleInfo("ArduCopter", "CubeOrange", "V4.6.3", 1, 4, 2, False),
            [AnalysisResult(title="Summary", status="good")]).generate()

        handled = re.search(r"writeText\(ta\.value\)\.then\(\w+, \w+\)", html)
        assert handled is not None


class TestLanguagePerClient:
    """The language was one global, so one user's click switched the page
    and the reports of everyone else on the server."""

    @pytest.fixture
    def default_zh(self, client, monkeypatch):
        monkeypatch.setattr(web_app, "_UI_LOCALE", "zh")

    def _lang(self, app) -> str:
        return re.search(r'<html lang="([^"]+)"', app.get("/").text).group(1)

    def test_choice_stays_with_the_client(self, client, default_zh):
        other = TestApp(web_app.app)

        client.post_json("/api/set-lang", {"lang": "en"})

        assert self._lang(client) == "en"
        assert self._lang(other) == "zh-CN"

    def test_cookie_is_scoped_to_this_site(self, client, default_zh):
        resp = client.post_json("/api/set-lang", {"lang": "en"})

        cookie = resp.headers["Set-Cookie"].lower()
        assert cookie.startswith("lang=en")
        assert "samesite=strict" in cookie and "httponly" in cookie

    def test_server_default_applies_without_a_choice(self, client, monkeypatch):
        monkeypatch.setattr(web_app, "_UI_LOCALE", "en")
        assert self._lang(client) == "en"

    def test_unknown_cookie_falls_back(self, client, default_zh):
        client.set_cookie("lang", "xx")
        assert self._lang(client) == "zh-CN"

    def test_report_uses_the_uploader_language(self, client, default_zh,
                                               monkeypatch):
        started = []
        monkeypatch.setattr(web_app, "_run_analysis",
                            lambda *args: started.append(args))
        client.post_json("/api/set-lang", {"lang": "en"})

        client.post("/api/upload?filename=x.BIN", b"data",
                    content_type="application/octet-stream")

        for _ in range(100):
            if started:
                break
            time.sleep(0.01)
        assert started[0][4] == "en"
