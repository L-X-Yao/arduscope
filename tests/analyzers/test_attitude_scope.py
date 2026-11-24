"""Attitude criticals apply only where the Copter controller runs.

AC_ATTITUDE_THRUST_ERROR_ANGLE (30 deg) and the autotune relaxation
ceilings come from AC_AttitudeControl, the multirotor controller. ArduPlane's
fixed-wing controller never uses them, and a QuadPlane is under the Copter
controller only in its VTOL phases.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from arduscope.analyzers.attitude import AttitudeAnalyzer
from arduscope.lib.attitude_scope import scope_grade_mask
from arduscope.lib.i18n import t as tr
from arduscope.lib.thresholds import Thresholds
from arduscope.lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

DT = 0.1
PLANE_Q_ENABLE = {"Q_ENABLE": 1}


def _vehicle(vt: str, quadplane: bool = False) -> VehicleInfo:
    return VehicleInfo(vehicle_type=vt, firmware_version="4.6.3",
                       board_name="synthetic", frame_class=1, motor_count=4,
                       imu_count=2, is_quadplane=quadplane)


def _att(n: int, des_pitch: np.ndarray, pitch: np.ndarray,
         des_roll: np.ndarray | None = None,
         roll: np.ndarray | None = None) -> dict:
    des_roll = np.zeros(n) if des_roll is None else des_roll
    roll = np.zeros(n) if roll is None else roll
    return {"_timestamp": np.arange(n) * DT, "DesRoll": des_roll, "Roll": roll,
            "DesPitch": des_pitch, "Pitch": pitch,
            "DesYaw": np.zeros(n), "Yaw": np.zeros(n)}


def _run(messages: dict, vehicle: VehicleInfo, params: dict | None = None,
         armed=None):
    reader = SyntheticReader(messages, params=params or {},
                             armed_intervals=armed or [(0.0, 1e9)])
    return AttitudeAnalyzer(reader, Thresholds(), vehicle).analyze()


def _at(times):
    return np.searchsorted(np.arange(4000) * DT, times)


def _sustained_pitch_excursion(peak: float, start: float, seconds: float):
    """Commanded level; the vehicle sits `peak` deg off for `seconds`."""
    n = 4000
    des = np.zeros(n)
    act = np.zeros(n)
    a, b = _at(start), _at(start + seconds)
    act[a:b] = peak
    return _att(n, des, act)


def _rms_tracking(error: float):
    n = 4000
    des = np.zeros(n)
    act = np.full(n, error)
    return _att(n, des, act)


def _noisy_rms(error: float, seed: int = 3):
    n = 4000
    rng = np.random.default_rng(seed)
    return _att(n, np.zeros(n), rng.normal(0.0, error, n))


class TestScopeMask:
    def _modes(self, switches):
        """switches: [(time, mode)]; MODE rows at each switch."""
        ts = np.array([s[0] for s in switches], dtype=float)
        return {"MODE": {"_timestamp": ts,
                         "Mode": np.array([s[1] for s in switches], float)}}

    def test_copter_grades_every_sample(self):
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(SyntheticReader({"ATT": {}}), _vehicle("ArduCopter"), ts)
        assert scope == "copter" and mask.all()

    def test_vendor_copter_banner_still_counts_as_copter(self):
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(
            SyntheticReader({}), _vehicle("FC-FPV-Rocket (ArduCopter)"), ts)
        assert scope == "copter" and mask.all()

    def test_fixed_wing_plane_is_not_graded(self):
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(SyntheticReader({}), _vehicle("ArduPlane"), ts)
        assert scope == "fixed_wing" and not mask.any()

    def test_unknown_vehicle_is_not_graded(self):
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(SyntheticReader({}), _vehicle("Unknown"), ts)
        assert scope == "unknown" and not mask.any()

    def test_quadplane_grades_q_modes_only(self):
        # 0-20 s: QHOVER(18), 20-40 s: FBWA(2), 40-60 s: QLOITER(19)
        reader = SyntheticReader(self._modes([(0, 18), (20, 2), (40, 19)]))
        ts = np.arange(600) * DT
        mask, scope = scope_grade_mask(reader, _vehicle("ArduPlane", True), ts)
        assert scope == "vtol"
        assert mask[(ts > 5) & (ts < 15)].all()
        assert not mask[(ts > 25) & (ts < 35)].any()
        assert mask[(ts > 45) & (ts < 55)].all()

    def test_quadplane_leaves_out_servo_passthrough_modes(self):
        # MANUAL(0) and TRAINING(3) run no attitude controller.
        reader = SyntheticReader(self._modes([(0, 18), (20, 0), (30, 3), (40, 19)]))
        ts = np.arange(600) * DT
        mask, _ = scope_grade_mask(reader, _vehicle("ArduPlane", True), ts)
        assert mask[(ts > 5) & (ts < 15)].all()
        assert not mask[(ts > 22) & (ts < 38)].any()
        assert mask[(ts > 45) & (ts < 55)].all()

    def test_quadplane_uses_the_q_controller_stream(self):
        # PIQR logs at 10 Hz between 50 s and 60 s: that is VTOL time even
        # without any MODE row, as inside an AUTO mission.
        q_t = np.arange(50.0, 60.0, DT)
        reader = SyntheticReader({"PIQR": {"_timestamp": q_t}})
        ts = np.arange(800) * DT
        mask, scope = scope_grade_mask(reader, _vehicle("ArduPlane", True), ts)
        assert scope == "vtol"
        assert mask[(ts > 51) & (ts < 59)].all()
        assert not mask[(ts > 20) & (ts < 40)].any()
        assert not mask[(ts > 65) & (ts < 75)].any()

    def test_quadplane_without_any_phase_source_keeps_every_sample(self):
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(SyntheticReader({}), _vehicle("ArduPlane", True), ts)
        assert scope == "vtol" and mask.all()


class TestFixedWingIsNotGraded:
    VEHICLE = _vehicle("ArduPlane")

    def test_a_sustained_35_deg_excursion_is_not_critical(self):
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        result = _run({"ATT": att}, self.VEHICLE)
        assert result.status != "critical"

    def test_a_7_deg_rms_is_not_critical(self):
        att = _rms_tracking(7.0)
        result = _run({"ATT": att}, self.VEHICLE)
        assert result.status != "critical"

    def test_neither_becomes_a_copter_warning(self):
        # The legacy 15 deg screen and the 2.5 deg RMS warning are Copter
        # thresholds too.
        peak = _run({"ATT": _sustained_pitch_excursion(35.0, 100.0, 20.0)},
                    self.VEHICLE)
        rms = _run({"ATT": _rms_tracking(3.0)}, self.VEHICLE)
        assert peak.status == "good"
        assert rms.status == "good"
        assert peak.action_level == "ok" and rms.action_level == "ok"
        assert peak.param_suggestions == [] and rms.param_suggestions == []

    def test_the_card_says_why_and_keeps_the_numbers(self):
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        result = _run({"ATT": att}, self.VEHICLE)
        assert tr("attitude.fixed_wing_not_graded") in result.details
        # The armed-wide numbers are still reported, in the suite's locale.
        # Format the template through the same function the analyzer uses.
        assert tr("attitude.pitch_peak", peak="35.0", p95="1.7") in result.details
        assert result.summary == tr("attitude.fixed_wing_summary",
                                   roll="0.00", pitch="7.83", peak="35.0")
        assert len(result.figures) == 4 and result.evidence_source == ""

    def test_an_8_deg_rms_that_is_critical_on_a_copter_stays_good_here(self):
        att = _noisy_rms(8.0)
        assert _run({"ATT": att}, _vehicle("ArduCopter")).status == "critical"
        assert _run({"ATT": att}, self.VEHICLE).status == "good"


class TestQuadPlane:
    VEHICLE = _vehicle("ArduPlane", True)

    def _modes(self, *switches):
        ts = np.array([s[0] for s in switches], dtype=float)
        return {"MODE": {"_timestamp": ts,
                         "Mode": np.array([s[1] for s in switches], float)}}

    def test_an_excursion_in_the_vtol_phase_is_critical(self):
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        reader = SyntheticReader(
            {"ATT": att, **self._modes((0.0, 18))},
            params=PLANE_Q_ENABLE, armed_intervals=[(0.0, 1e9)])
        result = AttitudeAnalyzer(reader, Thresholds(), self.VEHICLE).analyze()
        assert result.status == "critical"
        assert result.summary.startswith(tr("attitude.vtol_summary_prefix"))

    def test_the_same_excursion_in_a_fixed_wing_segment_is_not_graded(self):
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        reader = SyntheticReader(
            {"ATT": att, **self._modes((0.0, 2))},  # FBWA the whole time
            params=PLANE_Q_ENABLE, armed_intervals=[(0.0, 1e9)])
        result = AttitudeAnalyzer(reader, Thresholds(), self.VEHICLE).analyze()
        assert result.status == "good"
        assert tr("attitude.vtol_no_samples") in result.details

    def test_a_7_deg_rms_in_vtol_is_critical_and_in_fixed_wing_is_not(self):
        att = _rms_tracking(7.0)
        vtol = SyntheticReader({"ATT": att, **self._modes((0.0, 19))},
                               params=PLANE_Q_ENABLE, armed_intervals=[(0.0, 1e9)])
        fw = SyntheticReader({"ATT": att, **self._modes((0.0, 15))},
                             params=PLANE_Q_ENABLE, armed_intervals=[(0.0, 1e9)])
        assert AttitudeAnalyzer(vtol, Thresholds(), self.VEHICLE).analyze().status == "critical"
        assert AttitudeAnalyzer(fw, Thresholds(), self.VEHICLE).analyze().status == "good"

    def test_a_fixed_wing_excursion_does_not_raise_a_vtol_flight(self):
        # Clean VTOL 0-40 s, 40 deg for 20 s while in FBWA 40-60 s, clean again.
        n = 1200
        act = np.zeros(n)
        act[_at(40.0):_at(60.0)] = 40.0
        att = _att(n, np.zeros(n), act)
        reader = SyntheticReader(
            {"ATT": att, **self._modes((0.0, 18), (40.0, 2), (60.0, 18))},
            params=PLANE_Q_ENABLE, armed_intervals=[(0.0, 1e9)])
        result = AttitudeAnalyzer(reader, Thresholds(), self.VEHICLE).analyze()
        assert result.status == "good"
        # The fixed-wing excursion is still visible in the armed numbers.
        # Both locales write the value as "<peak> deg", so match on that.
        assert any(re.search(r"(?<![\d.])40\.0 deg", d) for d in result.details)



class TestCopterIsUnchangedByScope:
    def test_the_same_data_is_graded_on_a_copter_and_not_on_a_plane(self):
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        copter = _run({"ATT": att}, _vehicle("ArduCopter"))
        plane = _run({"ATT": att}, _vehicle("ArduPlane"))
        assert copter.status == "critical"
        assert plane.status == "good"


class TestUnknownVehicle:
    def test_stops_instead_of_guessing(self):
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        result = _run({"ATT": att}, _vehicle("Unknown"))
        assert result.status == "info"
        assert result.summary == tr("attitude.unknown_vehicle")
        assert result.action_level != "apply_param"
