"""GPS.VZ of 0 from a driver with no vertical velocity is not a measurement.

fill_3d_velocity() zeroes it and clears have_vertical_velocity, which GPA.VV
carries into the log. NMEA receivers do this, and 14 fleet logs fly on them.
"""

from __future__ import annotations

import pytest

import binlog
from analyzers.ekf_vel_divergence import EKFVelDivergenceAnalyzer
from analyzers.flight_profile import FlightProfileAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_STEP_US = 100_000
_CLIMB = 8.0            # m/s up, far past every tolerance in both analyzers
_SPEED = 12.0


def _climb_log(tmp_path, gps_vz=0.0, vv=lambda secs: 0, ekf_north=_SPEED,
               gpa=True) -> LogReader:
    """60 s armed: a steady 12 m/s with an 8 m/s climb from t=20 to t=40."""
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    log.write("EV", 1_000_000, 10)
    for i in range(600):
        secs = i * _STEP_US / 1e6
        time_us = 1_000_000 + i * _STEP_US
        climbing = 20.0 <= secs < 40.0
        log.write("GPS", time_us, 0, 3, 100000 + i * 100, 2300, 14, 0.8,
                  34.1 + i * 1e-5, 108.9, 400.0, _SPEED, 0.0,
                  gps_vz if climbing else 0.0, 0.0, 1)
        if gpa:
            log.write("GPA", time_us, 0, 1.0, 0.8, 1.2, 0.2, 0.0, vv(secs),
                      200, 200, 0.0, 0, 0)
        log.write("XKF1", time_us, 0, 0.0, 0.0, 0.0, ekf_north, 0.0,
                  -_CLIMB if climbing else 0.0, 0.0, 0.0, 0.0, 0.0,
                  0.0, 0.0, 0.0, 0.0)
    return LogReader(log.save(tmp_path / "climb.bin"))


def _run(cls, reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return cls(reader, thresholds, detect_vehicle(reader)).analyze()


class TestFlightProfile:
    def test_climb_rate_falls_back_to_the_ekf(self, tmp_path):
        reader = _climb_log(tmp_path)
        gps = reader.get_messages_as_arrays("GPS", ["VZ"])
        assert not gps["VZ"].any()
        res = _run(FlightProfileAnalyzer, reader)
        assert _t("flight_profile.climb_rate",
                  up="8.0", down="0.0") in res.details, res.details

    def test_three_d_speed_comes_from_the_ekf(self, tmp_path):
        res = _run(FlightProfileAnalyzer, _climb_log(tmp_path))
        assert _t("flight_profile.speed_3d_single", mx="14.4", h="12.0",
                  dir=_t("flight_profile.dir_climb"), v="8.0",
                  src="EKF") in res.details, res.details

    def test_measured_vz_keeps_the_dual_source_check(self, tmp_path):
        reader = _climb_log(tmp_path, gps_vz=-_CLIMB, vv=lambda secs: 1)
        res = _run(FlightProfileAnalyzer, reader)
        assert any(line.startswith(_t("flight_profile.speed_3d", mx="14.4",
                                      h="12.0", v="8.0", delta="0.0",
                                      dir=_t("flight_profile.dir_climb")))
                   for line in res.details), res.details

    def test_log_without_gpa_keeps_trusting_vz(self, tmp_path):
        reader = _climb_log(tmp_path, gps_vz=-_CLIMB, gpa=False)
        assert not reader.has_message("GPA")
        res = _run(FlightProfileAnalyzer, reader)
        assert _t("flight_profile.climb_rate",
                  up="8.0", down="0.0") in res.details, res.details


class TestDivergence:
    def test_climb_without_vz_is_not_divergence(self, tmp_path):
        res = _run(EKFVelDivergenceAnalyzer, _climb_log(tmp_path))
        assert res.status == "good", res.summary
        assert _t("ekf_vel_div.horizontal_only") in res.details

    def test_horizontal_runaway_is_still_caught(self, tmp_path):
        reader = _climb_log(tmp_path, ekf_north=_SPEED * 2.5)
        res = _run(EKFVelDivergenceAnalyzer, reader)
        assert res.status == "critical"
        assert _t("ekf_vel_div.horizontal_only") in res.details

    def test_rows_without_vz_are_dropped_when_others_have_it(self, tmp_path):
        # VZ only starts at t=45: the climb before it has no measurement
        # to compare, and the level flight after it agrees.
        reader = _climb_log(tmp_path, vv=lambda secs: int(secs >= 45.0))
        res = _run(EKFVelDivergenceAnalyzer, reader)
        assert res.status == "good", res.summary
        assert _t("ekf_vel_div.horizontal_only") not in res.details
