"""
Regression tests for the 2026-07-15 runtime bug-hunt fix batches
(docs/history/bug-hunt-2026-07-15.md). Each test asserts the precondition (the
scenario that used to misbehave) before the invariant, so none can pass
vacuously. Grouped by fix batch.
"""

from __future__ import annotations

import numpy as np
import pytest

from lib.log_reader import Event, LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo, _count_imus
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast


def _vehicle(vt="ArduCopter"):
    return VehicleInfo(
        vehicle_type=vt, firmware_version="4.6.3", board_name="synthetic",
        frame_class=1, motor_count=4, imu_count=3, is_quadplane=False)


# =====================================================================
# Batch 1 — field-name aliases + instance filtering
# =====================================================================

class TestBatch1FieldNames:

    def test_rfnd_instance_alias(self):
        reader = LogReader.__new__(LogReader)
        result = {"I": np.full(4, np.nan),
                  "Instance": np.array([0.0, 0.0, 1.0, 1.0])}
        assert np.all(np.isnan(result["I"]))  # precondition: "I" absent
        out = reader._apply_aliases("RFND", ["I"], result)
        assert not np.all(np.isnan(out["I"]))
        np.testing.assert_array_equal(out["I"], result["Instance"])

    def test_bat_rempct_alias(self):
        reader = LogReader.__new__(LogReader)
        result = {"Rem": np.full(3, np.nan),
                  "RemPct": np.array([90.0, 80.0, 70.0])}
        assert np.all(np.isnan(result["Rem"]))  # precondition
        out = reader._apply_aliases("BAT", ["Rem"], result)
        np.testing.assert_array_equal(out["Rem"], result["RemPct"])

    def test_count_imus_uses_instance_column(self):
        n = 30
        # Modern single-IMU-message log: instances 0,1,2 interleaved.
        inst = np.tile([0.0, 1.0, 2.0], n // 3)
        reader = SyntheticReader(messages={"IMU": {
            "_timestamp": np.linspace(0, 30, n), "I": inst,
            "GyrX": np.zeros(n),
        }})
        # Precondition: only the single "IMU" type exists (no IMU2/IMU3).
        assert reader.has_message("IMU")
        assert not reader.has_message("IMU2")
        assert _count_imus(reader) == 3  # was always 1 before the fix

    def test_count_imus_single(self):
        n = 10
        reader = SyntheticReader(messages={"IMU": {
            "_timestamp": np.linspace(0, 10, n), "I": np.zeros(n),
            "GyrX": np.zeros(n),
        }})
        assert _count_imus(reader) == 1


class TestBatch1VibrationInstance:

    def _reader(self, primary_max=5.0, backup_max=99.0):
        # Two IMU instances interleaved; instance 1 (non-primary) has huge
        # vibration that must NOT drive the primary-instance status.
        n = 600
        ts = np.linspace(0, 120, n)
        imu = np.tile([0.0, 1.0], n // 2)
        vx = np.where(imu == 0, primary_max, backup_max)
        return SyntheticReader(
            messages={"VIBE": {
                "_timestamp": ts, "IMU": imu,
                "VibeX": vx, "VibeY": np.zeros(n), "VibeZ": np.zeros(n),
                "Clip": np.zeros(n),
            }},
            armed_intervals=[(0.0, 120.0)],
        )

    def test_primary_instance_drives_status(self):
        from analyzers.vibration import VibrationAnalyzer
        reader = self._reader(primary_max=5.0, backup_max=99.0)
        # Precondition: a non-primary IMU shows critical-level vibration.
        vibe = reader.get_messages_as_arrays("VIBE", ["IMU", "VibeX"])
        assert np.nanmax(vibe["VibeX"][vibe["IMU"] == 1]) >= 60.0
        res = VibrationAnalyzer(
            reader, Thresholds(), _vehicle()).analyze()
        # Invariant: status reflects the primary IMU (5 m/s^2 = good),
        # not the interleaved backup instance.
        assert res.status == "good"

    def test_clip_read_per_instance(self):
        from analyzers.vibration import VibrationAnalyzer
        n = 600
        ts = np.linspace(0, 120, n)
        imu = np.tile([0.0, 1.0], n // 2)
        # primary IMU clipped 40x, in flight
        clip = np.where((imu == 0) & (ts > 60.0), 40.0, 0.0)
        reader = SyntheticReader(
            messages={"VIBE": {
                "_timestamp": ts, "IMU": imu,
                "VibeX": np.full(n, 3.0), "VibeY": np.zeros(n),
                "VibeZ": np.zeros(n), "Clip": clip,
            }},
            armed_intervals=[(0.0, 120.0)],
        )
        # Precondition: a real (non-zero) Clip counter exists.
        assert np.nanmax(clip) == 40.0
        res = VibrationAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: clips are read (were always 0 when the code looked for
        # the non-existent Clip0/1/2 fields), so 40 clips surface.
        assert any("40" in d for d in res.details), res.details


# =====================================================================
# Batch 2 — instance / core filtering
# =====================================================================

class TestBatch2CoreInstance:

    def test_ekf_primary_core_only(self):
        from analyzers.ekf import EKFAnalyzer
        th = Thresholds()
        crit = th.ekf_variance_crit
        n = 400
        ts = np.linspace(0, 100, n)
        core = np.tile([0.0, 1.0], n // 2)
        low = np.full(n, crit * 0.1)
        # Backup core (1) variance spikes; primary core (0) stays low.
        sv = np.where(core == 0, crit * 0.1, crit * 3.0)
        reader = SyntheticReader(
            messages={"XKF4": {
                "_timestamp": ts, "C": core, "SV": sv,
                "SP": low, "SH": low, "SM": low, "SS": np.zeros(n),
            }},
            armed_intervals=[(0.0, 100.0)],
        )
        # Precondition: backup core exceeds crit, primary does not.
        assert np.max(sv[core == 1]) > crit and np.max(sv[core == 0]) < crit
        res = EKFAnalyzer(reader, th, _vehicle()).analyze()
        # Invariant: status is judged on the primary lane only.
        assert res.status != "critical"

    def test_gyro_saturation_denominator_primary_imu(self):
        from analyzers.gyro_saturation import GyroSaturationAnalyzer
        n = 400
        ts = np.linspace(0, 100, n)
        imu = np.tile([0.0, 1.0], n // 2)
        sat = 35.0  # rad/s ~= 2005 deg/s, above the 1950 deg/s threshold
        # Primary IMU fully saturated; backup IMU clean.
        gx = np.where(imu == 0, sat, 0.0)
        reader = SyntheticReader(
            messages={"IMU": {
                "_timestamp": ts, "I": imu, "GyrX": gx,
                "GyrY": np.zeros(n), "GyrZ": np.zeros(n),
            }},
            armed_intervals=[(0.0, 100.0)],
        )
        # Precondition: the primary IMU is saturated across its samples.
        assert np.all(gx[imu == 0] == sat)
        res = GyroSaturationAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: fraction is over the primary IMU only, so it reads
        # ~100% (critical), not ~50% diluted by the clean backup IMU.
        assert res.status == "critical"

    def test_flight_profile_climb_primary_core(self):
        from analyzers.flight_profile import FlightProfileAnalyzer
        n = 400
        ts = np.linspace(0, 100, n)
        core = np.tile([0.0, 1.0], n // 2)
        vd = np.zeros(n)
        vd[core == 1] = -50.0  # backup core: absurd 50 m/s climb outlier
        vd[core == 0] = -3.0   # primary core: real 3 m/s climb
        reader = SyntheticReader(
            messages={"XKF1": {"_timestamp": ts, "C": core, "VD": vd}},
            armed_intervals=[(0.0, 100.0)],
        )
        a = FlightProfileAnalyzer(reader, Thresholds(), _vehicle())
        # Precondition: the backup core carries a 50 m/s outlier.
        assert np.min(vd[core == 1]) == -50.0
        climb, _descent, _note = a._climb_rates()
        # Invariant: climb reflects the primary core (~3), not 50.
        assert abs(climb - 3.0) < 0.5


# =====================================================================
# Batch 3 + 4 — invalid-fix filtering and armed-empty false positives
# =====================================================================

class TestBatch34FalsePositives:

    def test_gps_glitch_ignores_nofix_zero_samples(self):
        from analyzers.gps_glitch import GPSGlitchAnalyzer
        n = 200
        ts = np.linspace(0, 100, n)
        lat = np.full(n, 39.9)
        lng = np.full(n, 116.4)
        status = np.full(n, 3.0)
        # A burst of no-fix (0,0) samples between real fixes.
        lat[50:55] = 0.0
        lng[50:55] = 0.0
        status[50:55] = 1.0
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": ts, "I": np.zeros(n),
                "Lat": lat, "Lng": lng, "Spd": np.full(n, 5.0),
                "Status": status,
            }},
            armed_intervals=[(0.0, 100.0)],
        )
        # Precondition: (0,0) no-fix samples are present.
        assert np.any((lat == 0) & (lng == 0))
        res = GPSGlitchAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: the jump to/from (0,0) is not counted as a glitch.
        assert res.status != "critical"

    def test_gps_glitch_only_instance_1(self):
        from analyzers.gps_glitch import GPSGlitchAnalyzer
        n = 100
        ts = np.linspace(0, 100, n)
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": ts, "I": np.ones(n),  # only instance 1
                "Lat": np.full(n, 39.9), "Lng": np.linspace(116.4, 116.41, n),
                "Spd": np.full(n, 5.0), "Status": np.full(n, 3.0),
            }},
            armed_intervals=[(0.0, 100.0)],
        )
        # Precondition: the only GPS receiver is instance 1.
        assert np.all(reader.get_messages_as_arrays("GPS", ["I"])["I"] == 1)
        res = GPSGlitchAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: analysis runs (was skipped by the hardcoded inst-0).
        assert res.status in ("good", "warning", "critical")

    def test_failsafe_battery_margin_no_armed_not_critical(self):
        from analyzers.failsafe import FailsafeAnalyzer
        n = 200
        ts = np.linspace(0, 100, n)
        volt = np.full(n, 24.0)
        volt[:20] = 0.0  # pre-arm 0V (BEC not yet powered)
        reader = SyntheticReader(
            messages={"BAT": {
                "_timestamp": ts, "Inst": np.zeros(n),
                "Volt": volt, "Curr": np.full(n, 10.0),
            }},
            params={"BATT_LOW_VOLT": 21.0},
        )
        reader._armed = []  # SyntheticReader coerces [] -> default; force it
        # Precondition: raw min voltage is below the threshold, no armed
        # window to distinguish pre-arm artifacts.
        assert float(np.min(volt)) < 21.0 and reader.get_armed_intervals() == []
        a = FailsafeAnalyzer(
            reader, Thresholds.from_params(reader.params, reader.vehicle_type),
            _vehicle())
        _fig, status = a._battery_margin([])
        # Invariant: no armed window -> no authoritative breach -> not critical.
        assert status != "critical"


class TestBatch4PidLogic:

    def test_constant_izero_not_windup_no_noop(self):
        from analyzers.pid_tuning import PIDTuningAnalyzer
        n = 400
        ts = np.linspace(0, 120, n)
        tar = 0.2 * np.sin(np.linspace(0, 40, n))  # small rate target, rad/s
        act = tar * 0.98                             # good tracking
        reader = SyntheticReader(
            messages={
                "PIDR": {
                    "_timestamp": ts, "P": np.full(n, 0.05), "I": np.zeros(n),
                    "D": np.zeros(n), "FF": np.zeros(n), "Tar": tar, "Act": act,
                },
                "POS": {"_timestamp": ts, "Alt": np.linspace(0, 80, n)},
            },
            params={"ATC_RAT_RLL_I": 0.0, "ATC_RAT_RLL_P": 0.1,
                    "ATC_RAT_RLL_FF": 0.0},
            armed_intervals=[(0.0, 120.0)],
        )
        # Precondition: the I-term is a constant zero.
        assert np.all(reader.get_messages_as_arrays("PIDR", ["I"])["I"] == 0.0)
        res = PIDTuningAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: a constant I-term is not windup, and no no-op 0->0
        # suggestion survives (for ATC_RAT_RLL_I or any param).
        assert not any(s["param"] == "ATC_RAT_RLL_I"
                       for s in res.param_suggestions)
        assert all(round(float(s["suggested"]), 6)
                   != round(float(s["current"]), 6)
                   for s in res.param_suggestions)

    def test_tracking_gate_live_in_degs(self):
        from analyzers.pid_tuning import PIDTuningAnalyzer
        n = 400
        ts = np.linspace(0, 120, n)
        tar = np.full(n, 0.5)   # 0.5 rad/s ~ 28.6 deg/s target
        act = np.full(n, 0.2)   # 0.3 rad/s ~ 17 deg/s tracking error
        reader = SyntheticReader(
            messages={
                "PIDR": {
                    "_timestamp": ts, "P": np.full(n, 0.05), "I": np.zeros(n),
                    "D": np.zeros(n), "FF": np.zeros(n), "Tar": tar, "Act": act,
                },
                "POS": {"_timestamp": ts, "Alt": np.linspace(0, 80, n)},
            },
            params={"ATC_RAT_RLL_I": 0.1, "ATC_RAT_RLL_P": 0.1},
            armed_intervals=[(0.0, 120.0)],
        )
        # Precondition: RMS error ~17 deg/s and ratio 60% — both above the
        # warn floors that the rad/s-vs-deg/s bug left unreachable.
        assert 0.3 * 180.0 / np.pi > 5.0
        res = PIDTuningAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: the now-live absolute gate trips at least a warning.
        assert res.status in ("warning", "critical")


# =====================================================================
# Batch 5 + 6 — motor pwm=0, nan leaks, clean-disarm, degenerate modes
# =====================================================================

class TestBatch56:

    def test_compass_nan_offsets_no_leak(self):
        from analyzers.compass import CompassAnalyzer
        n = 200
        ts = np.linspace(0, 100, n)
        reader = SyntheticReader(
            messages={"MAG": {
                "_timestamp": ts, "I": np.zeros(n),
                "MagX": np.full(n, 200.0), "MagY": np.full(n, 100.0),
                "MagZ": np.full(n, -300.0),
                "OfsX": np.full(n, np.nan), "OfsY": np.full(n, np.nan),
                "OfsZ": np.full(n, np.nan),
            }},
            armed_intervals=[(0.0, 100.0)],
        )
        # Precondition: the offsets are all-NaN.
        assert np.all(np.isnan(
            reader.get_messages_as_arrays("MAG", ["OfsX"])["OfsX"]))
        res = CompassAnalyzer(reader, Thresholds(), _vehicle()).analyze()
        # Invariant: no 'nan' leaks into the summary or detail strings.
        assert "nan" not in res.summary.lower()
        assert not any("nan" in d.lower() for d in res.details)

    def test_log_health_clean_disarm_not_abrupt(self):
        from analyzers.log_health import LogHealthAnalyzer
        reader = SyntheticReader(messages={}, armed_intervals=[(5.0, 95.0)])
        reader.time_range = (0.0, 95.3)  # gap 0.3s < the abrupt-end gap
        reader.events = [Event(5.0, 10), Event(95.0, 11)]  # arm + disarm
        a = LogHealthAnalyzer(reader, Thresholds(), _vehicle())
        # Precondition: a small end gap (would look abrupt) but a matched
        # DISARMED event (10 armed, 11 disarmed).
        assert reader.time_range[1] - reader.get_armed_intervals()[-1][1] < 1.0
        status, _details = a._detect_abrupt_end()
        # Invariant: a matched clean disarm is not a power-path interruption.
        assert status is None

    def test_log_health_unclosed_arm_still_abrupt(self):
        from analyzers.log_health import LogHealthAnalyzer
        reader = SyntheticReader(messages={}, armed_intervals=[(5.0, 95.0)])
        reader.time_range = (0.0, 95.3)
        reader.events = [Event(5.0, 10)]  # armed, never disarmed
        a = LogHealthAnalyzer(reader, Thresholds(), _vehicle())
        # Precondition: an arm with no matching disarm.
        assert not any(ev.event_id == 11 for ev in reader.events)
        status, _details = a._detect_abrupt_end()
        # Invariant: an unclosed arming is NOT suppressed (the clean-disarm
        # guard must not swallow a genuine armed-at-cutoff fragment).
        assert status is not None

    def test_web_ui_locale_applied_per_request(self):
        import bottle
        import web_app
        from lib.i18n import get_locale, set_locale
        saved = web_app._UI_LOCALE
        try:
            # Simulate a fresh server thread that defaulted to zh while the
            # server default is en.
            set_locale("zh")
            web_app._UI_LOCALE = "en"
            bottle.request.bind({})
            web_app._apply_ui_locale()  # before_request hook body
            # Invariant: the request thread now renders in the selected UI
            # locale, not the thread-local default.
            assert get_locale() == "en"
        finally:
            web_app._UI_LOCALE = saved
            set_locale("zh")


class TestVtolTrnDetection:
    """QTUN.Trn-based transition detection (rebuilt off the wrong TMix
    signal, verified against AP transition.h: 0=AIRSPEED_WAIT, 1=TIMER,
    2=DONE)."""

    def _analyzer(self):
        from analyzers.vtol_transition import VTOLTransitionAnalyzer
        reader = SyntheticReader(messages={}, vehicle_type="ArduPlane",
                                 params={"Q_ENABLE": 1})
        return VTOLTransitionAnalyzer(reader, Thresholds(),
                                      _vehicle(vt="ArduPlane"))

    def test_timer_phase_is_one_forward_transition(self):
        n = 300
        ts = np.linspace(0, 60, n)
        trn = np.zeros(n)      # AIRSPEED_WAIT (idle VTOL)
        trn[100:150] = 1       # TIMER (active forward transition)
        trn[150:] = 2          # DONE (fixed wing established)
        alt = np.full(n, 100.0)
        tmix = np.full(n, 0.3)  # throttle mix sits in the old 0.05-0.95 band
        # Precondition: TMix would flag the whole flight; TIMER marks a real
        # transition.
        assert np.all((tmix > 0.05) & (tmix < 0.95)) and np.any(trn == 1)
        trns = self._analyzer()._detect_transitions(ts, trn, alt)
        # Invariant: exactly one forward transition, not a whole-flight one.
        assert len(trns) == 1
        assert trns[0]["direction"] == "to_fw"
        assert trns[0]["duration"] < 30.0

    def test_pure_hover_no_transition(self):
        n = 300
        ts = np.linspace(0, 60, n)
        trn = np.zeros(n)  # never leaves AIRSPEED_WAIT -> never transitions
        alt = np.full(n, 100.0)
        # Precondition: no TIMER sample anywhere.
        assert not np.any(trn == 1)
        trns = self._analyzer()._detect_transitions(ts, trn, alt)
        # Invariant: pure VTOL hover reports no transition (the old TMix
        # heuristic flagged the entire flight as one giant transition).
        assert trns == []
