"""
Behavioral tests: verify analyzer outputs against known synthetic data.

Unlike structural tests (contract, i18n), these feed controlled data into
analyzers and check that the diagnostic conclusions (status, param_suggestions,
details) are correct.  They catch the class of bugs found in the 36-round
deep audit: wrong parameter names, missing armed filtering, incorrect
thresholds, NaN handling, etc.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from tests.test_synthetic_coverage import SyntheticReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo

pytestmark = pytest.mark.fast

_DEFAULT_VI = VehicleInfo(
    vehicle_type="ArduCopter", board_name="test", firmware_version="4.6",
    frame_class=1, motor_count=4, imu_count=3, is_quadplane=False,
)
_QP_VI = VehicleInfo(
    vehicle_type="ArduPlane", board_name="test", firmware_version="4.6",
    frame_class=1, motor_count=4, imu_count=2, is_quadplane=True,
)


def _make_ts(n=500, dt=0.01):
    return np.arange(n, dtype=float) * dt


# ====================================================================
# 1. batch_report analyze_one — the NameError crash guard
# ====================================================================

class TestBatchReportImport:
    def test_analyze_one_signature(self):
        """analyze_one must reference pipeline.health_score, not bare health_score."""
        import inspect
        from batch_report import analyze_one
        src = inspect.getsource(analyze_one)
        assert "health_score=health_score," not in src
        assert "pipeline.health_score" in src


# ====================================================================
# 2. notch_tuning param_suggestions uses "old" key
# ====================================================================

class TestNotchParamKey:
    def test_param_suggestions_has_current(self):
        """notch_tuning must map 'old' → 'current' in param_suggestions."""
        import inspect
        from analyzers.notch_tuning import NotchTuningAnalyzer
        src = inspect.getsource(NotchTuningAnalyzer.analyze)
        assert 'r.get("old"' in src


# ====================================================================
# 3. mission WP_RADIUS vs WPNAV_RADIUS
# ====================================================================

class TestMissionWPRadius:
    """The reach radius must come from WPNAV_RADIUS (cm) on copters and
    WP_RADIUS (m) on planes. One waypoint, track parked ~11 m away:
    copter radius 2 m -> 0 reached; plane radius 30 m -> 1 reached.
    (The old test only grepped the analyzer SOURCE for the substring
    "WPNAV_RADIUS" — both names are always present in the if/else.)"""

    def _run(self, vehicle_info):
        ts = _make_ts(200, 0.5)
        n = len(ts)
        reader = SyntheticReader(
            messages={
                "CMD": {
                    "_timestamp": np.array([0.0]),
                    "CNum": np.array([1]),
                    "CId": np.array([16]),          # NAV_WAYPOINT
                    "Lat": np.array([30.0]),
                    "Lng": np.array([120.0]),
                    "Alt": np.array([50.0]),
                },
                "POS": {
                    "_timestamp": ts,
                    # ~11.1 m north of the waypoint, deterministic
                    "Lat": np.full(n, 30.0001),
                    "Lng": np.full(n, 120.0),
                    "Alt": np.full(n, 50.0),
                },
            },
            params={"WPNAV_RADIUS": 200, "WP_RADIUS": 30},
            vehicle_type=vehicle_info.vehicle_type,
        )
        from analyzers.mission import MissionAnalyzer
        a = MissionAnalyzer(reader, Thresholds(), vehicle_info)
        # Precondition: the offset really is between the two radii
        dist_m = 0.0001 * 111320
        assert 200 / 100.0 < dist_m < 30
        return a.analyze()

    def test_copter_uses_wpnav_radius(self):
        from lib.i18n import t
        result = self._run(_DEFAULT_VI)
        assert _DEFAULT_VI.is_copter          # precondition
        expected = t("mission.wp_reached", radius="2", reached=0, total=1)
        assert expected in result.details

    def test_plane_uses_wp_radius(self):
        from lib.i18n import t
        result = self._run(_QP_VI)
        assert not _QP_VI.is_copter           # precondition
        expected = t("mission.wp_reached", radius="30", reached=1, total=1)
        assert expected in result.details


class TestMissionRadiusGrading:
    """Grade waypoint arrival by the aircraft's radius, not a fixed 20 m."""

    def _run(self, offset_deg, radius_m=None, *, params=None, vehicle=None):
        if params is None:
            params = {"WP_RADIUS": radius_m}
        if vehicle is None:
            vehicle = _QP_VI
        ts = _make_ts(200, 0.5)
        n = len(ts)
        reader = SyntheticReader(
            messages={
                "CMD": {
                    "_timestamp": np.array([0.0]),
                    "CNum": np.array([1]),
                    "CId": np.array([16]),
                    "Lat": np.array([30.0]),
                    "Lng": np.array([120.0]),
                    "Alt": np.array([50.0]),
                },
                "POS": {
                    "_timestamp": ts,
                    "Lat": np.full(n, 30.0 + offset_deg),
                    "Lng": np.full(n, 120.0),
                    "Alt": np.full(n, 50.0),
                },
            },
            params=params,
            vehicle_type=vehicle.vehicle_type,
        )
        reader.mode_list = [SimpleNamespace(mode_name="AUTO")]
        from analyzers.mission import MissionAnalyzer
        return MissionAnalyzer(reader, Thresholds(), vehicle).analyze()

    def test_mean_over_20m_inside_wp_radius_is_good(self):
        result = self._run(offset_deg=0.002, radius_m=300.0)
        distance_m = 0.002 * 111320
        assert 20.0 < distance_m < 300.0       # regression precondition
        assert result.status == "good"
        accuracy_fig = result.figures[-1]
        assert set(accuracy_fig.data[0].marker.color) == {"#5cb85c"}
        assert len(accuracy_fig.layout.shapes) == 1
        assert accuracy_fig.layout.shapes[0].y0 == 300.0
        from lib.i18n import t
        assert accuracy_fig.layout.annotations[0].text == t(
            "mission.thresh_reached", val="300")

    def test_a_waypoint_outside_wp_radius_is_warning(self):
        result = self._run(offset_deg=0.003, radius_m=200.0)
        distance_m = 0.003 * 111320
        assert distance_m > 200.0              # regression precondition
        assert result.status == "warning"
        assert "WP_RADIUS" in result.evidence_source
        accuracy_fig = result.figures[-1]
        assert set(accuracy_fig.data[0].marker.color) == {"#d9534f"}
        assert len(accuracy_fig.layout.shapes) == 1
        assert accuracy_fig.layout.shapes[0].y0 == 200.0

    @pytest.mark.parametrize("params, vehicle, param_name", [
        ({"WPNAV_RADIUS": 0}, _DEFAULT_VI, "WPNAV_RADIUS"),
        ({"WP_RADIUS": 0}, _QP_VI, "WP_RADIUS"),
    ])
    def test_zero_radius_returns_info_but_keeps_distances(
            self, params, vehicle, param_name):
        from lib.i18n import t
        result = self._run(0.002, params=params, vehicle=vehicle)
        assert result.status == "info"
        assert result.summary == t("mission.invalid_radius",
                                   param=param_name, radius="0")
        assert t("mission.wp_accuracy", mean="222.6", max="222.6") \
            in result.details
        assert result.figures  # the route map survives the refused grading

    def test_mixed_waypoints_show_both_colors_and_warn(self):
        """One waypoint inside the radius, one outside: the chart shows
        both colors and the warning matches the arrival count."""
        ts = _make_ts(200, 0.5)
        n = len(ts)
        reader = SyntheticReader(messages={
            "CMD": {
                "_timestamp": np.array([0.0, 1.0]),
                "CNum": np.array([1, 2]),
                "CId": np.array([16, 16]),
                "Lat": np.array([30.0, 30.006]),
                "Lng": np.array([120.0, 120.0]),
                "Alt": np.array([50.0, 50.0]),
            },
            "POS": {
                "_timestamp": ts,
                "Lat": np.full(n, 30.0005),
                "Lng": np.full(n, 120.0),
                "Alt": np.full(n, 50.0),
            },
        }, params={"WP_RADIUS": 300.0}, vehicle_type="ArduPlane")
        reader.mode_list = [SimpleNamespace(mode_name="AUTO")]
        from analyzers.mission import MissionAnalyzer
        from lib.i18n import t
        result = MissionAnalyzer(reader, Thresholds(), _QP_VI).analyze()

        assert 0.0005 * 111320 < 300.0 < 0.0055 * 111320  # preconditions
        accuracy_fig = result.figures[-1]
        assert set(accuracy_fig.data[0].marker.color) == \
            {"#5cb85c", "#d9534f"}
        assert t("mission.wp_reached",
                 radius="300", reached=1, total=2) in result.details
        assert result.status == "warning"


# ====================================================================
# 4. IMU consistency — armed interval filtering
# ====================================================================

class TestIMUConsistencyArmedFilter:
    def test_armed_rms_uses_armed_intervals(self):
        """A disagreement only on the bench must not reach the status."""
        from analyzers.imu_consistency import IMUConsistencyAnalyzer
        n = 3000
        ts = np.repeat(np.linspace(0.0, 300.0, n), 2)
        inst = np.tile([0.0, 1.0], n)
        zeros = np.zeros(2 * n)
        acc_x = np.where((inst == 1) & (ts < 50.0), 8.0, 0.0)
        reader = SyntheticReader(messages={"IMU": {
            "_timestamp": ts, "I": inst, "AccX": acc_x, "AccY": zeros,
            "AccZ": zeros - 9.8, "GyrX": zeros, "GyrY": zeros,
            "GyrZ": zeros, "T": zeros + 40.0,
        }}, armed_intervals=[(100.0, 290.0)])
        whole_log_rms = 8.0 * np.sqrt(50.0 / 300.0)
        assert whole_log_rms > Thresholds().imu_accel_diff_crit
        result = IMUConsistencyAnalyzer(
            reader, Thresholds(), _DEFAULT_VI).analyze()
        assert result.status == "good"


# ====================================================================
# 5. compass offset correction
# ====================================================================

class TestCompassOffsetCorrection:
    def test_compass_uses_offset(self):
        from analyzers.compass import CompassAnalyzer
        import inspect
        src = inspect.getsource(CompassAnalyzer._motor_interference)
        assert "ofs_x" in src or "OfsX" in src

    def test_compass_consistency_uses_offset(self):
        from analyzers.compass_consistency import CompassConsistencyAnalyzer
        import inspect
        src = inspect.getsource(CompassConsistencyAnalyzer.analyze)
        assert "OfsX" in src


# ====================================================================
# 7. trend_analyzer label translation
# ====================================================================

class TestTrendLabelTranslation:
    def test_next_step_translates_labels(self):
        from lib.trend_analyzer import _build_next_step
        import inspect
        src = inspect.getsource(_build_next_step)
        assert "_t(m.label)" in src


# ====================================================================
# 8. BaseAnalyzer helpers exist
# ====================================================================

class TestBaseAnalyzerHelpers:
    def test_get_armed_mask(self):
        from analyzers import BaseAnalyzer
        assert hasattr(BaseAnalyzer, "get_armed_mask")

    def test_safe_interp(self):
        from analyzers import BaseAnalyzer
        x = np.array([1.0, 2.0, 3.0])
        xp = np.array([0.0, np.nan, 2.0, 4.0])
        fp = np.array([10.0, np.nan, 20.0, 40.0])
        result = BaseAnalyzer.safe_interp(x, xp, fp)
        assert np.isfinite(result).all()
        assert abs(result[1] - 20.0) < 0.01

    def test_safe_interp_all_nan(self):
        from analyzers import BaseAnalyzer
        x = np.array([1.0])
        xp = np.array([np.nan])
        fp = np.array([np.nan])
        result = BaseAnalyzer.safe_interp(x, xp, fp)
        assert np.isnan(result[0])

    def test_motor_param_copter(self):
        from analyzers import BaseAnalyzer
        reader = SyntheticReader({}, vehicle_type="ArduCopter")
        th = Thresholds()
        a = BaseAnalyzer(reader, th, _DEFAULT_VI)
        assert a.motor_param("SPIN_MIN") == "MOT_SPIN_MIN"

    def test_motor_param_quadplane(self):
        from analyzers import BaseAnalyzer
        reader = SyntheticReader({}, vehicle_type="ArduPlane")
        th = Thresholds()
        a = BaseAnalyzer(reader, th, _QP_VI)
        assert a.motor_param("SPIN_MIN") == "Q_M_SPIN_MIN"

    def test_read_primary_imu_filters_interleaved_instances(self):
        """Multi-IMU logs interleave IMU0/1/2 at identical TimeUS.

        read_primary_imu must return only the primary instance so an FFT
        sees a uniform single-sensor series — otherwise dt/rate is inflated
        by the instance count and the spectrum gains ghost peaks.
        """
        from analyzers import BaseAnalyzer
        n_cycles = 300
        ts = np.repeat(np.linspace(0, 30, n_cycles), 3)  # 3 samples/cycle
        inst = np.tile([0.0, 1.0, 2.0], n_cycles)
        gyrx = np.select(
            [inst == 0, inst == 1, inst == 2], [1.0, 2.0, 3.0])
        reader = SyntheticReader(messages={"IMU": {
            "_timestamp": ts, "I": inst, "GyrX": gyrx,
        }})
        a = BaseAnalyzer(reader, Thresholds(), _DEFAULT_VI)
        out = a.read_primary_imu(["GyrX"])
        # One instance's worth of samples, not 3x.
        assert len(out["_timestamp"]) == n_cycles
        # Only the primary instance (I==0, GyrX==1.0).
        assert np.all(out["GyrX"] == 1.0)

    def test_read_primary_imu_oldstyle_no_instance_field(self):
        """Old-style logs without an I field fall back to unfiltered data."""
        from analyzers import BaseAnalyzer
        n = 400
        ts = np.linspace(0, 30, n)
        reader = SyntheticReader(messages={"IMU": {
            "_timestamp": ts, "GyrX": np.full(n, 0.5),
        }})
        a = BaseAnalyzer(reader, Thresholds(), _DEFAULT_VI)
        out = a.read_primary_imu(["GyrX"])
        assert len(out["_timestamp"]) == n  # unfiltered


# ====================================================================
# 9. i18n format error resilience
# ====================================================================

class TestI18nResilience:
    def test_missing_kwarg_no_crash(self):
        from lib.i18n import t, set_locale
        set_locale("zh")
        result = t("vibration.good_summary")
        assert "{val}" in result
        assert isinstance(result, str)

    def test_normal_call(self):
        from lib.i18n import t, set_locale
        set_locale("zh")
        result = t("vibration.good_summary", val="12.3")
        assert "12.3" in result
        assert "{val}" not in result
