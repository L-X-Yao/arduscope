"""Vibration severity uses the armed primary IMU's per-axis P99."""

from __future__ import annotations

import numpy as np
import pytest

from arduscope.analyzers.vibration import VibrationAnalyzer
from arduscope.lib.i18n import t
from arduscope.lib.thresholds import Thresholds
from tests.test_synthetic_coverage import SyntheticReader, _vehicle

pytestmark = pytest.mark.fast
N = 1000


def _reader(vibe_x, clips=(0, 0, 0)):
    n = len(vibe_x)
    return SyntheticReader(messages={"VIBE": {
        "_timestamp": np.linspace(0.0, 300.0, n),
        "IMU": np.zeros(n),
        "VibeX": np.asarray(vibe_x, dtype=float),
        "VibeY": np.zeros(n),
        "VibeZ": np.zeros(n),
        "Clip0": np.linspace(0.0, float(clips[0]), n),
        "Clip1": np.linspace(0.0, float(clips[1]), n),
        "Clip2": np.linspace(0.0, float(clips[2]), n),
    }}, params={"INS_HNTCH_ENABLE": 1}, armed_intervals=[(0.0, 300.0)])


def _run(vibe_x, clips=(0, 0, 0)):
    return VibrationAnalyzer(_reader(vibe_x, clips), Thresholds(),
                             _vehicle()).analyze()


def test_one_sample_at_31_is_not_critical_and_max_stays_visible():
    values = np.full(N, 2.0)
    values[N // 2] = 31.0
    assert np.percentile(values, 99) < 15.0
    result = _run(values)
    assert result.status == "good"
    assert any("31.0" in detail for detail in result.details)


def test_p99_at_31_is_critical_and_the_summary_says_p99():
    result = _run(np.full(N, 31.0))
    assert result.status == "critical"
    assert "P99" in result.summary
    assert "31.0" in result.summary


def test_p99_at_29_is_warning_not_critical():
    result = _run(np.full(N, 29.0))
    assert result.status == "warning"


@pytest.mark.parametrize("level,expected", [(14.99, "good"), (15.0, "warning"),
                                             (30.0, "critical")])
def test_warning_and_critical_boundaries(level, expected):
    assert _run(np.full(N, level)).status == expected


def test_sustained_high_vibration_stays_critical():
    assert _run(np.full(N, 35.0)).status == "critical"


def test_crash_with_p99_32_max_56_and_83_clips_stays_critical():
    values = np.zeros(N)
    values[-11:] = 32.0
    values[-1] = 56.0
    assert np.percentile(values, 99) == pytest.approx(32.0)
    assert np.max(values) == 56.0
    result = _run(values, clips=(83, 0, 0))
    assert result.status == "critical"
    assert any("56.0" in detail for detail in result.details)


def test_near_ground_single_spike_is_not_critical():
    values = np.full(N, 2.9)
    values[823] = 31.1
    assert np.percentile(values, 99) < 15.0
    assert _run(values).status == "good"


def test_axis_detail_keeps_avg_p99_and_max():
    values = np.full(N, 2.0)
    values[-1] = 56.0
    result = _run(values)
    assert t("vibration.vibe_axis_detail", axis="X", avg="2.1",
             p99="2.0", mx="56.0") in result.details
    assert "P99" in result.details[0]
