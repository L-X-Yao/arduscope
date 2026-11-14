"""Web entry points for the online agent: settings and the upload switch."""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from webtest import TestApp

from lib.i18n import set_locale


@pytest.fixture
def app(monkeypatch, tmp_path):
    import web_app
    import lib.agent.config as agent_config

    original_report_dir = web_app.REPORT_DIR
    original_history_file = web_app.HISTORY_FILE
    original_history = web_app.HISTORY[:]
    original_jobs = dict(web_app.JOBS)
    original_allowed = web_app._ALLOW_SETTINGS

    config_path = tmp_path / "agent.json"
    monkeypatch.setattr(agent_config, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(web_app, "_ALLOW_SETTINGS", True)

    with tempfile.TemporaryDirectory() as tmpdir:
        web_app.REPORT_DIR = tmpdir
        web_app.HISTORY_FILE = os.path.join(tmpdir, "history.json")
        web_app.HISTORY = []
        web_app.JOBS = {}
        yield TestApp(web_app.app)
        web_app.REPORT_DIR = original_report_dir
        web_app.HISTORY_FILE = original_history_file
        web_app.HISTORY = original_history
        web_app.JOBS = original_jobs
        web_app._ALLOW_SETTINGS = original_allowed
    set_locale("zh")


def test_settings_page_opens_on_loopback(app):
    resp = app.get("/settings")
    assert resp.status_int == 200
    assert "ArduScope" in resp.text


def test_settings_page_names_the_configured_path(app):
    """The page used to read a CONFIG_PATH copied at import time, so a
    patched config still showed the real path."""
    import lib.agent.config as agent_config
    resp = app.get("/settings")
    assert agent_config.CONFIG_PATH in resp.text


def test_settings_page_is_404_when_exposed(app, monkeypatch):
    import web_app
    monkeypatch.setattr(web_app, "_ALLOW_SETTINGS", False)
    resp = app.get("/settings", expect_errors=True)
    assert resp.status_int == 404


def test_settings_post_saves_and_hides_the_key(app, tmp_path):
    import lib.agent.config as agent_config
    resp = app.post_json("/api/settings", {
        "provider": "openai",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key": "sk-secret-9876",
        "allow_gps": False,
        "allow_serial": False,
        "allow_text": False,
    })
    assert resp.status_int == 200
    data = resp.json
    assert data["has_key"] is True
    assert data["key_hint"] == "...9876"
    assert "api_key" not in data

    saved = json.loads(open(agent_config.CONFIG_PATH, encoding="utf-8").read())
    assert saved["model"] == "deepseek-chat"
    assert saved["api_key"] == "sk-secret-9876"


def test_settings_post_refuses_a_bad_provider(app):
    resp = app.post_json("/api/settings", {
        "provider": "nope", "model": "m", "api_key": "k",
    }, expect_errors=True)
    assert resp.status_int == 400


def test_upload_with_agent_refuses_when_not_configured(app):
    resp = app.post(
        "/api/upload?filename=test.BIN&agent=1",
        params=b"\x00" * 10,
        content_type="application/octet-stream",
        expect_errors=True,
    )
    assert resp.status_int == 400
    assert resp.json["error"]
    import web_app
    assert web_app.JOBS == {}


def test_settings_test_refuses_an_unconfigured_agent(app):
    resp = app.post_json("/api/settings/test", {
        "provider": "openai", "model": "", "api_key": "",
    })
    assert resp.json["ok"] is False
