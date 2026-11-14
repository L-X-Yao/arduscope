"""
Tests for lib.executive_summary — the top-level report aggregator.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.fast

from analyzers import (
    AnalysisResult,
    ACTION_OK,
    ACTION_APPLY_PARAM,
    ACTION_PHYSICAL_CHECK,
    ACTION_RUN_AUTOTUNE,
    ACTION_SAFETY_STOP,
)
from lib.executive_summary import (
    build_executive_summary,
    get_level_display,
)
from lib.i18n import t as _t
from lib.vehicle_detect import VehicleInfo


def _vehicle(**overrides) -> VehicleInfo:
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


def _result(title: str, status: str = "good",
            action_level: str | None = None,
            plain_summary: str = "",
            action_text: str = "") -> AnalysisResult:
    return AnalysisResult(
        title=title,
        status=status,
        action_level=action_level,
        plain_summary=plain_summary,
        action_text=action_text,
    )


# ====================================================================
# Empty / degenerate cases
# ====================================================================
class TestEmptyResults:
    def test_no_results_does_not_claim_good(self):
        """Empty list must not produce a misleading 'all good' headline."""
        es = build_executive_summary([])
        assert es.overall_action_level == ACTION_OK
        assert _t("headline.no_data") in es.headline
        assert es.actions == []
        assert es.counts == {
            ACTION_OK: 0,
            ACTION_APPLY_PARAM: 0,
            ACTION_PHYSICAL_CHECK: 0,
            ACTION_RUN_AUTOTUNE: 0,
            ACTION_SAFETY_STOP: 0,
        }

    def test_no_results_with_vehicle_keeps_label(self):
        es = build_executive_summary(
            [], vehicle_info=_vehicle(vehicle_type="ArduPlane",
                                      board_name="AP-QuadPlane",
                                      motor_count=8),
        )
        assert "ArduPlane" in es.vehicle_label
        assert "AP-QuadPlane" in es.vehicle_label
        assert "8" in es.vehicle_label


# ====================================================================
# All good case
# ====================================================================
class TestAllGood:
    def test_aggregates_to_ok(self):
        results = [_result(t) for t in
                   ["飞行概览", "电池", "电机", "振动", "EKF"]]
        es = build_executive_summary(results)
        assert es.overall_action_level == ACTION_OK
        assert es.overall_status == "good"
        assert es.actions == []
        assert "METHODIC" in es.headline
        assert es.counts[ACTION_OK] == 5


# ====================================================================
# Single safety_stop action
# ====================================================================
class TestSingleSafetyStop:
    def test_battery_critical_surfaces(self):
        results = [
            _result("飞行概览", "good"),
            _result(
                "电池",
                status="critical",
                action_level=ACTION_SAFETY_STOP,
                plain_summary="电池电压过低",
                action_text="请立即停飞并更换电池",
            ),
            _result("GPS", "good"),
        ]
        es = build_executive_summary(results)
        assert es.overall_action_level == ACTION_SAFETY_STOP
        assert es.overall_status == "critical"
        assert _t("action.safety_stop") in es.headline
        assert len(es.actions) == 1
        item = es.actions[0]
        assert item.title == "电池"
        assert item.icon == "🚨"
        assert "电压" in item.plain_summary
        assert "更换电池" in item.action_text


# ====================================================================
# Multiple actions sorted by severity (v2: safety_stop > others)
# ====================================================================
class TestActionSorting:
    def test_safety_stop_sorts_first(self):
        results = [
            _result("A", "warning", ACTION_PHYSICAL_CHECK,
                    plain_summary="A", action_text="check A"),
            _result("B", "critical", ACTION_SAFETY_STOP,
                    plain_summary="B", action_text="stop"),
            _result("C", "warning", ACTION_APPLY_PARAM,
                    plain_summary="C", action_text="apply"),
            _result("D", "good"),
        ]
        es = build_executive_summary(results)
        # safety_stop must come first; the rest are at same severity (1)
        # so retain input order.
        first_level = es.actions[0].level
        assert first_level == ACTION_SAFETY_STOP

    def test_same_level_keeps_input_order(self):
        """Two physical_check items must come out in input order."""
        results = [
            _result("First", "warning", ACTION_PHYSICAL_CHECK,
                    plain_summary="1", action_text="x"),
            _result("Second", "warning", ACTION_PHYSICAL_CHECK,
                    plain_summary="2", action_text="y"),
        ]
        es = build_executive_summary(results)
        assert [a.title for a in es.actions] == ["First", "Second"]


# ====================================================================
# Headline generation
# ====================================================================
class TestHeadline:
    def test_safety_stop_headline(self):
        es = build_executive_summary([
            _result("X", "critical", ACTION_SAFETY_STOP,
                    plain_summary="x", action_text="y"),
        ])
        assert _t("action.safety_stop") in es.headline

    def test_apply_param_singular_vs_plural(self):
        # 1 apply_param
        es1 = build_executive_summary([
            _result("X", "warning", ACTION_APPLY_PARAM,
                    plain_summary="x", action_text="y"),
        ])
        assert _t("headline.one_action") == es1.headline
        # 2 apply_param
        es2 = build_executive_summary([
            _result("X", "warning", ACTION_APPLY_PARAM,
                    plain_summary="x", action_text="y"),
            _result("Y", "warning", ACTION_APPLY_PARAM,
                    plain_summary="x", action_text="y"),
        ])
        assert _t("headline.n_actions", n=2) == es2.headline

    def test_physical_check_headline(self):
        es = build_executive_summary([
            _result("X", "warning", ACTION_PHYSICAL_CHECK,
                    plain_summary="x", action_text="y"),
        ])
        # diagnostic tool: headline references diagnosis result
        assert es.headline and es.headline != _t("headline.no_data")

    def test_only_info_does_not_warn(self):
        """Info-only results should not trigger any warning headline."""
        es = build_executive_summary([
            _result("X", "info"),
            _result("Y", "info"),
        ])
        assert es.overall_action_level == ACTION_OK
        # diagnostic headline references METHODIC recommendation range
        assert "METHODIC" in es.headline

    def test_only_info_overall_status_is_info(self):
        """All-info results must NOT report overall_status as 'good'."""
        es = build_executive_summary([
            _result("X", "info"),
            _result("Y", "info"),
            _result("Z", "info"),
        ])
        assert es.overall_status == "info"

    def test_good_beats_info_in_overall_status(self):
        """When some results are 'good', overall_status should be 'good'."""
        es = build_executive_summary([
            _result("X", "info"),
            _result("Y", "good"),
        ])
        assert es.overall_status == "good"


# ====================================================================
# Sub-results flattening
# ====================================================================
class TestSubResultsFlattened:
    def test_critical_subresult_surfaces(self):
        """A critical sub-result must show up in the executive summary."""
        sub = AnalysisResult(
            title="Roll PID",
            status="critical",
            action_level=ACTION_RUN_AUTOTUNE,
            plain_summary="Roll 跟踪误差大",
            action_text="跑 AUTOTUNE",
        )
        parent = AnalysisResult(
            title="PID 调参质量",
            status="critical",
            action_level=ACTION_RUN_AUTOTUNE,
            plain_summary="PID 需 AutoTune",
            action_text="跑 AUTOTUNE",
            sub_results=[sub],
        )
        es = build_executive_summary([parent])
        # Both parent and sub should appear in counts
        assert es.counts[ACTION_RUN_AUTOTUNE] == 2
        # Both should appear in actions
        titles = [a.title for a in es.actions]
        assert "PID 调参质量" in titles
        assert "Roll PID" in titles


# ====================================================================
# Fallback when analyzer didn't fill plain_summary
# ====================================================================
class TestFallbackContent:
    def test_missing_plain_summary_uses_summary(self):
        """If plain_summary empty, fall back to summary so card isn't blank."""
        results = [
            AnalysisResult(
                title="电池",
                status="critical",
                action_level=ACTION_PHYSICAL_CHECK,
                summary="技术 summary",
                plain_summary="",  # explicit empty
                action_text="",
            ),
        ]
        es = build_executive_summary(results)
        assert len(es.actions) == 1
        assert es.actions[0].plain_summary == "技术 summary"
        # Fallback action_text
        assert es.actions[0].action_text != ""


# ====================================================================
# Real analyzer integration: build summary from real-shape results
# ====================================================================
class TestRealAnalyzerShape:
    def test_battery_critical_shape(self):
        """A real battery critical result must produce safety_stop."""
        battery = AnalysisResult(
            title="电池状态",
            status="critical",
            summary="电池电压低于临界阈值，存在安全隐患",
            plain_summary="电池电压过低，请勿继续使用此电池飞行。",
            action_text="立即停飞并更换电池",
            action_level=ACTION_SAFETY_STOP,
        )
        es = build_executive_summary([battery])
        assert es.overall_action_level == ACTION_SAFETY_STOP
        assert _t("action.safety_stop") in es.headline
        assert es.actions[0].icon == "🚨"

    def test_pid_run_autotune_shape(self):
        """A real pid_tuning result must produce run_autotune level."""
        pid = AnalysisResult(
            title="PID 调参质量",
            status="warning",
            summary="各轴状态: Roll=⚠",
            plain_summary="飞控姿态控制略有偏差，建议跑 AutoTune。",
            action_text="切到 AUTOTUNE 模式重飞",
            action_level=ACTION_RUN_AUTOTUNE,
        )
        es = build_executive_summary([pid])
        assert es.overall_action_level == ACTION_RUN_AUTOTUNE
        assert es.actions[0].icon == "🎯"

    def test_notch_apply_param_shape(self):
        """A real notch_tuning result must produce apply_param level."""
        notch = AnalysisResult(
            title="调参建议",
            status="critical",
            summary="发现 5 项需要调整的参数",
            plain_summary="工具已为你算好 5 个参数",
            action_text="在 Mission Planner Full Parameter List 改",
            action_level=ACTION_APPLY_PARAM,
        )
        es = build_executive_summary([notch])
        assert es.overall_action_level == ACTION_APPLY_PARAM
        assert es.actions[0].icon == "⚙️"


# ====================================================================
# get_level_display utility (v2)
# ====================================================================
class TestLevelDisplay:
    @pytest.mark.parametrize("level,icon", [
        (ACTION_OK, "✅"),
        (ACTION_APPLY_PARAM, "⚙️"),
        (ACTION_PHYSICAL_CHECK, "🔧"),
        (ACTION_RUN_AUTOTUNE, "🎯"),
        (ACTION_SAFETY_STOP, "🚨"),
    ])
    def test_icons(self, level, icon):
        assert get_level_display(level)["icon"] == icon

    def test_severity_ordering(self):
        """v2: ok < (apply/physical/autotune) < safety_stop"""
        ok = get_level_display(ACTION_OK)["severity"]
        apply_p = get_level_display(ACTION_APPLY_PARAM)["severity"]
        physical = get_level_display(ACTION_PHYSICAL_CHECK)["severity"]
        autotune = get_level_display(ACTION_RUN_AUTOTUNE)["severity"]
        stop = get_level_display(ACTION_SAFETY_STOP)["severity"]
        assert ok < apply_p
        assert apply_p == physical == autotune  # all action-level severity 1
        assert autotune < stop

    def test_unknown_level_falls_back_to_ok(self):
        d = get_level_display("garbage")
        assert d["icon"] == "✅"
