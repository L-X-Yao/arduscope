"""What "at the floor" and "airborne" mean for CTUN.ThO.

ThO is motors->get_throttle(), a thrust from 0 to 1. MOT_SPIN_MIN is an
actuator level: thrust 0 maps to it (thrust_to_actuator), so the mixer
floor is ThO 0 whatever MOT_SPIN_MIN says. CTUN.Alt is measured from the
EKF origin, so it does not tell a parked frame from one in the air.
"""

from __future__ import annotations

import math

import pytest

import binlog
from analyzers.throttle_collapse import ThrottleCollapseAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_STEP_US = 40_000
_ALT_HOLD = 2
_FLOOR_PWM = 1150          # 1000 + (2000 - 1000) * MOT_SPIN_MIN 0.15


def _hover(secs):
    return 0.35


def _climb_to_30(secs):
    return min(max(secs - 3.0, 0.0) * 3.0, 30.0)


def _log(tmp_path, tho=_hover, alt=_climb_to_30, events=None, seconds=60.0,
         params=None, pwm=1400) -> LogReader:
    """ALT_HOLD for `seconds`: armed at 1 s, off the ground at 3 s, landed
    and disarmed at the end. events replaces that sequence."""
    if events is None:
        events = [(1.0, 10), (3.0, 28), (seconds - 2.0, 18),
                  (seconds - 1.0, 11)]
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    for name, value in {**binlog.COPTER_PARAMS, **(params or {})}.items():
        log.write("PARM", 1_000_000, name, float(value), float(value))
    log.write("MODE", 1_000_000, _ALT_HOLD, _ALT_HOLD, 1)
    pending = sorted(events)
    for i in range(int(seconds * 1e6 / _STEP_US)):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        while pending and pending[0][0] <= secs:
            log.write("EV", time_us, pending.pop(0)[1])
        value = tho(secs)
        log.write("CTUN", time_us, value, 0.0, value, 0.35, alt(secs),
                  alt(secs), alt(secs), 0.0, 0.0, 0.0, 0.0, 0.0)
        log.write("ATT", time_us, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 3)
        log.write("RCOU", time_us, *([pwm(secs) if callable(pwm) else pwm] * 4),
                  *([1500] * 10))
    return LogReader(log.save(tmp_path / "collapse.bin"))


def _run(reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return ThrottleCollapseAnalyzer(reader, thresholds,
                                    detect_vehicle(reader)).analyze()


def _cut_at_30(secs):
    return 0.0 if 30.0 <= secs < 34.0 else 0.35


def _falling(secs):
    """Falling 9 m/s once the thrust is cut at 30 s."""
    return _climb_to_30(secs) - 9.0 * min(max(secs - 30.0, 0.0), 2.5)


class TestFloorIsThrustZero:
    def test_hover_below_spin_min_margin_is_not_a_collapse(self, tmp_path):
        # An overpowered frame hovers at ThO 0.05 with MOT_SPIN_MIN 0.15:
        # 0.18 was "the floor", and a normal flight sat under it.
        reader = _log(tmp_path, tho=lambda secs: 0.05)
        assert reader.params["MOT_SPIN_MIN"] == pytest.approx(0.15)
        result = _run(reader)
        assert result.status == "good", result.details
        assert result.summary == _t("throttle_collapse.none", floor="0.03")

    def test_real_floor_pin_in_flight(self, tmp_path):
        result = _run(_log(tmp_path, tho=_cut_at_30, alt=_falling))
        assert result.status == "warning"
        assert _t("throttle_collapse.found", count=1,
                  floor="0.03") in result.details

    def test_floor_does_not_move_with_spin_min(self, tmp_path):
        reader = _log(tmp_path, tho=lambda secs: 0.07,
                      params={"MOT_SPIN_MIN": 0.05})
        assert _run(reader).status == "good"


class TestAirborneGate:
    def test_parked_frame_with_origin_offset(self, tmp_path):
        # Armed on the ground, idle, CTUN.Alt 5 m from the EKF origin.
        reader = _log(tmp_path, tho=lambda secs: 0.0, alt=lambda secs: 5.0,
                      events=[(1.0, 10), (50.0, 11)])
        assert _run(reader).status == "good"

    def test_height_counts_from_the_takeoff_point(self, tmp_path):
        # Takeoff from 20 m on the EKF scale; 1 m above it is not airborne.
        def alt(secs):
            return 21.0 if 25.0 <= secs < 35.0 else 20.0
        reader = _log(tmp_path, alt=alt,
                      tho=lambda secs: 0.0 if 28 <= secs < 32 else 0.35)
        assert _run(reader).status == "good"

    def test_sitting_on_the_ground_before_landed_is_declared(self, tmp_path):
        # Landed 4 m above the takeoff point: Alt stops, thrust is at the
        # floor, and the land detector takes its time to say so.
        def alt(secs):
            return min(_climb_to_30(secs), 4.0)

        def tho(secs):
            return 0.0 if secs >= 50.0 else 0.35
        reader = _log(tmp_path, tho=tho, alt=alt,
                      events=[(1.0, 10), (3.0, 28), (56.0, 18), (58.0, 11)])
        assert _run(reader).status == "good"

    def test_no_takeoff_means_no_collapse(self, tmp_path):
        reader = _log(tmp_path, tho=_cut_at_30, alt=_falling,
                      events=[(1.0, 10), (58.0, 11)])
        assert _run(reader).status == "good"


class TestMotorFloorNote:
    def test_outputs_at_spin_min_read_as_pinned(self, tmp_path):
        def pwm(secs):
            return _FLOOR_PWM if 30.0 <= secs < 34.0 else 1400
        result = _run(_log(tmp_path, tho=_cut_at_30, alt=_falling, pwm=pwm))
        assert any(line == _t("throttle_collapse.rcou_floor", pct="100")
                   for line in result.details), result.details

    def test_outputs_above_spin_min_do_not(self, tmp_path):
        result = _run(_log(tmp_path, tho=_cut_at_30, alt=_falling, pwm=1400))
        assert result.status == "warning"
        assert not any(line == _t("throttle_collapse.rcou_floor", pct="100")
                       for line in result.details), result.details


def _attitude_log(tmp_path, roll) -> LogReader:
    """A 4 s floor pin at 30 m; roll(secs) drives ATT."""
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    for name, value in binlog.COPTER_PARAMS.items():
        log.write("PARM", 1_000_000, name, float(value), float(value))
    log.write("MODE", 1_000_000, _ALT_HOLD, _ALT_HOLD, 1)
    log.write("EV", 1_000_000, 10)
    log.write("EV", 1_000_000, 28)
    for i in range(1500):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        value = _cut_at_30(secs)
        log.write("CTUN", time_us, value, 0.0, value, 0.35, _falling(secs),
                  _falling(secs), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        log.write("ATT", time_us, 0.0, roll(secs), 0.0, 1.0, 0.0, 0.0, 3)
    return LogReader(log.save(tmp_path / "attitude.bin"))


class TestAttitudeAfterCollapse:
    def test_steady_bank_with_noise_is_not_a_limit_cycle(self, tmp_path):
        reader = _attitude_log(
            tmp_path, lambda secs: 22.0 + 0.5 * math.sin(2 * math.pi * 3 * secs))
        result = _run(reader)
        assert result.status == "warning", result.details

    def test_swing_around_level_is_a_limit_cycle(self, tmp_path):
        reader = _attitude_log(
            tmp_path, lambda secs: 25.0 * math.sin(2 * math.pi * 1.5 * secs))
        assert _run(reader).status == "critical"

    def test_swing_around_a_steady_bank_is_a_limit_cycle(self, tmp_path):
        reader = _attitude_log(
            tmp_path,
            lambda secs: 10.0 + 25.0 * math.sin(2 * math.pi * 1.5 * secs))
        assert _run(reader).status == "critical"
