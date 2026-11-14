"""PM.Load: its scale, and which rows of it say anything about a flight."""

from __future__ import annotations

import pytest

import binlog
from analyzers.log_health import LogHealthAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_STEP_US = 500_000
_ARMED_BIT = 4


def _log(tmp_path, load, events=((5.0, 10), (45.0, 11)), powr=True):
    """50 s of PM rows; load(secs) is PM.Load as logged."""
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    pending = sorted(events)
    armed = False
    for i in range(100):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        while pending and pending[0][0] <= secs:
            event = pending.pop(0)[1]
            armed = event == 10
            log.write("EV", time_us, event)
        log.write("PM", time_us, 400, 0, 400, 2500, 100000, load(secs), 0, 0,
                  0, 0, 0, 0, 0, 0)
        if powr:
            log.write("POWR", time_us, 5.0, 5.0, 3, 3,
                      1 | (_ARMED_BIT if armed else 0))
    return LogReader(log.save(tmp_path / "pm.bin"))


def _run(reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return LogHealthAnalyzer(reader, thresholds,
                             detect_vehicle(reader)).analyze()


class TestScale:
    def test_quiet_log_is_not_read_as_percent(self, tmp_path):
        # load_average() * 1000: 90 is 9%, not the 90% that used to pass
        # the 80% warning line because no row went over 100.
        result = _run(_log(tmp_path, lambda secs: 90))
        assert result.status == "good", result.details
        assert _t("log_health.cpu_load", avg="9.0", max="9.0") in result.details

    def test_busy_log_keeps_its_scale(self, tmp_path):
        result = _run(_log(tmp_path, lambda secs: 900))
        assert result.status == "warning"
        assert _t("log_health.cpu_load", avg="90.0",
                  max="90.0") in result.details


def _spike_at(when):
    return lambda secs: 1000 if secs == when else 240


class TestWhichRowsCount:
    def test_one_disarmed_row_is_not_a_cpu_problem(self, tmp_path):
        # The row covers the last 32 loops only. A stall while parked
        # says nothing about the flight, and LOG_BITMASK will not fix it.
        reader = _log(tmp_path, _spike_at(2.0))
        assert max(reader.get_messages_as_arrays("PM", ["Load"])["Load"]) == 1000
        result = _run(reader)
        assert result.status == "good", result.details
        assert _t("log_health.cpu_load", avg="24.0",
                  max="24.0") in result.details

    def test_armed_row_still_counts(self, tmp_path):
        result = _run(_log(tmp_path, _spike_at(20.0)))
        assert result.status == "critical"

    def test_never_armed_log_gets_no_verdict(self, tmp_path):
        reader = _log(tmp_path, _spike_at(20.0), events=())
        assert reader.never_armed()
        result = _run(reader)
        assert result.status == "good", result.details
        assert _t("log_health.cpu_load", avg="24.8",
                  max="100.0") in result.details

    def test_log_that_cannot_say_grades_every_row(self, tmp_path):
        reader = _log(tmp_path, _spike_at(20.0), events=(), powr=False)
        assert not reader.never_armed()
        assert _run(reader).status == "critical"


class TestLongLoopLimitText:
    def test_states_the_limit_ap_counts_with(self, tmp_path):
        # NLon counts loops over 1.2 periods (PerfInfo::set_loop_rate): at
        # 400 Hz that is 3.0 ms, whatever limit the analyzer grades with.
        log = binlog.BinLog()
        log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
        log.write("PARM", 1_000_000, "SCHED_LOOP_RATE", 400.0, 400.0)
        for i in range(20):
            log.write("PM", 1_000_000 + i * 500_000, 400, 0, 400, 2500,
                      100000, 240, 0, 0, 0, 0, 0, 0, 0, 0)
        result = _run(LogReader(log.save(tmp_path / "loop.bin")))
        assert _t("log_health.long_loop_frac", pct="0.00",
                  threshold="3.0") in result.details, result.details
