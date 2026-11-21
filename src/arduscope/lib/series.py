"""Shared time-series helpers: armed masks and LTTB downsampling."""

from __future__ import annotations

import numpy as np

VIBE_ARMED_FLOOR = 100
EKF_ARMED_FLOOR = 10


def armed_mask(reader, ts, *, empty_is_all: bool = True) -> np.ndarray:
    """True on samples inside an armed interval.

    empty_is_all keeps the historical analyzer/fleet behaviour: a log with no
    arming events is read whole unless the caller knows it never armed.
    """
    armed = reader.get_armed_intervals()
    if not armed:
        return np.ones(len(ts), dtype=bool) if empty_is_all else np.zeros(
            len(ts), dtype=bool)
    mask = np.zeros(len(ts), dtype=bool)
    for start, end in armed:
        mask |= (ts >= start) & (ts <= end)
    return mask


def armed_rows(reader, ts, rows, floor: int,
               *, empty_is_all: bool = True) -> np.ndarray:
    """Narrow rows to armed time, but only when more than floor remain."""
    narrowed = rows & armed_mask(reader, ts, empty_is_all=empty_is_all)
    return narrowed if int(np.sum(narrowed)) > floor else rows


def lttb_downsample(x: np.ndarray, y: np.ndarray,
                    threshold: int) -> tuple[np.ndarray, np.ndarray]:
    """Largest Triangle Three Buckets downsampling.

    Reduces the number of points while preserving the visual shape; peaks
    and valleys survive. Vectorized over each bucket.
    """
    n = len(x)
    if n <= threshold or threshold < 3:
        return x, y

    out_idx = np.empty(threshold, dtype=np.int64)
    out_idx[0] = 0
    out_idx[-1] = n - 1

    bucket_size = (n - 2) / (threshold - 2)
    a_idx = 0

    for i in range(1, threshold - 1):
        bucket_start = int((i - 1) * bucket_size) + 1
        bucket_end = min(int(i * bucket_size) + 1, n)
        next_start = int(i * bucket_size) + 1
        next_end = min(int((i + 1) * bucket_size) + 1, n - 1)

        avg_x = np.mean(x[next_start:next_end + 1])
        avg_y = np.mean(y[next_start:next_end + 1])

        bx = x[bucket_start:bucket_end]
        by = y[bucket_start:bucket_end]
        ax, ay = x[a_idx], y[a_idx]
        areas = np.abs((ax - avg_x) * (by - ay) - (ax - bx) * (avg_y - ay))

        max_idx = bucket_start + np.argmax(areas)
        out_idx[i] = max_idx
        a_idx = max_idx

    return x[out_idx], y[out_idx]
