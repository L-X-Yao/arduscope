"""
Unit tests for lib.fft_helpers.

Tests exercise the pure FFT helpers with synthetic signals where
the correct answer is known analytically, so we can assert exact
behaviour.
"""

from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.fast

from lib.fft_helpers import do_fft, find_flight_window, find_peaks


def _make_sine(freq_hz: float, fs: float, duration_s: float,
               amp: float = 1.0, noise: float = 0.0) -> np.ndarray:
    n = int(duration_s * fs)
    t = np.arange(n) / fs
    sig = amp * np.sin(2 * np.pi * freq_hz * t)
    if noise > 0:
        rng = np.random.default_rng(42)
        sig = sig + noise * rng.standard_normal(n)
    return sig


class TestDoFFT:
    def test_pure_sine_peak_location(self):
        """A 100 Hz sine at 1000 Hz fs should peak near 100 Hz."""
        fs = 1000.0
        sig = _make_sine(100.0, fs, duration_s=4.0)
        freqs, amps = do_fft(sig, dt=1.0 / fs)
        peak_idx = int(np.argmax(amps))
        assert abs(freqs[peak_idx] - 100.0) < 1.0

    def test_dc_bin_dropped(self):
        """Output length = N/2 (DC bin dropped)."""
        fs = 1000.0
        sig = _make_sine(50.0, fs, duration_s=2.0)
        freqs, amps = do_fft(sig, dt=1.0 / fs)
        expected_len = len(sig) // 2  # rfft gives N/2+1, we drop DC
        assert len(freqs) == expected_len
        assert len(amps) == expected_len

    def test_nan_input_does_not_crash(self):
        """NaNs in input are replaced with 0 before FFT."""
        fs = 1000.0
        sig = _make_sine(50.0, fs, duration_s=2.0)
        sig[100:110] = np.nan
        freqs, amps = do_fft(sig, dt=1.0 / fs)
        assert np.all(np.isfinite(amps))


class TestFindPeaks:
    def test_single_peak(self):
        fs = 1000.0
        sig = _make_sine(80.0, fs, duration_s=4.0)
        freqs, amps = do_fft(sig, dt=1.0 / fs)
        peaks = find_peaks(freqs, amps, n_peaks=3)
        assert len(peaks) >= 1
        top_freq, top_amp = peaks[0]
        assert abs(top_freq - 80.0) < 2.0

    def test_two_separated_peaks(self):
        fs = 2000.0
        sig = _make_sine(120.0, fs, 4.0) + _make_sine(300.0, fs, 4.0, amp=0.8)
        freqs, amps = do_fft(sig, dt=1.0 / fs)
        peaks = find_peaks(freqs, amps, n_peaks=3)
        peak_freqs = sorted(p[0] for p in peaks)
        assert any(abs(f - 120.0) < 2.0 for f in peak_freqs)
        assert any(abs(f - 300.0) < 2.0 for f in peak_freqs)

    def test_merge_hz_combines_adjacent_peaks(self):
        """Peaks within merge_hz collapse to one."""
        fs = 2000.0
        sig = (
            _make_sine(100.0, fs, 4.0)
            + _make_sine(101.0, fs, 4.0, amp=0.9)
        )
        freqs, amps = do_fft(sig, dt=1.0 / fs)
        peaks = find_peaks(freqs, amps, n_peaks=3, merge_hz=3.0)
        assert len(peaks) == 1

    def test_empty_window_returns_empty(self):
        freqs = np.array([10.0, 20.0, 30.0])
        amps = np.array([1.0, 2.0, 3.0])
        # min_freq > max_freq in the array → empty
        assert find_peaks(freqs, amps, min_freq=100.0) == []


class TestFindFlightWindow:
    def test_high_activity_region_selected(self):
        """A quiet-loud-quiet trace: the window should land in loud."""
        fs = 100.0
        quiet = np.zeros(int(30 * fs))
        loud = _make_sine(5.0, fs, 30.0, amp=10.0)
        trace = np.concatenate([quiet, loud, quiet])
        start, end = find_flight_window(trace, fs, window_sec=10.0)
        # Start of window should be inside the loud segment
        loud_start = len(quiet)
        loud_end = loud_start + len(loud)
        window_center = (start + end) // 2
        assert loud_start <= window_center <= loud_end

    def test_short_trace_returns_whole(self):
        """Trace shorter than window_sec returns (0, len)."""
        fs = 100.0
        trace = np.zeros(50)  # < 10 s at 100 Hz
        start, end = find_flight_window(trace, fs, window_sec=10.0)
        assert (start, end) == (0, len(trace))

    def test_extend_sec_span_when_middle(self):
        """Activity in the middle → full extend_sec span."""
        fs = 100.0
        quiet = np.zeros(int(100 * fs))
        loud = _make_sine(5.0, fs, 30.0, amp=10.0)
        trace = np.concatenate([quiet, loud, quiet])
        start, end = find_flight_window(trace, fs, extend_sec=60.0)
        # Middle placement → neither end clamps
        assert end - start == int(60.0 * fs)

    def test_last_window_not_missed_off_by_one(self):
        """Activity only in the last n_win samples must be found.

        At fs=100, n_win=1000, step_n=200: range(0, 200, 200) → [0] only.
        The window [200, 1200] was silently skipped before the fix.
        """
        fs = 100.0
        quiet = np.zeros(200)              # 2 s silence
        loud = _make_sine(5.0, fs, 10.0, amp=10.0)  # 1000 samples, loud
        trace = np.concatenate([quiet, loud])        # total 1200
        start, end = find_flight_window(
            trace, fs, window_sec=10.0, extend_sec=10.0
        )
        window_center = (start + end) // 2
        # Without fix, best_start=0 and center lands in the quiet zone
        assert window_center >= 200, (
            f"center={window_center} in quiet zone — last window was missed"
        )
