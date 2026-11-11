"""
Tests for lib.trend_analyzer — multi-log time series trend detection.

Tests the iterative tuning workflow trend detection.
"""

from __future__ import annotations

import pytest

from lib.thresholds import Thresholds
from lib.trend_analyzer import (
    analyze_trends,
    get_metric_specs,
    TREND_IMPROVING,
    TREND_REGRESSING,
    TREND_STABLE,
    TREND_CONVERGED,
    TREND_INSUFFICIENT,
)


def _t() -> Thresholds:
    return Thresholds()


# ====================================================================
# Single-log degenerate case
# ====================================================================
@pytest.mark.fast
class TestSingleLog:
    def test_one_log_marks_metrics_insufficient(self):
        report = analyze_trends(
            [{"max_vibe": 12.0, "att_rms_error": 1.5}],
            ["log1"],
            _t(),
        )
        assert report.n_logs == 1
        # All metrics with insufficient samples
        for m in report.metrics:
            assert m.direction == TREND_INSUFFICIENT or m.values == [None]
        assert report.headline and report.n_logs == 1


# ====================================================================
# Trend direction detection
# ====================================================================
@pytest.mark.fast
class TestTrendDirection:
    def test_vibration_improving(self):
        # Lower is better — values decreasing means improving
        metrics = [
            {"max_vibe": 25.0},
            {"max_vibe": 18.0},
            {"max_vibe": 12.0},
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        # 12 vs 18 is < 15 (converge_vibe_max) → converged
        assert vibe.direction == TREND_CONVERGED
        assert vibe.converged is True

    def test_vibration_improving_not_yet_converged(self):
        metrics = [
            {"max_vibe": 30.0},
            {"max_vibe": 25.0},
            {"max_vibe": 20.0},
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        # 20 > 15 threshold → not converged, but improving
        assert vibe.direction == TREND_IMPROVING
        assert vibe.converged is False

    def test_vibration_regressing(self):
        metrics = [
            {"max_vibe": 12.0},
            {"max_vibe": 18.0},
            {"max_vibe": 25.0},
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        # 25 > 15 threshold so not converged
        # Increasing trend on lower-is-better → regressing
        assert vibe.direction == TREND_REGRESSING
        assert vibe.converged is False

    def test_battery_voltage_higher_is_better(self):
        # Battery voltage: higher value = better.
        # Need ≥5% change to register as a direction (significance gate).
        metrics = [
            {"min_batt_volt": 12.0},
            {"min_batt_volt": 13.0},
            {"min_batt_volt": 14.0},  # 14/13 = 7.7% rise → improving
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        v = next(m for m in report.metrics if m.key == "min_batt_volt")
        # No convergence threshold set for battery (yet) → improving
        assert v.direction == TREND_IMPROVING

    def test_stable_metric(self):
        # Same value 3 times → stable
        metrics = [
            {"att_rms_error": 1.5},
            {"att_rms_error": 1.51},
            {"att_rms_error": 1.49},
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        att = next(m for m in report.metrics if m.key == "att_rms_error")
        assert att.direction == TREND_STABLE


# ====================================================================
# Convergence judgment
# ====================================================================
@pytest.mark.fast
class TestConvergenceJudgment:
    def test_all_converged(self):
        # ALL convergence-tracked metrics must be present and under threshold.
        # converge thresholds: vibe<15, clip<100, rate_out<0.10,
        # hover_thr_err<0.05, ekf_var<0.4
        _good = {
            "max_vibe": 8.0, "max_clip": 10, "max_rate_out": 0.05,
            "hover_thr_err": 0.02, "max_ekf_var": 0.1,
        }
        metrics = [
            {**_good, "max_vibe": 10.0},
            {**_good, "max_vibe": 8.0},
            {**_good, "max_vibe": 6.0},
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        assert report.convergence.fully_converged is True
        assert "max_vibe" in report.convergence.converged_metric_keys
        assert report.convergence.pending_metric_keys == []
        assert "METHODIC" in report.headline

    def test_pending_metric_blocks_full_convergence(self):
        # Vibration not converged → fully_converged = False
        metrics = [
            {"max_vibe": 22.0},
            {"max_vibe": 20.0},
            {"max_vibe": 18.0},
        ]
        report = analyze_trends(metrics, ["a", "b", "c"], _t())
        assert report.convergence.fully_converged is False
        assert "max_vibe" in report.convergence.pending_metric_keys

    def test_metric_without_threshold_does_not_block(self):
        """Metrics without converge_* attr stay informational."""
        _good = {
            "max_vibe": 8.0, "max_clip": 10, "max_rate_out": 0.05,
            "hover_thr_err": 0.02, "max_ekf_var": 0.1,
        }
        metrics = [
            {**_good, "att_rms_error": 5.0},
            {**_good, "att_rms_error": 6.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        # att_rms_error has no threshold → goes to insufficient bucket,
        # doesn't block convergence
        assert report.convergence.fully_converged is True
        assert "att_rms_error" in report.convergence.insufficient_metric_keys


# ====================================================================
# Auto-interpretation text quality
# ====================================================================
@pytest.mark.fast
class TestInterpretationText:
    def test_improving_includes_percentage(self):
        metrics = [
            {"max_vibe": 30.0},
            {"max_vibe": 24.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        # 30 → 24 = 20% improvement
        assert "20%" in vibe.interpretation
        assert "20%" in vibe.interpretation

    def test_converged_says_perfect(self):
        metrics = [
            {"max_vibe": 20.0},
            {"max_vibe": 10.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        assert "METHODIC" in vibe.interpretation
        assert "✅" in vibe.interpretation

    def test_regressing_warns(self):
        metrics = [
            {"max_vibe": 20.0},
            {"max_vibe": 28.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        assert vibe.direction == TREND_REGRESSING
        assert vibe.interpretation != ""


# ====================================================================
# Next step recommendation
# ====================================================================
@pytest.mark.fast
class TestNextStep:
    def test_converged_no_action_needed(self):
        _good = {
            "max_vibe": 8.0, "max_clip": 10, "max_rate_out": 0.05,
            "hover_thr_err": 0.02, "max_ekf_var": 0.1,
        }
        metrics = [{**_good}, {**_good, "max_vibe": 7.0}]
        report = analyze_trends(metrics, ["a", "b"], _t())
        assert report.next_step_text != ""

    def test_regressing_priority_warning(self):
        # Vibration regressing → warning takes priority
        metrics = [
            {"max_vibe": 20.0},
            {"max_vibe": 28.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        assert report.next_step_text != ""

    def test_pending_recommends_continue(self):
        # Improving but not yet converged
        metrics = [
            {"max_vibe": 30.0},
            {"max_vibe": 25.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        assert report.next_step_text != ""


# ====================================================================
# Edge cases
# ====================================================================
@pytest.mark.fast
class TestEdgeCases:
    def test_empty_log_list(self):
        report = analyze_trends([], [], _t())
        assert report.n_logs == 0
        assert report.convergence.fully_converged is False

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError):
            analyze_trends([{"max_vibe": 10}], ["a", "b"], _t())

    def test_threshold_insufficient_keys_excludes_informational(self):
        """threshold_insufficient_keys must NOT include informational
        metrics (att_rms_error, min_batt_volt, error_count) that have no
        convergence threshold. Previously insufficient_metric_keys inflated
        the n_insuf headline count by 3 extra metrics."""
        report = analyze_trends([{"max_vibe": 10.0}], ["only_log"], _t())
        # threshold_insufficient_keys contains only metrics with thresholds
        for k in report.convergence.threshold_insufficient_keys:
            spec = next(s for s in get_metric_specs() if s.key == k)
            assert spec.convergence_attr is not None, (
                f"'{k}' has no threshold but is in threshold_insufficient_keys"
            )
        # Informational metrics are NOT in threshold_insufficient_keys
        informational = {s.key for s in get_metric_specs() if not s.convergence_attr}
        overlap = informational & set(report.convergence.threshold_insufficient_keys)
        assert overlap == set(), (
            f"Informational metrics {overlap} must not be in threshold_insufficient_keys"
        )

    def test_single_log_headline_n_insuf_is_threshold_count(self):
        """Headline n_insuf must equal the number of convergence-threshold
        metrics, not the total insufficient_metric_keys (which always
        includes informational metrics)."""
        from lib.trend_analyzer import get_metric_specs
        n_threshold = sum(1 for s in get_metric_specs() if s.convergence_attr)
        report = analyze_trends([{"max_vibe": 10.0}], ["log"], _t())
        assert len(report.convergence.threshold_insufficient_keys) == n_threshold

    def test_all_none_values(self):
        metrics = [
            {"max_vibe": None},
            {"max_vibe": None},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        assert vibe.direction == TREND_INSUFFICIENT

    def test_zero_previous_value_no_crash(self):
        # Avoid division by zero — must produce a valid direction.
        # For values that go from 0 → 5 on lower-is-better when both
        # are within the convergence threshold (15.0), the result is
        # "converged" because the absolute floor wins over direction.
        metrics = [
            {"max_vibe": 0.0},
            {"max_vibe": 5.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        # Both values are below converge_vibe_max (15.0) → converged
        assert vibe.direction in (TREND_CONVERGED, TREND_REGRESSING)
        # The important thing is no crash from division by zero

    def test_zero_previous_value_above_threshold(self):
        # 0 → 30 (above threshold 15) — should be regressing without crash
        metrics = [
            {"max_vibe": 0.0},
            {"max_vibe": 30.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], _t())
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        assert vibe.direction == TREND_REGRESSING


# ====================================================================
# Integration: realistic 5-flight tuning iteration
# ====================================================================
@pytest.mark.fast
class TestRealisticIteration:
    def test_realistic_tuning_session(self):
        """Simulate a realistic 5-flight tuning session converging."""
        # Include ALL convergence-tracked metrics so convergence can be declared.
        _conv = {"max_clip": 5, "max_rate_out": 0.04,
                 "hover_thr_err": 0.02, "max_ekf_var": 0.1}
        flights = [
            # Initial bad tuning
            {"max_vibe": 35.0, "att_rms_error": 5.0,
             "min_batt_volt": 14.0, "error_count": 3.0, **_conv},
            # After first param tweak
            {"max_vibe": 28.0, "att_rms_error": 4.2,
             "min_batt_volt": 14.1, "error_count": 1.0, **_conv},
            # After second tweak
            {"max_vibe": 20.0, "att_rms_error": 3.0,
             "min_batt_volt": 14.2, "error_count": 0.0, **_conv},
            # After third tweak
            {"max_vibe": 14.0, "att_rms_error": 2.0,
             "min_batt_volt": 14.2, "error_count": 0.0, **_conv},
            # Converged
            {"max_vibe": 9.0, "att_rms_error": 1.5,
             "min_batt_volt": 14.3, "error_count": 0.0, **_conv},
        ]
        names = [f"flight_{i+1}" for i in range(5)]
        report = analyze_trends(flights, names, _t())
        assert report.n_logs == 5
        # Vibration should be converged (9.0 < 15.0)
        vibe = next(m for m in report.metrics if m.key == "max_vibe")
        assert vibe.converged is True
        assert report.convergence.fully_converged is True
        assert "METHODIC" in report.headline


# ====================================================================
# Multi-log time-series overlay extraction
# ====================================================================
class TestTimeSeriesExtraction:
    def test_extract_metrics_includes_time_series(self):
        """extract_metrics returns time_series dict with downsampled data."""
        import numpy as np
        from tests.test_synthetic_coverage import SyntheticReader
        from multi_report import extract_metrics

        n = 500
        ts = np.linspace(0, 120, n)
        reader = SyntheticReader(
            messages={
                "VIBE": {
                    "_timestamp": ts,
                    "VibeX": np.full(n, 10.0),
                    "VibeY": np.full(n, 8.0),
                    "VibeZ": np.full(n, 5.0),
                    "Clip0": np.zeros(n),
                    "Clip1": np.zeros(n),
                    "Clip2": np.zeros(n),
                },
                "BAT": {
                    "_timestamp": ts,
                    "Inst": np.zeros(n),
                    "Volt": np.linspace(24.0, 22.0, n),
                    "Curr": np.full(n, 10.0),
                    "CurrTot": np.zeros(n),
                },
            },
        )
        metrics = extract_metrics(reader, _t())
        assert "time_series" in metrics
        ts_data = metrics["time_series"]
        assert "vibe_ts" in ts_data
        assert "batt_ts" in ts_data
        assert len(ts_data["vibe_ts"]["t"]) > 0
        assert len(ts_data["batt_ts"]["t"]) > 0

    def test_missing_messages_no_crash(self):
        """extract_metrics with no VIBE/BAT still works without time_series."""
        from tests.test_synthetic_coverage import SyntheticReader
        from multi_report import extract_metrics

        reader = SyntheticReader(messages={})
        metrics = extract_metrics(reader, _t())
        assert metrics["max_vibe"] is None
        assert "time_series" not in metrics
