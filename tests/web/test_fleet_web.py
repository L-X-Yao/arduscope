"""
Fast handler-level tests for the web fleet pages (Phase 2 of
docs/fleet_history_design.md).

These drive the real bottle handlers (no HTTP server) against a real
fleet.db under tmp_path, via bottle's request/response binding — the
same pattern as the upload test in test_edge_cases.py.
"""

from __future__ import annotations

import io
from urllib.parse import urlencode

import bottle
import pytest

import web_app as wa
from lib import fleet_history
from lib.i18n import t
from lib.log_reader import TextMessage
from tests.lib.test_fleet_history import SERIAL, _logfile, _pipeline, _reader

pytestmark = pytest.mark.fast

SERIAL_B = "002C003D-3030510E-35363738"
BANNER_B = "Acme-H743 002C003D 3030510E 35363738"


def _reader_b():
    r = _reader()
    r.text_messages = [TextMessage(timestamp=0.6, text=BANNER_B)]
    return r


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    """Point web_app at a tmp report dir and bind a fresh response."""
    monkeypatch.setattr(wa, "REPORT_DIR", str(tmp_path))
    bottle.response.bind()
    return tmp_path


def _populate(tmp_path, n_a=2, n_b=1):
    """Archive n_a flights for airframe A and n_b for airframe B."""
    db = str(tmp_path / "fleet.db")
    for i in range(n_a):
        fleet_history.record(
            db, _pipeline(_reader(), score=90 - i * 10),
            _logfile(tmp_path, f"a{i}.BIN", f"payload-a{i}".encode()))
    for i in range(n_b):
        fleet_history.record(
            db, _pipeline(_reader_b(), score=70),
            _logfile(tmp_path, f"b{i}.BIN", f"payload-b{i}".encode()))
    return db


class TestFleetListPage:

    def test_empty_db_shows_empty_state(self, report_dir):
        ret = wa.fleet_page()
        assert t("fleet.web_empty") in ret
        assert bottle.response.status_code == 200

    def test_lists_airframes_with_links_and_scores(self, report_dir):
        db = _populate(report_dir)
        fleet_history.set_alias(db, SERIAL, "试验3号")
        ret = wa.fleet_page()
        # Both airframes present, serial links to the detail page
        assert SERIAL in ret and SERIAL_B in ret
        assert f'/fleet/{SERIAL}' in ret
        assert "试验3号" in ret
        # Latest score per airframe: A recorded 90 then 80, B recorded 70
        assert ">80</span>" in ret
        assert ">70</span>" in ret


class TestFleetDetailPage:

    def test_trend_figure_and_flight_rows(self, report_dir):
        _populate(report_dir, n_a=2)
        ret = wa.fleet_detail(SERIAL)
        assert bottle.response.status_code == 200
        # Plotly trend figure is embedded (2+ flights)
        assert "Plotly.newPlot" in ret
        # Every archived flight appears in the table
        assert "a0.BIN" in ret and "a1.BIN" in ret
        assert SERIAL in ret

    def test_single_flight_has_no_figure(self, report_dir):
        _populate(report_dir, n_a=0, n_b=1)
        ret = wa.fleet_detail(SERIAL_B)
        assert t("fleet.web_too_few") in ret
        assert "Plotly.newPlot" not in ret
        assert "b0.BIN" in ret

    def test_resolves_prefix_and_alias(self, report_dir):
        db = _populate(report_dir)
        fleet_history.set_alias(db, SERIAL, "试验3号")
        # unique serial prefix
        assert SERIAL in wa.fleet_detail("001A002B")
        # alias as typed (direct handler call)
        assert SERIAL in wa.fleet_detail("试验3号")
        # alias as bottle delivers it (URL path decoded as Latin-1)
        mangled = "试验3号".encode("utf-8").decode("latin-1")
        assert SERIAL in wa.fleet_detail(mangled)

    def test_unknown_key_is_404(self, report_dir):
        _populate(report_dir)
        ret = wa.fleet_detail("no-such-airframe")
        assert bottle.response.status_code == 404
        assert t("fleet.web_not_found", key="no-such-airframe") in ret


def _bind_post(path, data):
    """Bind bottle.request to a form POST (same idiom as test_edge_cases)."""
    body = urlencode(data).encode("utf-8")
    environ = {
        'REQUEST_METHOD': 'POST',
        'PATH_INFO': path,
        'CONTENT_LENGTH': str(len(body)),
        'CONTENT_TYPE': 'application/x-www-form-urlencoded; charset=utf-8',
        'HTTP_HOST': 'localhost',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'wsgi.input': io.BytesIO(body),
        'wsgi.errors': io.StringIO(),
        'wsgi.url_scheme': 'http',
    }
    bottle.request.bind(environ)
    bottle.response.bind()


class TestFleetAliasForm:

    def test_detail_page_carries_alias_form(self, report_dir):
        db = _populate(report_dir)
        fleet_history.set_alias(db, SERIAL, "试验3号")
        ret = wa.fleet_detail(SERIAL)
        assert f'action="/fleet/{SERIAL}/alias"' in ret
        assert 'value="试验3号"' in ret       # prefilled with current alias

    def test_post_sets_alias_and_redirects(self, report_dir):
        _populate(report_dir)
        _bind_post(f"/fleet/{SERIAL}/alias", {"alias": "试验3号"})
        with pytest.raises(bottle.HTTPResponse) as exc:
            wa.fleet_set_alias(SERIAL)
        assert exc.value.status_code in (302, 303)
        assert exc.value.headers["Location"].endswith(f"/fleet/{SERIAL}")
        frames = fleet_history.list_airframes(str(report_dir / "fleet.db"))
        row = next(f for f in frames if f["serial"] == SERIAL)
        assert row["alias"] == "试验3号"

    def test_post_empty_clears_alias(self, report_dir):
        db = _populate(report_dir)
        fleet_history.set_alias(db, SERIAL, "旧名")
        _bind_post(f"/fleet/{SERIAL}/alias", {"alias": "   "})
        with pytest.raises(bottle.HTTPResponse):
            wa.fleet_set_alias(SERIAL)
        row = next(f for f in fleet_history.list_airframes(db)
                   if f["serial"] == SERIAL)
        assert row["alias"] == ""

    def test_post_resolves_mangled_alias_key(self, report_dir):
        db = _populate(report_dir)
        fleet_history.set_alias(db, SERIAL, "试验3号")
        # key arrives Latin-1-mangled (bottle path decoding), form UTF-8
        mangled = "试验3号".encode("utf-8").decode("latin-1")
        _bind_post(f"/fleet/{SERIAL}/alias", {"alias": "新名字"})
        with pytest.raises(bottle.HTTPResponse) as exc:
            wa.fleet_set_alias(mangled)
        assert exc.value.headers["Location"].endswith(f"/fleet/{SERIAL}")
        row = next(f for f in fleet_history.list_airframes(db)
                   if f["serial"] == SERIAL)
        assert row["alias"] == "新名字"

    def test_post_unknown_key_is_404(self, report_dir):
        _populate(report_dir)
        _bind_post("/fleet/no-such/alias", {"alias": "x"})
        ret = wa.fleet_set_alias("no-such")
        assert bottle.response.status_code == 404
        assert t("fleet.web_not_found", key="no-such") in ret


class TestAliasConflict:
    def test_taken_alias_is_refused_with_the_reason(self, report_dir):
        _populate(report_dir)
        db = str(report_dir / "fleet.db")
        assert fleet_history.set_alias(db, SERIAL, "3号机")
        _bind_post(f"/fleet/{SERIAL_B}/alias", {"alias": "3号机"})
        page = wa.fleet_set_alias(SERIAL_B)
        assert bottle.response.status_code == 409
        assert t("fleet.alias_taken", alias="3号机", serial=SERIAL) in page
        assert fleet_history.resolve_serial(db, "3号机") == SERIAL
