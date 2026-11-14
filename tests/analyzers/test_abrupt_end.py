"""Abrupt log end: was the vehicle armed at cutoff, and was it in the air?

Run on real-format logs because the answer depends on which messages a
vehicle writes: Plane's CTUN has no Alt, and CTUN.Alt on Copter counts from
the EKF origin, not from the ground.
"""

from __future__ import annotations

import numpy as np
import pytest

import binlog
from analyzers.log_health import LogHealthAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_STEP_US = 100_000
_PLANE_CTUN = ("QccccffffBfi",
               "TimeUS,NavRoll,Roll,NavPitch,Pitch,ThO,RdO,ThD,As,AsT,E2T,GU")
_ARMED, _DISARMED = 5, 1            # POWR.Safety: bit 2 plus safety state


def _log(tmp_path, *, plane=False, events=(), safety=None, ctun_alt=None,
         home_alt=None, seconds=40.0) -> LogReader:
    """A log that stops dead at `seconds`, holding only what is passed."""
    log = binlog.BinLog()
    banner = "ArduPlane V4.6.3 (abc12345)" if plane else \
        "ArduCopter V4.6.3 (abc12345)"
    log.write("MSG", 1_000_000, banner)
    if plane:
        log.declare("CTUN", *_PLANE_CTUN)
        log.write("PARM", 1_000_000, "Q_ENABLE", 1.0, 1.0)
    pending = sorted(events)
    for i in range(int(seconds * 1e6 / _STEP_US)):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        while pending and pending[0][0] <= secs:
            log.write("EV", time_us, pending.pop(0)[1])
        log.write("PM", time_us, 400, 0, 400, 2500, 100000, 200, 0, 0, 0, 0,
                  0, 0, 0, 0)
        if safety is not None:
            log.write("POWR", time_us, 5.0, 5.0, 3, 3, safety)
        if plane:
            log.write("CTUN", time_us, 0, 0, 0, 0, 40.0, 0.0, 40.0, 20.0, 1,
                      1.0, 0)
        elif ctun_alt is not None:
            log.write("CTUN", time_us, 0.3, 0.0, 0.3, 0.3, ctun_alt, ctun_alt,
                      ctun_alt, 0.0, 0.0, 0.0, 0.0, 0.0)
        if home_alt is not None:
            log.write("POS", time_us, 34.1, 108.9, 400.0 + home_alt, home_alt,
                      home_alt)
    return LogReader(log.save(tmp_path / "cut.bin"))


def _run(reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return LogHealthAnalyzer(reader, thresholds,
                             detect_vehicle(reader)).analyze()


class TestNeverArmed:
    def test_bench_log_is_not_a_power_cut(self, tmp_path):
        reader = _log(tmp_path, safety=_DISARMED, ctun_alt=3.8)
        assert reader.never_armed()
        result = _run(reader)
        assert result.status == "good", result.details
        assert result.summary != _t("log_health.abrupt_summary")

    def test_log_that_cannot_say_keeps_the_altitude_fallback(self, tmp_path):
        reader = _log(tmp_path, ctun_alt=30.0)
        assert not reader.never_armed() and not reader.get_armed_intervals()
        result = _run(reader)
        assert result.status == "critical"
        assert result.summary == _t("log_health.abrupt_summary")


class TestAirborneAtCutoff:
    def test_plane_in_the_air_is_critical(self, tmp_path):
        reader = _log(tmp_path, plane=True, events=[(1.0, 10)],
                      safety=_ARMED, home_alt=92.0)
        ctun = reader.get_messages_as_arrays("CTUN", ["Alt"])
        assert len(ctun["_timestamp"]) and np.all(np.isnan(ctun["Alt"]))
        result = _run(reader)
        assert result.status == "critical", result.details
        assert _t("log_health.abrupt_airborne", alt="92.0") in result.details

    def test_plane_with_no_arm_information_is_caught(self, tmp_path):
        reader = _log(tmp_path, plane=True, home_alt=102.0)
        assert not reader.get_armed_intervals()
        result = _run(reader)
        assert result.status == "critical"
        assert result.summary == _t("log_health.abrupt_summary")

    def test_copter_on_the_ground_with_origin_offset(self, tmp_path):
        # CTUN.Alt reads 5 m from the EKF origin, home says it is parked.
        reader = _log(tmp_path, events=[(1.0, 10)], safety=_ARMED,
                      ctun_alt=5.0, home_alt=0.4)
        result = _run(reader)
        assert result.status == "warning", result.details
        assert result.summary == _t("log_health.abrupt_summary")

    def test_copter_without_pos_uses_ctun(self, tmp_path):
        reader = _log(tmp_path, events=[(1.0, 10)], safety=_ARMED,
                      ctun_alt=30.0)
        assert not reader.has_message("POS")
        assert _run(reader).status == "critical"


class TestCleanDisarmIsTheLastEvent:
    def test_arm_after_an_unpaired_disarm_is_still_open(self, tmp_path):
        # One of each, so a count says "closed", yet the last thing the
        # log knows is ARMED.
        reader = _log(tmp_path, events=[(5.0, 11), (10.0, 10)],
                      safety=_ARMED, home_alt=40.0)
        assert [e.event_id for e in reader.events] == [11, 10]
        result = _run(reader)
        assert result.status == "critical"
        assert result.summary == _t("log_health.abrupt_summary")

    def test_repeated_arm_closed_by_one_disarm_is_clean(self, tmp_path):
        # Two ARMED and one DISARMED, last thing in the log is DISARMED,
        # right at its end: disarm then unplug.
        reader = _log(tmp_path, events=[(5.0, 10), (6.0, 10), (39.8, 11)],
                      safety=_ARMED, home_alt=0.2)
        result = _run(reader)
        assert result.status == "good", result.details
