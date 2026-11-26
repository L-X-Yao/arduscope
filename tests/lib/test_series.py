"""The shared armed-mask and LTTB helpers keep one implementation."""

from __future__ import annotations

import numpy as np
import pytest

from arduscope.lib.series import (
    armed_mask,
    armed_rows,
    lttb_downsample,
)


class _Reader:
    def __init__(self, intervals):
        self._intervals = intervals

    def get_armed_intervals(self):
        return self._intervals


@pytest.mark.fast
def test_armed_mask_uses_intervals_and_empty_fallbacks():
    ts = np.array([0.0, 1.0, 1.5, 2.0, 3.0])
    reader = _Reader([(1.0, 2.0)])
    assert armed_mask(reader, ts).tolist() == [False, True, True, True, False]

    empty = _Reader([])
    assert armed_mask(empty, ts).all()
    assert not armed_mask(empty, ts, empty_is_all=False).any()


@pytest.mark.fast
def test_armed_rows_narrows_even_to_one_armed_row():
    """A short armed window is still the armed window: 100 or fewer rows
    used to fall back to the whole log, ground samples included."""
    ts = np.array([0.0, 1.0, 1.5, 2.0, 3.0])
    rows = np.ones(len(ts), dtype=bool)
    reader = _Reader([(1.0, 1.0)])

    assert armed_rows(reader, ts, rows).tolist() == [
        False, True, False, False, False]


@pytest.mark.fast
def test_armed_rows_keeps_every_row_without_an_armed_row():
    """Intervals that hold none of the rows leave no armed evidence to
    narrow to, so the rows are returned whole, as before."""
    ts = np.array([0.0, 1.0, 2.0])
    rows = np.ones(len(ts), dtype=bool)

    assert armed_rows(_Reader([(10.0, 20.0)]), ts, rows).tolist() == rows.tolist()
    assert armed_rows(_Reader([]), ts, rows).tolist() == rows.tolist()


@pytest.mark.fast
def test_lttb_keeps_the_endpoints_and_the_requested_size():
    x = np.arange(1000, dtype=float)
    y = np.sin(x / 20.0)
    out_x, out_y = lttb_downsample(x, y, 100)

    assert len(out_x) == len(out_y) == 100
    assert out_x[0] == x[0] and out_x[-1] == x[-1]
    assert out_y[0] == y[0] and out_y[-1] == y[-1]
