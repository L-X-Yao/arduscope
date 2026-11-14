"""
Tests for the action_level / confidence / plain_summary fields on
AnalysisResult, plus per-analyzer overrides for the safety analyzers.

Action levels: ACTION_APPLY_PARAM, ACTION_PHYSICAL_CHECK,
ACTION_RUN_AUTOTUNE, ACTION_SAFETY_STOP, ACTION_OK.
Default mapping: warning/critical → physical_check.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.fast

from analyzers import (
    AnalysisResult,
    derive_default_action_level,
    worst_action_level,
    ACTION_OK,
    ACTION_APPLY_PARAM,
    ACTION_PHYSICAL_CHECK,
    ACTION_RUN_AUTOTUNE,
    ACTION_SAFETY_STOP,
    VALID_ACTION_LEVELS,
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
    VALID_CONFIDENCE_LEVELS,
)


# ====================================================================
# AnalysisResult new fields — defaults and back-compat
# ====================================================================
class TestAnalysisResultDefaults:
    def test_minimal_construction_back_compat(self):
        """Existing analyzers building with positional/old kwargs still work."""
        r = AnalysisResult(title="t")
        assert r.status == "info"
        assert r.confidence == CONFIDENCE_HIGH
        assert r.action_level == ACTION_OK  # info → ok
        assert r.action_text == ""
        assert r.plain_summary == ""
        assert r.evidence == []

    def test_status_to_action_level_mapping(self):
        """Default mapping: v2 routes warning/critical to physical_check."""
        cases = [
            ("good", ACTION_OK),
            ("info", ACTION_OK),
            ("warning", ACTION_PHYSICAL_CHECK),
            ("critical", ACTION_PHYSICAL_CHECK),
        ]
        for status, expected in cases:
            r = AnalysisResult(title="t", status=status)
            assert r.action_level == expected, \
                f"{status} → {r.action_level}, want {expected}"

    def test_explicit_override_wins(self):
        """An analyzer can override the default mapping per-result."""
        r = AnalysisResult(title="t", status="critical",
                           action_level=ACTION_SAFETY_STOP)
        assert r.action_level == ACTION_SAFETY_STOP

    def test_unknown_status_defaults_to_ok(self):
        r = AnalysisResult(title="t", status="weird")
        assert r.action_level == ACTION_OK

    def test_evidence_defaults_to_empty_list(self):
        r1 = AnalysisResult(title="a")
        r2 = AnalysisResult(title="b")
        # Each instance must get its own list (no shared default)
        r1.evidence.append({"x": 1})
        assert r2.evidence == []

    def test_action_level_constants_exhaustive(self):
        """v2: 5 action levels."""
        assert set(VALID_ACTION_LEVELS) == {
            ACTION_OK,
            ACTION_APPLY_PARAM,
            ACTION_PHYSICAL_CHECK,
            ACTION_RUN_AUTOTUNE,
            ACTION_SAFETY_STOP,
        }

    def test_confidence_constants_exhaustive(self):
        assert set(VALID_CONFIDENCE_LEVELS) == {
            CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
        }


# ====================================================================
# derive_default_action_level helper (v2 mapping)
# ====================================================================
class TestDeriveDefault:
    @pytest.mark.parametrize("status,expected", [
        ("good", ACTION_OK),
        ("info", ACTION_OK),
        ("warning", ACTION_PHYSICAL_CHECK),
        ("critical", ACTION_PHYSICAL_CHECK),
        ("nonsense", ACTION_OK),
    ])
    def test_mapping(self, status, expected):
        assert derive_default_action_level(status) == expected


# ====================================================================
# worst_action_level aggregation
# ====================================================================
class TestWorstActionLevel:
    def test_severity_ordering(self):
        # Empty → ok
        assert worst_action_level() == ACTION_OK
        # Single
        assert worst_action_level(ACTION_OK) == ACTION_OK
        assert worst_action_level(ACTION_SAFETY_STOP) == ACTION_SAFETY_STOP
        # Pairs — physical/apply/autotune are at same severity (1)
        assert worst_action_level(ACTION_OK, ACTION_PHYSICAL_CHECK) \
            == ACTION_PHYSICAL_CHECK
        assert worst_action_level(ACTION_OK, ACTION_APPLY_PARAM) \
            == ACTION_APPLY_PARAM
        assert worst_action_level(ACTION_PHYSICAL_CHECK, ACTION_SAFETY_STOP) \
            == ACTION_SAFETY_STOP
        # Order independent
        assert worst_action_level(ACTION_SAFETY_STOP, ACTION_OK) \
            == ACTION_SAFETY_STOP

    def test_unknown_level_treated_as_ok(self):
        """Unknown action levels shouldn't crash; treat as severity 0."""
        assert worst_action_level("garbage", ACTION_PHYSICAL_CHECK) \
            == ACTION_PHYSICAL_CHECK


# ====================================================================
# Per-analyzer source-level checks: safety analyzers must use v2
# action_level names with concrete action_text guidance.
# ====================================================================
class TestSafetyCriticalAnalyzersExplicit:
    """Safety analyzers must route real verdicts to the right action
    level — asserted on BEHAVIOR (synthetic scenarios through the real
    analyze()), not on source substrings, which both branches of every
    if/else always contain. Each test asserts the triggering status as
    a precondition first. (notch_tuning's apply_param routing is
    asserted behaviorally in test_notch_tuning.py on a real log.)"""

    def test_battery_critical_escalates_to_safety_stop(self):
        import numpy as np  # noqa: F401 — fixture module needs it loaded
        from analyzers.battery import BatteryAnalyzer
        from tests.test_synthetic_coverage import (
            TestBatterySynthetic, _thresholds, _vehicle)
        reader = TestBatterySynthetic()._build_reader(
            volt_range=(24.0, 19.5))   # below BATT_CRT_VOLT=20
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert result.status == "critical"     # precondition
        assert result.action_level == ACTION_SAFETY_STOP
        assert result.action_text                # concrete instructions

    def test_battery_warning_routes_to_physical_check(self):
        from analyzers.battery import BatteryAnalyzer
        from tests.test_synthetic_coverage import (
            TestBatterySynthetic, _thresholds, _vehicle)
        reader = TestBatterySynthetic()._build_reader(
            volt_range=(24.0, 20.5))   # below LOW=21, above CRT=20
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert result.status == "warning"      # precondition
        assert result.action_level == ACTION_PHYSICAL_CHECK

    def test_ekf_critical_escalates_to_safety_stop(self):
        import numpy as np
        from analyzers.ekf import EKFAnalyzer
        from tests.test_synthetic_coverage import (
            SyntheticReader, _thresholds, _vehicle)
        n = 100
        reader = SyntheticReader(messages={"XKF4": {
            "_timestamp": np.arange(n, dtype=float),
            "SV": np.full(n, 1.5),    # sustained variance >> crit
            "SP": np.full(n, 1.5),
            "SH": np.zeros(n),
            "SM": np.zeros(n),
            "SS": np.zeros(n),
            "C": np.zeros(n),
        }}, params={"FS_EKF_THRESH": 0.8})
        result = EKFAnalyzer(reader, _thresholds(reader),
                             _vehicle()).analyze()
        assert result.status == "critical"     # precondition
        assert result.action_level == ACTION_SAFETY_STOP
        assert result.action_text

    def test_vibration_critical_routes_to_physical_check(self):
        from analyzers.vibration import VibrationAnalyzer
        from tests.test_synthetic_coverage import (
            TestVibrationSynthetic, _thresholds, _vehicle)
        reader = TestVibrationSynthetic()._build_reader(max_vibe=35.0)
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "critical"     # precondition
        assert result.action_level == ACTION_PHYSICAL_CHECK
        assert result.action_text

    def test_motor_critical_without_gains_physical_check(self):
        import numpy as np
        from analyzers.motor import MotorAnalyzer
        from tests.test_synthetic_coverage import (
            SyntheticReader, _thresholds, _vehicle)
        n = 600
        ts = np.linspace(0, 300, n)
        # 21.4% imbalance (crit >20%), no tunable-gap params in the log
        reader = SyntheticReader(messages={"RCOU": {
            "_timestamp": ts,
            "C1": np.full(n, 1700.0),
            "C2": np.full(n, 1300.0),
            "C3": np.full(n, 1300.0),
            "C4": np.full(n, 1300.0),
            **{f"C{i}": np.full(n, 1500.0) for i in range(5, 9)},
        }}, params={"MOT_PWM_MIN": 1000, "MOT_PWM_MAX": 2000,
                    "FRAME_CLASS": 1},
            armed_intervals=[(0.0, 300.0)])
        result = MotorAnalyzer(reader, _thresholds(reader),
                               _vehicle()).analyze()
        assert result.status == "critical"     # precondition
        assert result.param_suggestions == []  # precondition: no fix
        assert result.action_level == ACTION_PHYSICAL_CHECK

    def test_failsafe_trigger_routes_to_physical_check(self):
        # A fired failsafe means "go find out why", never apply_param —
        # the unsourced BATT_LOW_VOLT auto-suggestion was removed.
        from analyzers.failsafe import FailsafeAnalyzer
        from lib.log_reader import ErrorEvent
        from lib.thresholds import Thresholds
        from tests.test_synthetic_coverage import (
            SyntheticReader, _vehicle)
        reader = SyntheticReader(messages={})
        reader.errors = [ErrorEvent(timestamp=10.0, subsys=6, ecode=1)]
        result = FailsafeAnalyzer(reader, Thresholds(),
                                  _vehicle()).analyze()
        assert result.status == "critical"     # precondition
        assert result.action_level == ACTION_PHYSICAL_CHECK
        assert result.param_suggestions == []

    def test_pid_tuning_without_gains_recommends_autotune(self):
        import numpy as np
        from analyzers.pid_tuning import PIDTuningAnalyzer
        from lib.thresholds import Thresholds
        from tests.test_synthetic_coverage import (
            SyntheticReader, _vehicle)
        n = 400
        ts = np.linspace(0.0, 100.0, n)
        tar = np.sin(2 * np.pi * 0.25 * ts)
        # Saturated rate output (RMS 0.9 > crit 0.15) + flying + no
        # ATC_RAT_* params in the log -> no computable suggestion.
        reader = SyntheticReader(messages={
            "PIDR": {"_timestamp": ts, "Tar": tar, "Act": 0.3 * tar,
                     "P": 0.3 * tar, "I": np.full(n, 0.05),
                     "D": np.full(n, 0.01), "FF": np.full(n, 0.02)},
            "RATE": {"_timestamp": ts,
                     "ROut": 0.9 * np.sign(np.sin(2 * np.pi * 2 * ts)),
                     "RDes": np.zeros(n)},
            "POS": {"_timestamp": ts, "Alt": np.linspace(0, 80, n)},
        }, params={})
        result = PIDTuningAnalyzer(reader, Thresholds(),
                                   _vehicle()).analyze()
        assert result.status == "critical"     # precondition
        assert result.param_suggestions == []  # precondition: no gains
        assert result.action_level == ACTION_RUN_AUTOTUNE


# ====================================================================
# make_result helper accepts new keyword-only fields
# ====================================================================
class TestBaseAnalyzerMakeResult:
    def test_make_result_passes_through_new_fields(self):
        from analyzers import BaseAnalyzer

        class _Dummy(BaseAnalyzer):
            registry_key = "_dummy"
            required_messages = []

            def analyze(self):
                return self.make_result(
                    title="t",
                    status="critical",
                    action_level=ACTION_SAFETY_STOP,
                    plain_summary="immediate stop",
                    action_text="land now",
                    confidence=CONFIDENCE_LOW,
                    evidence=[{"v": 1}],
                )

        d = _Dummy.__new__(_Dummy)  # bypass __init__
        result = d.analyze()
        assert result.action_level == ACTION_SAFETY_STOP
        assert result.plain_summary == "immediate stop"
        assert result.action_text == "land now"
        assert result.confidence == CONFIDENCE_LOW
        assert result.evidence == [{"v": 1}]


# ====================================================================
# v2 specific: thresholds.py must reference convergence_criteria.md
# ====================================================================
class TestThresholdsHaveAuthoritativeSources:
    """Every threshold MUST cite a source from
    docs/convergence_criteria.md."""

    def test_thresholds_module_documented(self):
        from lib import thresholds as th
        src = inspect.getsource(th)
        # Required source identifiers from convergence_criteria.md
        assert "METHODIC" in src, \
            "thresholds must cite METHODIC tuning guide"
        assert "ARDU-WIKI-VIBE" in src, \
            "thresholds must cite ArduPilot vibration wiki"

    def test_pid_rate_out_threshold_present(self):
        """The METHODIC RATE.*out criterion is the canonical PID metric."""
        from lib.thresholds import Thresholds
        t = Thresholds()
        assert hasattr(t, 'pid_rate_out_warn')
        assert hasattr(t, 'pid_rate_out_crit')
        assert t.pid_rate_out_warn == 0.10
        assert t.pid_rate_out_crit == 0.15

    def test_convergence_criteria_present(self):
        """Convergence (drone fully tuned) thresholds present."""
        from lib.thresholds import Thresholds
        t = Thresholds()
        assert hasattr(t, 'converge_vibe_max')
        assert hasattr(t, 'converge_clip_max')
        assert hasattr(t, 'converge_rate_out_max')
        assert hasattr(t, 'converge_hover_thr_err')
        assert hasattr(t, 'converge_ekf_var_max')
        assert t.converge_vibe_max == 15.0
        # ARDU-WIKI-VIBE: < 100 acceptable. Strict 0 would block
        # convergence forever after any landing clip.
        assert t.converge_clip_max == 100
        assert t.converge_rate_out_max == 0.10
        assert t.converge_hover_thr_err == 0.05
        # EKF ceiling = 0.5 × FS_EKF_THRESH (default 0.8)
        assert t.converge_ekf_var_max == 0.4


# ====================================================================
# param_suggestions: analyzers that set apply_param must produce them
# ====================================================================
class TestParamSuggestionShape:
    """The {param, current, suggested, reason} structure is enforced
    mechanically by AnalysisResult.__post_init__ — every analyzer that
    constructs a result passes through it. (The old tests grepped each
    module's SOURCE for the word "param_suggestions", which asserted
    nothing about the data.)"""

    def _mk(self, sugg):
        return AnalysisResult(
            title="x", status="warning",
            action_level=ACTION_APPLY_PARAM,
            param_suggestions=[sugg])

    def test_well_formed_accepted(self):
        r = self._mk({"param": "MOT_SPIN_MIN", "current": 0.0,
                      "suggested": 0.12, "reason": "r"})
        assert r.param_suggestions

    @pytest.mark.parametrize("bad", [
        {"current": 1, "suggested": 2, "reason": "r"},           # no param
        {"param": "", "current": 1, "suggested": 2, "reason": "r"},
        {"param": "P", "suggested": 2, "reason": "r"},           # no current
        {"param": "P", "current": 1, "reason": "r"},             # no suggested
        {"param": "P", "current": 1, "suggested": 2},            # no reason
        "MOT_SPIN_MIN=0.12",                                     # not a dict
    ])
    def test_malformed_rejected(self, bad):
        with pytest.raises(AssertionError):
            self._mk(bad)


# ====================================================================
# Executive summary: headline wording (diagnostic tool positioning)
# ====================================================================
class TestExecutiveSummaryHeadline:
    def test_all_ok_headline(self):
        """All analyzers OK → diagnostic headline, no 'perfect' claim."""
        from lib.executive_summary import build_executive_summary
        results = [
            AnalysisResult(title="a", status="good", action_level=ACTION_OK),
            AnalysisResult(title="b", status="good", action_level=ACTION_OK),
        ]
        es = build_executive_summary(results)
        assert "METHODIC" in es.headline

    def test_has_issues_headline(self):
        """Issues present → headline describes diagnostic result."""
        from lib.executive_summary import build_executive_summary
        results = [
            AnalysisResult(title="a", status="good", action_level=ACTION_OK),
            AnalysisResult(
                title="b", status="warning",
                action_level=ACTION_APPLY_PARAM,
                plain_summary="x", action_text="y",
            ),
        ]
        es = build_executive_summary(results)
        # Issues present: headline must be non-empty and must NOT carry
        # the all-clear "METHODIC 推荐范围内" claim that test_all_ok_headline
        # asserts for a clean flight.
        assert es.headline != ""
        assert "METHODIC" not in es.headline

    def test_empty_results(self):
        from lib.executive_summary import build_executive_summary
        es = build_executive_summary([])
        assert es.headline != ""


# ====================================================================
# _format_action_text: line breaks and decimal safety
# ====================================================================
class TestFormatActionText:
    def test_numbered_steps_get_breaks(self):
        from lib.html_report import _format_action_text
        result = _format_action_text("检查：1. 电池2. GPS3. EKF")
        assert "<br>1. " in result
        assert "<br>2. " in result
        assert "<br>3. " in result

    def test_circled_numbers_get_breaks(self):
        from lib.html_report import _format_action_text
        result = _format_action_text("步骤：① 减少② 检查③ SD")
        assert "<br>①" in result
        assert "<br>②" in result

    def test_decimals_not_broken(self):
        from lib.html_report import _format_action_text
        result = _format_action_text("FS_EKF_THRESH=0.8 和 42.0V")
        assert "<br>0." not in result
        assert "<br>42." not in result

    def test_empty_string(self):
        from lib.html_report import _format_action_text
        assert _format_action_text("") == ""
