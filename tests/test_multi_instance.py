"""
Multi-instance handling: dual GPS, dual airspeed, multi-IMU, multi-EKF core.

Verifies that analyzers correctly filter by instance/core instead of
mixing data from all instances together.
"""

from conftest import (LOG_QUADPLANE_FLIGHT, LOG_OCTA_AUTOTUNE, run_analyzer)
from analyzers.temperature import TemperatureAnalyzer
from analyzers.airspeed import AirspeedAnalyzer
from analyzers.wind import WindAnalyzer
from analyzers.power import PowerAnalyzer


class TestTemperatureMultiIMU:
    """QuadPlane with 3 IMUs — temperature should report all 3."""

    def test_reports_three_imus(self):
        result = run_analyzer(TemperatureAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert "3" in result.summary and "IMU" in result.summary, \
            f"Should report 3 IMUs, got: {result.summary}"

    def test_has_per_imu_stats(self):
        result = run_analyzer(TemperatureAnalyzer, LOG_QUADPLANE_FLIGHT)
        # Should have detail lines for IMU0, IMU1, IMU2
        imu_labels = [d for d in result.details if "IMU" in d and "°C" in d]
        assert len(imu_labels) >= 3, \
            f"Expected 3+ IMU detail lines, got {len(imu_labels)}"


class TestAirspeedInstanceFiltering:
    """Airspeed should not mix data from multiple instances."""

    def test_no_crash_on_single_instance(self):
        result = run_analyzer(AirspeedAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")

    def test_no_crash_on_the_octaquad_log(self):
        result = run_analyzer(AirspeedAnalyzer, LOG_OCTA_AUTOTUNE)
        assert result.status in ("good", "warning", "critical", "info")


class TestWindCoreFiltering:
    """Wind analyzer should filter to EKF core 0."""

    def test_no_crash(self):
        result = run_analyzer(WindAnalyzer, LOG_QUADPLANE_FLIGHT)
        assert result.status in ("good", "warning", "critical", "info")


class TestPowerGPSInstance:
    """Power efficiency should not double-count dual GPS distance."""

    def test_no_crash(self):
        result = run_analyzer(PowerAnalyzer, LOG_OCTA_AUTOTUNE)
        assert result.status in ("good", "warning", "critical", "info")

    @staticmethod
    def _power_reader(gps_instances):
        """Synthetic 600 s cruise at 5 m/s, 24 V / 25 A."""
        import numpy as np
        from tests.test_synthetic_coverage import SyntheticReader
        n = 600
        ts = np.linspace(0.0, 600.0, n)
        gps_ts = np.repeat(ts, len(gps_instances))
        gps_i = np.tile(
            np.array(gps_instances, dtype=float), n)
        return SyntheticReader(messages={
            "BAT": {
                "_timestamp": ts,
                "Inst": np.zeros(n),
                "Volt": np.full(n, 24.0),
                "Curr": np.full(n, 25.0),
                "CurrTot": np.linspace(0.0, 4000.0, n),
            },
            "GPS": {
                "_timestamp": gps_ts,
                "I": gps_i,
                "Lat": np.full(len(gps_ts), 30.0),
                "Lng": np.full(len(gps_ts), 120.0),
                "Spd": np.full(len(gps_ts), 5.0),
            },
        }, armed_intervals=[(0.0, 600.0)])

    def _wh_km(self, result):
        import re
        lines = [d for d in result.details if "Wh/km" in d]
        assert lines, "no Wh/km line produced"       # precondition
        m = re.search(r"([\d.]+)\s*Wh/km", lines[0])
        assert m is not None, f"no value in {lines[0]!r}"
        return float(m.group(1))

    def test_dual_gps_does_not_double_distance(self):
        """Interleaved dual-GPS rows must not double the integrated
        distance (and thus halve Wh/km). The old test looped over a
        hover log that never produces a Wh/km line — zero assertions
        ever ran."""
        from analyzers.power import PowerAnalyzer
        from lib.thresholds import Thresholds
        from tests.test_synthetic_coverage import _vehicle
        single = PowerAnalyzer(self._power_reader([0]),
                               Thresholds(), _vehicle()).analyze()
        dual = PowerAnalyzer(self._power_reader([0, 1]),
                             Thresholds(), _vehicle()).analyze()
        wh_single = self._wh_km(single)
        wh_dual = self._wh_km(dual)
        # 3 km at ~600 Wh -> ~200 Wh/km; doubling the distance would
        # halve this. Require the two runs to agree within 5%.
        assert abs(wh_dual - wh_single) / wh_single < 0.05, (
            f"dual-GPS efficiency {wh_dual} deviates from "
            f"single-GPS {wh_single} (distance double-count?)")
