"""The shared armed-mask and LTTB helpers keep one implementation."""

from __future__ import annotations

import numpy as np
import pytest

from arduscope.lib.series import (
    EKF_ARMED_FLOOR,
    VIBE_ARMED_FLOOR,
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
def test_armed_rows_only_narrows_above_the_floor():
    ts = np.array([0.0, 1.0, 1.5, 2.0, 3.0])
    rows = np.ones(len(ts), dtype=bool)
    reader = _Reader([(1.0, 2.0)])

    assert armed_rows(reader, ts, rows, 2).tolist() == [
        False, True, True, True, False]
    assert armed_rows(reader, ts, rows, 3).tolist() == rows.tolist()
    assert (VIBE_ARMED_FLOOR, EKF_ARMED_FLOOR) == (100, 10)


@pytest.mark.fast
def test_lttb_keeps_the_endpoints_and_the_requested_size():
    x = np.arange(1000, dtype=float)
    y = np.sin(x / 20.0)
    out_x, out_y = lttb_downsample(x, y, 100)

    assert len(out_x) == len(out_y) == 100
    assert out_x[0] == x[0] and out_x[-1] == x[-1]
    assert out_y[0] == y[0] and out_y[-1] == y[-1]
