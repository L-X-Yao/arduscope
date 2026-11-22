"""Readable log damage reaches the live job payload and its card."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from unittest import mock

import pytest
from webtest import TestApp

import web_app
from arduscope.lib.i18n import set_locale, t
from tests.binlog import flight_log


@pytest.fixture
def client(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    monkeypatch.setattr(web_app, "REPORT_DIR", str(report_dir))
    monkeypatch.setattr(web_app.STATE.history, "path",
                        str(tmp_path / "history.json"))
    monkeypatch.setattr(web_app.STATE.history, "entries", [])
    monkeypatch.setattr(web_app.STATE.jobs, "jobs", {})
    return TestApp(web_app.app)


def test_job_payload_carries_damage_without_persisting_it(client, tmp_path):
    source = flight_log(duration_s=8.0).save(tmp_path / "source.bin")
    data = open(source, "rb").read()
    cut = len(data) // 2
    upload = tmp_path / "reports" / "upload_damage.bin"
    upload.write_bytes(data[:cut] + bytes(8192) + data[cut:])

    class NoWorker:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    with mock.patch.object(web_app.threading, "Thread", NoWorker):
        web_app._start_job("damage", str(upload), "damage.bin", len(data) + 8192)
    assert client.get("/api/jobs/damage").json["damage"] is None

    web_app._run_analysis("damage", str(upload), "damage.bin")

    payload = client.get("/api/jobs/damage").json
    assert payload["status"] == "done"
    assert abs(payload["damage"]["not_log_bytes"] - 8192) <= 255
    assert cut < payload["damage"]["first_offset"] <= cut + 255
    assert web_app.STATE.history.entries
    assert all("damage" not in entry
               for entry in web_app.STATE.history.entries)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_rendered_job_card_badges_only_damage():
    set_locale("en")
    try:
        page = web_app._build_index_html()
        i18n = re.search(r"const I18N = (\{.*?\});", page, re.S).group(1)
        render = re.search(
            r"function renderJobCard\(job\) \{.*?\n\}", page, re.S).group(0)
        esc = re.search(r"function esc\(s\) \{.*?\n\}", page, re.S).group(0)
        job = {
            "id": "damage", "filename": "damage.bin", "status": "idle",
            "file_size": 0, "vehicle_info": None, "health_score": None,
            "elapsed": 0, "agent": False, "issues": [],
            "analyzer_results": None, "agent_fallback": "",
            "archive_warning": "",
            "damage": {"not_log_bytes": 8192, "first_offset": 1234},
        }
        script = (
            f"const I18N = {i18n};\n{esc}\n{render}\n"
            f"const damaged = {json.dumps(job)};\n"
            f"const clean = {{...damaged, damage: null}};\n"
            "process.stdout.write(JSON.stringify("
            "[renderJobCard(damaged), renderJobCard(clean)]));"
        )
        done = subprocess.run(["node", "-e", script], capture_output=True,
                              text=True, check=True, timeout=30)
        damaged, clean = json.loads(done.stdout)

        assert t("web.js_damage") in damaged
        assert "8192 bytes at byte 1234" in damaged
        assert t("web.js_damage") not in clean
    finally:
        set_locale("zh")
