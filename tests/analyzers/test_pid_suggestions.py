"""The tracking ratio has no direction, so it must never move a gain."""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.pid_tuning import PIDTuningAnalyzer
from lib.i18n import t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from test_synthetic_coverage import SyntheticReader

N = 2000
TS = np.linspace(0.0, 100.0, N)


def _vehicle(vehicle_type: str, quadplane: bool = False) -> VehicleInfo:
    return VehicleInfo(vehicle_type=vehicle_type, firmware_version="4.6.3",
                       board_name="s", frame_class=1, motor_count=4,
                       imu_count=1, is_quadplane=quadplane)


def _pid(tar: np.ndarray, act: np.ndarray) -> dict:
    return {"_timestamp": TS, "Tar": tar, "Act": act, "P": 0.3 * tar,
            "I": 0.05 + 0.001 * np.sin(TS), "D": np.full(N, 0.01),
            "FF": np.full(N, 0.02)}


def _run(messages, params=None, vehicle_type="ArduCopter", quadplane=False):
    flying = {"POS": {"_timestamp": TS, "Alt": np.linspace(0.0, 80.0, N)}}
    reader = SyntheticReader({**flying, **messages}, params,
                             vehicle_type=vehicle_type)
    analyzer = PIDTuningAnalyzer(reader, Thresholds(),
                                 _vehicle(vehicle_type, quadplane))
    return analyzer.analyze()


def _tar() -> np.ndarray:
    return np.sin(2 * np.pi * 0.25 * TS)


GAINS = {"ATC_RAT_RLL_P": 0.2, "ATC_RAT_RLL_I": 0.2, "ATC_RAT_RLL_D": 0.004,
         "ATC_RAT_RLL_FF": 0.1}


@pytest.mark.parametrize("name,act", [
    ("no response at all", np.zeros(N)),
    ("half the commanded rate", 0.5 * _tar()),
    ("double the commanded rate", 2.0 * _tar()),
    ("uncorrelated", np.cos(2 * np.pi * 3.0 * TS)),
])
def test_no_gain_change_from_the_ratio(name, act):
    result = _run({"PIDR": _pid(_tar(), act)}, GAINS)
    axis = result.sub_results[0]
    assert any("ATC_RAT_RLL_P" in d for d in axis.details), name
    assert not [s for s in result.param_suggestions
                if s["param"].endswith(("_P", "_FF"))]


def test_poor_tracking_points_to_autotune():
    result = _run({"PIDR": _pid(_tar(), np.zeros(N))}, GAINS)
    assert result.status in ("warning", "critical")
    assert result.sub_results[0].action_level == "run_autotune"
    assert result.action_level == "run_autotune"


def _offset_held() -> dict:
    """Tracking is fine, so P has little to do and I holds most of the output."""
    tar = 0.01 * _tar()
    pid = _pid(tar, tar)
    pid["I"] = 0.2 + 0.001 * np.sin(TS)
    return {"PIDR": pid}


class TestGrowingI:
    """In SITL, ATC_RAT_YAW_I x 0.7 alone raised yaw error 22%."""

    def test_says_so_and_changes_nothing(self):
        tar = 0.01 * _tar()
        pid = _pid(tar, tar)
        pid["I"] = np.linspace(0.0, 0.2, N)
        result = _run({"PIDR": pid}, GAINS)

        axis = result.sub_results[0]
        assert t("pid_tuning.suggest_i_windup", param="ATC_RAT_RLL_I") in " ".join(
            axis.details)
        assert axis.param_suggestions == []
        assert result.param_suggestions == []


class TestHighIShare:
    """The I term holding an offset is doing its job. No gain moves for it."""

    def test_says_so_and_changes_nothing(self):
        result = _run(_offset_held(), GAINS)

        axis = result.sub_results[0]
        assert t("pid_tuning.suggest_i_high", param="ATC_RAT_RLL_I") in " ".join(
            axis.details)
        assert axis.param_suggestions == []
        assert result.param_suggestions == []

    def test_points_at_the_airframe_not_autotune(self):
        result = _run(_offset_held(), GAINS)

        assert result.status == "warning"
        assert result.sub_results[0].action_level == "physical_check"
        assert result.action_level == "physical_check"
        assert result.action_text == t("pid_tuning.check_airframe")

    def test_poor_tracking_on_another_axis_still_means_autotune(self):
        messages = {**_offset_held(), "PIDP": _pid(_tar(), np.zeros(N))}
        result = _run(messages, GAINS)

        assert result.action_level == "run_autotune"
        assert result.action_text == t("pid_tuning.autotune_steps")


class TestDTermGate:
    """D-term share alone moves no gain: the wiki lowers D to end an oscillation."""

    @staticmethod
    def _noisy_d() -> dict:
        tar = 0.01 * _tar()
        pid = _pid(tar, tar)
        pid["D"] = 0.05 * np.sin(2 * np.pi * 7.0 * TS)
        return {"PIDR": pid}

    @staticmethod
    def _rate(out: float) -> dict:
        return {"RATE": {"_timestamp": TS, "RDes": np.zeros(N),
                         "ROut": np.full(N, out)}}

    def _suggested(self, *messages) -> list:
        merged = {k: v for m in messages for k, v in m.items()}
        return [s["param"] for s in _run(merged, GAINS).param_suggestions]

    def test_share_alone_changes_nothing(self):
        assert "ATC_RAT_RLL_D" not in self._suggested(
            self._noisy_d(), self._rate(0.01))

    def test_no_rate_output_changes_nothing(self):
        assert "ATC_RAT_RLL_D" not in self._suggested(self._noisy_d())

    def test_an_oscillating_axis_gets_the_step(self):
        assert "ATC_RAT_RLL_D" in self._suggested(
            self._noisy_d(), self._rate(0.12))

    def test_fltd_gets_a_pointer_with_no_value(self):
        merged = {**self._noisy_d(), **self._rate(0.12)}
        result = _run(merged, {**GAINS, "ATC_RAT_RLL_FLTD": 30.0})
        names = [s["param"] for s in result.param_suggestions]
        assert "ATC_RAT_RLL_D" in names  # the gate was open
        assert "ATC_RAT_RLL_FLTD" not in names
        pointer = t("pid_tuning.suggest_fltd_reduce",
                    param="ATC_RAT_RLL_FLTD", cur="30.0")
        axes = [line for sub in result.sub_results for line in sub.details]
        assert any(pointer in line for line in axes)

    def test_shaking_on_the_ground_counts_by_its_peak(self):
        # A QuadPlane that oscillated and never took off: the RMS stayed low.
        out = np.zeros(N)
        out[1000:1010] = 0.6
        rate = {"RATE": {"_timestamp": TS, "RDes": np.zeros(N), "ROut": out}}
        parked = {"POS": {"_timestamp": TS, "Alt": np.zeros(N)}}
        assert "ATC_RAT_RLL_D" in self._suggested(self._noisy_d(), rate, parked)

    def test_a_quiet_d_term_is_left_alone_even_when_oscillating(self):
        tar = 0.5 * _tar()
        pid = _pid(tar, tar)
        assert "ATC_RAT_RLL_D" not in self._suggested(
            {"PIDR": pid}, self._rate(0.12))


def test_small_absolute_error_gets_no_advice():
    tar = 0.001 * _tar()                       # about 0.06 deg/s
    result = _run({"PIDR": _pid(tar, np.zeros(N))}, GAINS)
    advice = t("pid_tuning.suggest_tracking_poor", param="ATC_RAT_RLL_P")
    assert not any(advice in d for d in result.sub_results[0].details)


class TestFixedWingUnits:
    """Fixed-wing PID rows are deg/s, copter rows rad/s."""

    PARAMS = {"RLL_RATE_P": 0.1, "RLL_RATE_I": 0.1, "Q_ENABLE": 0}

    def _plane_axis(self, tar, act):
        result = _run({"PIDR": _pid(tar, act)}, self.PARAMS,
                      vehicle_type="ArduPlane")
        return result.sub_results[0]

    def test_small_deg_per_s_error_is_below_the_floor(self):
        th = Thresholds()
        tar = 5.0 * _tar()
        act = 0.3 * tar                        # ratio 0.7, about 2.5 deg/s
        rms = float(np.sqrt(np.mean((tar - act) ** 2)))
        assert rms < th.pid_tracking_abs_warn  # precondition
        assert rms / np.sqrt(np.mean(tar ** 2)) >= th.pid_tracking_warn
        assert self._plane_axis(tar, act).status == "good"

    def test_large_deg_per_s_error_is_flagged(self):
        th = Thresholds()
        tar = 40.0 * _tar()
        act = 0.3 * tar
        rms = float(np.sqrt(np.mean((tar - act) ** 2)))
        assert rms > th.pid_tracking_abs_crit  # precondition
        assert self._plane_axis(tar, act).status != "good"

    def test_same_numbers_on_a_copter_are_rad_per_s(self):
        tar = 5.0 * _tar()
        result = _run({"PIDR": _pid(tar, 0.3 * tar)}, GAINS)
        assert result.sub_results[0].status != "good"


class TestYawDamper:
    def _yaw(self):
        n = np.zeros(N)
        return {"_timestamp": TS, "Tar": n, "Act": 0.1 * _tar(), "P": n,
                "I": n, "D": 0.2 * _tar(), "FF": n}

    def test_damper_is_not_graded_as_a_rate_loop(self):
        result = _run({"PIDY": self._yaw()}, {"YAW_RATE_ENABLE": 0},
                      vehicle_type="ArduPlane")
        assert not result.sub_results
        assert result.status == "info"
        assert not result.param_suggestions

    def test_enabled_yaw_rate_loop_is_graded(self):
        result = _run({"PIDY": self._yaw()}, {"YAW_RATE_ENABLE": 1},
                      vehicle_type="ArduPlane")
        assert result.sub_results

    def test_copter_yaw_is_always_a_rate_loop(self):
        result = _run({"PIDY": self._yaw()}, {})
        assert result.sub_results


def test_manual_flight_is_excluded_on_a_pure_plane():
    """Plane::stabilize skips the rate controllers in MANUAL, so a pure
    Plane logs stale rows there exactly like a QuadPlane does."""
    tar = 200.0 * _tar()
    act = np.where(TS < 50.0, 0.0, tar)        # stale in MANUAL, then tracks
    manual_then_fbwa = {"_timestamp": np.array([0.0, 50.0]),
                        "Mode": np.array([0.0, 5.0])}
    params = {"RLL_RATE_P": 0.1, "RLL_RATE_I": 0.1}
    without_modes = _run({"PIDR": _pid(tar, act)}, params,
                         vehicle_type="ArduPlane")
    assert without_modes.sub_results[0].status == "critical"  # precondition
    result = _run({"PIDR": _pid(tar, act), "MODE": manual_then_fbwa}, params,
                  vehicle_type="ArduPlane")
    assert result.sub_results[0].status == "good"
