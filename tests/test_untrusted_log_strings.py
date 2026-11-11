"""A .BIN can come from anyone: nothing in it may become live markup."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

import binlog
import batch_report
import multi_report
from lib.ai_diagnosis import generate_diagnosis
from lib.html_report import HtmlReportGenerator, _safe_detail
from lib.pipeline import run_analysis

ATTACK = '<svg/onload=alert(1337)>'
ESCAPED = '&lt;svg/onload=alert(1337)&gt;'
ATTR_ATTACK = '" onmouseover="alert(1337)" x="'


def _render(path: str) -> str:
    pipeline = run_analysis(path)
    diagnosis = generate_diagnosis(
        pipeline.results_by_key, pipeline.vehicle_info,
        pipeline.health_score, pipeline.reader.get_flight_duration())
    generator = HtmlReportGenerator(
        vehicle_info=pipeline.vehicle_info,
        results=[diagnosis] + pipeline.results,
        log_filename=os.path.basename(path),
        flight_duration=pipeline.reader.get_flight_duration(),
        health_score=pipeline.health_score,
        gps_track=None,
    )
    return generator.generate()


class TestSafeDetail:
    def test_log_markup_is_neutralised(self):
        assert "<svg" not in _safe_detail(ATTACK)

    def test_tag_with_attributes_is_not_a_kept_tag(self):
        assert "<b " not in _safe_detail('<b onclick="x">t</b>')

    @pytest.mark.parametrize("tag", ["strong", "em", "b", "pre"])
    def test_project_markup_survives(self, tag):
        assert _safe_detail(f"<{tag}>x</{tag}>") == f"<{tag}>x</{tag}>"

    def test_line_break_survives(self):
        assert _safe_detail("a<br>b") == "a<br>b"

    def test_already_escaped_text_is_not_escaped_twice(self):
        assert _safe_detail("a &gt; b") == "a &gt; b"

    def test_encoded_attack_cannot_slip_through_the_decode(self):
        assert "<svg" not in _safe_detail("&lt;svg onload=x&gt;")


class TestReport:
    def test_firmware_string_from_msg(self, tmp_path):
        log = binlog.flight_log("copter")
        log.write("MSG", 39_000_000, f"ArduCopter {ATTACK}")
        html = _render(log.save(tmp_path / "x.bin"))
        assert ESCAPED in html, "attack string never reached the report"
        assert ATTACK not in html

    def test_mode_name_declared_as_string(self, tmp_path):
        log = binlog.BinLog()
        log.declare("MODE", "QZBB", "TimeUS,Mode,ModeNum,Rsn")
        flight = binlog.flight_log("copter")
        flight.write("MSG", 39_000_000, "ArduCopter V4.6.3 (abc12345)")
        path = tmp_path / "x.bin"
        log.write("MODE", 3_000_000, ATTACK, 5, 1)
        path.write_bytes(bytes(log.data) + bytes(flight.data[89:]))
        html = _render(str(path))
        assert ESCAPED in html, "attack string never reached the report"
        assert ATTACK not in html

    def test_file_name(self, tmp_path):
        name = ATTACK.replace("/", "_")
        path = tmp_path / f"{name}.bin"
        html = _render(binlog.flight_log("copter").save(path))
        assert ESCAPED.replace("/", "_") in html
        assert name not in html


class TestMultiLog:
    def test_parameter_name_used_as_category(self):
        changed = [{"name": "<svg onload=x>_X", "category": "<svg onload=x>",
                    "values": [1.0, 2.0], "transitions": [0]}]
        html = multi_report._render_param_diff_html(["a", "b"], changed)
        assert html.count("&lt;svg onload=x&gt;") >= 2
        assert "<svg" not in html


class TestBatchOverview:
    def test_report_link_cannot_break_out_of_the_attribute(self, tmp_path):
        name = f'a{ATTR_ATTACK}_report.html'
        row = {
            "filename": "a.bin", "filepath": "a.bin",
            "report_path": str(tmp_path / name), "vehicle_type": "ArduCopter",
            "firmware": "V4.6.3", "board": "x", "duration": 60.0,
            "health_score": 90, "metrics": {}, "params": {},
            "n_warnings": 0, "n_criticals": 0, "error": None, "elapsed": 1.0,
        }
        out = tmp_path / "overview.html"
        batch_report.write_summary_html([row], str(out))
        html = out.read_text(encoding="utf-8")
        assert "_report.html" in html
        assert ATTR_ATTACK not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestWebEscaper:
    @staticmethod
    def _esc(value) -> str:
        import web_app
        page = web_app._build_index_html()
        function = re.search(r"function esc\(s\) \{.*?\n\}", page, re.S).group(0)
        script = f"{function}\nprocess.stdout.write(esc({json.dumps(value)}));"
        return subprocess.run(["node", "-e", script], capture_output=True,
                              text=True, check=True, timeout=30).stdout

    def test_quotes_cannot_leave_an_attribute(self):
        out = self._esc(ATTR_ATTACK)
        assert '"' not in out and "&quot;" in out

    def test_markup_is_neutralised(self):
        assert "<" not in self._esc(ATTACK)

    def test_zero_is_shown(self):
        assert self._esc(0) == "0"
