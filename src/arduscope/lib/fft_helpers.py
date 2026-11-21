"""
Shared FFT helpers for frequency-domain analyzers.

These functions were extracted from analyzers/vibration.py so that
analyzers/notch_tuning.py can reuse them without duplicating the
hanning-window / rfft / peak-finding logic.

All functions here are pure (no self, no reader access) — I/O lives
in the analyzers.
"""

from __future__ import annotations

import numpy as np


def do_fft(data: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray]:
    """Single-sided FFT amplitude spectrum with hanning window.

    DC bin (index 0) is dropped. Returns (freqs, amplitudes) with
    the same length as rfft(hanning(N)) minus one.
    """
    data = data - np.nanmean(data)
    data = np.nan_to_num(data, nan=0.0)
    window = np.hanning(len(data))
    fft_vals = np.fft.rfft(data * window)
    freqs = np.fft.rfftfreq(len(data), d=dt)
    # Divide by the window's sum, not N: a Hann window halves the amplitude.
    amplitudes = 2.0 * np.abs(fft_vals) / np.sum(window)
    return freqs[1:], amplitudes[1:]


def find_peaks(
    freqs: np.ndarray,
    amps: np.ndarray,
    min_freq: float = 3.0,
    max_freq: float | None = None,
    n_peaks: int = 3,
    min_prominence_ratio: float = 0.15,
    merge_hz: float = 2.0,
) -> list[tuple[float, float]]:
    """Top-N peaks in the spectrum above `min_prominence_ratio` of max.

    Peaks closer than `merge_hz` are merged (keeps the taller one,
    which is the first encountered since we pre-sort by amplitude).
    """
    if max_freq is None:
        max_freq = freqs[-1]
    mask = (freqs >= min_freq) & (freqs <= max_freq)
    f = freqs[mask]
    a = amps[mask]
    if len(a) == 0:
        return []

    threshold = np.max(a) * min_prominence_ratio
    peaks: list[tuple[float, float]] = []
    for i in range(1, len(a) - 1):
        if a[i] > a[i - 1] and a[i] > a[i + 1] and a[i] > threshold:
            peaks.append((float(f[i]), float(a[i])))

    peaks.sort(key=lambda p: p[1], reverse=True)
    merged: list[tuple[float, float]] = []
    for freq, amp in peaks:
        if not any(abs(freq - mf) < merge_hz for mf, _ in merged):
            merged.append((freq, amp))
        if len(merged) >= n_peaks:
            break
    return merged


def find_flight_window(
    gyr_x: np.ndarray,
    fs: float,
    window_sec: float = 10.0,
    extend_sec: float = 60.0,
) -> tuple[int, int]:
    """Pick the highest-gyro-activity contiguous segment.

    Slides a `window_sec` window at 2-second step, finds the peak
    RMS, then expands to span `extend_sec` centered on the peak.
    Fallback: middle half of the trace when gyr_x is too short.
    """
    n_win = int(window_sec * fs)
    if len(gyr_x) < n_win:
        return 0, len(gyr_x)

    step_n = max(1, int(2.0 * fs))
    best_rms = 0.0
    best_start = len(gyr_x) // 2

    for i in range(0, len(gyr_x) - n_win + 1, step_n):
        rms = float(np.std(gyr_x[i:i + n_win]))
        if rms > best_rms:
            best_rms = rms
            best_start = i

    center = best_start + n_win // 2
    half_span = int((extend_sec / 2.0) * fs)
    start = max(0, center - half_span)
    end = min(len(gyr_x), center + half_span)
    return start, end
