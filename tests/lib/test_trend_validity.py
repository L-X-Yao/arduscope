"""What the trend verdict is allowed to rest on."""

from __future__ import annotations

import html
import math
import re

import numpy as np
import pytest

from lib.i18n import t
from lib.thresholds import Thresholds
from lib.trend_analyzer import (
    TREND_IMPROVING,
    TREND_INSUFFICIENT,
    TREND_REGRESSING,
    analyze_trends,
)
from multi_report import compute_param_diff, extract_metrics, generate_html
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

NAN = float("nan")
INF = float("inf")


def _metric(report, key):
    return next(m for m in report.metrics if m.key == key)


class TestNonFiniteValues:
    @pytest.mark.parametrize("bad", [NAN, INF, -INF])
    def test_bad_value_is_no_data_not_a_regression(self, bad):
        report = analyze_trends([{"max_vibe": 20.0}, {"max_vibe": bad}],
                                ["a", "b"], Thresholds())
        vibe = _metric(report, "max_vibe")
        assert vibe.direction == TREND_INSUFFICIENT
        assert not any(word in vibe.interpretation
                       for word in ("nan", "inf"))

    def test_bad_value_between_good_ones_is_stepped_over(self):
        report = analyze_trends(
            [{"max_vibe": 30.0}, {"max_vibe": NAN}, {"max_vibe": 20.0}],
            ["a", "b", "c"], Thresholds())
        assert _metric(report, "max_vibe").direction == TREND_IMPROVING

    def test_good_values_keep_working(self):
        report = analyze_trends([{"max_vibe": 20.0}, {"max_vibe": 28.0}],
                                ["a", "b"], Thresholds())
        assert _metric(report, "max_vibe").direction == TREND_REGRESSING


class TestAttitudeError:
    def _reader(self, roll_err, pitch_err):
        n = 200
        ts = np.linspace(0.0, 100.0, n)
        return SyntheticReader(messages={"ATT": {
            "_timestamp": ts,
            "DesRoll": np.full(n, roll_err), "Roll": np.zeros(n),
            "DesPitch": np.full(n, pitch_err), "Pitch": np.zeros(n),
        }})

    def test_no_data_is_none_not_nan(self):
        metrics = extract_metrics(self._reader(NAN, NAN), Thresholds())
        assert metrics["att_rms_error"] is None

    @pytest.mark.parametrize("roll,pitch", [(NAN, 3.0), (3.0, NAN)])
    def test_one_good_axis_is_used_whichever_it_is(self, roll, pitch):
        metrics = extract_metrics(self._reader(roll, pitch), Thresholds())
        assert metrics["att_rms_error"] == pytest.approx(3.0)
        assert math.isfinite(metrics["att_rms_error"])


DAY = 86400.0
FIRST = 1_780_000_000.0


def _flight(vibe, days=None, **extra):
    metrics = {"max_vibe": vibe, "serial": "A", **extra}
    if days is not None:
        metrics["flight_time"] = FIRST + days * DAY
    return metrics


def _row_classes(report: str) -> list[str]:
    return re.findall(r'class="trend-row (trend-\w+)"', report)


class TestFlightOrder:
    """Vibration fell from 40 to 20 over time, however the caller lists it."""

    def test_newest_first_input_still_reads_as_improving(self):
        report = generate_html(["new.BIN", "old.BIN"],
                               [_flight(20.0, days=1), _flight(40.0, days=0)])
        assert _row_classes(report) == ["trend-improving"]

    def test_name_order_does_not_decide(self):
        names = ["log_10.BIN", "log_9.BIN"]
        assert sorted(names) == names                # precondition
        report = generate_html(names,
                               [_flight(20.0, days=1), _flight(40.0, days=0)])
        assert _row_classes(report) == ["trend-improving"]

    def test_columns_follow_the_sorted_order(self):
        report = generate_html(
            ["new.BIN", "old.BIN"],
            [_flight(20.0, days=1), _flight(40.0, days=0)],
            [{"ATC_P": 0.2}, {"ATC_P": 0.1}])
        table = report[report.index('<table class="comparison-table">'):]
        assert table.index("old.BIN") < table.index("new.BIN")
        params = report[report.index('<table class="comparison-table param'):]
        assert params.index("<td>0.1</td>") < params.index(
            '<td class="val-changed">0.2</td>')

    def test_undated_log_keeps_its_slot(self):
        report = generate_html(
            ["c.BIN", "no-gps.BIN", "a.BIN"],
            [_flight(10.0, days=2), _flight(30.0), _flight(50.0, days=0)])
        table = report[report.index('<table class="comparison-table">'):]
        assert (table.index("a.BIN") < table.index("no-gps.BIN")
                < table.index("c.BIN"))

    def test_undated_logs_are_declared(self):
        report = analyze_trends([_flight(40.0, days=0), _flight(20.0)],
                                ["a", "b"], Thresholds())
        assert report.notices == [
            t("trend.notice_undated", n=1, total=2)]
        rendered = generate_html(
            ["a", "b"], [_flight(40.0, days=0), _flight(20.0)])
        assert html.escape(report.notices[0]) in rendered

    def test_fully_dated_flights_need_no_notice(self):
        report = analyze_trends([_flight(40.0, days=0), _flight(20.0, days=1)],
                                ["a", "b"], Thresholds())
        assert report.notices == []

    def test_table_shows_the_flight_date(self):
        report = generate_html(["a.BIN", "b.BIN"],
                               [_flight(40.0, days=0), _flight(20.0)])
        assert t("multi.row_flight_date") in report
        assert "2026-05-28 20:26" in report


class TestParameterDiff:
    TABLE = {f"P{i}": float(i) for i in range(50)}

    def test_no_parameter_data_is_not_called_identical(self):
        report = generate_html(["a", "b"], [{}, {}], [{}, {}])
        assert t("multi.param_no_diff") not in report
        assert t("multi.param_unknown") in report

    def test_no_parameter_list_at_all_is_not_called_identical(self):
        report = generate_html(["a", "b"], [{}, {}])
        assert t("multi.param_no_diff") not in report
        assert t("multi.param_unknown") in report

    def test_one_log_with_parameters_compares_nothing(self):
        report = generate_html(["a", "b"], [{}, {}], [self.TABLE, {}])
        assert t("multi.param_no_diff") not in report
        assert t("multi.param_unknown") in report

    def test_identical_real_parameters_are_still_called_identical(self):
        report = generate_html(["a", "b"], [{}, {}],
                               [self.TABLE, dict(self.TABLE)])
        assert t("multi.param_no_diff") in report

    def test_failed_log_does_not_flag_every_parameter(self):
        assert compute_param_diff([self.TABLE, {}, self.TABLE]) == []

    def test_change_across_a_failed_log_is_found(self):
        changed = {**self.TABLE, "P7": 99.0}
        diff = compute_param_diff([self.TABLE, {}, changed])
        assert [d["name"] for d in diff] == ["P7"]
        assert diff[0]["values"] == [7.0, None, 99.0]
        assert diff[0]["transitions"] == [1]         # new value is column 2

    def test_parameter_missing_from_a_real_log_is_a_change(self):
        reduced = {k: v for k, v in self.TABLE.items() if k != "P7"}
        diff = compute_param_diff([self.TABLE, reduced])
        assert [d["name"] for d in diff] == ["P7"]


GOOD = {"max_vibe": 8.0, "max_clip": 5.0, "max_rate_out": 0.05,
        "hover_thr_err": 0.02, "max_ekf_var": 0.1, "serial": "A",
        "flight_time": FIRST}


class TestComparableFlights:
    def test_different_airframes_get_no_trend(self):
        report = analyze_trends(
            [{**GOOD, "max_vibe": 40.0, "serial": "A"},
             {**GOOD, "max_vibe": 20.0, "serial": "B"}],
            ["a", "b"], Thresholds())
        assert report.convergence.fully_converged is False
        assert {m.direction for m in report.metrics} == {TREND_INSUFFICIENT}
        assert report.headline == t("trend.headline_not_comparable", n=2,
                                    kinds=2)
        assert report.next_step_text == t("trend.next_not_comparable")
        vibe = _metric(report, "max_vibe")
        assert "20.00" in vibe.interpretation

    def test_different_vehicles_get_no_trend(self):
        report = analyze_trends(
            [{**GOOD, "serial": None, "vehicle": "ArduCopter"},
             {**GOOD, "serial": None, "vehicle": "ArduPlane"}],
            ["a", "b"], Thresholds())
        assert report.convergence.fully_converged is False
        assert report.headline == t("trend.headline_not_comparable", n=2,
                                    kinds=2)

    def test_same_airframe_new_firmware_is_trended_with_a_notice(self):
        report = analyze_trends(
            [{**GOOD, "max_vibe": 40.0, "firmware": "V4.5.1"},
             {**GOOD, "max_vibe": 20.0, "firmware": "V4.6.3"}],
            ["a", "b"], Thresholds())
        assert _metric(report, "max_vibe").direction == TREND_IMPROVING
        assert report.notices == [t("trend.notice_firmware",
                                    versions="V4.5.1, V4.6.3")]

    def test_log_without_a_serial_is_declared_unconfirmed(self):
        report = analyze_trends(
            [{**GOOD, "max_vibe": 40.0}, {**GOOD, "serial": None}],
            ["a", "b"], Thresholds())
        assert report.notices == [t("trend.notice_unverified", n=1, total=2)]

    def test_ground_log_from_another_board_does_not_block_the_trend(self):
        report = analyze_trends(
            [{**GOOD, "max_vibe": 40.0}, {**GOOD, "max_vibe": 30.0},
             {**GOOD, "serial": "B", "flew": False}],
            ["a", "b", "c"], Thresholds())
        assert _metric(report, "max_vibe").direction == TREND_IMPROVING


class TestFlightsThatCount:
    def test_ground_run_is_not_a_converged_flight(self):
        # log_88 never left the ground; log_82 before it flew at 25.
        report = analyze_trends(
            [{**GOOD, "max_vibe": 25.05, "flew": True},
             {**GOOD, "max_vibe": 1.26, "flew": False}],
            ["log_82", "log_88"], Thresholds())
        assert "max_vibe" not in report.convergence.converged_metric_keys
        assert _metric(report, "max_vibe").direction == TREND_INSUFFICIENT
        assert report.notices == [t("trend.notice_ground", n=1, total=2)]

    def test_ground_run_between_flights_is_stepped_over(self):
        report = analyze_trends(
            [{**GOOD, "max_vibe": 30.0, "flew": True},
             {**GOOD, "max_vibe": 1.0, "flew": False},
             {**GOOD, "max_vibe": 20.0, "flew": True}],
            ["a", "b", "c"], Thresholds())
        assert _metric(report, "max_vibe").direction == TREND_IMPROVING

    def test_log_that_may_have_flown_stays_in(self):
        # flew is None when the log cannot say: no ARM event, or no POS.
        report = analyze_trends(
            [{**GOOD, "max_vibe": 30.0, "flew": None},
             {**GOOD, "max_vibe": 20.0, "flew": None}],
            ["a", "b"], Thresholds())
        assert _metric(report, "max_vibe").direction == TREND_IMPROVING

    def test_only_ground_runs_is_no_verdict(self):
        report = analyze_trends([{**GOOD, "flew": False}] * 2, ["a", "b"],
                                Thresholds())
        assert report.convergence.fully_converged is False
        assert report.convergence.converged_metric_keys == []

    def test_latest_flight_without_data_is_not_judged_on_older_ones(self):
        report = analyze_trends([GOOD, GOOD, {}], ["a", "b", "c"],
                                Thresholds())
        assert report.convergence.fully_converged is False
        vibe = _metric(report, "max_vibe")
        assert vibe.direction == TREND_INSUFFICIENT
        assert vibe.interpretation == t(
            "trend.latest_missing", label=t("trend.metric.max_vibe"))

    def test_all_good_flights_still_converge(self):
        report = analyze_trends([GOOD, GOOD], ["a", "b"], Thresholds())
        assert report.convergence.fully_converged is True
