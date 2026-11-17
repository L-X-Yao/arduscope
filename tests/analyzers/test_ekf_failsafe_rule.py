"""ArduPilot's EKF failsafe rule, on synthetic XKF4 data.

A single variance over FS_EKF_THRESH is not the failsafe: the test needs
position with another indicator, or two indicators, held for about a
second. The reference log of 2025-07-29 (QuadPlane, SM 1.81, SV 0.26,
SP 0.45) had no ERR event for exactly this reason; the old rule called a
compass variance of 1.81 a failsafe.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.ekf import EKFAnalyzer
from analyzers.failsafe import FailsafeAnalyzer
from lib.ekf_failsafe import (
    in_modes, over_threshold, plane_posvel_mask, sustained)
from lib.i18n import t
from lib.log_reader import ErrorEvent, ModeChange
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import (
    SyntheticReader, _thresholds, _vehicle)

pytestmark = pytest.mark.fast


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
            vehicle_type="ArduCopter",
            armed_intervals=None) -> SyntheticReader:
    n = int(seconds * rate)
    ts = np.arange(n, dtype=float) / rate
    kf4 = {
        "_timestamp": ts,
        "C": np.zeros(n),
        "PI": np.zeros(n),
        "SS": np.zeros(n),
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
        res = _failsafe(_reader(sm=1.8, sp=0.9))
        assert res.status == "critical"
        assert any(t("failsafe.ekf_triggered", seconds="1") in d
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
        assert any(t("failsafe.ekf_triggered", seconds="1") in d
                   for d in res.details), res.details

    def test_auto_fixed_wing_stage_with_the_same_breach_does_not_run(self):
        reader = _phase_plane([(0.0, 3)])
        res = _failsafe(reader, _quadplane())
        assert res.status == "good"
        assert t("failsafe.ekf_mode_gate") in res.details

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
        res = _ekf(_reader(sm=1.8, sp=0.9))
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
