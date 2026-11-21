"""Primary-lane selection: PI wins, then the lowest core, then every row."""

from __future__ import annotations

import numpy as np
import pytest

from arduscope.lib.ekf_lanes import primary_lane_rows


@pytest.mark.fast
def test_pi_selects_each_rows_lane():
    core = np.array([0, 0, 1, 1])
    primary = np.array([0, np.nan, 1, np.nan])
    assert primary_lane_rows(core, primary).tolist() == [
        True, False, True, False]


@pytest.mark.fast
def test_all_nan_core_keeps_every_row():
    core = np.full(4, np.nan)
    primary = np.full(4, np.nan)
    assert primary_lane_rows(core, primary).all()


@pytest.mark.fast
def test_missing_pi_falls_back_to_the_lowest_core():
    core = np.array([1, 1, 2])
    primary = np.full(3, np.nan)
    assert primary_lane_rows(core, primary).tolist() == [True, True, False]
