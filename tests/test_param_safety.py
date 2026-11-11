"""Suggestions taken out in v0.9.4 stay out, and what replaced them says enough.

A .param file may get loaded on an aircraft. These rules had no source for
their value, or a source against them, so the report names the parameter and
leaves the number to the person with the aircraft. compass, servo_dither and
pid_tuning have the same cases next to their other tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from analyzers.ekf import EKFAnalyzer
from analyzers.mission import MissionAnalyzer
from analyzers.vtol_transition import VTOLTransitionAnalyzer
from lib.i18n import t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_ekf_primary_lane import _SPIKE, _analyzer, _variance_log
from tests.test_rescan_e import _qtun_reader
from tests.test_synthetic_coverage import SyntheticReader, _thresholds, _vehicle

pytestmark = pytest.mark.fast

_COPTER = VehicleInfo(
    vehicle_type="ArduCopter", board_name="test", firmware_version="4.7",
    frame_class=1, motor_count=4, imu_count=3, is_quadplane=False,
)
_PLANE = VehicleInfo(
    vehicle_type="ArduPlane", board_name="test", firmware_version="4.7",
    frame_class=1, motor_count=0, imu_count=2, is_quadplane=False,
)


def _missed_waypoint(params: dict, vehicle: VehicleInfo):
    """AUTO, one waypoint, the track about 11 m off it."""
    ts = np.arange(200, dtype=float) * 0.5
    n = len(ts)
    reader = SyntheticReader(
        messages={
            "CMD": {"_timestamp": np.array([0.0]), "CNum": np.array([1]),
                    "CId": np.array([16]), "Lat": np.array([30.0]),
                    "Lng": np.array([120.0]), "Alt": np.array([50.0])},
            "POS": {"_timestamp": ts, "Lat": np.full(n, 30.0001),
                    "Lng": np.full(n, 120.0), "Alt": np.full(n, 50.0)},
        },
        params=params,
        vehicle_type=vehicle.vehicle_type,
    )
    reader.mode_list = [SimpleNamespace(mode_name="AUTO")]
    return MissionAnalyzer(reader, Thresholds(), vehicle).analyze()


class TestMissionRadius:
    @pytest.mark.parametrize("params, vehicle, name, radius", [
        ({"WP_RADIUS_M": 2.0}, _COPTER, "WP_RADIUS_M", "2"),
        ({"WP_RADIUS_M": 2.5}, _COPTER, "WP_RADIUS_M", "2.5"),
        ({"WPNAV_RADIUS": 200}, _COPTER, "WPNAV_RADIUS", "2"),
        ({"WP_RADIUS": 5}, _PLANE, "WP_RADIUS", "5"),
    ])
    def test_names_the_parameter_in_use_and_no_value(self, params, vehicle,
                                                     name, radius):
        result = _missed_waypoint(params, vehicle)
        assert result.status == "warning"
        assert not result.param_suggestions
        assert result.action_level == "physical_check"
        assert result.action_text == t("mission.low_completion_action",
                                       param=name, radius=radius)

    def test_wp_radius_m_is_not_scaled_to_centimetres(self):
        """v0.9.3 suggested 500 for a radius of 2, in a 0.05 to 10 m range."""
        result = _missed_waypoint({"WP_RADIUS_M": 2.0}, _COPTER)
        assert "500" not in result.action_text
        assert "200" not in result.action_text


class TestEkfFailsafe:
    def _critical(self, tmp_path, limit):
        reader = _variance_log(tmp_path, lambda secs: 0,
                               lambda core, secs: _SPIKE)
        reader.params["FS_EKF_THRESH"] = limit
        result = _analyzer(EKFAnalyzer, reader).analyze()
        assert result.status == "critical"
        return result

    def test_a_stricter_limit_is_stated_not_loosened(self, tmp_path):
        result = self._critical(tmp_path, 0.6)
        assert not result.param_suggestions
        assert result.action_level == "safety_stop"
        assert t("ekf.fs_thresh_strict", thresh="0.60") in result.details

    def test_the_default_limit_needs_no_note(self, tmp_path):
        result = self._critical(tmp_path, 0.8)
        assert t("ekf.fs_thresh_strict", thresh="0.80") not in result.details


class TestVtolTransition:
    def _critical(self, **params):
        reader = _qtun_reader(20.0, [(0.0, "FBWA", 5), (50.0, "QLOITER", 19)])
        reader.params.update(params)
        result = VTOLTransitionAnalyzer(
            reader, _thresholds(reader), _vehicle("ArduPlane")).analyze()
        assert result.status == "critical"
        return result

    def test_says_where_the_parameters_are_and_no_target(self):
        result = self._critical(Q_TRANSITION_MS=3000, Q_TRAN_PIT_MAX=5)
        assert not result.param_suggestions
        assert t("vtol_transition.param_trans_ms",
                 current=3000) in result.action_text
        assert t("vtol_transition.param_pit_max",
                 current=5) in result.action_text
        assert "→" not in result.action_text

    def test_the_default_pitch_limit_is_not_mentioned(self):
        result = self._critical(Q_TRANSITION_MS=3000, Q_TRAN_PIT_MAX=3)
        assert "Q_TRAN_PIT_MAX" not in result.action_text
