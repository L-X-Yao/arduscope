"""STAT lookups and the fixed-wing windows shared by Plane analyzers."""

from __future__ import annotations

import numpy as np
import pytest

from lib.plane_phase import covered_seconds, stat_at, vtol_windows_mask
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast


def _stat_reader(times, stages) -> SyntheticReader:
    return SyntheticReader(messages={"STAT": {
        "_timestamp": np.array(times, dtype=float),
        "Stage": np.array(stages, dtype=float)}})


class TestStatAt:
    def test_holds_the_last_row_until_the_next(self):
        reader = _stat_reader([0.0, 10.0], [1, 3])

        values = stat_at(reader, np.array([0.0, 9.9, 10.0, 50.0]), "Stage")

        assert list(values) == [1, 1, 3, 3]

    def test_rows_before_the_first_stat_take_its_value(self):
        reader = _stat_reader([5.0, 10.0], [1, 3])

        assert list(stat_at(reader, np.array([0.0]), "Stage")) == [1]

    def test_field_the_log_lacks_reads_nan(self):
        reader = _stat_reader([0.0, 10.0], [1, 3])

        values = stat_at(reader, np.array([1.0]), "isFlying")

        assert np.isnan(values).all()

    def test_no_stat(self):
        assert stat_at(SyntheticReader(messages={}), np.array([1.0]),
                       "Stage") is None

    def test_one_stat_row_is_not_enough(self):
        assert stat_at(_stat_reader([0.0], [3]), np.array([1.0]),
                       "Stage") is None


class TestCoveredSeconds:
    def test_adds_up_sample_intervals(self):
        assert covered_seconds(np.arange(0.0, 10.5, 0.5)) == 10.0

    def test_leaves_out_gaps(self):
        ts = np.concatenate([np.arange(0.0, 5.5, 0.5),
                             np.arange(100.0, 105.5, 0.5)])

        assert covered_seconds(ts) == 10.0

    @pytest.mark.parametrize("ts", [[], [1.0]])
    def test_too_few_samples(self, ts):
        assert covered_seconds(np.array(ts)) == 0.0


class TestVtolWindows:
    TS = np.arange(0.0, 100.0, 1.0)

    def test_nothing_to_go_on(self):
        assert vtol_windows_mask(SyntheticReader(messages={}), self.TS) is None

    def test_qtun_stream_marks_its_neighbourhood(self):
        reader = SyntheticReader(messages={
            "QTUN": {"_timestamp": np.arange(10.0, 20.0, 0.04)}})

        mask = vtol_windows_mask(reader, self.TS)

        assert np.array_equal(np.flatnonzero(mask), np.arange(10, 21))

    def test_q_mode_runs_until_the_next_mode(self):
        # QHOVER = 18, FBWA = 5.
        reader = SyntheticReader(messages={"MODE": {
            "_timestamp": np.array([30.0, 40.0]),
            "Mode": np.array([18.0, 5.0])}})

        mask = vtol_windows_mask(reader, self.TS)

        assert np.array_equal(np.flatnonzero(mask), np.arange(30, 40))

    def test_windows_add_up(self):
        reader = SyntheticReader(messages={
            "QTUN": {"_timestamp": np.arange(10.0, 12.0, 0.04)},
            "MODE": {"_timestamp": np.array([30.0, 40.0]),
                     "Mode": np.array([18.0, 5.0])}})

        mask = vtol_windows_mask(reader, self.TS)

        assert mask[10] and mask[35]
        assert not mask[20] and not mask[50]
