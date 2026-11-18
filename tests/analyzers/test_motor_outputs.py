"""Motors are wherever SERVOn_FUNCTION puts them, not on C1..Cn."""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.compass import CompassAnalyzer
from analyzers.motor import MotorAnalyzer
from analyzers.servo_dither import ServoDitherAnalyzer
from lib.outputs import motor_channels, read_outputs
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from test_synthetic_coverage import SyntheticReader

N = 2000
TIMES = np.linspace(0.0, 100.0, N)


def _info(motors: int = 4, quadplane: bool = True,
          vehicle: str = "ArduPlane") -> VehicleInfo:
    return VehicleInfo(vehicle_type=vehicle, firmware_version="V4.6.3",
                       board_name="b", frame_class=1, motor_count=motors,
                       imu_count=1, is_quadplane=quadplane)


def _servo_params(mapping: dict[int, int], **extra) -> dict:
    params = {f"SERVO{out}_FUNCTION": fn for out, fn in mapping.items()}
    params.update(extra)
    return params


OCTAQUAD = _servo_params(
    {1: 33, 2: 34, 3: 35, 4: 36, 5: 70, 6: 4, 7: 19, 8: 21,
     10: 37, 11: 38, 12: 39, 13: 40},
    Q_ENABLE=1, Q_FRAME_CLASS=4, Q_M_PWM_MIN=1000, Q_M_PWM_MAX=2000,
    Q_M_SPIN_MIN=0.15, Q_M_SPIN_ARM=0.10)

SCATTERED_QUAD = _servo_params(
    {3: 33, 7: 34, 9: 35, 15: 36, 6: 70, 1: 4, 2: 19, 4: 21},
    Q_ENABLE=1, Q_FRAME_CLASS=1, Q_M_PWM_MIN=1000, Q_M_PWM_MAX=2000,
    Q_M_SPIN_MIN=0.15, Q_M_SPIN_ARM=0.10)


def _rcou(**channels) -> dict:
    data = {"_timestamp": TIMES}
    for number in range(1, 15):
        data[f"C{number}"] = np.full(N, 1500.0)
    data.update({ch: np.full(N, float(v)) if np.isscalar(v) else v
                 for ch, v in channels.items()})
    return data


class TestMotorChannels:
    def test_octaquad_with_rotors_split_across_outputs(self):
        reader = SyntheticReader({}, OCTAQUAD)
        assert motor_channels(reader) == [
            "C1", "C2", "C3", "C4", "C10", "C11", "C12", "C13"]

    def test_order_follows_motor_number_not_output_number(self):
        reader = SyntheticReader({}, _servo_params({1: 34, 2: 33}))
        assert motor_channels(reader) == ["C2", "C1"]

    @pytest.mark.parametrize("function,number", [(40, 8), (82, 9), (85, 12),
                                                 (160, 13), (179, 32)])
    def test_every_motor_function_counts(self, function, number):
        reader = SyntheticReader({}, _servo_params({20: function}))
        assert motor_channels(reader) == ["C20"]

    def test_surfaces_and_pusher_are_not_motors(self):
        reader = SyntheticReader({}, _servo_params({1: 4, 2: 19, 3: 70}))
        assert motor_channels(reader) == []


class TestFallback:
    def _channels(self, reader, info):
        analyzer = MotorAnalyzer(reader, Thresholds(), info)
        return analyzer.motor_channels()

    def test_no_servo_params_means_first_n_channels(self):
        reader = SyntheticReader({}, {})
        assert self._channels(reader, _info(6, False, "ArduCopter")) == [
            "C1", "C2", "C3", "C4", "C5", "C6"]

    def test_pure_plane_has_no_lift_motors(self):
        reader = SyntheticReader({}, {}, vehicle_type="ArduPlane")
        assert self._channels(reader, _info(0, False)) == []


class TestReadOutputs:
    def test_merges_rco2_on_the_rcou_time_base(self):
        rco2 = {"_timestamp": TIMES + 0.0001, "C15": np.arange(N) + 1000.0}
        reader = SyntheticReader({"RCOU": _rcou(), "RCO2": rco2})
        data = read_outputs(reader, ["C3", "C15"])
        assert len(data["C15"]) == N
        np.testing.assert_array_equal(data["C15"], rco2["C15"])

    def test_channel_the_log_lacks_is_nan(self):
        reader = SyntheticReader({"RCOU": _rcou()})
        data = read_outputs(reader, ["C15"])
        assert np.all(np.isnan(data["C15"]))


class TestMotorAnalyzer:
    def _run(self, params, messages, motors):
        reader = SyntheticReader(messages, params, vehicle_type="ArduPlane")
        return MotorAnalyzer(reader, Thresholds(), _info(motors)).analyze()

    def test_pusher_and_surfaces_are_not_read_as_rotors(self):
        rotors = {f"C{i}": 1400.0 for i in (1, 2, 3, 4, 10, 11, 12, 13)}
        result = self._run(OCTAQUAD,
                           {"RCOU": _rcou(C5=1100, C6=1520, C7=1600, C8=1000,
                                          **rotors)}, 8)
        assert result.status == "good", result.details
        assert "C5" not in " ".join(
            d for d in result.details if "balance" in d or "平衡" in d)

    def test_real_rotor_on_a_high_output_is_checked(self):
        rotors = {f"C{i}": 1500.0 for i in (1, 2, 3, 4, 10, 11, 12)}
        result = self._run(OCTAQUAD,
                           {"RCOU": _rcou(C13=1100, **rotors)}, 8)
        assert result.status == "critical"
        assert any("C13" in d for d in result.details)

    def test_rotor_above_output_14_is_read_from_rco2(self):
        rco2 = {"_timestamp": TIMES, "C15": np.full(N, 1100.0)}
        rotors = {"C3": 1500.0, "C7": 1500.0, "C9": 1500.0}
        result = self._run(SCATTERED_QUAD,
                           {"RCOU": _rcou(**rotors), "RCO2": rco2}, 4)
        assert result.status == "critical"
        assert any("C15" in d for d in result.details)


def test_detail_text_does_not_double_the_channel_prefix():
    rotors = {f"C{i}": 1400.0 for i in (1, 2, 3, 4, 10, 11, 12, 13)}
    reader = SyntheticReader({"RCOU": _rcou(**rotors)}, OCTAQUAD,
                             vehicle_type="ArduPlane")
    result = MotorAnalyzer(reader, Thresholds(), _info(8)).analyze()
    assert not any("CC" in d for d in result.details)


class TestCompassInterference:
    def _run(self, params, rcou, mag):
        reader = SyntheticReader({"RCOU": rcou, "MAG": mag}, params,
                                 vehicle_type="ArduPlane")
        analyzer = CompassAnalyzer(reader, Thresholds(), _info(8))
        inst = mag["I"]
        return analyzer._motor_interference(
            mag["_timestamp"], inst, mag["MagX"], mag["MagY"], mag["MagZ"],
            mag["OfsX"], mag["OfsY"], mag["OfsZ"],
            sorted({int(v) for v in inst}), False)

    @staticmethod
    def _mag(follows_throttle, instance):
        throttle = np.linspace(0.0, 1.0, N)
        zeros = np.zeros(N)
        x = 400.0 + (300.0 * throttle if follows_throttle else 0.0)
        return {"_timestamp": TIMES, "I": np.full(N, float(instance)),
                "MagX": np.full(N, 1.0) * x, "MagY": zeros, "MagZ": zeros,
                "OfsX": zeros, "OfsY": zeros, "OfsZ": zeros}

    def test_uses_real_motors_and_the_worst_instance(self):
        ramp = np.linspace(1100.0, 1900.0, N)
        rcou = _rcou(C1=1100, C2=1100, C3=1100, C4=1100, C10=ramp, C11=ramp,
                     C12=ramp, C13=ramp)
        quiet = self._mag(False, 0)
        noisy = self._mag(True, 1)
        mag = {k: (np.concatenate([quiet[k], noisy[k]]))
               for k in quiet}
        mag["_timestamp"] = np.concatenate([TIMES, TIMES])
        order = np.argsort(mag["_timestamp"], kind="stable")
        mag = {k: v[order] for k, v in mag.items()}
        result = self._run(OCTAQUAD, rcou, mag)
        assert result.scalar_pct is not None and result.scalar_pct > 30.0


def _test_dcm(roll, pitch, yaw):
    """Body-to-earth 321 rotation, independent test fixture arithmetic."""
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    rot = np.empty((len(roll), 3, 3))
    rot[:, 0, 0] = cp * cy
    rot[:, 0, 1] = sr * sp * cy - cr * sy
    rot[:, 0, 2] = cr * sp * cy + sr * sy
    rot[:, 1, 0] = cp * sy
    rot[:, 1, 1] = sr * sp * sy + cr * cy
    rot[:, 1, 2] = cr * sp * sy - sr * cy
    rot[:, 2, 0] = -sp
    rot[:, 2, 1] = sr * cp
    rot[:, 2, 2] = cr * cp
    return rot


def _vector_log(impacts=(None,), current=False, short_baseline=False):
    """A rotating compass with optional body-fixed motor impact vectors."""
    n = 600
    ts = np.linspace(0.0, 60.0, n)
    throttle = 0.15 + 0.75 * (0.5 + 0.5 * np.sin(0.31 * ts))
    if short_baseline:
        throttle[:] = 0.8
        throttle[:4] = 0.0
    roll = np.radians(12.0 * np.sin(0.17 * ts))
    pitch = np.radians(9.0 * np.cos(0.13 * ts))
    yaw = np.radians(35.0 * np.sin(0.07 * ts))
    rot = _test_dcm(roll, pitch, yaw)
    earth = np.array([400.0, 0.0, 300.0])
    clean = np.einsum("nji,j->ni", rot, earth)
    excitation = 40.0 * throttle if current else throttle

    parts = []
    for instance, impact in enumerate(impacts):
        field = clean.copy()
        if impact is not None:
            field += excitation[:, None] * np.asarray(impact)[None, :]
        parts.append({
            "_timestamp": ts, "I": np.full(n, instance),
            "MagX": field[:, 0], "MagY": field[:, 1], "MagZ": field[:, 2],
            "OfsX": np.zeros(n), "OfsY": np.zeros(n), "OfsZ": np.zeros(n),
        })
    mag = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    order = np.argsort(mag["_timestamp"], kind="stable")
    mag = {k: v[order] for k, v in mag.items()}
    pwm = 1000.0 + 1000.0 * throttle
    messages = {
        "MAG": mag,
        "RCOU": {"_timestamp": ts, **{f"C{i}": pwm for i in range(1, 5)}},
        "ATT": {"_timestamp": ts, "Roll": np.degrees(roll),
                "Pitch": np.degrees(pitch), "Yaw": np.degrees(yaw)},
    }
    if current:
        messages["BAT"] = {"_timestamp": ts, "Inst": np.zeros(n),
                           "Curr": excitation}
    return SyntheticReader(messages, {"MOT_PWM_MIN": 1000,
                                      "MOT_PWM_MAX": 2000},
                           vehicle_type="ArduCopter")


def _vector_result(reader):
    analyzer = CompassAnalyzer(reader, Thresholds(),
                               _info(4, False, "ArduCopter"))
    mag = reader._messages["MAG"]
    return analyzer._motor_interference(
        mag["_timestamp"], mag["I"], mag["MagX"], mag["MagY"], mag["MagZ"],
        mag["OfsX"], mag["OfsY"], mag["OfsZ"],
        sorted({int(v) for v in mag["I"]}), False)


class TestCompassVectorInterference:
    def test_body_fixed_vector_is_detected_with_rcou_only(self):
        impact = np.array([250.0, 180.0, 200.0])
        result = _vector_result(_vector_log((impact,)))
        expected = np.linalg.norm(impact) / 530.0 * 100.0
        assert result.vector_pct == pytest.approx(expected, rel=1e-6)
        assert result.vector_driver == "RCOU"
        reader = _vector_log((impact,))
        res = CompassAnalyzer(reader, Thresholds(),
                              _info(4, False, "ArduCopter")).analyze()
        assert res.status == "critical"
        assert res.evidence[0]["value"] == pytest.approx(expected, rel=1e-6)

    def test_vector_crosses_threshold_when_scalar_does_not(self):
        result = _vector_result(_vector_log((np.array([100.0, 100.0, 100.0]),)))
        assert result.scalar_pct < 30.0 < result.vector_pct

    def test_pure_attitude_rotation_has_no_vector_impact(self):
        reader = _vector_log()
        result = _vector_result(reader)
        assert result.vector_pct == pytest.approx(0.0, abs=1e-8)
        res = CompassAnalyzer(reader, Thresholds(),
                              _info(4, False, "ArduCopter")).analyze()
        assert res.status == "good", res.details

    def test_insufficient_low_throttle_baseline_returns_none(self):
        result = _vector_result(_vector_log(short_baseline=True))
        assert result.vector_pct is None

    def test_worst_compass_instance_wins(self):
        impact = np.array([250.0, 180.0, 200.0])
        result = _vector_result(_vector_log((None, impact)))
        assert result.vector_label == "MAG1"
        assert result.vector_pct == pytest.approx(
            np.linalg.norm(impact) / 530.0 * 100.0, rel=1e-6)

    def test_primary_current_is_normalized_like_compassmot(self):
        reader = _vector_log((np.array([7.0, 7.0, 7.0]),), current=True)
        result = _vector_result(reader)
        # current=40*throttle, so current_max/throttle_max is 40 A.
        expected = 7.0 * np.sqrt(3.0) * 40.0 / 530.0 * 100.0
        assert result.vector_driver == "BAT.Curr"
        assert result.vector_pct == pytest.approx(expected, rel=1e-6)

    def test_missing_bat_instance_falls_back_to_rcou(self):
        reader = _vector_log((np.array([7.0, 7.0, 7.0]),), current=True)
        del reader._messages["BAT"]["Inst"]
        analyzer = CompassAnalyzer(reader, Thresholds(),
                                   _info(4, False, "ArduCopter"))
        assert analyzer._primary_current() is None
        assert _vector_result(reader).vector_driver == "RCOU"


def test_pure_plane_aileron_dither_does_not_suggest_spin_min():
    samples = 20000                      # 200 Hz, 100 Hz of zero crossings
    rcou = {"_timestamp": np.linspace(0.0, 100.0, samples),
            "C1": 1500.0 + 100.0 * (-1.0) ** np.arange(samples)}
    params = _servo_params({1: 4, 2: 19, 3: 70, 4: 21}, Q_ENABLE=0)
    reader = SyntheticReader({"RCOU": rcou}, params, vehicle_type="ArduPlane")
    result = ServoDitherAnalyzer(reader, Thresholds(),
                                 _info(0, False)).analyze()
    assert result.status in ("warning", "critical"), result.details
    assert not result.param_suggestions
