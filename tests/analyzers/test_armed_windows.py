"""Armed windows when the log's ARM/DISARM events are missing or partial.

The logger can open after the ARMED event was sent, which leaves a DISARMED
with no partner, or no events at all. POWR.Safety bit 2 still holds the
armed state (AP_Logger::Write_Power), so a log can be told apart from one
that never armed.
"""

from __future__ import annotations

import pytest

import binlog
from analyzers.board_power import BoardPowerAnalyzer
from analyzers.ekf_vel_divergence import EKFVelDivergenceAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_STEP_US = 100_000
_ARMED_BIT = 4


def _log(tmp_path, events=(), armed=lambda t: False, seconds=40.0,
         powr=True) -> LogReader:
    """A log holding only what armed state is derived from.

    events is [(seconds, 10 or 11)]; armed(seconds) drives POWR.Safety.
    """
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    pending = sorted(events)
    for i in range(int(seconds * 1e6 / _STEP_US)):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        while pending and pending[0][0] <= secs:
            log.write("EV", time_us, pending.pop(0)[1])
        safety = 1 | (_ARMED_BIT if armed(secs) else 0)
        if powr:
            log.write("POWR", time_us, 5.0, 5.0, 3, 3, safety)
        else:
            log.write("PM", time_us, 400, 0, 400, 2500, 100000, 200, 0, 0,
                      0, 0, 0, 0, 0, 0)
    return LogReader(log.save(tmp_path / "log.bin"))


def _spans(intervals):
    return [(round(a, 1), round(b, 1)) for a, b in intervals]


class TestDisarmWithoutArm:
    def test_window_reaches_back_to_log_start(self, tmp_path):
        reader = _log(tmp_path, events=[(30.0, 11)], powr=False)
        assert [e.event_id for e in reader.events] == [11]
        assert _spans(reader.get_armed_intervals()) == [(0.0, 30.0)]

    def test_armed_bit_finds_the_real_start(self, tmp_path):
        reader = _log(tmp_path, events=[(30.0, 11)],
                      armed=lambda t: 25.0 <= t < 30.0)
        assert _spans(reader.get_armed_intervals()) == [(25.0, 30.0)]

    def test_later_arm_cycle_is_kept(self, tmp_path):
        reader = _log(tmp_path, events=[(5.0, 11), (10.0, 10), (20.0, 11)],
                      powr=False)
        assert _spans(reader.get_armed_intervals()) == [(0.0, 5.0),
                                                        (10.0, 20.0)]

    def test_repeated_disarm_opens_no_window(self, tmp_path):
        reader = _log(tmp_path, events=[(10.0, 10), (20.0, 11), (25.0, 11)],
                      powr=False)
        assert _spans(reader.get_armed_intervals()) == [(10.0, 20.0)]


class TestArmedBitFallback:
    def test_log_that_opened_armed_uses_the_bit(self, tmp_path):
        reader = _log(tmp_path, armed=lambda t: t < 30.0)
        assert reader.events == []
        assert _spans(reader.get_armed_intervals()) == [(0.0, 29.9)]

    def test_motor_test_is_not_an_armed_window(self, tmp_path):
        # The logger was already running, so a real arming would have
        # written ARMED. A motor test sets the same bit and writes nothing.
        reader = _log(tmp_path,
                      armed=lambda t: 5.0 <= t < 8.0 or 20.0 <= t < 30.0)
        assert reader.events == []
        assert reader.get_armed_intervals() == []

    def test_events_lead_when_present(self, tmp_path):
        # A motor test sets the armed bit with no ARMED event, and is not
        # the armed window of the flight that follows.
        reader = _log(tmp_path, events=[(10.0, 10), (30.0, 11)],
                      armed=lambda t: 2.0 <= t < 4.0 or 10.0 <= t < 30.0)
        assert _spans(reader.get_armed_intervals()) == [(10.0, 30.0)]


class TestNeverArmed:
    def test_bit_clear_for_the_whole_log(self, tmp_path):
        reader = _log(tmp_path)
        assert reader.get_armed_intervals() == []
        assert reader.never_armed()

    def test_no_powr_means_unknown_not_never(self, tmp_path):
        reader = _log(tmp_path, powr=False)
        assert not reader.has_message("POWR")
        assert not reader.never_armed()

    def test_opened_armed(self, tmp_path):
        assert not _log(tmp_path, armed=lambda t: t < 5.0).never_armed()

    def test_motor_test_only(self, tmp_path):
        assert _log(tmp_path, armed=lambda t: 5.0 <= t < 8.0).never_armed()

    def test_disarm_event_proves_it_armed(self, tmp_path):
        assert not _log(tmp_path, events=[(30.0, 11)]).never_armed()


def _gps_vs_ekf_log(tmp_path, armed_bit: bool) -> LogReader:
    """EKF at rest and GPS reporting 10 m/s: multipath on a parked frame."""
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    for i in range(400):
        time_us = 1_000_000 + i * _STEP_US
        log.write("GPS", time_us, 0, 3, 100000 + i * 100, 2300, 14, 0.8,
                  34.1, 108.9, 400.0, 10.0, 0.0, 0.0, 0.0, 1)
        log.write("GPA", time_us, 0, 1.0, 0.8, 1.2, 0.2, 0.0, 1, 200, 200,
                  0.0, 0, 0)
        log.write("XKF1", time_us, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                  0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        log.write("POWR", time_us, 5.0, 5.0, 3, 3,
                  1 | (_ARMED_BIT if armed_bit else 0))
    return LogReader(log.save(tmp_path / "gps.bin"))


def _analyzer(cls, reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return cls(reader, thresholds, detect_vehicle(reader))


class TestEkfDivergenceArmedGate:
    def test_never_armed_log_is_not_graded(self, tmp_path):
        reader = _gps_vs_ekf_log(tmp_path, armed_bit=False)
        assert reader.never_armed()
        result = _analyzer(EKFVelDivergenceAnalyzer, reader).analyze()
        assert result.status == "info"
        assert result.summary == _t("ekf_vel_div.never_armed")

    def test_armed_without_events_is_graded(self, tmp_path):
        reader = _gps_vs_ekf_log(tmp_path, armed_bit=True)
        assert reader.events == []
        result = _analyzer(EKFVelDivergenceAnalyzer, reader).analyze()
        assert result.status == "critical"


class TestBoardPowerArmedGate:
    def test_armed_bit_counts_as_a_flight(self, tmp_path):
        reader = _log(tmp_path, armed=lambda t: t < 35.0)
        assert reader.events == []
        result = _analyzer(BoardPowerAnalyzer, reader).analyze()
        assert result.status == "good"
        assert result.summary != _t("board_power.summary_unarmed")

    def test_never_armed_stays_a_ground_test(self, tmp_path):
        reader = _log(tmp_path)
        assert reader.never_armed()
        result = _analyzer(BoardPowerAnalyzer, reader).analyze()
        assert result.summary == _t("board_power.summary_unarmed")
