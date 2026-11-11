"""log_health's figure of ATT intervals, on timestamps that misbehave."""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.log_health import LogHealthAnalyzer
from lib.html_report import MAX_POINTS_PER_TRACE, HtmlReportGenerator
from lib.results import AnalysisResult
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

_VEHICLE = VehicleInfo("ArduCopter", "CubeOrange", "V4.6.3", 1, 4, 2, False)

ROWS = 4000
STEP = 0.02
GAP_AT = 3000


def _timestamps(repeat_at: int | None = None,
                back_at: int | None = None) -> np.ndarray:
    """50 Hz, with one second missing before row GAP_AT."""
    ts = np.arange(ROWS) * STEP
    ts[GAP_AT:] += 1.0
    if repeat_at is not None:
        ts[repeat_at] = ts[repeat_at - 1]
    if back_at is not None:
        ts[back_at] = ts[back_at - 1] - 0.005
    return ts


def _figure(ts: np.ndarray):
    reader = SyntheticReader(messages={
        "ATT": {"_timestamp": ts, "Roll": np.zeros(len(ts))}})
    return LogHealthAnalyzer(
        reader, Thresholds(), _VEHICLE)._detect_timing_gaps()


def _points(fig) -> int:
    return sum(len(trace.y) for trace in fig.data)


class TestAxes:
    """Intervals that were not positive were dropped from the values and
    kept on the time axis. Every later point was drawn early, and a trace
    over 2000 points could not be thinned: no report came out."""

    @pytest.mark.parametrize("damage", [
        {"repeat_at": 1000}, {"back_at": 2000},
        {"repeat_at": 1000, "back_at": 2000}], ids=str)
    def test_each_interval_has_its_own_time(self, damage):
        ts = _timestamps(**damage)
        steps = np.diff(ts)
        assert np.sum(steps <= 0) == len(damage)  # precondition

        fig, _gaps = _figure(ts)

        for trace in fig.data:
            assert len(trace.x) == len(trace.y)
        assert _points(fig) == np.sum(steps > 0)

    def test_report_is_written(self):
        fig, _gaps = _figure(_timestamps(repeat_at=1000, back_at=2000))
        assert _points(fig) > MAX_POINTS_PER_TRACE  # precondition: thinned
        card = AnalysisResult(title="Log health", status="good", figures=[fig])

        html = HtmlReportGenerator(_VEHICLE, [card]).generate()

        assert "Log health" in html

    def test_points_after_a_repeat_keep_their_time(self):
        ts = _timestamps(repeat_at=1000)

        fig, _gaps = _figure(ts)

        # The longest interval is the gap, 2000 rows after the repeat.
        times = np.concatenate([np.asarray(tr.x, dtype=float)
                                for tr in fig.data])
        intervals = np.concatenate([np.asarray(tr.y, dtype=float)
                                    for tr in fig.data])
        assert times[np.argmax(intervals)] == pytest.approx(
            (ts[GAP_AT - 1] + ts[GAP_AT]) / 2)


class TestGapsAreMarked:
    """One trace carried a colour for each point, and thinning it to 2000
    points left the colours where they were: the red ones landed on
    whichever points came first."""

    def test_gap_is_on_its_own_trace_at_its_own_time(self):
        ts = _timestamps()

        fig, gaps = _figure(ts)

        assert gaps == 1
        normal, over = fig.data
        assert len(over.x) == 1
        assert over.x[0] == pytest.approx((ts[GAP_AT - 1] + ts[GAP_AT]) / 2)
        assert over.y[0] == pytest.approx(1000.0 + STEP * 1000.0)
        assert len(normal.x) == ROWS - 2

    def test_no_trace_carries_a_colour_for_each_point(self):
        fig, _gaps = _figure(_timestamps())

        for trace in fig.data:
            assert isinstance(trace.marker.color, str)

    def test_no_gap_no_figure(self):
        ts = np.arange(ROWS) * STEP

        assert _figure(ts) == (None, 0)
