"""Suggestions must be justified, in range, and never hide a disagreement."""

from __future__ import annotations

import numpy as np
import pytest

import flight_report
from analyzers.compass_consistency import CompassConsistencyAnalyzer
from analyzers.log_health import LogHealthAnalyzer
from lib.log_reader import Event
from lib.param_conflict import (
    detect_param_conflicts,
    distinct_values,
    merge_suggestions,
)
from lib.param_limits import limit_suggestions, param_range
from lib.results import AnalysisResult
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast


def _suggestion(param, current, suggested) -> dict:
    return {"param": param, "current": current, "suggested": suggested,
            "reason": "r"}


class TestParamLimits:
    @pytest.mark.parametrize("name,low,high", [
        ("INS_HNTCH_BW", 5.0, 250.0),
        ("INS_HNTC2_FREQ", 10.0, 495.0),
        ("ATC_RAT_RLL_I", 0.01, 2.0),
        ("Q_A_RAT_YAW_D", 0.0, 0.02),
        ("RLL_RATE_P", 0.08, 0.35),
        ("Q_M_THST_HOVER", 0.125, 0.6875),
    ])
    def test_known_ranges(self, name, low, high):
        assert param_range(name) == (low, high)

    def test_unknown_parameter_has_no_range(self):
        assert param_range("LOG_BITMASK") is None

    def test_prefix_of_a_longer_name_does_not_match(self):
        assert param_range("INS_HNTCH_BW_EXTRA") is None

    def test_value_past_the_limit_is_pulled_back(self):
        kept = limit_suggestions([_suggestion("INS_HNTCH_BW", 40, 290)])
        assert kept[0]["suggested"] == 250.0

    def test_current_already_out_of_range_drops_the_suggestion(self):
        assert limit_suggestions(
            [_suggestion("RLL_RATE_P", 0.58, 0.65)]) == []

    def test_limit_that_leaves_no_change_drops_the_suggestion(self):
        assert limit_suggestions(
            [_suggestion("ATC_RAT_RLL_I", 0.01, 0.007)]) == []

    def test_value_inside_the_range_is_untouched(self):
        original = _suggestion("INS_HNTCH_BW", 40, 80)
        assert limit_suggestions([original]) == [original]

    def test_non_numeric_value_is_left_alone(self):
        original = _suggestion("INS_HNTCH_FREQ", "-", "-")
        assert limit_suggestions([original]) == [original]

    @pytest.mark.parametrize("number", [np.float32, np.float64, np.int64])
    def test_numpy_numbers_are_limited_too(self, number):
        """float32 and int64 are neither int nor float to isinstance, and
        went through the limiter unchecked."""
        kept = limit_suggestions(
            [_suggestion("INS_HNTCH_BW", number(40), number(290))])
        assert kept[0]["suggested"] == 250.0


class TestLimitsThroughAnalysisResult:
    def test_every_result_is_limited(self):
        result = AnalysisResult(
            title="x", action_level="apply_param",
            param_suggestions=[_suggestion("INS_HNTCH_BW", 40, 290)])
        assert result.param_suggestions[0]["suggested"] == 250.0

    def test_action_falls_back_when_nothing_is_left(self):
        result = AnalysisResult(
            title="x", status="warning", action_level="apply_param",
            param_suggestions=[_suggestion("RLL_RATE_P", 0.58, 0.65)])
        assert result.param_suggestions == []
        assert result.action_level == "physical_check"


def _vehicle(vehicle_type="ArduCopter") -> VehicleInfo:
    return VehicleInfo(vehicle_type=vehicle_type, firmware_version="4.6.3",
                       board_name="s", frame_class=1, motor_count=4,
                       imu_count=1, is_quadplane=False)


class TestLogBitmask:
    def _suggested(self, mask, vehicle_type):
        n = 200
        ts = np.linspace(0.0, 100.0, n)
        reader = SyntheticReader(
            {"PM": {"_timestamp": ts, "Load": np.full(n, 950.0),
                    "NLon": np.zeros(n), "NL": np.full(n, 1000.0),
                    "MaxT": np.full(n, 2000.0), "Mem": np.full(n, 100000.0),
                    "LR": np.full(n, 400.0)}},
            {"LOG_BITMASK": mask, "SCHED_LOOP_RATE": 400},
            armed_intervals=[(1.0, 95.0)], vehicle_type=vehicle_type)
        reader.events = [Event(1.0, 10), Event(95.0, 11)]
        result = LogHealthAnalyzer(reader, Thresholds(),
                                   _vehicle(vehicle_type)).analyze()
        assert result.status in ("warning", "critical")     # precondition
        return [s["suggested"] for s in result.param_suggestions
                if s["param"] == "LOG_BITMASK"], result

    def test_plane_on_its_default_gets_no_suggestion(self):
        suggested, result = self._suggested(65535, "ArduPlane")
        assert suggested == []
        assert result.action_level == "physical_check"

    def test_plane_above_its_default_goes_back_to_it(self):
        assert self._suggested(0x1FFFF, "ArduPlane")[0] == [65535]

    def test_copter_above_its_default_goes_back_to_it(self):
        assert self._suggested(0xFFFFF, "ArduCopter")[0] == [176126]

    def test_logging_less_than_default_is_never_raised(self):
        assert self._suggested(1, "ArduCopter")[0] == []
        assert self._suggested(1, "ArduPlane")[0] == []

    def test_other_vehicles_get_no_number(self):
        assert self._suggested(0xFFFFF, "ArduRover")[0] == []


class TestCompassOutlier:
    N = 300

    def _mag(self, offsets: dict[int, float]) -> dict:
        ts, inst, x = [], [], []
        base = np.linspace(0.0, 100.0, self.N)
        for instance, offset in offsets.items():
            ts.append(base)
            inst.append(np.full(self.N, float(instance)))
            x.append(np.full(self.N, 400.0 + offset))
        order = np.argsort(np.concatenate(ts), kind="stable")
        n = self.N * len(offsets)
        return {"_timestamp": np.concatenate(ts)[order],
                "I": np.concatenate(inst)[order],
                "MagX": np.concatenate(x)[order],
                "MagY": np.zeros(n), "MagZ": np.zeros(n)}

    def _run(self, offsets, params=None):
        reader = SyntheticReader({"MAG": self._mag(offsets)}, params or {})
        return CompassConsistencyAnalyzer(reader, Thresholds(),
                                          _vehicle()).analyze()

    @pytest.mark.parametrize("bad,param", [(0, "COMPASS_USE"),
                                           (1, "COMPASS_USE2"),
                                           (2, "COMPASS_USE3")])
    def test_third_compass_identifies_the_bad_one(self, bad, param):
        result = self._run({i: 400.0 if i == bad else 0.0 for i in range(3)})
        assert result.status == "critical"
        assert [s["param"] for s in result.param_suggestions] == [param]

    def test_two_compasses_cannot_say_which(self):
        result = self._run({0: 0.0, 1: 400.0})
        assert result.status == "critical"
        assert result.param_suggestions == []

    def test_three_way_disagreement_names_nobody(self):
        result = self._run({0: 0.0, 1: 400.0, 2: 900.0})
        assert result.status == "critical"
        assert result.param_suggestions == []

    def test_already_disabled_compass_is_not_suggested(self):
        result = self._run({0: 0.0, 1: 0.0, 2: 400.0}, {"COMPASS_USE3": 0})
        assert result.param_suggestions == []


class TestConflicts:
    @staticmethod
    def _result(title, value, param="MOT_SPIN_MIN"):
        return AnalysisResult(
            title=title, action_level="apply_param",
            param_suggestions=[_suggestion(param, 0.15, value)])

    def test_agreement_collapses_to_one_row(self):
        merged, conflicts = merge_suggestions(
            [self._result("a", 0.17), self._result("b", 0.17)])
        assert [s["param"] for s in merged] == ["MOT_SPIN_MIN"]
        assert conflicts == []

    def test_disagreement_is_not_applied(self):
        merged, conflicts = merge_suggestions(
            [self._result("a", 0.17), self._result("b", 0.23),
             self._result("c", 40, "INS_HNTCH_BW")])
        assert [s["param"] for s in merged] == ["INS_HNTCH_BW"]
        assert [c["param"] for c in conflicts] == ["MOT_SPIN_MIN"]

    def test_distinct_values_skip_repeats_of_the_same_value(self):
        parent = AnalysisResult(
            title="pid", action_level="apply_param",
            param_suggestions=[_suggestion("X_P", 1, 2)])
        parent.sub_results = [AnalysisResult(
            title="roll", action_level="apply_param",
            param_suggestions=[_suggestion("X_P", 1, 2)])]
        other = AnalysisResult(
            title="notch", action_level="apply_param",
            param_suggestions=[_suggestion("X_P", 1, 3)])
        conflict = detect_param_conflicts([parent, other])[0]
        assert [e["value"] for e in distinct_values(conflict)] == [2, 3]

    def test_param_file_lists_conflicts_as_comments(self, tmp_path):
        report = tmp_path / "r.html"
        flight_report._export_param_file(
            {"a": self._result("motor", 0.23),
             "b": self._result("dither", 0.17),
             "c": self._result("notch", 40, "INS_HNTCH_BW")}, str(report))
        lines = (tmp_path / "r.param").read_text(encoding="utf-8").splitlines()
        assert "INS_HNTCH_BW,40" in lines
        assert not any(line.startswith("MOT_SPIN_MIN") for line in lines)
        comment = next(ln for ln in lines if "CONFLICT" in ln)
        assert "motor=0.23" in comment and "dither=0.17" in comment
