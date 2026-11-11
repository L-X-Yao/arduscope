"""Regression tests for the rescan-2026-11-05 batch E (S4-10 .. S4-31)."""

from __future__ import annotations

import warnings

import numpy as np

from analyzers.airspeed import AirspeedAnalyzer
from analyzers.attitude import AttitudeAnalyzer
from analyzers.cruise_efficiency import CruiseEfficiencyAnalyzer
from analyzers.battery import BatteryAnalyzer
from analyzers.vtol_transition import VTOLTransitionAnalyzer
from analyzers.rangefinder import RangefinderAnalyzer
from types import SimpleNamespace

from lib.i18n import t as _t
from lib.log_reader import ModeChange
from lib.vehicle_detect import _detect_board_name
from tests.test_synthetic_coverage import (
    SyntheticReader, _thresholds, _vehicle,
)


def _att_reader(des_roll, roll, des_yaw=None, yaw=None, armed=None):
    n = len(roll)
    reader = SyntheticReader(
        messages={"ATT": {
            "_timestamp": np.linspace(0, 120, n),
            "DesRoll": np.asarray(des_roll, dtype=float),
            "Roll": np.asarray(roll, dtype=float),
            "DesPitch": np.zeros(n),
            "Pitch": np.zeros(n),
            "DesYaw": np.full(n, 90.0) if des_yaw is None else des_yaw,
            "Yaw": np.full(n, 90.0) if yaw is None else yaw,
        }},
        params={"ATC_RAT_RLL_P": 0.135, "ATC_RAT_PIT_P": 0.135},
    )
    # SyntheticReader turns [] into the whole log; set it afterwards.
    reader._armed = armed if armed is not None else [(0.0, 120.0)]
    return reader


def _run_att(reader):
    return AttitudeAnalyzer(reader, _thresholds(reader), _vehicle()).analyze()


class TestAttitudeArmedFallback:
    def test_empty_armed_intervals_analyses_whole_log(self):
        # 10 deg of constant roll error, far past the critical threshold.
        n = 300
        reader = _att_reader(np.zeros(n), np.full(n, 10.0), armed=[])
        assert reader.get_armed_intervals() == []
        result = _run_att(reader)
        assert result.status == "critical"
        assert _t("attitude.insufficient_data") not in result.summary


    def test_log_that_never_armed_is_not_graded(self):
        # POWR shows the vehicle sat disarmed (bench log): hand-moved
        # attitude there is not tracking error.
        n = 300
        reader = _att_reader(np.zeros(n), np.full(n, 10.0), armed=[])
        reader.never_armed = lambda: True
        assert reader.get_armed_intervals() == []
        result = _run_att(reader)
        assert result.status == "info"
        assert result.summary == _t("attitude.never_armed")


class TestAttitudeEmptySlice:
    def test_missing_desired_yaw_raises_no_runtime_warning(self):
        # S9-21: with no DesYaw column the yaw error is all NaN.
        n = 300
        reader = _att_reader(np.zeros(n), np.zeros(n),
                             des_yaw=np.full(n, np.nan))
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = _run_att(reader)
        assert result.status == "good"
        assert any("N/A" in d for d in result.details)


class TestAttitudeWrap:
    def test_roll_across_180_is_one_degree_not_359(self):
        # S4-24: desired +179, actual -180 is 1 degree apart.
        n = 300
        reader = _att_reader(np.full(n, 179.0), np.full(n, -180.0))
        result = _run_att(reader)
        assert result.status == "good"


class TestRangefinderOutOfRangeHigh:
    def test_target_beyond_range_is_not_an_unreliable_reading(self):
        # S4-25: Stat 3 (OutOfRangeHigh) means the ground is further than
        # the sensor's maximum, for example a copter above its range.
        n = 200
        reader = SyntheticReader(
            messages={"RFND": {
                "_timestamp": np.linspace(0, 60, n),
                "I": np.zeros(n),
                "Dist": np.full(n, 7.0),
                "Stat": np.full(n, 3.0),
                "Orient": np.full(n, 25.0),
            }},
            params={"RNGFND1_MAX_CM": 700},
        )
        result = RangefinderAnalyzer(reader, _thresholds(reader),
                                     _vehicle()).analyze()
        assert any("100.0%" in d for d in result.details)
        assert result.status == "good"


class TestBatteryWithoutInst:
    def test_bat_without_inst_column_is_analysed_as_one_battery(self):
        # S4-26: a BAT message that has no Inst column. 10 V on a 4S-class
        # pack is far below any low-voltage limit.
        n = 200
        reader = SyntheticReader(
            messages={"BAT": {
                "_timestamp": np.linspace(0, 100, n),
                "Volt": np.full(n, 10.0),
                "Curr": np.full(n, 5.0),
                "CurrTot": np.linspace(0, 100, n),
                "Rem": np.full(n, 50.0),
            }},
            params={"BATT_LOW_VOLT": 14.0, "BATT_CRT_VOLT": 13.0,
                    "BATT_CAPACITY": 5000},
        )
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert result.status in ("warning", "critical")
        assert result.evidence_source != ""


def _dual_arsp_reader(n0, n1, as1):
    ts0 = np.linspace(0, 100, n0)
    ts1 = np.linspace(0, 100, n1)
    ts = np.concatenate([ts0, ts1])
    order = np.argsort(ts, kind="stable")
    return SyntheticReader(
        messages={"ARSP": {
            "_timestamp": ts[order],
            "I": np.concatenate([np.zeros(n0), np.ones(n1)])[order],
            "Airspeed": np.concatenate([np.full(n0, 20.0), as1])[order],
            "DiffPress": np.full(n0 + n1, 200.0),
            "Temp": np.full(n0 + n1, 20.0),
            "RawPress": np.full(n0 + n1, 200.0),
            "Offset": np.zeros(n0 + n1),
            "U": np.ones(n0 + n1),
            "Health": np.ones(n0 + n1),
            "Primary": np.zeros(n0 + n1),
        }},
        params={},
        vehicle_type="ArduPlane",
    )


class TestDualAirspeedShapes:
    def test_second_sensor_with_fewer_samples_does_not_raise(self):
        # S4-27: 100 samples on sensor 0, 60 on sensor 1 of which none is
        # a number, so there is nothing to interpolate.
        reader = _dual_arsp_reader(100, 60, np.full(60, np.nan))
        result = AirspeedAnalyzer(reader, _thresholds(reader),
                                  _vehicle("ArduPlane")).analyze()
        assert result.status in ("good", "info", "warning", "critical")

    def test_different_sample_counts_are_compared_on_one_time_base(self):
        # Sensor 1 reads 10 m/s below sensor 0 at a different rate:
        # 10 m/s rms is far past the dual-sensor critical threshold.
        reader = _dual_arsp_reader(100, 60, np.full(60, 10.0))
        result = AirspeedAnalyzer(reader, _thresholds(reader),
                                  _vehicle("ArduPlane")).analyze()
        assert result.status == "critical"


class TestCruiseEfficiencyMultiBattery:
    def test_total_power_sums_all_battery_instances(self):
        # S4-29: 37 V x 9 A = 333 W on battery 0 and 37 V x 3 A = 111 W
        # on battery 1, so the vehicle draws 444 W.
        n = 200
        ts = np.linspace(0, 100, n)
        reader = SyntheticReader(
            messages={"BAT": {
                "_timestamp": np.repeat(ts, 2),
                "Inst": np.tile([0.0, 1.0], n),
                "Volt": np.full(2 * n, 37.0),
                "Curr": np.tile([9.0, 3.0], n),
            }},
            params={},
            vehicle_type="ArduPlane",
        )
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle("ArduPlane")).analyze()
        assert any("444.0 W" in d for d in result.details)
        # 444 W for 100 s
        assert any("12.3 Wh" in d for d in result.details)


class TestBoardName:
    @staticmethod
    def _reader(texts):
        return SimpleNamespace(
            text_messages=[SimpleNamespace(text=x) for x in texts],
            get_param=lambda name, default=0.0: default,
        )

    def test_board_name_is_the_boot_banner_not_the_kernel_hash(self):
        # S4-31: the log carries both lines; the ChibiOS one is a git hash.
        reader = self._reader([
            "ChibiOS: 5d8f2c1b",
            "CubeOrange 00500061 30305112 33343536",
        ])
        assert _detect_board_name(reader) == "CubeOrange"

    def test_kernel_hash_alone_is_not_a_board_name(self):
        reader = self._reader(["ChibiOS: 5d8f2c1b"])
        assert _detect_board_name(reader) == "Unknown"


def _qtun_reader(alt_loss, modes, backtrans_ms=None):
    ts = np.linspace(0, 100, 1001)
    # Altitude falls by alt_loss over the 3 s after t=50.
    alt = 100.0 - alt_loss * np.clip((ts - 50.0) / 3.0, 0.0, 1.0)
    n = len(ts)
    params = {"Q_ENABLE": 1}
    if backtrans_ms is not None:
        params["Q_BACKTRANS_MS"] = backtrans_ms
    reader = SyntheticReader(
        messages={"QTUN": {
            "_timestamp": ts,
            "TMix": np.zeros(n), "Trn": np.zeros(n), "Alt": alt,
            "DAlt": np.full(n, 100.0), "CRt": np.zeros(n),
            "DCRt": np.zeros(n),
        }},
        params=params,
        vehicle_type="ArduPlane",
    )
    reader.mode_list = [ModeChange(tm, name, num) for tm, name, num in modes]
    return reader


class TestVtolBackTransition:
    def test_altitude_loss_after_switch_to_vtol_mode_is_reported(self):
        # S4-28: FBWA -> QLOITER at t=50, 20 m lost in the next 3 s.
        # 20 m is past the 10 m critical threshold.
        reader = _qtun_reader(20.0, [(0.0, "FBWA", 5), (50.0, "QLOITER", 19)])
        result = VTOLTransitionAnalyzer(
            reader, _thresholds(reader), _vehicle("ArduPlane")).analyze()
        assert result.status == "critical"
        assert result.evidence_source != ""
        assert result.action_level == "physical_check"
        assert any(_t("vtol_transition.to_hover") in d
                   for d in result.details)

    def test_small_loss_after_back_transition_stays_good(self):
        reader = _qtun_reader(1.0, [(0.0, "FBWA", 5), (50.0, "QLOITER", 19)])
        result = VTOLTransitionAnalyzer(
            reader, _thresholds(reader), _vehicle("ArduPlane")).analyze()
        assert result.status == "good"
        assert any(_t("vtol_transition.to_hover") in d
                   for d in result.details)

    def test_vtol_to_vtol_mode_change_is_not_a_back_transition(self):
        reader = _qtun_reader(
            20.0, [(0.0, "QHOVER", 18), (50.0, "QLOITER", 19)])
        result = VTOLTransitionAnalyzer(
            reader, _thresholds(reader), _vehicle("ArduPlane")).analyze()
        assert result.status == "good"
