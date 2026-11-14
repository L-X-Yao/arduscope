"""
Notch tuning analyzer: verify correct recommendations for each scenario.

Each test case represents a real configuration that previously
produced wrong advice.
"""

import re

import numpy as np

from conftest import (LOG_QUADPLANE_FLIGHT, LOG_QUADPLANE_GROUND, LOG_OCTA_NOTCH_OFF,
                      LOG_OCTA_AUTOTUNE, run_analyzer)
from analyzers.notch_tuning import NotchTuningAnalyzer
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader


def _get_param_names(result):
    """Extract recommended parameter names from the result."""
    params = []
    for det in result.details:
        m = re.search(r"<pre>\n?(.*?)\n?</pre>", det, re.DOTALL)
        if m:
            for line in m.group(1).strip().splitlines():
                if "," in line:
                    params.append(line.split(",")[0])
    return params


class TestNotchDisabled:
    """Log 82: ENABLE=0, should recommend full HNTCH setup."""

    def test_recommends_enable(self):
        result = run_analyzer(NotchTuningAnalyzer, LOG_QUADPLANE_FLIGHT)
        params = _get_param_names(result)
        assert "INS_HNTCH_ENABLE" in params

    def test_recommends_freq(self):
        result = run_analyzer(NotchTuningAnalyzer, LOG_QUADPLANE_FLIGHT)
        params = _get_param_names(result)
        assert "INS_HNTCH_FREQ" in params

    def test_hover_throttle_checked(self):
        """Hover throttle should be checked. If QTUN.ThO shows the
        configured value is already accurate, no recommendation is needed
        (improvement: ThO-based hover is more precise than PWM median)."""
        result = run_analyzer(NotchTuningAnalyzer, LOG_QUADPLANE_FLIGHT)
        params = _get_param_names(result)
        hover_detail = any("悬停油门" in d for d in result.details)
        assert hover_detail, "Hover throttle check should always run"
        # With ThO-based hover, the configured value may already be
        # accurate — either a recommendation or a match report is valid.
        hover_rec = "Q_M_THST_HOVER" in params
        hover_match = any("实测" in d for d in result.details)
        assert hover_rec or hover_match

    def test_status_not_good(self):
        result = run_analyzer(NotchTuningAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert result.status in ("warning", "critical")


class TestPureFixedWingHover:
    """Pure fixed-wing Plane: CTUN.ThO is 0-100 (%), not a 0-1 hover
    fraction. The analyzer must treat hover throttle as N/A rather than
    misreading a ~45 output as 4500% and emitting a false critical +
    a MOT_THST_HOVER suggestion (a param that doesn't exist on Plane)."""

    def _plane_reader(self):
        n = 500
        ts = np.linspace(0, 120, n)
        return SyntheticReader(
            messages={
                "RCOU": {"_timestamp": ts, "C3": np.full(n, 1600.0)},
                # Plane CTUN.ThO in output-percent units (0-100).
                "CTUN": {"_timestamp": ts, "ThO": np.full(n, 45.0)},
            },
            params={"MOT_THST_HOVER": 0.35},
            vehicle_type="ArduPlane",
        )

    def _plane_vehicle(self):
        return VehicleInfo(
            vehicle_type="ArduPlane", firmware_version="4.6.3",
            board_name="synthetic", frame_class=0, motor_count=0,
            imu_count=1, is_quadplane=False,
        )

    def test_hover_throttle_not_applicable(self):
        reader = self._plane_reader()
        a = NotchTuningAnalyzer(reader, Thresholds(), self._plane_vehicle())
        assert a._compute_hover_throttle("") == -1.0

    def test_no_false_hover_critical_or_suggestion(self):
        reader = self._plane_reader()
        a = NotchTuningAnalyzer(reader, Thresholds(), self._plane_vehicle())
        a._actual_hover_throttle = a._compute_hover_throttle("")
        status, _details, recs = a._check_hover_throttle("")
        assert status == "good"
        assert recs == []


class TestNotchLowFreqResonance:
    """Autotune log: ENABLE=1 FREQ=13 BW=8 (low-freq structural resonance).
    Should NOT overwrite FREQ. Should suggest HNTC2 for motor vibration."""

    def test_does_not_change_freq(self):
        result = run_analyzer(NotchTuningAnalyzer, LOG_OCTA_AUTOTUNE)
        params = _get_param_names(result)
        assert "INS_HNTCH_FREQ" not in params, \
            "Should not overwrite user's intentional low-freq notch"

    def test_suggests_second_notch(self):
        result = run_analyzer(NotchTuningAnalyzer, LOG_OCTA_AUTOTUNE)
        params = _get_param_names(result)
        assert "INS_HNTC2_ENABLE" in params, \
            "Should suggest second notch for motor vibration"
        assert "INS_HNTC2_FREQ" in params

    def test_detects_bw_issue(self):
        """BW=8 barely covers 9 Hz peak — should recommend wider BW."""
        result = run_analyzer(NotchTuningAnalyzer, LOG_OCTA_AUTOTUNE)
        params = _get_param_names(result)
        assert "INS_HNTCH_BW" in params, \
            "Should recommend BW adjustment for edge coverage"


class TestNotchJunkValues:
    """未开滤波器 log: ENABLE=1 FREQ=12 BW=6 (junk/unconfigured).
    Should treat as not-properly-configured and suggest HNTC2."""

    def test_does_not_only_change_freq(self):
        """Should not just say 'change FREQ to 95' — that overwrites the
        user's config which may be intentional."""
        result = run_analyzer(NotchTuningAnalyzer, LOG_OCTA_NOTCH_OFF)
        params = _get_param_names(result)
        # Should suggest HNTC2, not just tweak HNTCH_FREQ
        assert "INS_HNTC2_FREQ" in params or "INS_HNTCH_ENABLE" in params


class TestGroundTestMinimal:
    """Log 83: Ground test, motors barely ran.
    Should not produce wrong recommendations."""

    def test_no_crash(self):
        result = run_analyzer(NotchTuningAnalyzer, LOG_QUADPLANE_GROUND)
        assert result.status in ("good", "warning", "critical", "info")

    def test_no_extreme_recommendations(self):
        """Ground test shouldn't trigger critical from noise."""
        result = run_analyzer(NotchTuningAnalyzer, LOG_QUADPLANE_GROUND)
        # Attitude check should not flag ground test as "severe oscillation"
        assert not any("姿态摇摆严重" in d for d in result.details), \
            "Ground test should not trigger severe oscillation warning"
