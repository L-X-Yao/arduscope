"""
SyntheticReader-based tests for the QuadPlane analyzers:
tecs_tuning, cruise_efficiency.
"""

from __future__ import annotations

import numpy as np

from analyzers.tecs_tuning import TECSTuningAnalyzer
from analyzers.cruise_efficiency import CruiseEfficiencyAnalyzer
from analyzers.vtol_transition import VTOLTransitionAnalyzer
from lib.i18n import t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader


def _vehicle(vt: str = "ArduPlane", qp: bool = False) -> VehicleInfo:
    return VehicleInfo(
        vehicle_type=vt,
        firmware_version="4.6.3",
        board_name="synthetic",
        frame_class=1,
        motor_count=4,
        imu_count=2,
        is_quadplane=qp,
    )


def _thresholds(reader: SyntheticReader) -> Thresholds:
    return Thresholds.from_params(reader.params, reader.vehicle_type)


# =====================================================================
# TECSTuningAnalyzer
# =====================================================================

class TestTECSTuning:

    def _build_reader(self, n=500, h_err=0.0, s_err=0.0, params=None):
        ts = np.linspace(0, 300, n)
        hdem = np.full(n, 100.0)
        hgt = np.full(n, 100.0) + np.random.normal(0, h_err, n) if h_err > 0 else np.full(n, 100.0)
        spdem = np.full(n, 20.0)
        spd = np.full(n, 20.0) + np.random.normal(0, s_err, n) if s_err > 0 else np.full(n, 20.0)
        return SyntheticReader(
            messages={
                "TECS": {
                    "_timestamp": ts,
                    "hdem": hdem,
                    "hgt": hgt,
                    "spdem": spdem,
                    "spd": spd,
                },
                "STAT": {
                    "_timestamp": ts,
                    "Stage": np.full(n, 3.0),
                    "isFlying": np.ones(n),
                },
                "ARSP": {"_timestamp": ts, "U": np.ones(n)},
            },
            params=params if params is not None else {"TECS_PITCH_MAX": 20, "TECS_SPDWEIGHT": 1.0},
            vehicle_type="ArduPlane",
        )

    def test_good_tracking(self):
        reader = self._build_reader(h_err=1.0, s_err=0.5)
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        assert analyzer.can_run()
        result = analyzer.analyze()
        assert result.status == "good"
        assert result.action_level == "ok"
        assert result.param_suggestions == []

    def test_height_warning(self):
        reader = self._build_reader(h_err=6.0, s_err=0.5)
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "warning"
        assert result.evidence_source != ""

    def test_height_critical(self):
        reader = self._build_reader(h_err=12.0, s_err=0.5)
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "critical"
        # hdem hides a pitch limit, so height error names no parameter.
        assert result.param_suggestions == []
        assert result.action_level == "physical_check"

    def test_speed_warning(self):
        reader = self._build_reader(h_err=1.0, s_err=4.0)
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "warning"

    def test_speed_critical(self):
        reader = self._build_reader(h_err=1.0, s_err=6.0)
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "critical"
        assert any(s["param"] == "TECS_SPDWEIGHT"
                   for s in result.param_suggestions)

    def test_both_critical(self):
        reader = self._build_reader(h_err=12.0, s_err=6.0)
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "critical"
        assert result.action_level == "apply_param"

    def test_empty_tecs(self):
        reader = SyntheticReader(
            messages={"TECS": {"_timestamp": np.array([])}},
            vehicle_type="ArduPlane",
        )
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "info"

    def test_produces_figures(self):
        reader = self._build_reader(h_err=1.0, s_err=0.5)
        result = TECSTuningAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert len(result.figures) == 2

    def test_copter_cannot_run(self):
        reader = self._build_reader()
        copter = VehicleInfo(
            vehicle_type="ArduCopter",
            firmware_version="4.6.3",
            board_name="synthetic",
            frame_class=1,
            motor_count=4,
            imu_count=2,
            is_quadplane=False,
        )
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), copter)
        assert not analyzer.can_run()

    def test_quadplane_can_run(self):
        reader = self._build_reader()
        analyzer = TECSTuningAnalyzer(
            reader, _thresholds(reader), _vehicle(qp=True))
        assert analyzer.can_run()

    def test_no_param_suggestions_when_params_missing(self):
        reader = self._build_reader(h_err=1.0, s_err=6.0, params={})
        analyzer = TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle())
        result = analyzer.analyze()
        assert result.status == "critical"
        # No TECS_SPDWEIGHT param available, so no suggestion for it
        assert result.param_suggestions == []


TAKEOFF, VTOL, NORMAL, LAND = 1.0, 2.0, 3.0, 4.0
SPEED_DEMAND = 20.0


def _flight(segments: list[tuple], extra: dict | None = None) -> SyntheticReader:
    """TECS at 10 Hz and STAT at 5 Hz.

    Each segment is (seconds, stage, flying, airspeed). Height tracks its
    demand throughout, so speed is the only error.
    """
    seconds = sum(s[0] for s in segments)
    ts = np.arange(0.0, seconds, 0.1)
    stat_ts = np.arange(0.0, seconds, 0.2)
    edges = np.cumsum([s[0] for s in segments])
    tecs_seg = np.searchsorted(edges, ts, side="right")
    stat_seg = np.searchsorted(edges, stat_ts, side="right")
    return SyntheticReader(
        messages={
            "TECS": {
                "_timestamp": ts,
                "hdem": np.full(len(ts), 100.0),
                "hgt": np.full(len(ts), 100.0),
                "spdem": np.full(len(ts), SPEED_DEMAND),
                "spd": np.array([s[3] for s in segments])[tecs_seg],
            },
            "STAT": {
                "_timestamp": stat_ts,
                "Stage": np.array([s[1] for s in segments])[stat_seg],
                "isFlying": np.array([float(s[2]) for s in segments])[stat_seg],
            },
            "ARSP": {"_timestamp": stat_ts, "U": np.ones(len(stat_ts))},
            **(extra or {}),
        },
        params={"TECS_PITCH_MAX": 20, "TECS_SPDWEIGHT": 1.0},
        vehicle_type="ArduPlane",
    )


def _analyze(reader: SyntheticReader):
    return TECSTuningAnalyzer(
        reader, _thresholds(reader), _vehicle(qp=True)).analyze()


def _speed_rms(result) -> float:
    return next(e["value"] for e in result.evidence
                if e["field"] == "speed_rms")


def _whole_log_speed_rms(reader: SyntheticReader) -> float:
    tecs = reader.get_messages_as_arrays("TECS", ["spdem", "spd"])
    return float(np.sqrt(np.mean((tecs["spdem"] - tecs["spd"]) ** 2)))


class TestTECSCruiseRows:
    """A row counts only if the aircraft was cruising on its wing."""

    def _assert_only_cruise_graded(self, reader: SyntheticReader):
        # Grading every row would have been critical.
        assert _whole_log_speed_rms(reader) >= 5.0
        result = _analyze(reader)
        assert result.status == "good"
        assert _speed_rms(result) == 0.0
        assert result.param_suggestions == []

    def test_vtol_stage_is_left_out(self):
        self._assert_only_cruise_graded(_flight(
            [(60, VTOL, 1, 0.0), (120, NORMAL, 1, SPEED_DEMAND)]))

    def test_takeoff_and_landing_are_left_out(self):
        self._assert_only_cruise_graded(_flight(
            [(40, TAKEOFF, 1, 30.0), (120, NORMAL, 1, SPEED_DEMAND),
             (60, LAND, 1, 3.0)]))

    def test_ground_is_left_out(self):
        # An armed plane waiting in a TECS mode reads NORMAL too.
        self._assert_only_cruise_graded(_flight(
            [(60, NORMAL, 0, 0.0), (120, NORMAL, 1, SPEED_DEMAND)]))

    def test_qtun_rows_are_left_out_of_normal_stage(self):
        # Outside AUTO, Plane::update_flight_stage reports NORMAL even
        # with the VTOL motors running. QTUN logs only while they run.
        self._assert_only_cruise_graded(_flight(
            [(60, NORMAL, 1, 0.0), (120, NORMAL, 1, SPEED_DEMAND)],
            extra={"QTUN": {"_timestamp": np.arange(0.0, 60.0, 0.04)}}))

    def test_q_mode_is_left_out_of_normal_stage(self):
        # QRTL = 21, CRUISE = 7, with no QTUN to mark the back transition.
        self._assert_only_cruise_graded(_flight(
            [(120, NORMAL, 1, SPEED_DEMAND), (60, NORMAL, 1, 3.0)],
            extra={"MODE": {"_timestamp": np.array([0.0, 120.0]),
                            "Mode": np.array([7.0, 21.0])}}))

    def test_poor_cruise_is_still_critical(self):
        reader = _flight([(60, VTOL, 1, 0.0),
                          (120, NORMAL, 1, SPEED_DEMAND - 6.0)])
        result = _analyze(reader)

        assert result.status == "critical"
        assert _speed_rms(result) == 6.0
        assert any(s["param"] == "TECS_SPDWEIGHT"
                   for s in result.param_suggestions)

    def test_rotor_only_flight_is_not_graded(self):
        reader = _flight([(60, VTOL, 1, 0.0)])
        assert _whole_log_speed_rms(reader) >= 5.0

        result = _analyze(reader)

        assert result.status == "info"
        assert result.summary == t("tecs_tuning.no_cruise", min_s="10")
        assert result.param_suggestions == []
        assert result.figures == []

    def test_a_sliver_of_cruise_is_not_graded(self):
        result = _analyze(_flight([(60, VTOL, 1, 0.0),
                                   (9, NORMAL, 1, SPEED_DEMAND - 6.0)]))

        assert result.status == "info"
        assert result.param_suggestions == []

    def test_cruise_floor_counts_flown_seconds(self):
        result = _analyze(_flight([(60, VTOL, 1, 0.0),
                                   (11, NORMAL, 1, SPEED_DEMAND - 6.0)]))

        assert result.status == "critical"

    def test_log_without_stat_is_not_graded(self):
        reader = _flight([(120, NORMAL, 1, 0.0)])
        del reader._messages["STAT"]

        result = _analyze(reader)

        assert result.status == "info"
        assert result.param_suggestions == []

    def test_stat_without_stage_is_not_graded(self):
        # Plane logged no flight stage before 2015.
        reader = _flight([(120, NORMAL, 1, 0.0)])
        reader._messages["STAT"]["Stage"][:] = np.nan

        assert _analyze(reader).status == "info"

    def test_figures_show_what_was_graded(self):
        result = _analyze(_flight([(60, VTOL, 1, 0.0),
                                   (120, NORMAL, 1, SPEED_DEMAND)]))

        actual_speed = np.asarray(result.figures[1].data[1].y, dtype=float)
        assert len(actual_speed) == 1800
        assert np.all(np.isnan(actual_speed[:600]))
        assert np.all(actual_speed[601:] == SPEED_DEMAND)

    def test_details_say_how_much_was_graded(self):
        result = _analyze(_flight([(60, VTOL, 1, 0.0),
                                   (120, NORMAL, 1, SPEED_DEMAND)]))

        assert t("tecs_tuning.cruise_time",
                 cruise_s="120", total_s="180") in result.details


# =====================================================================
# CruiseEfficiencyAnalyzer
# =====================================================================

class TestCruiseEfficiency:

    def _build_reader(self, n=500, volt=22.0, curr=10.0,
                      with_airspeed=False, with_gps=False,
                      params=None):
        ts = np.linspace(0, 300, n)
        v = np.full(n, volt)
        c = np.full(n, curr)
        messages = {
            "BAT": {
                "_timestamp": ts,
                "Volt": v,
                "Curr": c,
            },
        }
        if with_airspeed:
            spd = np.linspace(10, 25, n)
            messages["ARSP"] = {
                "_timestamp": ts,
                "Airspeed": spd,
            }
        if with_gps:
            spd = np.linspace(10, 25, n)
            messages["GPS"] = {
                "_timestamp": ts,
                "Spd": spd,
            }
        return SyntheticReader(
            messages=messages,
            params=params or {},
            vehicle_type="ArduPlane",
        )

    def test_basic_power_info(self):
        reader = self._build_reader()
        analyzer = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle())
        assert analyzer.can_run()
        result = analyzer.analyze()
        assert result.status == "info"
        assert result.action_level == "ok"
        assert len(result.figures) >= 1

    def test_with_airspeed_scatter(self):
        reader = self._build_reader(with_airspeed=True)
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"
        assert len(result.figures) == 2

    def test_with_gps_speed_fallback(self):
        reader = self._build_reader(with_gps=True)
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"
        assert len(result.figures) == 2
        assert any("GPS" in d for d in result.details)

    def test_energy_calculation(self):
        reader = self._build_reader(volt=22.0, curr=10.0, n=1000)
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        # 220W * 300s / 3600 = 18.33 Wh
        assert any("Wh" in d for d in result.details)

    def test_empty_bat(self):
        reader = SyntheticReader(
            messages={"BAT": {"_timestamp": np.array([])}},
            vehicle_type="ArduPlane",
        )
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_nan_current(self):
        n = 100
        ts = np.linspace(0, 60, n)
        reader = SyntheticReader(
            messages={"BAT": {
                "_timestamp": ts,
                "Volt": np.full(n, 22.0),
                "Curr": np.full(n, np.nan),
            }},
            vehicle_type="ArduPlane",
        )
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_copter_cannot_run(self):
        reader = self._build_reader()
        copter = VehicleInfo(
            vehicle_type="ArduCopter",
            firmware_version="4.6.3",
            board_name="synthetic",
            frame_class=1,
            motor_count=4,
            imu_count=2,
            is_quadplane=False,
        )
        analyzer = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), copter)
        assert not analyzer.can_run()

    def test_quadplane_can_run(self):
        reader = self._build_reader()
        analyzer = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle(qp=True))
        assert analyzer.can_run()

    def test_single_sample_bat(self):
        reader = SyntheticReader(
            messages={"BAT": {
                "_timestamp": np.array([0.0]),
                "Volt": np.array([22.0]),
                "Curr": np.array([10.0]),
            }},
            vehicle_type="ArduPlane",
        )
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "info"

    def test_never_warning_or_critical(self):
        reader = self._build_reader(volt=5.0, curr=100.0, with_airspeed=True)
        result = CruiseEfficiencyAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status in ("info", "good")
        assert result.action_level == "ok"


class TestSpeedWeightNeedsAirspeed:
    """AP_TECS::use_airspeed: without it TECS_SPDWEIGHT is forced to 0."""

    @staticmethod
    def _result(arsp_used, params=None):
        n = 2000
        ts = np.linspace(0.0, 200.0, n)
        messages = {
            "TECS": {"_timestamp": ts, "hdem": np.full(n, 100.0),
                     "hgt": np.full(n, 100.0), "h": np.full(n, 100.0),
                     "spdem": np.full(n, 20.0), "spd": np.full(n, 8.0),
                     "sp": np.full(n, 8.0)},
            "STAT": {"_timestamp": ts, "Stage": np.full(n, 3.0),
                     "isFlying": np.ones(n)},
        }
        if arsp_used is not None:
            messages["ARSP"] = {"_timestamp": ts,
                                "U": np.full(n, float(arsp_used))}
        reader = SyntheticReader(
            messages=messages,
            params={"TECS_SPDWEIGHT": 1.0, **(params or {})},
            vehicle_type="ArduPlane")
        return TECSTuningAnalyzer(reader, _thresholds(reader), _vehicle()).analyze()

    @staticmethod
    def _suggests_it(result):
        return any(s["param"] == "TECS_SPDWEIGHT" for s in result.param_suggestions)

    def test_a_sensor_in_use_gets_the_suggestion(self):
        assert self._suggests_it(self._result(1))

    def test_a_sensor_not_in_use_gets_none(self):
        result = self._result(0)
        assert result.status != "good"
        assert not self._suggests_it(result)
        assert t("tecs_tuning.no_airspeed") in result.details

    def test_no_sensor_at_all_gets_none(self):
        assert not self._suggests_it(self._result(None))

    def test_synthetic_airspeed_counts(self):
        assert self._suggests_it(self._result(None, {"TECS_SYNAIRSPEED": 1}))


class TestAssistReport:
    N = 1000

    def _detail(self, ast, ts=None):
        ts = np.linspace(0.0, 100.0, self.N) if ts is None else ts
        reader = SyntheticReader(
            messages={"QTUN": {"_timestamp": ts, "Ast": np.asarray(ast, float)}},
            params={"Q_ENABLE": 1}, vehicle_type="ArduPlane")
        return VTOLTransitionAnalyzer(
            reader, _thresholds(reader), _vehicle())._assist_detail()

    def test_no_assist_is_not_reported(self):
        assert self._detail(np.zeros(self.N)) is None

    def test_firmware_without_the_field_is_not_reported(self):
        assert self._detail(np.full(self.N, np.nan)) is None

    def test_time_periods_and_causes(self):
        ast = np.zeros(self.N)
        ast[100:200] = 1 | 4      # low speed, about 10 s
        ast[500:550] = 1 | 16     # attitude error, about 5 s
        expected = t("vtol_transition.assist_used", sec="15.0", count=2,
                     speed="10.0", alt="0.0", angle="5.0", forced="0.0")
        assert self._detail(ast) == expected

    def test_a_cause_alone_is_not_assist(self):
        # Bit 0 is the assist itself. The others only say why.
        assert self._detail(np.full(self.N, 4.0)) is None

    def test_a_gap_in_the_rows_is_not_assist_time(self):
        ts = np.concatenate([np.arange(0.0, 5.0, 0.1), 500.0 + np.arange(0.0, 5.0, 0.1)])
        detail = self._detail(np.ones(len(ts)), ts)
        assert t("vtol_transition.assist_used", sec="10.8", count=1, speed="0.0",
                 alt="0.0", angle="0.0", forced="0.0") == detail

