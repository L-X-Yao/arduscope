"""
Flight checks judge the armed window, not the whole log.

Logs carry ground time: with LOG_DISARMED set the file holds the bench,
the walk to the field and the pack change. Grading that as flight raises
false alarms from handling, and hours of quiet ground data dilute a real
fault below its threshold. Every test states the whole-log value that
used to decide the result, then the verdict on the armed window.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.clock_health import ClockHealthAnalyzer
from analyzers.compass import CompassAnalyzer
from analyzers.compass_consistency import CompassConsistencyAnalyzer
from analyzers.flight_profile import FlightProfileAnalyzer
from analyzers.gps_glitch import GPSGlitchAnalyzer
from analyzers.imu_consistency import IMUConsistencyAnalyzer
from analyzers.rc_input import RCInputAnalyzer
from analyzers.vibration import VibrationAnalyzer
from lib.i18n import t as _t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast


def _vehicle() -> VehicleInfo:
    return VehicleInfo(
        vehicle_type="ArduCopter", firmware_version="4.6.3",
        board_name="synthetic", frame_class=1, motor_count=4, imu_count=2,
        is_quadplane=False)


def _reader(messages: dict, armed: list, params: dict | None = None):
    """SyntheticReader that keeps an empty armed list empty."""
    reader = SyntheticReader(messages=messages, params=params or {})
    reader._armed = armed
    return reader


def _run(analyzer_cls, reader):
    return analyzer_cls(reader, Thresholds(), _vehicle()).analyze()


# =====================================================================
# rc_input: RC loss (S4-03)
# =====================================================================

class TestRcFailsafeWindow:
    @staticmethod
    def _rcin(lost):
        """RCIN over 300 s with every channel at 900 us where `lost`."""
        n = 3000
        ts = np.linspace(0.0, 300.0, n)
        channels = {f"C{i}": np.where(lost(ts), 900.0, 1500.0)
                    for i in range(1, 9)}
        return {"RCIN": {"_timestamp": ts, **channels}}

    def test_transmitter_off_on_the_ground_is_not_a_failsafe(self):
        messages = self._rcin(lambda ts: ts < 120.0)
        assert Thresholds().rc_failsafe_pwm > 900     # really below it
        result = _run(RCInputAnalyzer, _reader(messages, [(150.0, 280.0)]))
        assert result.status == "good"
        assert _t("rc_input.failsafe_disarmed", count=1) in result.details

    def test_rc_loss_in_flight_is_still_critical(self):
        messages = self._rcin(lambda ts: (ts > 200.0) & (ts < 210.0))
        result = _run(RCInputAnalyzer, _reader(messages, [(150.0, 280.0)]))
        assert result.status == "critical"
        assert result.evidence_source

    def test_loss_outside_any_known_flight_is_noted_not_critical(self):
        """A boot with the receiver still silent is every bench log."""
        messages = self._rcin(lambda ts: ts < 5.0)
        result = _run(RCInputAnalyzer, _reader(messages, []))
        assert result.status == "good"
        assert _t("rc_input.failsafe_disarmed", count=1) in result.details


# =====================================================================
# gps_glitch (S4-05)
# =====================================================================

class TestGpsGlitchWindow:
    @staticmethod
    def _gps(lat, ts):
        n = len(ts)
        return {"GPS": {"_timestamp": ts, "I": np.zeros(n), "Lat": lat,
                        "Lng": np.full(n, 120.0), "Spd": np.zeros(n),
                        "Status": np.full(n, 3.0)}}

    def test_disarmed_drift_is_not_a_glitch(self):
        ts = np.linspace(0.0, 500.0, 1000)
        rng = np.random.default_rng(1)
        lat = np.full(len(ts), 30.0)
        ground = ts < 250.0
        lat[ground] += rng.normal(0.0, 0.0005, ground.sum())  # ~55 m hops
        reader = _reader(self._gps(lat, ts), [(300.0, 480.0)])
        whole_log = np.mean(np.abs(np.diff(lat)) * 111000.0 > 5.0)
        assert whole_log > Thresholds().gps_glitch_crit       # used to fire
        assert _run(GPSGlitchAnalyzer, reader).status == "good"

    def test_a_fix_settling_before_arming_is_not_a_jump(self):
        # 08.13 FPV: 245 m while parked, 57 s before arming.
        ts = np.linspace(0.0, 500.0, 5000)
        lat = np.full(len(ts), 30.0)
        lat[ts > 230.0] += 245.0 / 111000.0
        reader = _reader(self._gps(lat, ts), [(287.0, 480.0)])
        assert _run(GPSGlitchAnalyzer, reader).status == "good"

    def test_a_log_that_never_armed_has_no_jump(self):
        # Board #35: 4061 m as the fix came in, on a bench.
        ts = np.linspace(0.0, 500.0, 5000)
        lat = np.full(len(ts), 30.0)
        lat[ts > 16.0] += 4061.0 / 111000.0
        reader = _reader(self._gps(lat, ts), [(0.0, 500.0)])
        assert _run(GPSGlitchAnalyzer, reader).status == "warning"
        reader._armed = []
        reader.never_armed = lambda: True
        result = _run(GPSGlitchAnalyzer, reader)
        assert result.status == "info"
        assert result.summary == _t("common.never_armed")

    def test_flight_glitches_are_not_diluted_by_ground_time(self):
        ts = np.linspace(0.0, 4000.0, 20000)
        lat = np.full(len(ts), 30.0)
        lat[np.arange(19000, 19800, 10)] += 0.001             # 111 m hops
        reader = _reader(self._gps(lat, ts), [(3700.0, 4000.0)])
        whole_log = 160 / (len(ts) - 1)
        assert whole_log < Thresholds().gps_glitch_warn       # used to hide
        assert _run(GPSGlitchAnalyzer, reader).status == "critical"

    def test_moving_the_aircraft_between_flights_is_not_a_jump(self):
        """No disarmed samples: LOG_DISARMED=0 leaves the two flights
        adjacent, so the pair across the gap must not count."""
        first = np.linspace(0.0, 100.0, 500)
        second = np.linspace(200.0, 300.0, 500)
        ts = np.concatenate([first, second])
        lat = np.concatenate([np.full(500, 30.0), np.full(500, 30.5)])
        reader = _reader(self._gps(lat, ts),
                         [(0.0, 100.0), (200.0, 300.0)])
        result = _run(GPSGlitchAnalyzer, reader)
        assert _t("gps_glitch.glitch_count", count=0, pct="0.00") \
            in result.details


# =====================================================================
# compass field variation (S4-06)
# =====================================================================

class TestCompassFieldWindow:
    @staticmethod
    def _mag(mag_x, ts):
        zeros = np.zeros(len(ts))
        return {"MAG": {"_timestamp": ts, "I": zeros, "MagX": mag_x,
                        "MagY": zeros + 50.0, "MagZ": zeros + 50.0,
                        "OfsX": zeros, "OfsY": zeros, "OfsZ": zeros}}

    def test_carrying_past_steel_while_disarmed_is_ignored(self):
        ts = np.linspace(0.0, 200.0, 2000)
        mag_x = np.full(len(ts), 300.0)
        ground = ts < 50.0
        mag_x[ground] = np.linspace(300.0, 700.0, ground.sum())
        whole_log = (mag_x.max() - mag_x.min()) / mag_x.mean() * 100
        assert whole_log > Thresholds().compass_field_var_warn
        result = _run(CompassAnalyzer,
                      _reader(self._mag(mag_x, ts), [(100.0, 190.0)]))
        assert result.status == "good"

    def test_variation_in_flight_still_warns(self):
        ts = np.linspace(0.0, 200.0, 2000)
        mag_x = np.full(len(ts), 300.0)
        flight = (ts > 120.0) & (ts < 170.0)
        mag_x[flight] = np.linspace(300.0, 700.0, flight.sum())
        result = _run(CompassAnalyzer,
                      _reader(self._mag(mag_x, ts), [(100.0, 190.0)]))
        assert result.status == "warning"
        assert result.evidence_source


# =====================================================================
# compass_consistency (S4-07)
# =====================================================================

class TestCompassConsistencyWindow:
    @staticmethod
    def _pair(bad):
        ts = np.repeat(np.linspace(0.0, 4300.0, 4300), 2)
        inst = np.tile([0.0, 1.0], 4300)
        mag_x = np.where((inst == 1) & bad(ts), 800.0, 300.0)
        zeros = np.zeros(len(ts))
        return {"MAG": {"_timestamp": ts, "I": inst, "MagX": mag_x,
                        "MagY": zeros + 1.0, "MagZ": zeros + 1.0,
                        "OfsX": zeros, "OfsY": zeros, "OfsZ": zeros}}

    def test_disagreement_in_flight_is_not_diluted(self):
        messages = self._pair(lambda ts: (ts > 4000.0) & (ts < 4180.0))
        whole_log_rms = 500.0 * np.sqrt(180.0 / 4300.0)
        assert whole_log_rms < Thresholds().compass_consist_crit
        result = _run(CompassConsistencyAnalyzer,
                      _reader(messages, [(3950.0, 4200.0)]))
        assert result.status == "critical"

    def test_disagreement_on_the_ground_is_ignored(self):
        messages = self._pair(lambda ts: ts < 3000.0)
        whole_log_rms = 500.0 * np.sqrt(3000.0 / 4300.0)
        assert whole_log_rms > Thresholds().compass_consist_crit
        result = _run(CompassConsistencyAnalyzer,
                      _reader(messages, [(3950.0, 4200.0)]))
        assert result.status == "good"


# =====================================================================
# imu_consistency, old IMU/IMU2 format (S4-08)
# =====================================================================

class TestImuOldFormatWindow:
    N = 3000

    def _imu(self, acc_x=None, gyr_x=0.0):
        ts = np.linspace(0.0, 300.0, self.N)
        zeros = np.zeros(self.N)
        return {"_timestamp": ts,
                "AccX": zeros if acc_x is None else acc_x(ts),
                "AccY": zeros, "AccZ": zeros - 9.8,
                "GyrX": zeros + gyr_x, "GyrY": zeros, "GyrZ": zeros,
                "T": zeros + 40.0}

    def test_bench_disagreement_is_ignored(self):
        second = self._imu(acc_x=lambda ts: np.where(ts < 50.0, 8.0, 0.0))
        reader = _reader({"IMU": self._imu(), "IMU2": second},
                         [(100.0, 290.0)])
        whole_log_rms = 8.0 * np.sqrt(50.0 / 300.0)
        assert whole_log_rms > Thresholds().imu_accel_diff_crit
        assert _run(IMUConsistencyAnalyzer, reader).status == "good"

    def test_gyro_disagreement_in_flight_is_critical(self):
        rads = Thresholds().imu_gyro_diff_crit * 50
        reader = _reader({"IMU": self._imu(), "IMU2": self._imu(gyr_x=rads)},
                         [(10.0, 290.0)])
        result = _run(IMUConsistencyAnalyzer, reader)
        assert result.status == "critical"
        assert result.evidence_source

    def test_accel_disagreement_in_flight_is_still_critical(self):
        second = self._imu(acc_x=lambda ts: np.full(len(ts), 8.0))
        reader = _reader({"IMU": self._imu(), "IMU2": second},
                         [(100.0, 290.0)])
        assert _run(IMUConsistencyAnalyzer, reader).status == "critical"


# =====================================================================
# clock_health (S4-09)
# =====================================================================

class TestClockHealthWindow:
    @staticmethod
    def _att(ts):
        return {"ATT": {"_timestamp": ts, "Roll": np.zeros(len(ts))}}

    def test_pause_between_two_flights_is_not_jitter(self):
        first = np.arange(0.0, 300.0, 0.04)
        second = np.arange(1200.0, 1500.0, 0.04)
        ts = np.concatenate([first, second])
        whole_log = np.std(np.diff(ts)) / np.median(np.diff(ts))
        assert whole_log > Thresholds().clock_jitter_warn
        result = _run(ClockHealthAnalyzer,
                      _reader(self._att(ts), [(0.0, 300.0), (1200.0, 1500.0)]))
        assert result.status == "good"

    def test_many_flights_in_one_log_are_not_counted_as_gaps(self):
        windows = [(i * 100.0, i * 100.0 + 60.0) for i in range(8)]
        ts = np.concatenate([np.arange(a, b, 0.04) for a, b in windows])
        assert len(windows) - 1 > Thresholds().clock_gap_count_warn
        result = _run(ClockHealthAnalyzer, _reader(self._att(ts), windows))
        assert result.status == "good"

    def test_a_stall_in_flight_still_warns(self):
        ts = np.concatenate([np.arange(0.0, 300.0, 0.04),
                             np.arange(330.0, 600.0, 0.04)])
        result = _run(ClockHealthAnalyzer,
                      _reader(self._att(ts), [(0.0, 600.0)]))
        assert result.status == "warning"
        assert result.evidence_source

    def test_time_going_backward_on_the_ground_is_still_critical(self):
        ts = np.arange(0.0, 300.0, 0.04)
        ts[100] = ts[90]                       # before arming at 100 s
        result = _run(ClockHealthAnalyzer,
                      _reader(self._att(ts), [(100.0, 290.0)]))
        assert result.status == "critical"


# =====================================================================
# vibration clip counter (S4-04)
# =====================================================================

class TestVibrationClipWindow:
    @staticmethod
    def _vibe(ts, **clip):
        n = len(ts)
        flat = np.full(n, 3.0)
        return {"VIBE": {"_timestamp": ts, "VibeX": flat, "VibeY": flat,
                         "VibeZ": flat, **clip}}

    def test_clips_banked_before_arming_do_not_count(self):
        ts = np.linspace(0.0, 200.0, 2000)
        clip = np.full(len(ts), 150.0)
        assert clip.max() > Thresholds().vibe_clip_warn       # used to fire
        result = _run(VibrationAnalyzer, _reader(
            self._vibe(ts, IMU=np.zeros(len(ts)), Clip=clip),
            [(50.0, 190.0)]))
        assert result.status == "good"
        assert _t("vibration.clip_count", c0=0, c1=0, c2=0, total=0) \
            in result.details

    def test_clips_during_flight_still_warn(self):
        ts = np.arange(1601) * 0.125       # lands exactly on the window edges
        clip = np.interp(ts, [0.0, 50.0, 190.0, 200.0],
                         [0.0, 40.0, 340.0, 340.0])
        result = _run(VibrationAnalyzer, _reader(
            self._vibe(ts, IMU=np.zeros(len(ts)), Clip=clip),
            [(50.0, 190.0)]))
        assert result.status == "warning"
        assert _t("vibration.clip_excess", total=300) in result.details

    def test_each_flight_adds_its_own_clips(self):
        ts = np.arange(3201) * 0.125
        clip = np.interp(ts, [0.0, 50.0, 100.0, 200.0, 300.0, 400.0],
                         [0.0, 0.0, 70.0, 500.0, 570.0, 570.0])
        result = _run(VibrationAnalyzer, _reader(
            self._vibe(ts, IMU=np.zeros(len(ts)), Clip=clip),
            [(50.0, 100.0), (200.0, 300.0)]))
        assert _t("vibration.clip_excess", total=140) in result.details

    def test_pre_4_0_counters_use_the_same_window(self):
        ts = np.linspace(0.0, 200.0, 2000)
        banked = np.full(len(ts), 150.0)
        result = _run(VibrationAnalyzer, _reader(
            self._vibe(ts, Clip0=banked, Clip1=banked, Clip2=banked),
            [(50.0, 190.0)]))
        assert result.status == "good"

    def test_without_arm_events_the_log_itself_is_the_window(self):
        ts = np.arange(1601) * 0.125
        clip = np.linspace(1000.0, 1300.0, len(ts))
        result = _run(VibrationAnalyzer, _reader(
            self._vibe(ts, IMU=np.zeros(len(ts)), Clip=clip), []))
        assert _t("vibration.clip_excess", total=300) in result.details


class TestArmedMask:
    TS = np.arange(5.0)

    @staticmethod
    def _analyzer(armed, never_armed):
        reader = _reader({}, armed)
        reader.never_armed = lambda: never_armed
        return GPSGlitchAnalyzer(reader, Thresholds(), _vehicle())

    def test_inside_the_window_only(self):
        mask = self._analyzer([(1.0, 2.0)], False).get_armed_mask(self.TS)
        assert mask.tolist() == [False, True, True, False, False]

    def test_a_log_that_never_armed_has_no_armed_rows(self):
        assert not self._analyzer([], True).get_armed_mask(self.TS).any()

    def test_a_log_that_cannot_say_keeps_every_row(self):
        # No arming events and no POWR.Safety: it may hold a whole flight.
        assert self._analyzer([], False).get_armed_mask(self.TS).all()


def test_a_log_that_never_armed_says_so():
    """It holds plenty of data: "insufficient" would send you looking for why."""
    ts = np.linspace(0.0, 100.0, 1000)
    reader = _reader({
        "GPS": {"_timestamp": ts, "I": np.zeros(1000), "Lat": np.full(1000, 30.0),
                "Lng": np.full(1000, 120.0), "Spd": np.zeros(1000),
                "Status": np.full(1000, 3.0), "Alt": np.zeros(1000)},
        "ATT": {"_timestamp": ts, "Roll": np.zeros(1000)},
    }, [])
    reader.never_armed = lambda: True
    assert _run(FlightProfileAnalyzer, reader).summary == _t("common.never_armed")
    assert _run(ClockHealthAnalyzer, reader).summary == _t("common.never_armed")
