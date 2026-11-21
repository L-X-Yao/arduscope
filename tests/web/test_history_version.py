"""history.json is versioned but still reads the old list format."""

from __future__ import annotations

import json

import pytest

import web_app


@pytest.fixture
def history_file(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    monkeypatch.setattr(web_app.STATE.history, "path", str(path))
    monkeypatch.setattr(web_app.STATE.history, "entries", [])
    yield path
    web_app.STATE.history.entries = []


@pytest.mark.fast
def test_a_legacy_list_loads_and_saves_with_a_version(history_file):
    history_file.write_text(json.dumps([{"filename": "a.BIN"}]), encoding="utf-8")

    web_app._load_history()
    assert web_app.STATE.history.entries[0]["filename"] == "a.BIN"

    web_app._save_history()
    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert saved["entries"][0]["filename"] == "a.BIN"


@pytest.mark.fast
def test_a_newer_history_version_is_set_aside(history_file):
    history_file.write_text(json.dumps({"version": 2, "entries": []}),
                            encoding="utf-8")

    web_app._load_history()

    assert web_app.STATE.history.entries == []
    assert (history_file.parent / "history.json.corrupt").is_file()
