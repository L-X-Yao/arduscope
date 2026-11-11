"""
Regression tests for the descriptive FlightProfileAnalyzer.

Covers the approved dimensions (altitude, ground/air speed, climb rate,
distance, range-from-home, attitude envelope), the armed-only window,
and the descriptive "good" status (not "info", which would drag the
health score).
"""

from __future__ import annotations

import numpy as np
import pytest

import binlog
from analyzers.flight_profile import FlightProfileAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast


def _vehicle() -> VehicleInfo:
    return VehicleInfo(
        vehicle_type="ArduPlane", firmware_version="4.6.3",
        board_name="synthetic", frame_class=0, motor_count=0,
        imu_count=1, is_quadplane=False,
    )


def _reader(n=300, spd=20.0, with_pos=True, with_xkf=True,
            with_att=True, with_arsp=True):
    ts = np.linspace(0, 150, n)
    # A track moving east from (39.9, 116.4) so range-from-home grows.
    lat = np.full(n, 39.9)
    lng = 116.4 + np.linspace(0, 0.005, n)  # ~430 m east
    msgs = {
        "GPS": {
            "_timestamp": ts, "I": np.zeros(n),
            "Spd": np.full(n, spd), "Lat": lat, "Lng": lng,
            "Alt": np.full(n, 600.0),
        },
    }
    if with_pos:
        msgs["POS"] = {"_timestamp": ts,
                       "RelHomeAlt": np.linspace(0, 80.0, n)}
    if with_xkf:
        vd = np.full(n, 0.0)
        vd[:10] = -5.0   # climbing 5 m/s
        vd[-10:] = 3.0   # descending 3 m/s
        msgs["XKF1"] = {"_timestamp": ts, "VD": vd}
    if with_att:
        roll = np.full(n, 0.0); roll[:5] = 28.0
        pitch = np.full(n, 0.0); pitch[:5] = -12.0
        msgs["ATT"] = {"_timestamp": ts, "Roll": roll, "Pitch": pitch}
    if with_arsp:
        msgs["ARSP"] = {"_timestamp": ts, "Airspeed": np.full(n, 24.0)}
    # Armed for the whole synthetic flight (ts spans 0..150).
    return SyntheticReader(messages=msgs, armed_intervals=[(0.0, 150.0)])


def _run(reader):
    return FlightProfileAnalyzer(
        reader, Thresholds.from_params(reader.params, reader.vehicle_type),
        _vehicle()).analyze()


class TestFlightProfile:

    def test_descriptive_good_not_info(self):
        """Status must be 'good' (100), not 'info' (80), so a purely
        descriptive analyzer never drags the health score."""
        res = _run(_reader())
        assert res.status == "good"
        assert res.action_level == "ok"
        assert res.param_suggestions == []

    def test_two_figures_alt_and_speed(self):
        res = _run(_reader())
        assert len(res.figures) == 2  # altitude profile + speed profile

    def test_empty_gps_info(self):
        reader = SyntheticReader(messages={"GPS": {"_timestamp": np.array([])}})
        res = _run(reader)
        assert res.status == "info"

    def test_too_few_valid_samples_info(self):
        n = 5
        reader = SyntheticReader(messages={"GPS": {
            "_timestamp": np.linspace(0, 5, n), "I": np.zeros(n),
            "Spd": np.full(n, 10.0), "Lat": np.full(n, 39.9),
            "Lng": np.full(n, 116.4), "Alt": np.full(n, 600.0),
        }})
        res = _run(reader)
        assert res.status == "info"

    def test_haversine_known_distance(self):
        a = FlightProfileAnalyzer(_reader(), Thresholds(), _vehicle())
        # 0.001 deg of longitude at the equator ~= 111.32 m.
        d = a._haversine(0.0, 0.0, np.array([0.0]), np.array([0.001]))
        assert abs(float(d[0]) - 111.32) < 1.0

    def test_climb_descent_positive_magnitudes(self):
        a = FlightProfileAnalyzer(_reader(), Thresholds(), _vehicle())
        climb, descent, _ = a._climb_rates()
        # VD -5 -> climb 5; VD +3 -> descent 3; both reported positive.
        assert abs(climb - 5.0) < 0.01
        assert abs(descent - 3.0) < 0.01

    def test_distance_integrates_speed(self):
        # 20 m/s over 150 s ~= 3.0 km (minus the final interval).
        res = _run(_reader(spd=20.0))
        assert any("2.9" in d or "3.0" in d for d in res.details), res.details

    def test_max_altitude_in_summary(self):
        res = _run(_reader())
        # RelHomeAlt peaks at 80 m.
        assert "80" in res.summary

    def test_armed_window_only(self):
        # Pre-arm samples (t<50) sit at 60 m/s; armed window (t>=50) at 20.
        n = 300
        ts = np.linspace(0, 150, n)
        spd = np.where(ts < 50, 60.0, 20.0)
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": ts, "I": np.zeros(n), "Spd": spd,
                "Lat": np.full(n, 39.9),
                "Lng": 116.4 + np.linspace(0, 0.005, n),
                "Alt": np.full(n, 600.0),
            }},
            armed_intervals=[(50.0, 150.0)],
        )
        res = _run(reader)
        # Max ground speed must reflect only the armed window (20), not 60.
        assert "20.0" in " ".join(res.details)
        assert "60.0" not in " ".join(res.details)

    def test_nan_speed_no_nan_leak(self):
        """Valid position but all-NaN GPS.Spd must not leak 'nan' into text."""
        n = 300
        ts = np.linspace(0, 150, n)
        reader = SyntheticReader(
            messages={"GPS": {
                "_timestamp": ts, "I": np.zeros(n),
                "Spd": np.full(n, np.nan), "Lat": np.full(n, 39.9),
                "Lng": 116.4 + np.linspace(0, 0.005, n),
                "Alt": np.full(n, 600.0),
            }},
            armed_intervals=[(0.0, 150.0)],
        )
        res = _run(reader)
        assert res.status == "good"
        assert "nan" not in res.summary.lower()
        assert not any("nan" in d.lower() for d in res.details)

    def test_no_climb_source_still_good(self):
        res = _run(_reader(with_xkf=True))
        # Remove EKF: analyzer still succeeds, just omits climb detail.
        res2 = _run(_reader(with_xkf=False))
        assert res2.status == "good"
        assert not any("爬升" in d for d in res2.details)

    # ---- 3D speed (#4b) ----

    @staticmethod
    def _reader_3d(with_xkf=True, with_vz=True, vz_sign=-1.0):
        """GPS Spd=4, |VZ|=3 -> 3D=5 (3-4-5 triangle); XKF1 matches."""
        n = 300
        ts = np.linspace(0, 150, n)
        gps = {
            "_timestamp": ts, "I": np.zeros(n),
            "Spd": np.full(n, 4.0), "Lat": np.full(n, 39.9),
            "Lng": 116.4 + np.linspace(0, 0.005, n),
            "Alt": np.full(n, 600.0),
        }
        if with_vz:
            gps["VZ"] = np.full(n, vz_sign * 3.0)
        msgs = {"GPS": gps}
        if with_xkf:
            msgs["XKF1"] = {
                "_timestamp": ts, "C": np.zeros(n),
                "VN": np.full(n, 4.0), "VE": np.zeros(n),
                "VD": np.full(n, vz_sign * 3.0),
            }
        return SyntheticReader(messages=msgs,
                               armed_intervals=[(0.0, 150.0)])

    def test_speed_3d_dual_source_pythagoras(self):
        res = _run(self._reader_3d())
        line = next((d for d in res.details if "3D" in d), None)
        assert line is not None, res.details
        # sqrt(4^2+3^2)=5.0, climbing (VD<0), sources agree -> 0.0%.
        assert "5.0" in line and "4.0" in line and "3.0" in line
        assert "爬升" in line
        assert "0.0%" in line

    def test_speed_3d_gps_only_descent(self):
        res = _run(self._reader_3d(with_xkf=False, vz_sign=1.0))
        line = next((d for d in res.details if "3D" in d), None)
        assert line is not None, res.details
        assert "5.0" in line and "下降" in line and "GPS" in line

    def test_speed_3d_absent_without_vertical_source(self):
        # No GPS.VZ and no EKF velocity -> dimension silently omitted.
        res = _run(self._reader_3d(with_xkf=False, with_vz=False))
        assert res.status == "good"
        assert not any("3D" in d for d in res.details)

    def test_speed_3d_divergent_ekf_gated(self):
        """A diverging EKF velocity state (variance event) must not
        become the headline 3D max; the rejected peak is reported in a
        separate transparency line. Real case: 2026-07-20 16-40-11,
        EKF ran to 88.2 m/s while GPS doppler held 43.3."""
        n = 300
        ts = np.linspace(0, 150, n)
        gps = {
            "_timestamp": ts, "I": np.zeros(n),
            "Spd": np.full(n, 4.0), "Lat": np.full(n, 39.9),
            "Lng": 116.4 + np.linspace(0, 0.005, n),
            "Alt": np.full(n, 600.0), "VZ": np.full(n, -3.0),
        }
        vn = np.full(n, 4.0)
        vn[140:160] = np.linspace(4.0, 80.0, 20)  # runaway ramp
        msgs = {"GPS": gps, "XKF1": {
            "_timestamp": ts, "C": np.zeros(n),
            "VN": vn, "VE": np.zeros(n), "VD": np.full(n, -3.0),
        }}
        reader = SyntheticReader(messages=msgs,
                                 armed_intervals=[(0.0, 150.0)])
        res = _run(reader)
        line = next((d for d in res.details
                     if "3D" in d and "剔除" not in d), None)
        assert line is not None, res.details
        assert "5.0" in line and "80" not in line, line
        reject = next((d for d in res.details if "剔除" in d), None)
        assert reject is not None, res.details
        assert "80.1" in reject and "EKF" in reject, reject

    def test_speed_3d_clean_log_no_reject_line(self):
        res = _run(self._reader_3d())
        assert not any("剔除" in d for d in res.details), res.details

    # ---- climb/descent dual-source gate ----

    def test_climb_rate_divergent_ekf_gated(self):
        """A diverged EKF VD spike must not become the reported climb
        max when GPS.VZ is present and disagrees."""
        n = 300
        ts = np.linspace(0, 150, n)
        vd = np.full(n, -5.0)
        vd[140:160] = -50.0      # runaway window
        vd[-10:] = 3.0
        reader = SyntheticReader(
            messages={
                "GPS": {
                    "_timestamp": ts, "I": np.zeros(n),
                    "Spd": np.full(n, 4.0), "Lat": np.full(n, 39.9),
                    "Lng": 116.4 + np.linspace(0, 0.005, n),
                    "Alt": np.full(n, 600.0), "VZ": np.where(
                        np.arange(n) >= n - 10, 3.0, -5.0),
                },
                "XKF1": {"_timestamp": ts, "C": np.zeros(n), "VD": vd},
            },
            armed_intervals=[(0.0, 150.0)])
        a = FlightProfileAnalyzer(
            reader, Thresholds.from_params(reader.params, reader.vehicle_type),
            _vehicle())
        climb, descent, note = a._climb_rates()
        assert abs(climb - 5.0) < 0.2, climb
        assert abs(descent - 3.0) < 0.2, descent
        # EKF diverged high while GPS stayed put: no GPS peak was lost,
        # so no single-source transparency note either
        assert note is None, note

    def test_climb_rate_gps_peak_note_when_clipped(self):
        """A GPS climb peak falling inside an EKF divergence window is
        excluded from the stat but must surface as a note."""
        n = 300
        ts = np.linspace(0, 150, n)
        gvz = np.full(n, -5.0)
        gvz[150:170] = -31.0     # real GPS climb peak...
        vd = np.full(n, -5.0)
        vd[150:170] = -12.0      # ...while EKF disagrees hard
        reader = SyntheticReader(
            messages={
                "GPS": {
                    "_timestamp": ts, "I": np.zeros(n),
                    "Spd": np.full(n, 4.0), "Lat": np.full(n, 39.9),
                    "Lng": 116.4 + np.linspace(0, 0.005, n),
                    "Alt": np.full(n, 600.0), "VZ": gvz,
                },
                "XKF1": {"_timestamp": ts, "C": np.zeros(n), "VD": vd},
            },
            armed_intervals=[(0.0, 150.0)])
        a = FlightProfileAnalyzer(
            reader, Thresholds.from_params(reader.params, reader.vehicle_type),
            _vehicle())
        climb, descent, note = a._climb_rates()
        assert abs(climb - 5.0) < 0.2, climb
        assert note is not None and "31.0" in note, note

    def test_climb_rate_ekf_only_unchanged(self):
        """No GPS.VZ: EKF-only fallback keeps the raw extreme."""
        climb, descent, note = FlightProfileAnalyzer(
            _reader(), Thresholds(), _vehicle())._climb_rates()
        assert abs(climb - 5.0) < 0.01
        assert abs(descent - 3.0) < 0.01
        assert note is None


def _dual_gps_reader(vz1_spike=-25.0):
    """Two receivers; instance 1 spikes while instance 0 and the EKF agree."""
    n = 300
    ts = np.linspace(0, 150, n)
    gps_ts = np.repeat(ts, 2)
    inst = np.tile([0.0, 1.0], n)
    vz = np.zeros(2 * n)
    vz[(inst == 0) & (gps_ts >= 50) & (gps_ts < 60)] = -4.0
    vz[(inst == 1) & (gps_ts >= 100) & (gps_ts < 101)] = vz1_spike
    vd = np.zeros(n)
    vd[(ts >= 50) & (ts < 60)] = -4.0
    return SyntheticReader(
        messages={
            "GPS": {"_timestamp": gps_ts, "I": inst,
                    "Spd": np.full(2 * n, 20.0), "VZ": vz,
                    "Lat": np.full(2 * n, 39.9),
                    "Lng": np.full(2 * n, 116.4),
                    "Alt": np.full(2 * n, 600.0)},
            "XKF1": {"_timestamp": ts, "VD": vd,
                     "VN": np.full(n, 20.0), "VE": np.zeros(n)},
        },
        armed_intervals=[(0.0, 150.0)])


class TestInstanceFiltering:
    def test_second_receiver_is_not_a_climb_peak(self):
        reader = _dual_gps_reader()
        gps = reader._messages["GPS"]
        assert np.min(gps["VZ"][gps["I"] == 1]) == -25.0
        res = _run(reader)
        peak = _t("flight_profile.climb_gps_peak", v="25.0", t="100.0")
        assert not any(peak[:8] in d for d in res.details), res.details
        assert any(_t("flight_profile.climb_rate", up="4.0", down="0.0")
                   == d for d in res.details), res.details

    def test_airspeed_follows_the_sensor_in_use(self, tmp_path):
        log = binlog.BinLog()
        log.write("MSG", 1_000_000, "ArduPlane V4.6.3 (abc12345)")
        log.write("EV", 1_000_000, 10)
        for i in range(100):
            time_us = 1_000_000 + i * 100_000
            log.write("GPS", time_us, 0, 3, 100000 + i * 100, 2300, 14, 0.8,
                      34.1, 108.9, 400.0, 20.0, 0.0, 0.0, 0.0, 1)
            # Sensor 0 is dead and reads high; AP fell back to sensor 1.
            for sensor, speed in ((0, 90.0), (1, 24.0)):
                log.write("ARSP", time_us, sensor, speed, 100.0, 25.0,
                          1000.0, 0.0, 1, 1, 1.0, 0.0, 1)
        res = _run(LogReader(log.save(tmp_path / "arsp.bin")))
        assert _t("flight_profile.airspeed", mx="24.0") in res.details

    def test_airspeed_without_a_primary_column_uses_the_first_sensor(self):
        n = 300
        ts = np.repeat(np.linspace(0, 150, n), 2)
        inst = np.tile([0.0, 1.0], n)
        reader = _reader()
        reader._messages["ARSP"] = {
            "_timestamp": ts, "I": inst,
            "Airspeed": np.where(inst == 0, 24.0, 90.0)}
        res = _run(reader)
        assert _t("flight_profile.airspeed", mx="24.0") in res.details
