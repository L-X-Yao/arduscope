"""
Tests for modules that had 0% coverage in the fourth audit's coverage run:
  - lib/templates.py
  - lib/ai_diagnosis.py (rule-based engine)
  - lib/html_report.py (HTML generation)

These modules are only exercised by the end-to-end CLI path, not by
individual analyzer unit tests. The goal here is to verify their core
behavior with mock AnalysisResult data so bugs don't sit undetected.
"""

from __future__ import annotations

from tests._repo import REPO_ROOT, TESTS_DIR

import os

import plotly.graph_objects as go

from analyzers import AnalysisResult
from lib.ai_diagnosis import generate_diagnosis
from lib.html_report import HtmlReportGenerator
from lib.templates import get_template, list_templates
from lib.i18n import t as _t
from lib.vehicle_detect import VehicleInfo


# Resolve source paths relative to this test file so CI and local
# environments both work (hardcoded absolute paths break in CI).
_TESTS_DIR = str(TESTS_DIR)
_PKG_DIR = str(REPO_ROOT)


def _mock_vehicle(**overrides) -> VehicleInfo:
    defaults = dict(
        vehicle_type="ArduCopter",
        board_name="AP-Copter",
        firmware_version="V4.6.3",
        frame_class=1,
        motor_count=4,
        imu_count=3,
        is_quadplane=False,
    )
    defaults.update(overrides)
    return VehicleInfo(**defaults)


def _mock_result(key: str, status: str = "good",
                 summary: str = "") -> AnalysisResult:
    titles = {
        "vibration": "振动",
        "motor": "电机",
        "gps": "GPS质量",
        "ekf": "EKF",
        "battery": "电池",
        "attitude": "姿态控制",
        "summary": "飞行概览",
    }
    return AnalysisResult(
        title=titles.get(key, key),
        status=status,
        summary=summary or f"{key} {status}",
        details=[f"{key} detail line 1", f"{key} detail line 2"],
        figures=[],
    )


# ====================================================================
# templates.py (was 0% coverage)
# ====================================================================
class TestTemplates:
    def test_list_templates_not_empty(self):
        tpls = list_templates()
        assert len(tpls) >= 5, "Expected at least the 5 shipped templates"

    def test_get_template_known(self):
        assert get_template("quadplane-accept") is not None
        assert get_template("tuning") is not None

    def test_get_template_unknown_returns_none(self):
        assert get_template("nonexistent-template") is None

    def test_templates_have_unique_names(self):
        names = [t.name for t in list_templates()]
        assert len(names) == len(set(names)), "Template names must be unique"

    def test_template_threshold_keys_valid(self):
        """Any template threshold overrides must match real Thresholds fields."""
        from lib.thresholds import Thresholds
        valid = {f.name for f in Thresholds.__dataclass_fields__.values()}
        for tpl in list_templates():
            for key in tpl.thresholds:
                assert key in valid, \
                    f"Template {tpl.name} sets unknown threshold {key}"

    def test_template_analyzer_keys_valid(self):
        """Any template analyzer list must reference real registry keys."""
        from analyzers import discover_analyzers
        valid = {k for k, _cls in discover_analyzers()}
        for tpl in list_templates():
            for key in tpl.analyzers:
                assert key in valid, \
                    f"Template {tpl.name} references unknown analyzer {key}"


# ====================================================================
# ai_diagnosis.py rule-based engine (was 0% coverage)
# ====================================================================
class TestAiDiagnosisRuleBased:
    def test_empty_results_returns_info(self):
        result = generate_diagnosis(
            results_by_key={},
            vehicle_info=_mock_vehicle(),
            health_score=100,
            flight_duration=0.0,
            provider="rule",
        )
        assert result.title == _t("ai.report_title")
        assert result.status in ("good", "warning", "critical", "info")

    def test_all_good_results_is_good(self):
        results = {
            k: _mock_result(k, "good") for k in
            ["vibration", "motor", "gps", "ekf", "battery", "attitude"]
        }
        result = generate_diagnosis(
            results_by_key=results,
            vehicle_info=_mock_vehicle(),
            health_score=95,
            flight_duration=120.0,
            provider="rule",
        )
        assert result.status == "good"

    def test_vibration_plus_motor_triggers_cross_rule(self):
        """Both vibration AND motor critical should fire the mechanical rule."""
        results = {
            "vibration": _mock_result("vibration", "critical"),
            "motor": _mock_result("motor", "critical"),
        }
        result = generate_diagnosis(
            results_by_key=results,
            vehicle_info=_mock_vehicle(),
            health_score=40,
            flight_duration=60.0,
            provider="rule",
        )
        # The cross rule should escalate status to warning or critical
        assert result.status in ("warning", "critical")
        # And mention mechanical/motor in details
        detail_text = " ".join(result.details)
        assert any(kw in detail_text for kw in
                   ("机械", "电机", "桨叶", "交叉关联")), \
            f"Missing cross-correlation text: {result.details}"

    def test_gps_plus_ekf_triggers_cross_rule(self):
        results = {
            "gps": _mock_result("gps", "warning"),
            "ekf": _mock_result("ekf", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results,
            vehicle_info=_mock_vehicle(),
            health_score=70,
            flight_duration=60.0,
            provider="rule",
        )
        detail_text = " ".join(result.details)
        # GPS-EKF correlation rule should fire
        assert "GPS" in detail_text or "EKF" in detail_text

    def test_single_critical_reports_individual_issue(self):
        """A lone critical analyzer should surface as an individual issue."""
        results = {
            "battery": _mock_result("battery", "critical",
                                     summary="电压过低 11.2V"),
        }
        result = generate_diagnosis(
            results_by_key=results,
            vehicle_info=_mock_vehicle(),
            health_score=30,
            flight_duration=120.0,
            provider="rule",
        )
        # Status should be elevated
        assert result.status in ("warning", "critical")

    def test_empty_related_keys_safe(self):
        """related_keys=[] must not crash the individual issues path."""
        # This is a regression test for Round 4 bug #46
        results = {
            "compass": _mock_result("compass", "warning"),
        }
        # Should not raise IndexError
        result = generate_diagnosis(
            results_by_key=results,
            vehicle_info=_mock_vehicle(),
            health_score=80,
            flight_duration=60.0,
            provider="rule",
        )
        assert result is not None

    def test_gps_glitch_vibration_cross_rule(self):
        results = {
            "gps_glitch": _mock_result("gps_glitch", "warning"),
            "vibration": _mock_result("vibration", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=60, flight_duration=60.0, provider="rule",
        )
        detail_text = " ".join(result.details)
        assert "GPS" in detail_text

    def test_battery_power_cross_rule(self):
        results = {
            "battery": _mock_result("battery", "critical"),
            "power": _mock_result("power", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=40, flight_duration=60.0, provider="rule",
        )
        assert result.status in ("warning", "critical")
        detail_text = " ".join(result.details)
        assert any(kw in detail_text for kw in ("电池", "Battery", "功率", "Power"))

    def test_servo_dither_pid_cross_rule(self):
        results = {
            "servo_dither": _mock_result("servo_dither", "warning"),
            "pid_tuning": _mock_result("pid_tuning", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=60, flight_duration=60.0, provider="rule",
        )
        detail_text = " ".join(result.details)
        assert any(kw in detail_text for kw in ("PID", "伺服", "Servo", "抖动", "dither"))

    def test_clock_log_cpu_cross_rule(self):
        results = {
            "clock_health": _mock_result("clock_health", "warning"),
            "log_health": _mock_result("log_health", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=60, flight_duration=60.0, provider="rule",
        )
        detail_text = " ".join(result.details)
        assert any(kw in detail_text for kw in ("CPU", "时钟", "Clock", "日志", "Log"))

    def test_compass_dual_fault_cross_rule(self):
        results = {
            "compass": _mock_result("compass", "critical"),
            "compass_consistency": _mock_result("compass_consistency", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=50, flight_duration=60.0, provider="rule",
        )
        detail_text = " ".join(result.details)
        assert any(kw in detail_text for kw in ("罗盘", "Compass", "compass"))

    def test_gyro_sat_attitude_cross_rule(self):
        results = {
            "gyro_saturation": _mock_result("gyro_saturation", "critical"),
            "attitude": _mock_result("attitude", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=40, flight_duration=60.0, provider="rule",
        )
        assert result.status in ("warning", "critical")

    def test_airspeed_tecs_cross_rule(self):
        results = {
            "airspeed": _mock_result("airspeed", "warning"),
            "tecs_tuning": _mock_result("tecs_tuning", "warning"),
        }
        result = generate_diagnosis(
            results_by_key=results, vehicle_info=_mock_vehicle(),
            health_score=60, flight_duration=60.0, provider="rule",
        )
        detail_text = " ".join(result.details)
        assert any(kw in detail_text for kw in ("TECS", "空速", "Airspeed", "airspeed"))


# ====================================================================
# html_report.py generator (was 0% coverage)
# ====================================================================
class TestHtmlReportGenerator:
    def test_generate_returns_full_html_document(self):
        """Basic smoke: generator produces valid HTML structure."""
        vehicle = _mock_vehicle()
        results = [
            _mock_result("summary", "good"),
            _mock_result("vibration", "warning"),
            _mock_result("gps", "good"),
        ]
        gen = HtmlReportGenerator(
            vehicle_info=vehicle,
            results=results,
            log_filename="test.bin",
            flight_duration=180.0,
            health_score=85,
        )
        html = gen.generate()
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "test.bin" in html
        assert "AP-Copter" in html
        # Each result title must appear in output
        for r in results:
            assert r.title in html, f"Missing title {r.title}"

    def test_generate_contains_version(self):
        """Report must embed ArduScope version string."""
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=[_mock_result("summary", "good")],
            log_filename="test.bin",
            flight_duration=60.0,
            health_score=90,
        )
        html = gen.generate()
        assert "ArduScope v" in html

    def test_generate_escapes_log_filename(self):
        """Log filenames with HTML-special characters must be escaped."""
        vehicle = _mock_vehicle()
        gen = HtmlReportGenerator(
            vehicle_info=vehicle,
            results=[],
            log_filename="<script>alert(1)</script>.bin",
        )
        html = gen.generate()
        assert "<script>alert(1)</script>.bin" not in html
        assert "&lt;script&gt;" in html

    def test_generate_without_gps_track_omits_map(self):
        """No GPS track → no map panel element or Leaflet assets."""
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=[_mock_result("summary", "good")],
            gps_track=None,
        )
        html = gen.generate()
        # The actual panel div and Leaflet CDN must be absent.
        # CSS rules for .map-panel are always emitted, so don't check that.
        assert 'id="map-panel"' not in html
        assert "leaflet.js" not in html
        assert "initMap" not in html

    def test_generate_with_gps_track_includes_map(self):
        """GPS track → map panel + Leaflet assets."""
        gps_track = {
            "lat": [39.9, 39.91, 39.92],
            "lng": [116.4, 116.41, 116.42],
            "alt": [10.0, 15.0, 20.0],
            "time_s": [0.0, 1.0, 2.0],
        }
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=[_mock_result("summary", "good")],
            gps_track=gps_track,
        )
        html = gen.generate()
        assert 'id="map-panel"' in html
        assert "leaflet.js" in html
        assert "39.9" in html

    def test_generate_handles_empty_results(self):
        """No analyzer results → still produces a valid document."""
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=[],
        )
        html = gen.generate()
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_generate_with_figures(self):
        """Results containing plotly figures must be rendered."""
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[1, 2, 3], y=[4, 5, 6], name="test"))
        result = AnalysisResult(
            title="测试图表",
            status="good",
            summary="has figure",
            details=["有一个图表"],
            figures=[fig],
        )
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=[result],
        )
        html = gen.generate()
        assert "测试图表" in html
        # Plotly embeds figures via script/div — look for the class
        assert "plotly" in html.lower()

    def test_write_produces_file(self, tmp_path):
        """write() must write valid UTF-8 to disk."""
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=[_mock_result("summary", "good", summary="测试 summary")],
            log_filename="测试.bin",
        )
        out_path = tmp_path / "report.html"
        gen.write(str(out_path))
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "测试 summary" in content
        assert "测试.bin" in content


# ====================================================================
# Bug #48: flight_report._export_param_file encoding
# ====================================================================
class TestFlightReportParamExport:
    def test_param_file_open_uses_utf8_encoding(self):
        """_export_param_file must specify encoding=utf-8."""
        with open(os.path.join(_PKG_DIR, "flight_report.py"),
                  encoding="utf-8") as f:
            src = f.read()
        # The open() for param_path must include encoding="utf-8"
        assert 'open(param_path, "w", encoding="utf-8")' in src, \
            "Param file must be written as UTF-8 for Chinese path safety"


# ====================================================================
# P1: Executive summary section in HTML report
# ====================================================================
class TestHtmlReportExecutiveSummary:
    """The HTML report must surface the executive summary."""

    def _gen(self, results, **kwargs):
        gen = HtmlReportGenerator(
            vehicle_info=_mock_vehicle(),
            results=results,
            log_filename=kwargs.get("log_filename", "t.bin"),
            flight_duration=kwargs.get("flight_duration", 60.0),
        )
        return gen.generate()

    def test_all_good_executive_summary_present(self):
        """A clean flight log must produce an exec-summary card with the
        OK label."""
        html_out = self._gen([
            _mock_result("summary", "good"),
            _mock_result("battery", "good"),
        ])
        assert 'class="exec-summary' in html_out
        assert "level-ok" in html_out
        assert 'class="exec-actions"' not in html_out
        assert _t("report.conclusion_label", level=_t("status.good")) in html_out

    def test_safety_stop_visible_in_summary(self):
        """A safety_stop critical must turn the exec-summary red and
        list the action."""
        from analyzers import ACTION_SAFETY_STOP
        html_out = self._gen([
            _mock_result("summary", "good"),
            AnalysisResult(
                title="电池状态",
                status="critical",
                action_level=ACTION_SAFETY_STOP,
                plain_summary="电池电压过低，请勿继续飞行。",
                action_text="立即停飞并更换电池",
            ),
        ])
        assert "level-safety-stop" in html_out
        assert _t("action.safety_stop") in html_out
        assert "电池电压过低" in html_out  # test fixture data
        assert "立即停飞并更换电池" in html_out  # test fixture data

    def test_apply_param_appears_in_summary(self):
        """An apply_param action must render with the gear icon."""
        from analyzers import ACTION_APPLY_PARAM
        html_out = self._gen([
            AnalysisResult(
                title="调参建议",
                status="critical",
                action_level=ACTION_APPLY_PARAM,
                plain_summary="工具已为你算好 5 个参数",
                action_text="在 Mission Planner Full Parameter List 改",
            ),
        ])
        assert "level-apply-param" in html_out
        assert "工具已为你算好" in html_out  # test fixture data
        assert "Mission Planner" in html_out
        assert _t("action.apply_param") in html_out

    def test_physical_check_appears_in_summary(self):
        from analyzers import ACTION_PHYSICAL_CHECK
        html_out = self._gen([
            AnalysisResult(
                title="振动分析",
                status="warning",
                action_level=ACTION_PHYSICAL_CHECK,
                plain_summary="机身振动偏大，需检查桨叶",
                action_text="检查桨叶/螺丝",
            ),
        ])
        assert "level-physical-check" in html_out
        assert "桨叶" in html_out  # test fixture data
        assert _t("action.physical_check") in html_out

    def test_run_autotune_appears_in_summary(self):
        from analyzers import ACTION_RUN_AUTOTUNE
        html_out = self._gen([
            AnalysisResult(
                title="PID 调参质量",
                status="warning",
                action_level=ACTION_RUN_AUTOTUNE,
                plain_summary="飞控姿态偏差",
                action_text="切到 AUTOTUNE 重飞",
            ),
        ])
        assert "level-run-autotune" in html_out
        assert "AUTOTUNE" in html_out
        assert _t("action.run_autotune") in html_out

    def test_actions_sorted_by_severity_in_html(self):
        """The HTML must render the most severe action first."""
        from analyzers import ACTION_SAFETY_STOP, ACTION_PHYSICAL_CHECK
        html_out = self._gen([
            AnalysisResult(
                title="振动",
                status="warning",
                action_level=ACTION_PHYSICAL_CHECK,
                plain_summary="vib",
                action_text="vt",
            ),
            AnalysisResult(
                title="电池",
                status="critical",
                action_level=ACTION_SAFETY_STOP,
                plain_summary="batt",
                action_text="bt",
            ),
        ])
        # Battery (safety_stop) must come before vibration (physical_check)
        idx_batt = html_out.find('<div class="exec-action-title">电池')
        idx_vib = html_out.find('<div class="exec-action-title">振动')
        assert idx_batt > 0 and idx_vib > 0
        assert idx_batt < idx_vib, "Higher severity must render first"

    def test_executive_summary_renders_before_dashboard(self):
        """Executive summary must appear above the technical dashboard."""
        html_out = self._gen([_mock_result("summary", "good")])
        idx_exec = html_out.find('class="exec-summary')
        idx_dash = html_out.find('class="dashboard')
        # Both blocks must exist: find() returning -1 previously made
        # `-1 < idx_dash` pass with the exec summary entirely missing.
        assert idx_exec != -1, "exec-summary block missing from report"
        assert idx_dash != -1, "dashboard block missing from report"
        assert idx_exec < idx_dash, "Executive summary must come first"

    def test_legacy_results_without_action_level_still_render(self):
        """Back-compat: an analyzer that doesn't set action_level must
        not break the report. Default mapping → physical_check."""
        html_out = self._gen([
            AnalysisResult(title="legacy", status="warning"),
        ])
        # v2 default mapping puts warning at physical_check
        assert "level-physical-check" in html_out

    def test_empty_results_still_renders(self):
        """No analyzer outputs at all → still produces a valid document
        with an "no data" message."""
        html_out = self._gen([])
        assert html_out.startswith("<!DOCTYPE html>")
        assert _t("headline.no_data") in html_out
        assert "level-ok" in html_out  # Default for empty


# ====================================================================
# Regression: t() must accept 'key' as a format kwarg
# ====================================================================
class TestI18nKeyKwarg:
    def test_t_accepts_key_as_format_kwarg(self):
        """t('web.analyzing', key='vibration') must not raise."""
        result = _t("web.analyzing", key="vibration")
        assert "vibration" in result
