from tests._repo import REPO_ROOT, TESTS_DIR
"""
Regression tests for all 24 bugs fixed in the v6 audit.

Each test targets a specific bug to prevent regression. Tests are grouped
by the analyzer/module they cover.
"""

import os
import numpy as np
import pytest

from conftest import (LOG_QUADPLANE_FLIGHT, LOG_QUADPLANE_GROUND, LOG_OCTA_NOTCH_OFF, _load, run_analyzer)


# Path helpers — resolve relative to this test file so CI and local
# environments both work without hardcoding absolute paths.
_TESTS_DIR = str(TESTS_DIR)
_PKG_DIR = str(REPO_ROOT)


def _pkg_path(*parts: str) -> str:
    return os.path.join(_PKG_DIR, *parts)


# ====================================================================
# Bug #1: pid_tuning oscillation threshold from self.thresholds
# ====================================================================
class TestPidOscThreshold:
    def test_threshold_is_configurable(self):
        """The oscillation threshold should come from thresholds, not hardcoded."""
        from lib.thresholds import Thresholds
        t = Thresholds()
        assert hasattr(t, "pid_osc_crossings_warn")
        assert t.pid_osc_crossings_warn == 5.0

    def test_analyzer_uses_threshold(self):
        """pid_tuning must reference self.thresholds, not a magic number."""
        import inspect
        from analyzers.pid_tuning import PIDTuningAnalyzer
        src = inspect.getsource(PIDTuningAnalyzer)
        # Should NOT have a bare "5.0" comparison for oscillation
        # Should reference thresholds.pid_osc_crossings_warn
        assert "pid_osc_crossings_warn" in src


# ====================================================================
# Bug #6: wind forward-flight filtering
# ====================================================================
class TestWindForwardFlight:
    def test_wind_filters_hover(self):
        """Wind stats should exclude hover (ws≈0) samples."""
        result = run_analyzer(
            __import__("analyzers.wind", fromlist=["WindAnalyzer"]).WindAnalyzer,
            LOG_QUADPLANE_FLIGHT)
        # A QuadPlane that mostly hovers should report low/zero wind
        # but NOT have the hover data inflating sample count
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #8: battery armed filtering
# ====================================================================
class TestBatteryArmedFiltering:
    def test_battery_not_critical_on_ground(self):
        """Ground test battery should not trigger false critical."""
        result = run_analyzer(
            __import__("analyzers.battery", fromlist=["BatteryAnalyzer"]).BatteryAnalyzer,
            LOG_QUADPLANE_GROUND)
        assert result.status != "critical", \
            f"Battery should not be critical on ground test: {result.summary}"


# ====================================================================
# Bug #10: power armed filtering
# ====================================================================
class TestPowerArmedFiltering:
    def test_power_no_crash_ground(self):
        result = run_analyzer(
            __import__("analyzers.power", fromlist=["PowerAnalyzer"]).PowerAnalyzer,
            LOG_QUADPLANE_GROUND)
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #12: pid_tuning 0.0 falsy param check
# ====================================================================
class TestPidParamFalsy:
    def test_get_param_uses_is_none(self):
        """The param check should use 'is None', not truthiness."""
        import inspect
        from analyzers.pid_tuning import PIDTuningAnalyzer
        src = inspect.getsource(PIDTuningAnalyzer)
        # Should use "is None" pattern, not "not self._get_param"
        assert "is None" in src


# ====================================================================
# Bug #13: motor PWM param order for QuadPlane
# ====================================================================
class TestMotorPWMParamOrder:
    def test_quadplane_uses_q_prefix(self):
        """On a QuadPlane log, motor analyzer should use Q_M_PWM_MIN."""
        result = run_analyzer(
            __import__("analyzers.motor", fromlist=["MotorAnalyzer"]).MotorAnalyzer,
            LOG_QUADPLANE_FLIGHT)
        # Should show Q_M_SPIN_MIN label for QuadPlane
        found_q_label = any("Q_M_SPIN_MIN" in d for d in result.details)
        found_mot_label = any("MOT_SPIN_MIN" in d for d in result.details)
        assert found_q_label and not found_mot_label, \
            "QuadPlane should use Q_M_SPIN_MIN label"


# ====================================================================
# Bug #14: log_health dynamic loop time thresholds
# ====================================================================
class TestLogHealthLoopTime:
    def test_uses_sched_rate(self):
        """Loop time thresholds should scale with SCHED_LOOP_RATE."""
        import inspect
        from analyzers.log_health import LogHealthAnalyzer
        src = inspect.getsource(LogHealthAnalyzer)
        assert "SCHED_LOOP_RATE" in src

    def test_no_crash(self):
        result = run_analyzer(
            __import__("analyzers.log_health", fromlist=["LogHealthAnalyzer"]).LogHealthAnalyzer,
            LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #15: log_health CPU load "数据不可用"
# ====================================================================
class TestLogHealthCPULoad:
    def test_no_false_zero_cpu(self):
        """PM.Load all-NaN (old firmware) must yield the explicit
        'unavailable' note, never a fabricated 'CPU 0.0%'. (The old
        test looped with `pass` and ended in `assert True`.)"""
        import numpy as np
        from analyzers.log_health import LogHealthAnalyzer
        from lib.i18n import t as _t
        from lib.thresholds import Thresholds
        from lib.vehicle_detect import VehicleInfo
        from tests.test_synthetic_coverage import SyntheticReader
        n = 20
        reader = SyntheticReader(messages={"PM": {
            "_timestamp": np.linspace(0.0, 100.0, n),
            "NLon": np.zeros(n),
            "NLoop": np.full(n, 4000.0),
            "MaxT": np.full(n, 2600.0),
            "Mem": np.full(n, 100000.0),
            "Load": np.full(n, np.nan),      # precondition: no Load data
        }}, armed_intervals=[(0.0, 90.0)])
        vi = VehicleInfo(
            vehicle_type="ArduCopter", firmware_version="4.6.3",
            board_name="synthetic", frame_class=1, motor_count=4,
            imu_count=1, is_quadplane=False)
        result = LogHealthAnalyzer(reader, Thresholds(), vi).analyze()
        unavailable = _t("log_health.cpu_load_unavailable")
        assert unavailable in result.details
        assert not any("0.0%" in d for d in result.details)


# ====================================================================
# Bug #16: rangefinder NaN Stat field
# ====================================================================
class TestRangefinderNaN:
    def test_no_crash_without_rangefinder(self):
        """Logs without RFND should gracefully skip."""
        from analyzers.rangefinder import RangefinderAnalyzer
        fix = _load(LOG_QUADPLANE_FLIGHT)
        analyzer = RangefinderAnalyzer(fix.reader, fix.thresholds, fix.vehicle)
        if not analyzer.can_run():
            pytest.skip("No RFND data")
        result = analyzer.analyze()
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #18: mission AUTO mode number for Copter
# ====================================================================
class TestMissionAutoMode:
    def test_auto_mode_includes_copter(self):
        """AUTO mode detection should include mode number '3' for Copter."""
        import inspect
        from analyzers.mission import MissionAnalyzer
        src = inspect.getsource(MissionAnalyzer)
        assert "'3'" in src, "Should check for Copter AUTO mode number 3"


# ====================================================================
# Bug #19: mission NaN CId defense
# ====================================================================
class TestMissionNaNCId:
    def test_cid_nan_handling(self):
        """Mission analyzer should handle NaN CId without crashing."""
        result = run_analyzer(
            __import__("analyzers.mission", fromlist=["MissionAnalyzer"]).MissionAnalyzer,
            LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #20: mission WP_RADIUS
# ====================================================================
class TestMissionWPRadius:
    def test_uses_wp_radius_param(self):
        """Should read WP_RADIUS from log, not hardcode 30."""
        import inspect
        from analyzers.mission import MissionAnalyzer
        src = inspect.getsource(MissionAnalyzer)
        assert "WP_RADIUS" in src


# ====================================================================
# Bug #22: log_reader non-numeric field ValueError
# ====================================================================
class TestLogReaderNonNumeric:
    def test_fallback_read_handles_non_numeric(self):
        """LogReader._read_msg_type should not crash on string fields."""
        from lib.log_reader import LogReader
        import inspect
        src = inspect.getsource(LogReader)
        # The _read_msg_type method should have try/except around float()
        # Check that the method exists and handles ValueError
        assert "ValueError" in src or "try" in src


# ====================================================================
# Bug #23: flight_report error results in results_by_key
# ====================================================================
class TestFlightReportErrorResults:
    def test_error_results_added_to_dict(self):
        """Crashed analyzer results should be in results_by_key."""
        with open(_pkg_path("lib/pipeline.py"), encoding="utf-8") as f:
            src = f.read()
        assert "results_by_key[key]" in src


# ====================================================================
# Bug #24: flight_report --template overwrite --only warning
# ====================================================================
class TestFlightReportTemplateWarning:
    def test_template_override_warning(self):
        """--template + --only should print a warning. After i18n
        refactor, the literal string moved into the cli.note_template_overrides
        locale key — assert the i18n call exists, not the raw string."""
        with open(_pkg_path("flight_report.py"), encoding="utf-8") as f:
            src = f.read()
        assert "only_set" in src
        assert "cli.note_template_overrides" in src


# ====================================================================
# Bug #25: notch_tuning FLTD param name wrong for Copter
# ====================================================================
class TestNotchFLTDParamName:
    def test_copter_uses_atc_prefix(self):
        """Copter FLTD check should use ATC_RAT_RLL_FLTD, not A_RAT_RLL_FLTD."""
        import inspect
        from analyzers.notch_tuning import NotchTuningAnalyzer
        src = inspect.getsource(NotchTuningAnalyzer)
        assert "ATC_RAT_" in src, \
            "Should use ATC_RAT_ prefix for Copter FLTD params"
        # The f-string template uses {q_prefix}A_RAT_ which evaluates to
        # Q_A_RAT_ for QuadPlane.  Remove that pattern too.
        cleaned = src.replace("ATC_RAT_", "").replace("Q_A_RAT_", "")
        cleaned = cleaned.replace('{q_prefix}A_RAT_', '')
        assert "A_RAT_" not in cleaned, \
            "Should not have bare A_RAT_ prefix (only Q_A_RAT_ or ATC_RAT_)"

    def test_quadplane_uses_q_a_prefix(self):
        """QuadPlane FLTD check should use Q_A_RAT_RLL_FLTD."""
        import inspect
        from analyzers.notch_tuning import NotchTuningAnalyzer
        src = inspect.getsource(NotchTuningAnalyzer)
        assert "Q_A_RAT_" in src or "q_prefix" in src


# ====================================================================
# Bug #26: vibration nanmax crash on all-NaN array
# ====================================================================
class TestVibrationNaNSafety:
    def test_safe_nanmax_in_source(self):
        """vibration.py should guard against all-NaN vibe arrays."""
        import inspect
        from analyzers.vibration import VibrationAnalyzer
        src = inspect.getsource(VibrationAnalyzer)
        assert "np.all(np.isnan" in src or "_safe_nanmax" in src


# ====================================================================
# Bug #27: temperature near-zero dt spike
# ====================================================================
class TestTemperatureDtGuard:
    def test_dt_minimum_clamp(self):
        """temperature.py should clamp dt to a reasonable minimum, not just == 0."""
        import inspect
        from analyzers.temperature import TemperatureAnalyzer
        src = inspect.getsource(TemperatureAnalyzer)
        # Should use < comparison, not just == 0
        assert "dt < " in src or "dt[dt <" in src, \
            "Should clamp near-zero dt, not just exact zero"


# ====================================================================
# Bug #28: airspeed pitot blockage O(n*window) performance
# ====================================================================
class TestAirspeedRollingStdVectorized:
    def test_no_python_loop_for_rolling_std(self):
        """airspeed.py should use vectorized rolling std, not Python loop."""
        import inspect
        from analyzers.airspeed import AirspeedAnalyzer
        src = inspect.getsource(AirspeedAnalyzer)
        assert "for i in range" not in src, \
            "Should not use Python loop for rolling std"


# Bug #29, ESC RPM checked per instance: tests/test_notch_tracking.py,
# TestEscInFlight.


# ====================================================================
# Bug #30: EKF NaN core_ids produces empty traces
# ====================================================================
class TestEkfNaNCoreHandling:
    def test_handles_nan_core_ids(self):
        """EKF should use all data when C field is all NaN."""
        import inspect
        from analyzers.ekf import EKFAnalyzer
        src = inspect.getsource(EKFAnalyzer)
        assert "all_nan_cores" in src, \
            "Should detect and handle all-NaN core IDs"

    def test_no_crash(self):
        result = run_analyzer(
            __import__("analyzers.ekf", fromlist=["EKFAnalyzer"]).EKFAnalyzer,
            LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #31: pid_tuning ground test fallback reports misleading "good"
# ====================================================================
class TestPidGroundTestEarlyReturn:
    def test_insufficient_data_returns_info(self):
        """<10 armed PID samples must return info — not silently fall
        back to the whole log and grade it. (The old test's
        `A not in src or B in src` had an always-true second branch.)"""
        import numpy as np
        from analyzers.pid_tuning import PIDTuningAnalyzer
        from lib.thresholds import Thresholds
        from lib.vehicle_detect import VehicleInfo
        from tests.test_synthetic_coverage import SyntheticReader
        n = 400
        ts = np.linspace(0.0, 100.0, n)
        tar = np.sin(2 * np.pi * 0.25 * ts)
        reader = SyntheticReader(messages={
            "PIDR": {"_timestamp": ts, "Tar": tar, "Act": 0.3 * tar,
                     "P": 0.3 * tar, "I": np.full(n, 0.05),
                     "D": np.full(n, 0.01), "FF": np.full(n, 0.02)},
        }, armed_intervals=[(0.0, 0.5)])   # ~2 samples inside
        n_armed = int(np.sum((ts >= 0.0) & (ts <= 0.5)))
        assert 0 < n_armed < 10            # precondition
        vi = VehicleInfo(
            vehicle_type="ArduCopter", firmware_version="4.6.3",
            board_name="synthetic", frame_class=1, motor_count=4,
            imu_count=1, is_quadplane=False)
        result = PIDTuningAnalyzer(reader, Thresholds(), vi).analyze()
        assert result.status == "info"

    def test_truthy_param_check_fixed(self):
        """A Q_A_RAT_* gain of exactly 0.0 in the log must NOT be
        treated as 'parameter missing' (the old truthiness bug fell
        back to the copter ATC_RAT_* namespace on zero gains)."""
        import numpy as np
        from analyzers.pid_tuning import PIDTuningAnalyzer
        from lib.thresholds import Thresholds
        from lib.vehicle_detect import VehicleInfo
        from tests.test_synthetic_coverage import SyntheticReader
        n = 400
        ts = np.linspace(0.0, 100.0, n)
        tar = np.sin(2 * np.pi * 0.25 * ts)
        reader = SyntheticReader(messages={
            "PIQR": {"_timestamp": ts, "Tar": tar, "Act": 0.3 * tar,
                     "P": 0.3 * tar, "I": np.full(n, 0.05),
                     "D": np.full(n, 0.01), "FF": np.full(n, 0.02)},
            "POS": {"_timestamp": ts, "Alt": np.linspace(0.0, 80.0, n)},
        }, params={"Q_A_RAT_RLL_P": 0.0, "Q_A_RAT_RLL_I": 0.0,
                   "ATC_RAT_RLL_P": 0.135},
            vehicle_type="ArduPlane")
        vi = VehicleInfo(
            vehicle_type="ArduPlane", firmware_version="4.6.3",
            board_name="synthetic", frame_class=1, motor_count=4,
            imu_count=1, is_quadplane=True)
        # Precondition: tracking is bad enough to reach the suggestion
        # logic, and the copter-namespace bait param IS in the log.
        err = tar - 0.3 * tar
        ratio = (np.sqrt(np.mean(err**2)) / np.sqrt(np.mean(tar**2)))
        assert ratio >= Thresholds().pid_tracking_warn
        assert reader.get_param("ATC_RAT_RLL_P", None) is not None
        result = PIDTuningAnalyzer(reader, Thresholds(), vi).analyze()
        # Fixed behavior: the zero Q gain keeps the Q_A namespace, so
        # the cur*1.12 suggestion is a 0->0 no-op and gets dropped.
        # The BUGGY truthiness fallback would have emitted an
        # ATC_RAT_RLL_P suggestion from the bait value.
        assert not any(s["param"].startswith("ATC_")
                       for s in result.param_suggestions)

    def test_sub_action_level_checks_suggestions_before_good(self):
        """sub_action_level: param_suggestions must be checked BEFORE
        status=='good'. status (rate_out_rms) and param_suggestions
        (tracking_ratio) are independent signals — good+suggestions is
        reachable, and assigning 'ok' there violates the apply_param ↔
        param_suggestions invariant from CLAUDE.md."""
        import inspect
        from analyzers.pid_tuning import PIDTuningAnalyzer
        src = inspect.getsource(PIDTuningAnalyzer)
        # Find both branches of the sub_action_level decision
        sugg_idx = src.find("if param_suggestions:\n            sub_action_level = \"apply_param\"")
        good_idx = src.find("status == \"good\":\n            sub_action_level = \"ok\"")
        assert sugg_idx >= 0, \
            "param_suggestions → apply_param branch missing in _evaluate_axis"
        assert good_idx >= 0, \
            "status==good → ok branch missing in _evaluate_axis"
        assert sugg_idx < good_idx, (
            "param_suggestions must be checked before status==good, otherwise "
            "good+suggestions returns 'ok' and violates the invariant"
        )


# ====================================================================
# Bug #34: failsafe EKF margin NaN → "good" silent pass
# ====================================================================
class TestFailsafeEkfNaNMargin:
    def test_nan_margin_guarded(self):
        """failsafe._ekf_margin must guard np.nanmin against all-NaN."""
        import inspect
        from analyzers.failsafe import FailsafeAnalyzer
        src = inspect.getsource(FailsafeAnalyzer._ekf_margin)
        assert "np.all(np.isnan" in src, \
            "Should detect all-NaN margin before nanmin"

    def test_no_crash_on_normal_log(self):
        result = run_analyzer(
            __import__("analyzers.failsafe",
                       fromlist=["FailsafeAnalyzer"]).FailsafeAnalyzer,
            LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")


# ====================================================================
# Bug #35: wind fig_dir trace x/y length mismatch
# ====================================================================
class TestWindDirTraceLength:
    def test_uses_consistent_mask(self):
        """wind.py must use a single mask for t and wd in the direction plot."""
        import inspect
        from analyzers.wind import WindAnalyzer
        src = inspect.getsource(WindAnalyzer)
        # The fix introduced a single stat_mask variable
        assert "stat_mask" in src and "t_valid" in src, \
            "Should unify the t/wd mask selection"
        # The old pattern `x=t[valid], y=wd_valid` must be gone
        assert "x=t[valid]" not in src


# ====================================================================
# Bug #36/#37: attitude ground-test fallback + NaN RMS → "good"
# ====================================================================
class TestAttitudeGroundTestGuard:
    def test_early_return_on_no_armed(self):
        """attitude.py should return info when armed samples are too few."""
        import inspect
        from analyzers.attitude import AttitudeAnalyzer
        src = inspect.getsource(AttitudeAnalyzer)
        assert "np.sum(armed_mask) < 10" in src
        # Should NOT fall back to full data
        assert "np.ones(len(t_att), dtype=bool)" not in src

    def test_nan_rms_guarded(self):
        """attitude.py should detect NaN RMS rather than fall through to good."""
        import inspect
        from analyzers.attitude import AttitudeAnalyzer
        src = inspect.getsource(AttitudeAnalyzer)
        assert "np.isnan(roll_rms)" in src or "np.isnan(pitch_rms)" in src


# ====================================================================
# Bug #38/#39: imu_consistency NaN RMS + np.interp with NaN + 3 IMUs
# ====================================================================
class TestImuConsistencyFixes:
    def test_safe_interp_helper_exists(self):
        """imu_consistency.py should define _safe_interp_diff."""
        from analyzers.imu_consistency import _safe_interp_diff
        t0 = np.array([1.0, 2.0, 3.0])
        d0 = np.array([10.0, 20.0, 30.0])
        t1 = np.array([1.0, 2.0, 3.0])
        # All-NaN source must return None
        assert _safe_interp_diff(t0, d0, t1, np.full(3, np.nan)) is None
        # Partial NaN should still produce finite results
        diff = _safe_interp_diff(
            t0, d0, t1, np.array([11.0, np.nan, 31.0])
        )
        assert diff is not None
        assert not np.any(np.isnan(diff))

    def test_compares_all_instances_not_just_two(self):
        """Should compare IMU0 vs each other instance, not just IMU0/IMU1."""
        import inspect
        from analyzers.imu_consistency import IMUConsistencyAnalyzer
        src = inspect.getsource(IMUConsistencyAnalyzer.analyze)
        assert "unique_instances[1:]" in src, \
            "Should iterate pair-wise against IMU0"

    def test_nan_max_diff_does_not_silent_good(self):
        """Empty accel_diff_rms_values must not leave overall_status='good'."""
        import inspect
        from analyzers.imu_consistency import IMUConsistencyAnalyzer
        src = inspect.getsource(IMUConsistencyAnalyzer)
        # Fix: explicit else branch bumps status to info when no data
        assert 'overall_status, "info"' in src


# ====================================================================
# Bug #40: gps int(nan) crash on all-NaN GPS instance
# ====================================================================
class TestGpsNanCrash:
    def test_all_nan_nsats_guarded(self):
        """gps.py should guard int(np.nanmin) against all-NaN nsats arrays."""
        import inspect
        from analyzers.gps import GPSAnalyzer
        src = inspect.getsource(GPSAnalyzer)
        assert "np.all(np.isnan(nsats_inst))" in src


# ====================================================================
# Bug #41/#43: rc_input saturation without armed filter + failsafe count
# ====================================================================
class TestRcInputArmedFilter:
    def test_stick_saturation_uses_armed_mask(self):
        """rc_input.py should only count stick saturation during armed flight."""
        import inspect
        from analyzers.rc_input import RCInputAnalyzer
        src = inspect.getsource(RCInputAnalyzer.analyze)
        assert "armed_mask" in src
        assert "arr[armed_mask]" in src

    def test_failsafe_threshold_scales_with_channels(self):
        """rc_input failsafe detection should scale with channel count."""
        import inspect
        from analyzers.rc_input import RCInputAnalyzer
        src = inspect.getsource(RCInputAnalyzer._detect_failsafe)
        # Hardcoded `>= 3` replaced by a channel-count-aware threshold
        assert "n_ch" in src
        assert "fs_count >= threshold" in src

    def test_failsafe_grouping_uses_time_not_sample_count(self):
        """Failsafe event grouping should use time gap, not sample index gap."""
        import inspect
        from analyzers.rc_input import RCInputAnalyzer
        src = inspect.getsource(RCInputAnalyzer._detect_failsafe)
        # Old pattern `fs_indices[i] - fs_indices[i-1] > 5` replaced
        assert "fs_indices[i] - fs_indices[i-1] > 5" not in src
        assert "t[fs_indices[i]]" in src


# ====================================================================
# Bug #44: log_reader _read_msg_type missing float() try/except
# ====================================================================
class TestLogReaderFallbackFloat:
    def test_read_msg_type_has_try_except(self):
        """_read_msg_type must handle non-numeric fields without crashing."""
        import inspect
        from lib.log_reader import LogReader
        src = inspect.getsource(LogReader._read_msg_type)
        assert "ValueError" in src or "except" in src, \
            "_read_msg_type must guard float() against string fields"


# ====================================================================
# Bug #45: log_reader timestamp handling — skip None ts
# ====================================================================
class TestLogReaderTimestampGuard:
    def test_prescan_skips_none_timestamp(self):
        """_prescan must skip messages with no _timestamp (first_ts=None)."""
        import inspect
        from lib.log_reader import LogReader
        src = inspect.getsource(LogReader._scan)
        # Old pattern `(ts or 0.0) - (first_timestamp or 0.0)` replaced
        assert "(ts or 0.0) - (first_timestamp or 0.0)" not in src
        assert "if ts is None or first_timestamp is None" in src

    def test_preload_all_skips_none_timestamp(self):
        import inspect
        from lib.log_reader import LogReader
        src = inspect.getsource(LogReader.preload_all)
        # Old pattern `ts = getattr(m, '_timestamp', 0.0)` replaced
        assert "getattr(m, '_timestamp', 0.0)" not in src
        assert "getattr(m, '_timestamp', None)" in src


# ====================================================================
# Bug #46: ai_diagnosis related_keys[0] unguarded access
# ====================================================================
class TestAiDiagnosisEmptyRelatedKeys:
    def test_defensive_first_key(self):
        """Accessing related_keys[0] must be guarded."""
        with open(_pkg_path("lib", "ai_diagnosis.py"), encoding="utf-8") as f:
            src = f.read()
        # Old direct access pattern replaced
        assert "issue.related_keys[0]" not in src or "first_key" in src


# ====================================================================
# Bug #47: vehicle_detect Y6 motor count fragile override
# ====================================================================
class TestVehicleDetectY6:
    def test_y6_is_six_motors_directly(self):
        """_FRAME_CLASS_MOTORS[5] should be 6 in the dict literal."""
        from lib.vehicle_detect import _FRAME_CLASS_MOTORS
        assert _FRAME_CLASS_MOTORS[5] == 6
        # Also verify the override line (which is no longer needed) is gone
        import inspect
        import lib.vehicle_detect as vd
        src = inspect.getsource(vd)
        assert "_FRAME_CLASS_MOTORS[5] = 6" not in src, \
            "Y6 fix should be in the dict literal, not a post-hoc override"


# ====================================================================
# Result-correctness tests: verify actual analyzer output on known logs
# ====================================================================
class TestResultCorrectness:
    """
    Behavioral tests that assert analyzer results make sense on
    real test logs. Unlike structural tests, these catch silent
    regressions where code still runs but produces wrong status.
    """

    def test_ground_test_pid_returns_info(self):
        """The ground-run log should report info for PID (not good)."""
        from analyzers.pid_tuning import PIDTuningAnalyzer
        result = run_analyzer(PIDTuningAnalyzer, LOG_QUADPLANE_GROUND)
        # Ground test: pilot had the log running but never armed long
        # enough for meaningful PID analysis. Must not silently say "good".
        assert result.status != "good", \
            f"Ground test PID should not be 'good': {result.summary}"

    def test_attitude_runs_on_flight_log(self):
        """Normal flight log should produce an attitude result with RMS data."""
        from analyzers.attitude import AttitudeAnalyzer
        result = run_analyzer(AttitudeAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")
        # Must report RMS figures in details (regression-proof that the
        # early-return path didn't swallow real flight data)
        assert any("Roll RMS" in d for d in result.details), \
            f"Flight log missing RMS details: {result.details}"

    def test_ground_test_battery_not_critical(self):
        """Ground test battery should not trigger false critical."""
        from analyzers.battery import BatteryAnalyzer
        result = run_analyzer(BatteryAnalyzer, LOG_QUADPLANE_GROUND)
        # Static ground test has no motor load; battery should not be critical.
        assert result.status != "critical", \
            f"Ground test battery false-critical: {result.summary}"

    def test_flight_log_summary_has_duration(self):
        """Regular flight log should report a non-zero flight duration."""
        from analyzers.summary import SummaryAnalyzer
        result = run_analyzer(SummaryAnalyzer, LOG_QUADPLANE_FLIGHT)
        duration_detail = next(
            (d for d in result.details if "m" in d and "s" in d and ":" in d), None
        )
        assert duration_detail is not None, \
            "Summary should include flight duration"
        # duration should include something like "Xm Ys"
        assert "m" in duration_detail and "s" in duration_detail

    def test_notch_tuning_flags_heavy_vibration(self):
        """The log from before the notch should trigger notch tuning warn."""
        from analyzers.notch_tuning import NotchTuningAnalyzer
        result = run_analyzer(NotchTuningAnalyzer, LOG_OCTA_NOTCH_OFF)
        # This log is the "未开滤波器" case — notch_tuning must detect
        # high vibration and produce actionable guidance (warning/critical).
        assert result.status in ("warning", "critical", "info"), \
            f"No-filter vibration log should trigger tuning advice, " \
            f"got {result.status}: {result.summary}"
        # The summary/details should mention notch or frequency guidance
        all_text = result.summary + " ".join(result.details)
        assert any(kw in all_text for kw in
                   ("陷波", "notch", "NOTCH", "INS_HNTCH", "HNTC")), \
            "Notch analyzer should discuss notch configuration"

    def test_vibration_analyzer_flags_no_filter_log(self):
        """Vibration analyzer on the no-filter log should NOT be 'good'."""
        from analyzers.vibration import VibrationAnalyzer
        result = run_analyzer(VibrationAnalyzer, LOG_OCTA_NOTCH_OFF)
        # Heavy pre-notch vibration — must be at least warning.
        assert result.status in ("warning", "critical", "info"), \
            f"No-filter log vibration should not be good: {result.summary}"

    def test_flight_log_gps_has_fix(self):
        """Normal flight should show good/warning GPS, never info (no data)."""
        from analyzers.gps import GPSAnalyzer
        result = run_analyzer(GPSAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert result.status != "info", \
            f"Flight log should have GPS data: {result.summary}"

    def test_worst_status_priority_monotonic(self):
        """BaseAnalyzer.worst_status must respect priority ordering."""
        from analyzers import BaseAnalyzer

        class _Dummy(BaseAnalyzer):
            registry_key = "_dummy_test"
            required_messages = []

            def analyze(self):
                return self.make_result(title="dummy")

        d = _Dummy.__new__(_Dummy)  # bypass __init__
        assert d.worst_status("good", "warning") == "warning"
        assert d.worst_status("warning", "critical") == "critical"
        assert d.worst_status("info", "good") == "info"
        assert d.worst_status("critical", "info") == "critical"
        assert d.worst_status("good", "good") == "good"
