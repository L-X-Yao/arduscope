"""Judgments live in Thresholds, so --thresholds can move them and every
one has a source. Each test proves the field is what decides the outcome."""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.airspeed import AirspeedAnalyzer
from analyzers.gps import GPSAnalyzer
from analyzers.motor import MotorAnalyzer
from analyzers.rangefinder import RangefinderAnalyzer
from analyzers.summary import SummaryAnalyzer
from analyzers.wind import WindAnalyzer
from lib.log_reader import ErrorEvent
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

N = 500
TS = np.linspace(0.0, 100.0, N)


def _vehicle() -> VehicleInfo:
    return VehicleInfo(vehicle_type="ArduCopter", firmware_version="4.6.3",
                       board_name="s", frame_class=1, motor_count=4,
                       imu_count=1, is_quadplane=False)


def _wind(speed: float) -> SyntheticReader:
    core = np.zeros(N)
    return SyntheticReader({"XKF2": {
        "_timestamp": TS, "C": core, "VWN": np.full(N, speed),
        "VWE": np.zeros(N)}})


class TestWind:
    def _status(self, speed, **overrides):
        return WindAnalyzer(_wind(speed), Thresholds(**overrides),
                            _vehicle()).analyze().status

    def test_defaults(self):
        assert self._status(12.0) == "warning"
        assert self._status(20.0) == "critical"

    def test_warn_level_is_the_field(self):
        assert self._status(12.0, wind_speed_warn=15.0) == "good"

    def test_crit_level_is_the_field(self):
        assert self._status(20.0, wind_speed_crit=25.0) == "warning"


class TestGps:
    @staticmethod
    def _run(no_fix_fraction, **overrides):
        status = np.full(N, 3.0)
        status[:int(N * no_fix_fraction)] = 1.0
        reader = SyntheticReader({"GPS": {
            "_timestamp": TS, "Status": status, "NSats": np.full(N, 14.0),
            "HDop": np.full(N, 0.8), "Lat": np.full(N, 34.1),
            "Lng": np.full(N, 108.9), "Alt": np.full(N, 400.0),
            "Spd": np.zeros(N), "I": np.zeros(N)}})
        return GPSAnalyzer(reader, Thresholds(**overrides),
                           _vehicle()).analyze()

    def test_no_fix_fraction_is_the_field(self):
        assert self._run(0.05).status == "critical"
        assert self._run(0.05, gps_no_fix_frac_crit=0.10).status != "critical"


class TestErrorCount:
    @staticmethod
    def _status(count, **overrides):
        reader = SyntheticReader({"ATT": {"_timestamp": TS}})
        reader.errors = [ErrorEvent(1.0, 2, 1) for _ in range(count)]
        return SummaryAnalyzer(reader, Thresholds(**overrides),
                               _vehicle()).analyze().status

    def test_count_is_the_field(self):
        assert self._status(6) == "warning"
        assert self._status(6, summary_error_count_warn=10) != "warning"


class TestSpinGap:
    @staticmethod
    def _run(**overrides):
        rcou = {"_timestamp": TS,
                **{f"C{i}": np.full(N, 1500.0) for i in range(1, 5)}}
        reader = SyntheticReader({"RCOU": rcou},
                                 {"MOT_SPIN_ARM": 0.10, "MOT_SPIN_MIN": 0.12})
        return MotorAnalyzer(reader, Thresholds(**overrides),
                             _vehicle()).analyze()

    def test_gap_is_the_field(self):
        assert [s["suggested"] for s in self._run().param_suggestions] == [0.13]
        assert not self._run(mot_spin_gap_min=0.01).param_suggestions


class TestRangefinder:
    @staticmethod
    def _status(**overrides):
        stat = np.where(np.arange(N) < N * 0.4, 4.0, 1.0)  # 40% good
        reader = SyntheticReader({"RFND": {
            "_timestamp": TS, "Instance": np.zeros(N),
            "Dist": np.full(N, 5.0), "Stat": stat,
            "Orient": np.full(N, 25.0)}})
        return RangefinderAnalyzer(reader, Thresholds(**overrides),
                                   _vehicle()).analyze().status

    def test_quality_is_the_field(self):
        assert self._status() == "warning"
        assert self._status(rangefinder_quality_pct_warn=30.0) == "good"


class TestAirspeedHealth:
    @staticmethod
    def _status(unhealthy_fraction, **overrides):
        health = np.ones(N)
        health[:int(N * unhealthy_fraction)] = 0.0
        speed = 20.0 + np.sin(TS)
        reader = SyntheticReader({"ARSP": {
            "_timestamp": TS, "Airspeed": speed, "H": health,
            "Health": health, "I": np.zeros(N), "DiffPress": speed}})
        return AirspeedAnalyzer(reader, Thresholds(**overrides),
                                _vehicle()).analyze().status

    def test_levels_are_the_fields(self):
        assert self._status(0.2) == "warning"
        assert self._status(0.6) == "critical"
        assert self._status(0.2, airspeed_unhealthy_pct_warn=30.0) == "good"
        assert self._status(0.6, airspeed_unhealthy_pct_crit=70.0) == "warning"
