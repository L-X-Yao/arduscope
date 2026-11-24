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
from arduscope.lib.attitude_scope import scope_grade_mask, sustained_seconds
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

    def test_quadplane_without_any_phase_source_grades_nothing(self):
        # With no phase source the log cannot say which time was under the
        # quad controller. Grading everything would put the Copter rules
        # back on fixed-wing time, so nothing is graded.
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(SyntheticReader({}), _vehicle("ArduPlane", True), ts)
        assert scope == "vtol_unknown_phase" and not mask.any()

    def test_an_empty_q_stream_is_not_a_phase_source(self):
        # A PIQR type with one row carries no proximity information.
        reader = SyntheticReader({"PIQR": {"_timestamp": np.array([5.0])}})
        ts = np.arange(50) * DT
        mask, scope = scope_grade_mask(reader, _vehicle("ArduPlane", True), ts)
        assert scope == "vtol_unknown_phase" and not mask.any()


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

    def test_no_phase_source_does_not_grade_a_35_deg_excursion(self):
        # The log cannot say which time was VTOL. Defaulting to "all VTOL"
        # would grade a fixed-wing excursion by the Copter rule.
        att = _sustained_pitch_excursion(35.0, 100.0, 20.0)
        reader = SyntheticReader({"ATT": att}, params=PLANE_Q_ENABLE,
                                 armed_intervals=[(0.0, 1e9)])
        result = AttitudeAnalyzer(reader, Thresholds(), self.VEHICLE).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"
        assert result.param_suggestions == []
        assert result.evidence_source == ""
        assert tr("attitude.vtol_unknown_phase") in result.details
        # The excursion is still reported as a number, just not graded.
        assert any(re.search(r"(?<![\d.])35\.0 deg", d) for d in result.details)

    def test_no_phase_source_does_not_grade_a_7_deg_rms(self):
        att = _rms_tracking(7.0)
        reader = SyntheticReader({"ATT": att}, params=PLANE_Q_ENABLE,
                                 armed_intervals=[(0.0, 1e9)])
        result = AttitudeAnalyzer(reader, Thresholds(), self.VEHICLE).analyze()
        assert result.status == "good"
        assert tr("attitude.vtol_unknown_phase") in result.details

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


class TestPeakDurationGuard:
    """A peak past 30 deg is critical only when it outlasts the
    controller's lean time constant (att_peak_min_duration_s = 1.0 s)."""
    VEHICLE = _vehicle("ArduCopter")

    def _excursion(self, samples: int, peak: float = 38.0):
        """`samples` consecutive samples at `peak`, starting at t=100 s."""
        n = 4000
        act = np.zeros(n)
        a = _at(100.0)
        act[a:a + samples] = peak
        return _att(n, np.zeros(n), act)

    def test_precondition_the_metric_is_first_to_last(self):
        # 10 Hz: N samples span (N - 1) / 10 s. Pins the fixture arithmetic
        # the boundary tests below depend on.
        att = self._excursion(11)
        over = np.abs(att["Pitch"]) > 30.0
        assert over.sum() == 11
        assert sustained_seconds(att["_timestamp"], over) == pytest.approx(1.0)
        att = self._excursion(12)
        over = np.abs(att["Pitch"]) > 30.0
        assert sustained_seconds(att["_timestamp"], over) == pytest.approx(1.1)

    @pytest.mark.parametrize("samples,label", [(2, "0.2 s"), (6, "0.6 s"),
                                               (11, "exactly 1.0 s")])
    def test_short_excursions_are_not_critical(self, samples, label):
        result = _run({"ATT": self._excursion(samples)}, self.VEHICLE)
        assert result.status != "critical", label

    def test_two_samples_at_38_deg_is_a_warning_not_critical(self):
        result = _run({"ATT": self._excursion(2)}, self.VEHICLE)
        assert result.status == "warning"
        assert result.action_level == "run_autotune"
        assert tr("attitude.peak_not_sustained") not in result.details  # has placeholders
        assert any(d.startswith(tr("attitude.peak_not_sustained",
                                   peak="", seconds="", limit="", guard="")
                       .split("{")[0]) or "30" in d and "not graded" in d.lower()
                   or "不判 critical" in d for d in result.details)
        assert result.evidence_source != ""

    def test_exactly_one_second_is_not_critical_the_rule_is_strict(self):
        result = _run({"ATT": self._excursion(11)}, self.VEHICLE)
        assert result.status == "warning"

    def test_just_over_one_second_is_critical(self):
        result = _run({"ATT": self._excursion(12)}, self.VEHICLE)
        assert result.status == "critical"

    def test_a_genuine_sustained_loss_stays_critical(self):
        # 5 s at 38 deg in a 400 s log. The pitch RMS is 4.25 deg, under
        # the 5 deg rule, so the peak rule alone decides: it must say so.
        result = _run({"ATT": self._excursion(50)}, self.VEHICLE)
        assert result.status == "critical"
        assert result.action_level in ("run_autotune", "apply_param")
        assert result.summary == tr("attitude.crit_peak_summary",
                                    peak="38.0", seconds="4.9", guard=1.0)
        assert tr("attitude.peak_sustained", limit=30.0, seconds="4.9",
                  guard=1.0) in result.details

    def test_a_long_loss_stays_critical_either_way(self):
        # 20 s at 38 deg: the RMS rule fires too (8.5 deg). Whichever rule
        # leads, the card is critical and the duration is still reported.
        result = _run({"ATT": self._excursion(200)}, self.VEHICLE)
        assert result.status == "critical"
        assert tr("attitude.peak_sustained", limit=30.0, seconds="19.9",
                  guard=1.0) in result.details

    def test_a_single_sample_at_47_deg_is_not_critical(self):
        n = 4000
        des = np.zeros(n)
        des[2000] = 47.6
        result = _run({"ATT": _att(n, des, np.zeros(n))}, self.VEHICLE)
        assert result.status != "critical"


class TestSustainedSeconds:
    """The duration metric: first-to-last over adjacent samples.

    A run ends at a sample that is not over, and at a telemetry gap of at
    least two lost samples. One lost sample is ordinary logger jitter and
    is counted in full."""

    def test_nothing_over_is_zero(self):
        assert sustained_seconds(np.arange(100) * DT, np.zeros(100, dtype=bool)) == 0.0

    def test_one_sample_over_is_zero(self):
        # A lone sample cannot show that the excursion lasted anything.
        over = np.zeros(100, dtype=bool)
        over[50] = True
        assert sustained_seconds(np.arange(100) * DT, over) == 0.0

    def test_a_run_is_first_over_to_last_over(self):
        over = np.zeros(100, dtype=bool)
        over[40:61] = True              # 21 samples
        assert sustained_seconds(np.arange(100) * DT, over) == pytest.approx(2.0)

    def test_the_longest_of_two_runs_counts(self):
        over = np.zeros(200, dtype=bool)
        over[20:31] = True              # 1.0 s
        over[100:126] = True            # 2.5 s
        assert sustained_seconds(np.arange(200) * DT, over) == pytest.approx(2.5)

    def test_a_sample_that_is_not_over_ends_the_run(self):
        t = np.arange(0.0, 1.1, DT)
        over = np.r_[np.ones(5, bool), np.zeros(1, bool), np.ones(5, bool)]
        assert sustained_seconds(t, over) == pytest.approx(0.4)

    def test_one_lost_sample_is_tolerated(self):
        # 0.0-0.9 s, the 1.0 s row missing, 1.1-1.2 s: one continuous run.
        t = np.r_[np.arange(0.0, 1.0, DT), 1.1, 1.2]
        assert sustained_seconds(t, np.ones(len(t), bool)) == pytest.approx(1.2)

    def test_a_telemetry_gap_ends_the_run(self):
        # 0.0-0.4 s over, 1.5 s missing, 2.0-2.4 s over. The missing time
        # is unknown and must not be added, nor may it weld the two.
        t = np.r_[np.arange(0.0, 0.5, DT), np.arange(2.0, 2.5, DT)]
        assert sustained_seconds(t, np.ones(len(t), bool)) == pytest.approx(0.4)

    def test_two_short_excursions_around_a_gap_do_not_add_up(self):
        # 0.8 s + 0.8 s would pass a 1.0 s guard if the gap joined them.
        t = np.r_[np.arange(0.0, 0.9, DT), np.arange(1.2, 2.1, DT)]
        over = np.ones(len(t), bool)
        assert sustained_seconds(t, over) < 1.0

    def test_ordinary_jitter_does_not_end_a_run(self):
        rng = np.random.default_rng(1)
        t = np.cumsum(np.r_[0.0, rng.uniform(0.07, 0.13, 60)])
        assert sustained_seconds(t, np.ones(len(t), bool)) == pytest.approx(t[-1] - t[0])


class TestGapCannotCreateACritical:
    """End to end: a dropout must not turn two short dips into one long one."""

    def _two_dips_with_a_gap(self, half_seconds: float, gap_seconds: float):
        n = 6000
        act = np.zeros(n)
        a = _at(100.0)
        act[a:a + int(round(half_seconds / DT))] = 38.0
        b = _at(100.0 + half_seconds + gap_seconds)
        act[b:b + int(round(half_seconds / DT))] = 38.0
        att = _att(n, np.zeros(n), act)
        lo = 100.0 + half_seconds
        hi = 100.0 + half_seconds + gap_seconds
        keep = ~((att["_timestamp"] > lo) & (att["_timestamp"] < hi))
        return {k: v[keep] for k, v in att.items()}

    def test_0_8s_dips_around_a_gap_are_not_critical(self):
        att = self._two_dips_with_a_gap(0.8, 0.3)
        result = _run({"ATT": att}, _vehicle("ArduCopter"))
        assert result.status != "critical"

    def test_the_same_data_without_the_gap_is_critical(self):
        # Control for the test above: it is the gap, not the shape.
        att = self._two_dips_with_a_gap(0.8, -0.0)
        n = 6000
        act = np.zeros(n)
        act[_at(100.0):_at(101.6)] = 38.0
        result = _run({"ATT": _att(n, np.zeros(n), act)}, _vehicle("ArduCopter"))
        assert result.status == "critical"
