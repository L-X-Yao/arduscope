"""
SyntheticReader-based tests for the 5 analyzers added in Iteration 4:
gyro_saturation, gps_glitch, clock_health, compass_consistency, servo_dither.
"""

from __future__ import annotations

import numpy as np

from analyzers.gyro_saturation import GyroSaturationAnalyzer
from analyzers.gps_glitch import GPSGlitchAnalyzer
from analyzers.clock_health import ClockHealthAnalyzer
from analyzers.compass_consistency import CompassConsistencyAnalyzer
from analyzers.servo_dither import ServoDitherAnalyzer
from lib.i18n import t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader


def _vehicle(motors: int = 4, vt: str = "ArduCopter") -> VehicleInfo:
    return VehicleInfo(
        vehicle_type=vt,
        firmware_version="4.6.3",
        board_name="synthetic",
        frame_class=1,
        motor_count=motors,
        imu_count=2,
        is_quadplane=False,
    )


def _thresholds(reader: SyntheticReader) -> Thresholds:
    return Thresholds.from_params(reader.params, reader.vehicle_type)


# =====================================================================
# GyroSaturationAnalyzer
# =====================================================================

class TestGyroSaturation:

    def _build_reader(self, n=500, max_gyr_rads=8.0,
                      saturated_frac=0.0, params=None):
        # ArduPilot logs IMU.GyrX/Y/Z in rad/s. Sensor saturation is
        # ~2000 deg/s = ~34.9 rad/s; a saturated sample sits just above
        # that. Normal aggressive flight peaks at a few rad/s.
        ts = np.linspace(0, 120, n)
        gyr_x = np.full(n, max_gyr_rads * 0.3)
        gyr_y = np.full(n, max_gyr_rads * 0.2)
        gyr_z = np.full(n, max_gyr_rads * 0.1)
        if saturated_frac > 0:
            n_sat = max(1, int(n * saturated_frac))
            gyr_x[:n_sat] = 35.0  # ~2005 deg/s, above the 1950 threshold
        return SyntheticReader(
            messages={
                "IMU": {
                    "_timestamp": ts,
                    "GyrX": gyr_x,
                    "GyrY": gyr_y,
                    "GyrZ": gyr_z,
                },
            },
            params=params or {"INS_GYRO_FILTER": 40},
        )

    def test_no_saturation_good(self):
        reader = self._build_reader()
        result = GyroSaturationAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"
        assert result.param_suggestions == []

    def test_warning_saturation(self):
        reader = self._build_reader(saturated_frac=0.005)
        result = GyroSaturationAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "warning"
        assert result.action_level == "physical_check"
        assert result.param_suggestions == []

    def test_critical_saturation(self):
        reader = self._build_reader(saturated_frac=0.02)
        result = GyroSaturationAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical"
        assert result.action_level == "physical_check"

    def test_gyro_units_are_rad_per_sec(self):
        """Regression: IMU gyro is rad/s, threshold is deg/s.

        A gyro physically pegged at the ~2000 deg/s sensor limit logs as
        ~34.9 rad/s. If the analyzer compared raw rad/s against the 1950
        (deg/s) threshold — the original bug — nothing would ever exceed
        it and saturation would be a guaranteed false negative. Feed
        values that ARE saturated in deg/s but tiny in rad/s and require
        detection.
        """
        n = 500
        ts = np.linspace(0, 120, n)
        # 34.9 rad/s == 2000 deg/s: physically saturated on 2% of samples.
        gyr_x = np.full(n, 2.0)  # 2 rad/s ≈ 115 deg/s, well below limit
        gyr_x[:10] = 34.9
        reader = SyntheticReader(
            messages={"IMU": {
                "_timestamp": ts, "GyrX": gyr_x,
                "GyrY": np.full(n, 1.0), "GyrZ": np.full(n, 0.5),
            }},
            params={"INS_GYRO_FILTER": 40},
        )
        result = GyroSaturationAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical", (
            "gyro at 34.9 rad/s (=2000 deg/s sensor limit) must register "
            "as saturation — raw rad/s must be converted to deg/s"
        )

    def test_empty_imu(self):
        reader = SyntheticReader(
            messages={"IMU": {"_timestamp": np.array([])}})
        result = GyroSaturationAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_produces_figure(self):
        reader = self._build_reader()
        result = GyroSaturationAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert len(result.figures) >= 1


# =====================================================================
# GPSGlitchAnalyzer
# =====================================================================

class TestGPSGlitch:

    def _build_reader(self, n=300, glitch_frac=0.0, jump_m=0.0, params=None):
        ts = np.linspace(0, 120, n)
        lat = np.full(n, 39.9) + np.linspace(0, 0.001, n)
        lng = np.full(n, 116.4) + np.linspace(0, 0.001, n)
        spd = np.full(n, 5.0)
        inst = np.zeros(n)
        if glitch_frac > 0:
            n_glitch = max(1, int(n * glitch_frac))
            indices = np.linspace(10, n - 2, n_glitch, dtype=int)
            for idx in indices:
                lat[idx] += 0.001  # ~111m jump
        lat[n // 2] += jump_m / 111000.0
        return SyntheticReader(
            messages={
                "GPS": {
                    "_timestamp": ts,
                    "I": inst,
                    "Lat": lat,
                    "Lng": lng,
                    "Spd": spd,
                    "Status": np.full(n, 3.0),
                },
            },
            params=params,
        )

    def _jump(self, jump_m, params=None):
        # 3000 samples keep the out-and-back pair far under the 1% share.
        reader = self._build_reader(n=3000, jump_m=jump_m, params=params)
        return GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()

    def test_one_jump_past_the_radius_is_a_warning(self):
        result = self._jump(100.0)
        assert result.status == "warning"
        assert result.summary == t("gps_glitch.summary_jump",
                                   dist="100", radius="25")
        assert "EK3_GLITCH_RAD" in result.evidence_source

    def test_a_jump_inside_the_radius_stays_good(self):
        assert self._jump(20.0).status == "good"

    def test_the_radius_is_the_aircraft_s_own(self):
        assert self._jump(100.0, {"EK3_GLITCH_RAD": 150}).status == "good"
        assert self._jump(20.0, {"EK3_GLITCH_RAD": 10}).status == "warning"

    def test_radius_zero_falls_back_to_the_default(self):
        # 0 means clip rather than reject, not a radius of no metres.
        assert self._jump(20.0, {"EK3_GLITCH_RAD": 0}).status == "good"
        assert self._jump(30.0, {"EK3_GLITCH_RAD": 0}).status == "warning"

    @staticmethod
    def _gap(metres, speed_before, speed_after):
        """16 s without a fix in the middle of an armed log."""
        n = 3000
        ts = np.linspace(0, 120, n)
        ts[n // 2:] += 16.0
        lat = 39.9 + np.arange(n) * 1e-7
        lat[n // 2:] += metres / 111000.0
        spd = np.where(np.arange(n) < n // 2, speed_before, speed_after)
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": ts, "I": np.zeros(n), "Lat": lat,
                "Lng": np.full(n, 116.4), "Spd": spd,
                "Status": np.full(n, 3.0),
            }},
            armed_intervals=[(0.0, 200.0)],
        )
        return GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()

    def test_a_gap_the_speed_explains_is_not_a_jump(self):
        assert self._gap(600.0, 20.0, 20.0).status == "good"

    def test_the_speed_after_the_gap_can_explain_it_too(self):
        # L_log_41: 101 m over 16 s, 0.9 m/s before the fix went and 5.6 after.
        assert self._gap(101.0, 0.9, 5.6).status == "good"

    def test_a_gap_the_speed_cannot_explain_is_a_jump(self):
        assert self._gap(600.0, 0.9, 5.6).status == "warning"

    def test_clean_gps_good(self):
        reader = self._build_reader()
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"

    def test_glitch_warning(self):
        # Each injected glitch creates both a jump and return, doubling effective rate
        reader = self._build_reader(glitch_frac=0.012)
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "warning"
        assert result.action_level == "physical_check"

    def test_glitch_critical(self):
        reader = self._build_reader(glitch_frac=0.10)
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical"
        assert result.action_level == "physical_check"

    def test_empty_gps(self):
        reader = SyntheticReader(
            messages={"GPS": {"_timestamp": np.array([])}})
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_single_sample(self):
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": np.array([0.0]),
                "I": np.array([0.0]),
                "Lat": np.array([39.9]),
                "Lng": np.array([116.4]),
                "Spd": np.array([5.0]),
                "Status": np.array([3.0]),
            }})
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_produces_figure(self):
        reader = self._build_reader()
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert len(result.figures) >= 1

    def test_backward_timestamps_not_counted_as_glitches(self):
        """Intervals with dt<=0 (backward/duplicate timestamps) must be
        excluded from glitch analysis. Previously these forced expected_dist
        negative → clamped to min_dist=5m → any movement flagged as glitch."""
        n = 100
        ts = np.linspace(0, 50, n)
        # Inject 20 backward timestamps — a severe clock anomaly
        for i in range(10, 90, 4):
            ts[i] = ts[i - 1] - 0.01   # backward
        lat = np.full(n, 39.9) + np.linspace(0, 0.0001, n)
        lng = np.full(n, 116.4)
        spd = np.full(n, 5.0)
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": ts,
                "I": np.zeros(n),
                "Lat": lat,
                "Lng": lng,
                "Spd": spd,
                "Status": np.full(n, 3.0),
            }})
        result = GPSGlitchAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        # Backward timestamps must NOT cause a GPS glitch warning/critical.
        # (The trajectory is smooth — only timestamps are broken.)
        assert result.status == "good", (
            f"Expected 'good' for smooth GPS with backward timestamps, "
            f"got '{result.status}'"
        )


# =====================================================================
# ClockHealthAnalyzer
# =====================================================================

class TestClockHealth:

    def _build_reader(self, n=500, jitter=0.0, backward=0, gaps=0):
        dt_base = 0.01
        ts = np.cumsum(np.full(n, dt_base))
        if jitter > 0:
            ts += np.random.normal(0, dt_base * jitter, n)
            ts = np.sort(ts)
        if backward > 0:
            indices = np.linspace(50, n - 50, backward, dtype=int)
            for idx in indices:
                ts[idx] = ts[idx] - 0.5
        if gaps > 0:
            gap_indices = np.linspace(100, n - 100, gaps, dtype=int)
            for idx in gap_indices:
                ts[idx:] += 1.0
        roll = np.random.normal(0, 2, n)
        return SyntheticReader(
            messages={
                "ATT": {
                    "_timestamp": ts,
                    "Roll": roll,
                },
            },
        )

    def test_clean_clock_good(self):
        reader = self._build_reader()
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"

    def test_backward_timestamps_critical(self):
        reader = self._build_reader(backward=3)
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical"
        assert result.action_level == "physical_check"

    def test_high_jitter_warning(self):
        # Mix of 0.005s and 0.1s intervals gives std/median >> 3.0
        n = 500
        dt = np.ones(n) * 0.005
        dt[::5] = 0.1
        ts = np.cumsum(dt)
        reader = SyntheticReader(
            messages={"ATT": {
                "_timestamp": ts,
                "Roll": np.random.normal(0, 2, n),
            }})
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status in ("warning", "critical")

    def test_many_gaps_warning(self):
        reader = self._build_reader(gaps=10)
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status in ("warning", "critical")

    def test_insufficient_data(self):
        reader = SyntheticReader(
            messages={"ATT": {
                "_timestamp": np.array([0.0, 1.0]),
                "Roll": np.array([0.0, 1.0]),
            }})
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_produces_figure(self):
        reader = self._build_reader()
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert len(result.figures) >= 1

    def test_all_backward_timestamps_is_critical_not_info(self):
        """All-backward timestamps: positive_dt is empty but must still
        report critical, not the spurious 'info' (insufficient data)."""
        ts = np.array([5.0, 4.0, 3.0, 2.0, 1.0])   # strictly decreasing
        reader = SyntheticReader(
            messages={"ATT": {
                "_timestamp": ts,
                "Roll": np.zeros(5),
            }})
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical", (
            f"Expected 'critical' for all-backward timestamps, got '{result.status}'"
        )
        assert result.action_level == "physical_check"

    def test_mostly_backward_timestamps_is_critical(self):
        """3 samples, 2 backward dts: positive_dt has 0 elements → must
        still be critical due to non_mono > 0."""
        ts = np.array([3.0, 1.0, 0.5])   # two backward jumps
        reader = SyntheticReader(
            messages={"ATT": {
                "_timestamp": ts,
                "Roll": np.zeros(3),
            }})
        result = ClockHealthAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical"


# =====================================================================
# CompassConsistencyAnalyzer
# =====================================================================

class TestCompassConsistency:

    def _build_reader(self, n=400, n_instances=2, rms_offset=0.0,
                      params=None):
        ts_per = np.linspace(0, 120, n)
        all_ts = np.tile(ts_per, n_instances)
        all_inst = np.concatenate(
            [np.full(n, float(i)) for i in range(n_instances)])
        base_field = 400.0
        all_x = np.concatenate([
            np.full(n, base_field * 0.6 + i * rms_offset)
            for i in range(n_instances)
        ])
        all_y = np.concatenate([
            np.full(n, base_field * 0.7) for _ in range(n_instances)
        ])
        all_z = np.concatenate([
            np.full(n, base_field * 0.3) for _ in range(n_instances)
        ])
        return SyntheticReader(
            messages={
                "MAG": {
                    "_timestamp": all_ts,
                    "I": all_inst,
                    "MagX": all_x,
                    "MagY": all_y,
                    "MagZ": all_z,
                },
            },
            params=params or {"COMPASS_USE2": 1},
        )

    def test_consistent_compasses_good(self):
        reader = self._build_reader(rms_offset=10.0)
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"

    def test_moderate_disagreement_warning(self):
        # RMS offset on MagX maps nonlinearly to field magnitude diff;
        # 180 mGauss X offset ≈ 115 mGauss magnitude diff (> 100 warn)
        reader = self._build_reader(rms_offset=180.0)
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "warning"
        assert result.action_level == "physical_check"

    def test_severe_disagreement_critical(self):
        # 300 mGauss X offset ≈ 210 mGauss magnitude diff (> 200 crit)
        reader = self._build_reader(rms_offset=300.0)
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical"
        # Two compasses cannot say which one is wrong
        assert result.param_suggestions == []

    def test_single_compass_info(self):
        reader = self._build_reader(n_instances=1)
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_empty_mag(self):
        reader = SyntheticReader(
            messages={"MAG": {"_timestamp": np.array([])}})
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_three_compasses(self):
        reader = self._build_reader(n_instances=3, rms_offset=10.0)
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good"
        assert len(result.details) >= 3

    def test_produces_figure(self):
        reader = self._build_reader()
        result = CompassConsistencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert len(result.figures) >= 1


# =====================================================================
# ServoDitherAnalyzer
# =====================================================================

class TestServoDither:

    def _build_reader(self, n=6000, dither_channels=None, params=None):
        # 6000 samples over 60s = 100 Hz sample rate
        ts = np.linspace(0, 60, n)
        channels = {}
        for i in range(1, 9):
            ch = f"C{i}"
            if dither_channels and ch in dither_channels:
                # 30 Hz sine → ~60 zero-crossings/s > 50 Hz crit threshold
                channels[ch] = 1500.0 + 50 * np.sin(
                    2 * np.pi * 30 * ts)
            elif i <= 4:
                # Smooth low-frequency signal: won't trigger ZC threshold
                channels[ch] = 1500.0 + 20 * np.sin(
                    2 * np.pi * 0.5 * ts)
            else:
                channels[ch] = np.full(n, np.nan)
        return SyntheticReader(
            messages={
                "RCOU": {
                    "_timestamp": ts,
                    **channels,
                },
            },
            params=params or {"MOT_SPIN_MIN": 0.15},
        )

    def test_stable_servos_good(self):
        reader = self._build_reader()
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"

    def test_dither_on_motor_channel_warns(self):
        reader = self._build_reader(dither_channels={"C1"})
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status in ("warning", "critical")
        # Nothing authoritative gives a value for this, so it stays a
        # pointer at the parameter.
        assert not result.param_suggestions
        assert result.action_level == "physical_check"
        assert t("servo_dither.spin_min_note",
                 param="MOT_SPIN_MIN") in result.details

    def test_dither_on_non_motor_channel(self):
        n = 6000
        ts = np.linspace(0, 60, n)
        channels = {}
        for i in range(1, 9):
            ch = f"C{i}"
            if i == 5:
                channels[ch] = 1500.0 + 50 * np.sin(
                    2 * np.pi * 30 * ts)
            elif i <= 4:
                # Smooth motor channels — no dither
                channels[ch] = 1500.0 + 20 * np.sin(
                    2 * np.pi * 0.5 * ts)
            else:
                channels[ch] = np.full(n, np.nan)
        reader = SyntheticReader(
            messages={"RCOU": {"_timestamp": ts, **channels}},
            params={"MOT_SPIN_MIN": 0.15},
        )
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status in ("warning", "critical")
        motor_suggestions = [s for s in result.param_suggestions
                             if s["param"] == "MOT_SPIN_MIN"]
        assert len(motor_suggestions) == 0

    def test_empty_rcou(self):
        reader = SyntheticReader(
            messages={"RCOU": {"_timestamp": np.array([])}})
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_all_constant_channels(self):
        n = 200
        ts = np.linspace(0, 60, n)
        channels = {f"C{i}": np.full(n, 1500.0) for i in range(1, 9)}
        reader = SyntheticReader(
            messages={"RCOU": {"_timestamp": ts, **channels}})
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_produces_figure(self):
        reader = self._build_reader()
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert len(result.figures) >= 1

    def test_prearm_dither_not_counted_as_armed_dither(self):
        """Dither before arming must NOT inflate the armed ZC rate.

        Before the fix: crossings counted over all data / armed_duration,
        so pre-arm motor spin-up (high crossings) falsely triggered warnings
        even when the flight itself was smooth.
        """
        n = 6000
        # 60-second log: 0-10s pre-arm (fast dither), 10-60s armed (smooth)
        ts = np.linspace(0, 60, n)
        c1 = np.zeros(n)
        for i, t in enumerate(ts):
            if t < 10.0:
                # 30 Hz dither pre-arm — many crossings
                c1[i] = 1500.0 + 50 * np.sin(2 * np.pi * 30 * t)
            else:
                # Smooth 0.5 Hz during flight
                c1[i] = 1500.0 + 20 * np.sin(2 * np.pi * 0.5 * t)
        reader = SyntheticReader(
            messages={"RCOU": {
                "_timestamp": ts,
                "C1": c1,
                "C2": 1500.0 + 20 * np.sin(2 * np.pi * 0.5 * ts),
                "C3": 1500.0 + 20 * np.sin(2 * np.pi * 0.5 * ts),
                "C4": 1500.0 + 20 * np.sin(2 * np.pi * 0.5 * ts),
                **{f"C{i}": np.full(n, np.nan) for i in range(5, 9)},
            }},
            armed_intervals=[(10.0, 60.0)],   # armed only during smooth phase
            params={"MOT_SPIN_MIN": 0.15},
        )
        result = ServoDitherAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "good", (
            f"Pre-arm dither should not trigger warning; got '{result.status}'. "
            "ZC crossings must be filtered to armed intervals only."
        )
