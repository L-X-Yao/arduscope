"""ArduPilot's EKF failsafe rule, on synthetic XKF4 data.

A single variance over FS_EKF_THRESH is not the failsafe: the test needs
position with another indicator, or two indicators, and the ten-check
counter. The reference log of 2025-07-29 (QuadPlane, SM 1.81, SV 0.26,
SP 0.45) had no ERR event for exactly this reason; the old rule called a
compass variance of 1.81 a failsafe.

Since the model completion the replay also carries the Copter-only
FS_EKF_FILT low-pass, the has_position gate from XKF4.SS, and the
up/down counter of EKF_CHECK_ITERATIONS_MAX.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from arduscope.analyzers.ekf import EKFAnalyzer
from arduscope.analyzers.failsafe import FailsafeAnalyzer
from arduscope.lib.ekf_failsafe import (
    ekf_failsafe_replay, in_modes, lowpass_filtered, over_threshold,
    plane_posvel_mask, sustained, vehicle_kind)
from arduscope.lib.i18n import t
from arduscope.lib.log_reader import ErrorEvent, ModeChange
from arduscope.lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import (
    SyntheticReader, _thresholds, _vehicle)

pytestmark = pytest.mark.fast

# XKF4.SS bit values (AP_NavEKF/AP_Nav_Common.h).
SS_ABS, SS_REL = 16, 8
SS_CONST_POS = 128
SS_PRED_REL, SS_PRED_ABS = 256, 512


def _quadplane() -> VehicleInfo:
    return VehicleInfo(
        vehicle_type="ArduPlane",
        firmware_version="4.6.3",
        board_name="synthetic",
        frame_class=1,
        motor_count=4,
        imu_count=2,
        is_quadplane=True,
    )


def _reader(sm=0.1, sp=0.1, sv=0.1, sh=0.1, seconds=10.0, rate=10.0,
            vehicle_type="ArduCopter", ss=SS_ABS,
            armed_intervals=None) -> SyntheticReader:
    n = int(seconds * rate)
    ts = np.arange(n, dtype=float) / rate
    kf4 = {
        "_timestamp": ts,
        "C": np.zeros(n),
        "PI": np.zeros(n),
        "SS": np.full(n, ss, dtype=float),
        "SV": np.full(n, sv),
        "SP": np.full(n, sp),
        "SH": np.full(n, sh),
        "SM": np.full(n, sm),
    }
    return SyntheticReader(
        messages={"XKF4": kf4},
        params={"FS_EKF_THRESH": 0.8},
        armed_intervals=([(0.0, seconds)] if armed_intervals is None
                         else armed_intervals),
        vehicle_type=vehicle_type,
    )


def _failsafe(reader, vehicle=None):
    return FailsafeAnalyzer(
        reader, _thresholds(reader), vehicle or _vehicle()).analyze()


def _phase_plane(stages, assist=0.0, with_qtun=True,
                 mode_name="AUTO", mode_number=10):
    """An AUTO/GUIDED QuadPlane with a two-indicator EKF breach throughout."""
    reader = _reader(sm=1.8, sp=0.9, vehicle_type="ArduPlane")
    reader.mode_list = [ModeChange(0.0, mode_name, mode_number)]
    times, values = zip(*stages)
    reader._messages["STAT"] = {
        "_timestamp": np.asarray(times, dtype=float),
        "Stage": np.asarray(values, dtype=float),
    }
    if with_qtun:
        qtun_ts = np.arange(0.0, 10.0, 0.04)
        reader._messages["QTUN"] = {
            "_timestamp": qtun_ts,
            "Ast": np.full(len(qtun_ts), assist),
        }
    return reader


def _ekf(reader, vehicle=None):
    return EKFAnalyzer(
        reader, _thresholds(reader), vehicle or _vehicle()).analyze()


class TestTheRule:
    def test_compass_alone_is_not_the_failsafe(self):
        assert not over_threshold([0.1], [0.1], [1.8], 0.8)[0]

    def test_position_with_compass_is(self):
        assert over_threshold([0.1], [0.9], [0.9], 0.8)[0]

    def test_velocity_at_twice_the_threshold_is(self):
        assert over_threshold([1.6], [0.1], [0.1], 0.8)[0]

    def test_velocity_alone_at_the_threshold_is_not(self):
        assert not over_threshold([0.9], [0.1], [0.1], 0.8)[0]

    def test_a_short_peak_is_not_sustained(self):
        ts = np.arange(100, dtype=float) / 10.0
        mask = np.zeros(100, dtype=bool)
        mask[20:25] = True            # 0.4 s
        assert not sustained(mask, ts).any()

    def test_a_one_second_run_is(self):
        ts = np.arange(100, dtype=float) / 10.0
        mask = np.zeros(100, dtype=bool)
        mask[20:31] = True            # 1.0 s
        assert sustained(mask, ts).any()

    def test_mode_gate_needs_a_mode_change(self):
        ts = np.arange(3, dtype=float)
        assert in_modes([], ts, (19,)) is None
        modes = [ModeChange(timestamp=1.0, mode_name="QLOITER",
                            mode_number=19)]
        assert list(in_modes(modes, ts, (19,))) == [False, True, True]


class TestFailsafeCard:
    def test_single_compass_variance_is_not_called_a_failsafe(self):
        res = _failsafe(_reader(sm=1.8, sp=0.4))
        assert res.status == "good"       # precondition: no ERR events
        assert t("failsafe.ekf_not_triggered") in res.details
        assert res.summary == t("failsafe.summary_good_ekf")

    def test_compass_and_position_over_is_a_failsafe(self):
        # One quiet second first: the firmware's has_ever_passed latch
        # needs an observed pass before anything counts.
        reader = _reader(seconds=11.0)
        reader._messages["XKF4"]["SM"][10:] = 1.8
        reader._messages["XKF4"]["SP"][10:] = 0.9
        res = _failsafe(reader)
        assert res.status == "critical"
        assert any(t("failsafe.ekf_triggered") in d
                   for d in res.details), res.details

    def test_plane_in_auto_does_not_run_the_test(self):
        reader = _reader(sm=1.8, sp=0.9, vehicle_type="ArduPlane")
        reader.mode_list = [ModeChange(0.0, "AUTO", 10)]
        res = _failsafe(reader, _quadplane())
        assert res.status == "good"
        assert t("failsafe.ekf_mode_gate") in res.details

    def test_auto_vtol_stage_runs_the_test_for_one_second(self):
        # STAT Stage: NORMAL=3, VTOL=2. The VTOL interval is t=2.0..3.0.
        reader = _phase_plane([(0.0, 3), (2.0, 2), (3.1, 3)])
        res = _failsafe(reader, _quadplane())
        assert res.status == "critical"
        assert any(t("failsafe.ekf_triggered") in d
                   for d in res.details), res.details

    def test_auto_fixed_wing_stage_with_the_same_breach_does_not_run(self):
        reader = _phase_plane([(0.0, 3)])
        res = _failsafe(reader, _quadplane())
        assert res.status == "good"
        assert t("failsafe.ekf_mode_gate") in res.details

    def test_fixed_wing_plane_does_not_get_counter_wording(self):
        # Q_ENABLE=0: the firmware never runs the check, so neither the
        # counter nor the continuous-second lines may appear; only the
        # disabled line explains the good badge.
        plane = VehicleInfo(
            vehicle_type="ArduPlane",
            firmware_version="4.6.3",
            board_name="synthetic",
            frame_class=1,
            motor_count=0,
            imu_count=2,
            is_quadplane=False,
        )
        reader = _reader(seconds=11.0, vehicle_type="ArduPlane")
        reader._messages["XKF4"]["SM"][10:] = 1.8
        reader._messages["XKF4"]["SP"][10:] = 0.9
        res = _failsafe(reader, plane)
        assert res.status == "good"
        assert t("failsafe.ekf_plane_disabled") in res.details
        assert not any(
            t("failsafe.ekf_transient_sustained") in d
            or t("failsafe.ekf_not_triggered") in d
            or t("failsafe.ekf_triggered_sustained") in d
            for d in res.details)

    def test_auto_stage_change_breaks_the_one_second_hold(self):
        reader = _phase_plane([(0.0, 3), (2.0, 2), (2.8, 3)])
        ts = reader._messages["XKF4"]["_timestamp"]
        gate = plane_posvel_mask(reader, ts)
        assert gate[20] and gate[27]       # 0.7 s between these endpoints
        assert not gate[19] and not gate[28]
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_auto_q_assist_stage_is_not_vtol_auto(self):
        # update_flight_stage also uses VTOL for fixed-wing Q assist.
        reader = _phase_plane([(0.0, 2)], assist=1.0)
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_auto_stage_without_qtun_assist_evidence_stays_closed(self):
        reader = _phase_plane([(0.0, 2)], with_qtun=False)
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_auto_stage_with_nan_ast_stays_closed(self):
        reader = _phase_plane([(0.0, 2)], assist=np.nan)
        ts = reader._messages["XKF4"]["_timestamp"]
        assert not plane_posvel_mask(reader, ts).any()
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_auto_stage_with_only_distant_qtun_stays_closed(self):
        reader = _phase_plane([(0.0, 2)])
        reader._messages["QTUN"] = {
            "_timestamp": np.array([100.0]),
            "Ast": np.array([0.0]),
        }
        ts = reader._messages["XKF4"]["_timestamp"]
        assert not plane_posvel_mask(reader, ts).any()
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_guided_qtun_is_not_enough_to_open_the_gate(self):
        reader = _phase_plane([(0.0, 2)], mode_name="GUIDED", mode_number=15)
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_plane_in_qloiter_runs_it(self):
        reader = _reader(sm=1.8, sp=0.9, vehicle_type="ArduPlane")
        reader.mode_list = [ModeChange(0.0, "QLOITER", 19)]
        assert _failsafe(reader, _quadplane()).status == "critical"


class TestEKFCard:
    def test_one_sensor_is_a_warning_not_a_stop(self):
        res = _ekf(_reader(sm=1.8, sp=0.4))
        assert res.status == "warning"
        assert res.action_level == "physical_check"

    def test_the_combination_is_critical(self):
        reader = _reader(seconds=11.0)
        reader._messages["XKF4"]["SM"][10:] = 1.8
        reader._messages["XKF4"]["SP"][10:] = 0.9
        res = _ekf(reader)
        assert res.status == "critical"
        assert res.action_level == "safety_stop"

    def test_height_variance_alone_cannot_go_critical(self):
        # Height is not one of the three indicators ArduPilot counts.
        res = _ekf(_reader(sh=5.0))
        assert res.status == "warning"

    def test_a_short_combination_is_not_sustained(self):
        reader = _reader()
        kf4 = reader._messages["XKF4"]
        kf4["SM"][20:25] = 1.8
        kf4["SP"][20:25] = 0.9
        res = _failsafe(reader)
        assert res.status == "good"

    def test_armed_firmware_event_pair_is_critical(self):
        # The firmware records its decision in both ERR streams a few
        # microseconds apart: EKFCHECK(16,2) + FAILSAFE_EKFINAV(17,1),
        # then the matching clears.
        reader = _reader()
        reader.errors = [
            ErrorEvent(2.0, 16, 2), ErrorEvent(2.0, 17, 1),
            ErrorEvent(5.0, 16, 0), ErrorEvent(5.0, 17, 0),
        ]
        res = _ekf(reader)
        assert res.status == "critical"
        assert res.action_level == "safety_stop"
        line = t("ekf.fw_event", start="2.0 s", end="5.0 s",
                 state=t("ekf.fw_state_armed"))
        assert any(line in d for d in res.details), res.details
        # Exactly one event line: the dual ERR streams must collapse.
        event_lines = [d for d in res.details
                       if "EKFCHECK/FAILSAFE_EKFINAV" in str(d)]
        assert len(event_lines) == 1, res.details
        assert t("ekf.fw_note") in "".join(res.details)

    def test_unarmed_firmware_events_stay_detail_lines(self):
        # Armed only after the events (SyntheticReader falls back to
        # always-armed on an empty list, so use a real later window).
        reader = _reader(armed_intervals=[(8.0, 10.0)])
        reader.errors = [
            ErrorEvent(2.0, 16, 2), ErrorEvent(2.0, 17, 1),
            ErrorEvent(5.0, 16, 0), ErrorEvent(5.0, 17, 0),
        ]
        res = _ekf(reader)
        assert res.status == "good"
        line = t("ekf.fw_event", start="2.0 s", end="5.0 s",
                 state=t("ekf.fw_state_unarmed"))
        assert any(line in d for d in res.details), res.details

    def test_uncleared_armed_trigger_lists_with_open_end(self):
        reader = _reader()
        reader.errors = [ErrorEvent(3.0, 17, 1)]
        res = _ekf(reader)
        assert res.status == "critical"
        line = t("ekf.fw_event", start="3.0 s", end="-",
                 state=t("ekf.fw_state_armed"))
        assert any(line in d for d in res.details), res.details

    def test_without_events_the_card_is_unchanged(self):
        res = _ekf(_reader())
        assert res.status == "good"
        assert not any("ekf.fw_event" in str(d) or "ERR" in str(d)
                       for d in res.details)

    def test_retrigger_within_a_second_of_the_clear_stays_two_events(self):
        # trigger 2.0, clear 3.0, re-trigger 3.5, clear 5.0: the second
        # trigger is a different transition, so nothing collapses.
        reader = _reader()
        reader.errors = [
            ErrorEvent(2.0, 16, 2), ErrorEvent(2.0, 17, 1),
            ErrorEvent(3.0, 16, 0), ErrorEvent(3.0, 17, 0),
            ErrorEvent(3.5, 16, 2), ErrorEvent(3.5, 17, 1),
            ErrorEvent(5.0, 16, 0), ErrorEvent(5.0, 17, 0),
        ]
        res = _ekf(reader)
        assert res.status == "critical"
        lines = [d for d in res.details
                 if "EKFCHECK/FAILSAFE_EKFINAV" in str(d)]
        assert len(lines) == 2, res.details
        assert any(t("ekf.fw_event", start="2.0 s", end="3.0 s",
                     state=t("ekf.fw_state_armed")) in d
                   for d in lines)
        assert any(t("ekf.fw_event", start="3.5 s", end="5.0 s",
                     state=t("ekf.fw_state_armed")) in d
                   for d in lines)

    def test_open_event_spanning_arming_stays_unarmed(self):
        # Armed state is read at the trigger: an event that starts
        # disarmed does not escalate, even if the vehicle arms while it
        # is still open (the real firmware cannot fire disarmed).
        reader = _reader(armed_intervals=[(4.0, 10.0)])
        reader.errors = [ErrorEvent(2.0, 16, 2), ErrorEvent(2.0, 17, 1)]
        res = _ekf(reader)
        assert res.status == "good"
        line = t("ekf.fw_event", start="2.0 s", end="-",
                 state=t("ekf.fw_state_unarmed"))
        assert any(line in d for d in res.details), res.details

    def test_clear_then_later_retrigger_lists_two_windows(self):
        reader = _reader()
        reader.errors = [
            ErrorEvent(2.0, 17, 1), ErrorEvent(4.0, 17, 0),
            ErrorEvent(6.0, 16, 2), ErrorEvent(8.0, 16, 0),
        ]
        res = _ekf(reader)
        assert res.status == "critical"
        lines = [d for d in res.details
                 if "EKFCHECK/FAILSAFE_EKFINAV" in str(d)]
        assert len(lines) == 2, res.details


def _replay_windows(reader, kind, thresh=0.8, gate=None):
    kf4 = reader._messages["XKF4"]
    ts = kf4["_timestamp"]
    return ekf_failsafe_replay(reader, kf4, ts, kind, thresh, gate)[0]


class TestDigitalLPFReplica:
    """The FS_EKF_FILT filter, against hand-computed DigitalLPF values."""

    def test_alpha_formula(self):
        from arduscope.lib.ekf_failsafe import _lowpass_alpha
        alpha = 0.1 / (0.1 + 1.0 / (2.0 * math.pi * 5.0))
        assert _lowpass_alpha(0.1, 5.0) == pytest.approx(alpha)

    def test_first_sample_passes_through(self):
        out = lowpass_filtered([0.1, 1.7, 1.7], [0.0, 0.1, 0.2], 5.0)
        assert out[0] == pytest.approx(0.1)   # initialised flag
        alpha = 0.1 / (0.1 + 1.0 / (2.0 * math.pi * 5.0))
        assert out[1] == pytest.approx(0.1 + (1.7 - 0.1) * alpha)
        assert out[2] == pytest.approx(
            out[1] + (1.7 - out[1]) * alpha)

    def test_zero_cutoff_is_passthrough(self):
        out = lowpass_filtered([0.1, 1.7, 0.3], [0.0, 0.1, 0.2], 0.0)
        assert out == pytest.approx([0.1, 1.7, 0.3])

    def test_nan_row_holds_the_output(self):
        out = lowpass_filtered(
            [1.7, np.nan, 1.7], [0.0, 0.1, 0.2], 5.0)
        assert out[1] == pytest.approx(out[0])
        assert np.isfinite(out).all()

    def test_nan_row_advances_the_clock_for_the_next_dt(self):
        # The row is a decode gap, not a flight state: the filter state
        # stands still but the clock does not, so the next finite sample
        # is applied with the dt of the whole gap.
        out = lowpass_filtered([0.1, np.nan, 1.7], [0.0, 0.1, 0.3], 5.0)
        assert out[1] == pytest.approx(0.1)
        alpha = 0.2 / (0.2 + 1.0 / (2.0 * math.pi * 5.0))
        assert out[2] == pytest.approx(0.1 + (1.7 - 0.1) * alpha)


class TestCopterReplay:
    """Copter: counter, filter and position gate together."""

    def test_flapping_variances_fire_via_the_leaky_counter(self):
        # Nine bad rows, one good row, repeating: never ten in a row, so
        # the old continuous-second model never fires, but the firmware
        # counter counts up faster than it counts down and reaches ten.
        reader = _reader(seconds=10.0)
        kf4 = reader._messages["XKF4"]
        sv = kf4["SV"]
        sv[:] = 1.7                      # velocity at twice the threshold
        sv[9::10] = 0.1                  # a good row every second
        from arduscope.lib.ekf_failsafe import over_threshold, sustained
        raw_over = over_threshold(sv, kf4["SP"], kf4["SM"], 0.8)
        assert not sustained(raw_over, kf4["_timestamp"]).any()
        assert _failsafe(reader).status == "critical"

    def test_recovery_takes_ten_good_checks(self):
        reader = _reader(seconds=6.0)
        reader.params["FS_EKF_FILT"] = 0.0       # exact, unfiltered
        kf4 = reader._messages["XKF4"]
        kf4["SV"][10:30] = 1.7
        windows = _replay_windows(reader, "copter")
        assert windows == [(1.9, 3.9)]

    def test_the_filter_delays_the_trigger_by_about_one_check(self):
        # The same breach with and without FS_EKF_FILT: the filtered
        # variance crosses 2*thresh one check after the raw one.
        def fire_ts(filt):
            reader = _reader(seconds=6.0)
            reader.params["FS_EKF_FILT"] = filt
            kf4 = reader._messages["XKF4"]
            kf4["SV"][20:40] = 1.7
            return _replay_windows(reader, "copter")[0][0]
        assert fire_ts(5.0) == pytest.approx(fire_ts(0.0) + 0.1)

    def test_quiet_variances_with_position_loss_fire(self):
        # The L_log_20 shape: the variances never breach, but the lane
        # loses HORIZ_POS_ABS for long enough while armed.
        reader = _reader(seconds=8.0)
        kf4 = reader._messages["XKF4"]
        kf4["SS"][30:50] = 0.0
        res = _failsafe(reader)
        assert res.status == "critical"
        assert t("failsafe.ekf_position_gate") in res.details

    def test_const_pos_mode_blocks_has_position(self):
        # Ten good rows first (the latch needs one), then CONST_POS_MODE
        # with the ABS bit still set: armed position is still refused.
        reader = _reader(seconds=4.0, ss=SS_ABS)
        reader._messages["XKF4"]["SS"][10:] = SS_ABS | SS_CONST_POS
        assert _failsafe(reader).status == "critical"
        assert _replay_windows(reader, "copter")[0][0] == pytest.approx(1.9)

    def test_disarmed_predicted_position_is_enough(self):
        # Origin is proven by early ABS rows; the unarmed middle relies
        # on PRED_HORIZ_POS_ABS for the position gate, so the latch from
        # the opening rows survives and the armed breach at t=7.0
        # counts. Without PRED acceptance those rows would fail the
        # gate and the breach would start counting late.
        reader = _reader(seconds=10.0, ss=SS_PRED_ABS,
                         armed_intervals=[(7.0, 10.0)])
        reader.params["FS_EKF_FILT"] = 0.0
        kf4 = reader._messages["XKF4"]
        kf4["SS"][:10] = SS_ABS
        kf4["SS"][70:] = SS_ABS          # position acquired at arming
        kf4["SV"][70:90] = 1.7
        assert _replay_windows(reader, "copter") == [(7.9, 9.9)]

    def test_pred_abs_alone_does_not_prove_the_origin(self):
        # PRED_HORIZ_POS_ABS only says GPS navigation is possible
        # (AP_NavEKF2_Control.cpp:513); it does not require validOrigin,
        # so a log that never shows HORIZ_POS_ABS or USING_GPS is held
        # closed no matter how long the variances are over.
        reader = _reader(seconds=6.0, ss=SS_PRED_ABS)
        reader.params["FS_EKF_FILT"] = 0.0
        reader._messages["XKF4"]["SV"][10:] = 1.7
        assert _replay_windows(reader, "copter") == []
        # Once ABS shows up the origin is proven and counting begins.
        healed = _reader(seconds=8.0, ss=SS_PRED_ABS)
        healed.params["FS_EKF_FILT"] = 0.0
        kf4 = healed._messages["XKF4"]
        kf4["SS"][30:] = SS_ABS
        kf4["SV"][40:60] = 1.7
        assert _replay_windows(healed, "copter") == [(4.9, 6.9)]

    def test_flow_type_enables_the_relative_branch(self):
        without = _reader(seconds=4.0)
        without._messages["XKF4"]["SS"][20:] = SS_REL
        assert _failsafe(without).status == "critical"
        with_flow = _reader(seconds=4.0)
        with_flow.params["FLOW_TYPE"] = 10
        with_flow._messages["XKF4"]["SS"][20:] = SS_REL
        assert _failsafe(with_flow).status == "good"

    def test_dead_reckoning_times_out(self):
        # Position goes relative on dead reckoning; FS_DR_TIMEOUT=2 s
        # ends it, and the check starts failing at that moment.
        reader = _reader(seconds=8.0)
        reader.params["FS_EKF_FILT"] = 0.0
        reader.params["FS_DR_TIMEOUT"] = 2.0
        kf4 = reader._messages["XKF4"]
        kf4["SS"][20:] = 262144 | SS_REL     # DEAD_RECKONING | HORIZ_POS_REL
        assert _replay_windows(reader, "copter")[0][0] == pytest.approx(5.0)
        patient = _reader(seconds=8.0)
        patient.params["FS_DR_TIMEOUT"] = 30.0
        kf4 = patient._messages["XKF4"]
        kf4["SS"][20:] = 262144 | SS_REL
        assert _replay_windows(patient, "copter") == []

    def test_before_origin_nothing_counts(self):
        # No global-position SS bit, no origin: the early breach must
        # not count, only the one after the origin appears.
        reader = _reader(seconds=8.0)
        reader.params["FS_EKF_FILT"] = 0.0
        kf4 = reader._messages["XKF4"]
        kf4["SS"][:20] = 0.0
        kf4["SV"][5:15] = 1.7
        kf4["SV"][40:60] = 1.7
        assert _replay_windows(reader, "copter") == [(4.9, 6.9)]

    def test_log_opening_in_a_bad_interval_never_fires(self):
        # The firmware sets has_ever_passed only from an observed pass;
        # being armed does not imply one. A log that opens with the
        # checks already failing (USING_GPS, no position estimate, the
        # variance over the limit from row zero) stays held closed --
        # the ERR events decide what really happened, not the model.
        reader = _reader(seconds=3.0, ss=8192)
        reader._messages["XKF4"]["SV"][:] = 1.7
        assert _replay_windows(reader, "copter") == []

    def test_good_rows_first_let_the_counter_fire(self):
        # The same position loss, but with ten passing rows at the
        # opening: the latch sets, then the counter runs and fires.
        reader = _reader(seconds=4.0, ss=8192 | SS_ABS)
        reader._messages["XKF4"]["SS"][10:] = 8192    # position lost
        assert _replay_windows(reader, "copter")[0][0] == pytest.approx(1.9)

    def test_missing_ss_keeps_variance_only_semantics(self):
        reader = _reader(seconds=11.0, ss=np.nan)
        reader._messages["XKF4"]["SM"][10:] = 1.8
        reader._messages["XKF4"]["SP"][10:] = 0.9
        assert _failsafe(reader).status == "critical"


class TestPlaneReplay:
    """Plane scores raw variances and resets the counter at the gate."""

    def _qloiter(self, seconds=5.0, **kw):
        reader = _reader(vehicle_type="ArduPlane", seconds=seconds, **kw)
        reader.mode_list = [ModeChange(0.0, "QLOITER", 19)]
        return reader

    def test_plane_does_not_filter_the_variances(self):
        # The same ramp breach fires one check later on Copter, where
        # FS_EKF_FILT has to catch up, than on Plane, which scores raw.
        plane = self._qloiter(seconds=6.0)
        plane._messages["XKF4"]["SV"][20:40] = 1.7
        ts = plane._messages["XKF4"]["_timestamp"]
        gate = plane_posvel_mask(plane, ts)
        assert _replay_windows(plane, "quadplane", gate=gate) \
            == [(2.9, 4.9)]

        copter = _reader(seconds=6.0)
        copter._messages["XKF4"]["SV"][20:40] = 1.7
        assert _replay_windows(copter, "copter") == [(3.0, 4.9)]

    def test_gate_close_resets_the_counter(self):
        # Nine bad checks, one gate-closed row, nine bad checks: without
        # the reset the counter would still reach ten.
        reader = self._qloiter()
        reader.mode_list = [
            ModeChange(0.0, "QLOITER", 19),
            ModeChange(0.9, "FBWA", 5),
            ModeChange(1.0, "QLOITER", 19),
        ]
        reader._messages["XKF4"]["SV"][:19] = 1.7
        reader._messages["XKF4"]["SV"][9] = 0.1
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_position_bits_do_not_gate_plane(self):
        # USING_GPS but no position bit: origin is ready, Copter's
        # position gate would fail, Plane does not have one.
        reader = self._qloiter(seconds=2.0, ss=8192)
        reader._messages["XKF4"]["SV"][:] = 1.7
        assert _failsafe(reader, _quadplane()).status == "critical"


class TestVehicleKindAndLegacy:
    def _rover(self) -> VehicleInfo:
        return VehicleInfo(
            vehicle_type="ArduRover",
            firmware_version="4.6.3",
            board_name="synthetic",
            frame_class=1,
            motor_count=4,
            imu_count=2,
            is_quadplane=False,
        )

    def test_vehicle_kind_dispatch(self):
        assert vehicle_kind(_reader()) == "copter"
        quad = _reader(vehicle_type="ArduPlane")
        quad.params["Q_ENABLE"] = 1
        assert vehicle_kind(quad) == "quadplane"
        assert vehicle_kind(_reader(vehicle_type="ArduPlane")) == "plane"
        assert vehicle_kind(_reader(vehicle_type="ArduRover")) == "other"
        assert vehicle_kind(_reader(vehicle_type="Mystery")) == "other"

    def test_legacy_vehicle_ignores_the_leaky_counter(self):
        # Nine bad rows, one good, repeating: the replay's leaky counter
        # fires on this pattern, the legacy continuous-second model does
        # not. A Rover VehicleInfo forces the legacy branch, proving the
        # dispatch (the default ArduCopter vehicle would take the replay
        # and read critical).
        reader = _reader(vehicle_type="ArduRover", seconds=10.0)
        sv = reader._messages["XKF4"]["SV"]
        sv[:] = 1.7
        sv[9::10] = 0.1
        res = _failsafe(reader, self._rover())
        assert res.status == "good"
        assert not any(t("failsafe.ekf_triggered") in d
                       for d in res.details)

    def test_legacy_vehicle_still_fires_on_a_full_second(self):
        reader = _reader(seconds=11.0, vehicle_type="ArduRover")
        reader._messages["XKF4"]["SM"][10:] = 1.8
        reader._messages["XKF4"]["SP"][10:] = 0.9
        res = _failsafe(reader, self._rover())
        assert res.status == "critical"
        assert any(t("failsafe.ekf_triggered_sustained") in d
                   for d in res.details), res.details


class TestEKFCardReplay:
    def test_window_spanning_into_arming_is_critical(self):
        # Copter counts while disarmed too: a breach that starts on the
        # bench and is still open at arming is a breach of armed flight.
        reader = _reader(seconds=10.0, armed_intervals=[(4.0, 10.0)])
        reader.params["FS_EKF_FILT"] = 0.0
        reader._messages["XKF4"]["SV"][25:45] = 1.7
        res = _ekf(reader)
        assert res.status == "critical"
        assert any(t("ekf.ap_triggered") in d for d in res.details)

    def test_position_only_trigger_has_its_own_wording(self):
        # Quiet variances, position lost while armed: the card must not
        # claim the variance combination was met.
        reader = _reader(seconds=8.0)
        reader._messages["XKF4"]["SS"][30:50] = 0.0
        res = _ekf(reader)
        assert res.status == "critical"
        assert res.summary == t("ekf.crit_pos_summary")
        assert res.plain_summary == t("ekf.crit_pos_plain")
        assert res.evidence_source == t("ekf.evidence_pos")
        assert t("ekf.crit_summary", val="0.100",
                 thresh="0.80") != res.summary

    def test_quadplane_fixed_wing_trigger_says_so(self):
        # The EKF card deliberately does not apply the Plane VTOL mode
        # gate: an armed fixed-wing breach is critical here while the
        # failsafe card stays good, and the trigger line must be the
        # quadplane wording, not a firmware-trigger claim.
        reader = _phase_plane([(0.0, 3)])
        ekf = _ekf(reader, _quadplane())
        assert ekf.status == "critical"
        assert any(t("ekf.ap_triggered_quadplane") in d
                   for d in ekf.details), ekf.details
        assert not any(t("ekf.ap_triggered") in d
                       for d in ekf.details), ekf.details
        assert _failsafe(reader, _quadplane()).status == "good"

    def test_transient_combination_wording(self):
        # The combination is reached for six checks -- raw over, counter
        # short of ten: the transient line, not the single-sensor one.
        reader = _reader(seconds=6.0)
        reader._messages["XKF4"]["SM"][20:26] = 1.8
        reader._messages["XKF4"]["SP"][20:26] = 0.9
        res = _ekf(reader)
        assert res.status == "warning"
        assert any(t("ekf.ap_transient") in d for d in res.details)
        assert not any(t("ekf.ap_single_sensor", thresh="0.80") in d
                       for d in res.details)


class TestTransientWording:
    def test_failsafe_card_transient_combination(self):
        reader = _reader(seconds=6.0)
        reader._messages["XKF4"]["SM"][20:26] = 1.8
        reader._messages["XKF4"]["SP"][20:26] = 0.9
        res = _failsafe(reader)
        assert res.status == "good"
        assert any(t("failsafe.ekf_transient") in d for d in res.details)
        assert not any(t("failsafe.ekf_not_triggered") in d
                       for d in res.details)

    def test_single_sensor_only_keeps_the_old_line(self):
        res = _failsafe(_reader(sm=1.8, sp=0.4, seconds=4.0))
        assert res.status == "good"
        assert t("failsafe.ekf_not_triggered") in res.details
        assert not any(t("failsafe.ekf_transient") in d
                       for d in res.details)
