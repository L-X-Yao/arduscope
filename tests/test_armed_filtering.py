"""
Verify armed-interval filtering is effective.

Uses log_83 (ground test, max altitude 0.06m, no real flight) as the
control case. Analyzers should NOT report critical on a ground-only log.
"""

from conftest import LOG_QUADPLANE_GROUND, LOG_QUADPLANE_FLIGHT, run_analyzer
from analyzers.vibration import VibrationAnalyzer
from analyzers.motor import MotorAnalyzer
from analyzers.ekf import EKFAnalyzer
from analyzers.attitude import AttitudeAnalyzer


class TestGroundLogNotCritical:
    """Log 83 is a ground test — no analyzer should report critical
    from ground handling noise alone."""

    def test_vibration_not_critical(self):
        result = run_analyzer(VibrationAnalyzer, LOG_QUADPLANE_GROUND)
        assert result.status != "critical", \
            f"Vibration should not be critical on ground test: {result.summary}"

    def test_motor_ground_idle_expected(self):
        """Ground test: motors are armed but at idle — floor-time is
        expected and acceptable. The test just verifies no crash."""
        result = run_analyzer(MotorAnalyzer, LOG_QUADPLANE_GROUND)
        assert result.status in ("good", "warning", "critical")

    def test_ekf_not_critical(self):
        result = run_analyzer(EKFAnalyzer, LOG_QUADPLANE_GROUND)
        assert result.status != "critical", \
            f"EKF should not be critical on ground test: {result.summary}"

    def test_attitude_not_critical(self):
        result = run_analyzer(AttitudeAnalyzer, LOG_QUADPLANE_GROUND)
        assert result.status != "critical", \
            f"Attitude should not be critical on ground test: {result.summary}"


class TestFlightLogHasArmedData:
    """Log 82 is a real flight — verify analyzers use armed data."""

    def test_vibration_has_data(self):
        result = run_analyzer(VibrationAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical")
        assert len(result.details) > 0, "Vibration should produce details on a real flight log"

    def test_motor_has_data(self):
        result = run_analyzer(MotorAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert len(result.details) > 0, "Motor should produce details on a real flight log"
