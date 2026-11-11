"""The lane that matters is the one XKF4.PI names, at that moment.

AP moves the primary lane between cores in flight, so the lowest core
number says nothing about which estimate is flying the aircraft.
"""

from __future__ import annotations

import pytest

import binlog
from analyzers.ekf import EKFAnalyzer
from analyzers.ekf_vel_divergence import EKFVelDivergenceAnalyzer
from analyzers.failsafe import FailsafeAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_STEP_US = 200_000
_SPIKE = 3.0                # far past FS_EKF_THRESH on any configuration
_CLEAN = 0.1


def _variance_log(tmp_path, primary, sv) -> LogReader:
    """Two cores for 60 s, armed throughout.

    primary(secs) gives XKF4.PI, sv(core, secs) gives that core's SV.
    """
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    log.write("EV", 1_000_000, 10)
    for i in range(300):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        for core in (0, 1):
            log.write("XKF4", time_us, core, sv(core, secs), _CLEAN, _CLEAN,
                      _CLEAN, 0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, primary(secs))
    return LogReader(log.save(tmp_path / "ekf.bin"))


def _analyzer(cls, reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return cls(reader, thresholds, detect_vehicle(reader))


def _switched_at_20(secs):
    return 0 if secs < 20.0 else 1


class TestVarianceFollowsPrimaryLane:
    def test_spike_on_a_lane_already_switched_away_from(self, tmp_path):
        reader = _variance_log(
            tmp_path, _switched_at_20,
            lambda core, secs: _SPIKE if core == 0 and secs >= 30 else _CLEAN)
        ekf = _analyzer(EKFAnalyzer, reader).analyze()
        assert ekf.status == "good"
        assert ekf.action_level != "safety_stop"
        assert _analyzer(FailsafeAnalyzer, reader).analyze().status != "critical"

    def test_spike_on_the_lane_that_took_over(self, tmp_path):
        reader = _variance_log(
            tmp_path, _switched_at_20,
            lambda core, secs: _SPIKE if core == 1 and secs >= 30 else _CLEAN)
        ekf = _analyzer(EKFAnalyzer, reader).analyze()
        assert ekf.status == "critical"
        assert ekf.action_level == "safety_stop"
        assert _analyzer(FailsafeAnalyzer, reader).analyze().status == "critical"

    def test_spike_before_the_switch_still_counts(self, tmp_path):
        reader = _variance_log(
            tmp_path, _switched_at_20,
            lambda core, secs: _SPIKE if core == 0 and secs < 20 else _CLEAN)
        assert _analyzer(EKFAnalyzer, reader).analyze().status == "critical"


def _speed_log(tmp_path, primary, speed) -> LogReader:
    """GPS steady at 20 m/s for 60 s, armed; speed(core, secs) is XKF1.VN."""
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    log.write("EV", 1_000_000, 10)
    for i in range(300):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        log.write("GPS", time_us, 0, 3, 100000 + i * 200, 2300, 14, 0.8,
                  34.1, 108.9, 400.0, 20.0, 0.0, 0.0, 0.0, 1)
        log.write("GPA", time_us, 0, 1.0, 0.8, 1.2, 0.2, 0.0, 1, 200, 200,
                  0.0, 0, 0)
        for core in (0, 1):
            log.write("XKF1", time_us, core, 0.0, 0.0, 0.0,
                      speed(core, secs), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                      0.0, 0.0, 0.0, 0.0)
            log.write("XKF4", time_us, core, _CLEAN, _CLEAN, _CLEAN, _CLEAN,
                      0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, primary(secs))
    return LogReader(log.save(tmp_path / "speed.bin"))


class TestDivergenceFollowsPrimaryLane:
    def test_standby_lane_is_reported_but_not_graded(self, tmp_path):
        reader = _speed_log(
            tmp_path, lambda secs: 0,
            lambda core, secs: 45.0 if core == 1 and 20 <= secs < 30 else 20.0)
        result = _analyzer(EKFVelDivergenceAnalyzer, reader).analyze()
        assert result.status == "good"
        assert any("core 1" in line for line in result.details), result.details

    def test_primary_lane_is_graded(self, tmp_path):
        reader = _speed_log(
            tmp_path, lambda secs: 1,
            lambda core, secs: 45.0 if core == 1 and 20 <= secs < 30 else 20.0)
        result = _analyzer(EKFVelDivergenceAnalyzer, reader).analyze()
        assert result.status == "critical"
        assert result.evidence_source == _t("ekf_vel_div.evidence")

    def test_lane_counts_only_while_it_is_primary(self, tmp_path):
        # Core 1 runs away from 20 s on, and only flies the aircraft from
        # 25 s on: 5 s of the run count, not all of it.
        reader = _speed_log(
            tmp_path, lambda secs: 0 if secs < 25.0 else 1,
            lambda core, secs: 45.0 if core == 1 and 20 <= secs < 40 else 20.0)
        result = _analyzer(EKFVelDivergenceAnalyzer, reader).analyze()
        assert result.status == "critical"
        assert "14.8" in result.summary, result.summary
