"""Gyro batch spectra: geometry, scaling, and when attenuation means anything."""

from __future__ import annotations

import math

import numpy as np
import pytest

import binlog
from analyzers.notch_tuning import NotchTuningAnalyzer
from lib.fft_helpers import do_fft
from lib.i18n import t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo, detect_vehicle
from tests.test_synthetic_coverage import SyntheticReader


def _analyzer(path: str) -> NotchTuningAnalyzer:
    reader = LogReader(path, preload_types=["IMU", "RCOU", "RATE"])
    analyzer = NotchTuningAnalyzer(
        reader, Thresholds.from_params(
            reader.params, reader.canonical_vehicle_type),
        detect_vehicle(reader))
    analyzer._armed_intervals = reader.get_armed_intervals()
    analyzer._actual_hover_throttle = 0.35
    return analyzer


def _copter() -> VehicleInfo:
    return VehicleInfo(vehicle_type="ArduCopter", firmware_version="4.6.3",
                       board_name="s", frame_class=1, motor_count=4,
                       imu_count=1, is_quadplane=False)


def _write_batch(log, seq, time_us, tone_hz, samples, mul=1, amplitude=2000.0,
                 rate_hz=1000.0, sweep_hz=0.0):
    log.write("ISBH", time_us, seq, 1, 0, mul, samples, time_us, rate_hz)
    # sweep_hz widens the tone into a band, as motors at differing speeds do.
    wave = [int(mul * amplitude * math.sin(
        2 * math.pi * (tone_hz + sweep_hz * i / (2 * samples)) * i / rate_hz))
        for i in range(samples)]
    for chunk, start in enumerate(range(0, samples, 32)):
        part = wave[start:start + 32]
        log.write("ISBD", time_us, seq, chunk, part, part, part)


def _log_with_batches(tmp_path, tone_hz, sizes, mul=1, amplitude=2000.0,
                      params=None):
    log = binlog.flight_log("copter", batches=0, params=params)
    for seq, samples in enumerate(sizes):
        _write_batch(log, seq, 5_000_000 + seq * 1_000_000, tone_hz, samples,
                     mul=mul, amplitude=amplitude)
    return log.save(tmp_path / "batches.bin")


class TestSpectrumGeometry:
    def test_one_short_batch_does_not_cut_the_spectrum(self, tmp_path):
        path = _log_with_batches(tmp_path, 450.0, [128, 1024, 1024, 1024])
        analyzer = _analyzer(path)
        peak, _, _ = analyzer._fft_from_batch()
        assert peak == pytest.approx(450.0, abs=3.0)
        assert analyzer._gyro_spectrum[0][-1] == pytest.approx(500.0, abs=2.0)

    def test_short_batches_in_the_majority_still_give_one_axis(self, tmp_path):
        path = _log_with_batches(tmp_path, 120.0, [128, 128, 128, 1024])
        analyzer = _analyzer(path)
        peak, _, _ = analyzer._fft_from_batch()
        freqs = analyzer._gyro_spectrum[0]
        assert len(freqs) == 64
        assert peak == pytest.approx(120.0, abs=8.0)


class TestMeasuredRates:
    """Each batch logs the rate it measured, so no two are quite equal."""

    @staticmethod
    def _log(tmp_path, rates, first_is_silent=False):
        log = binlog.flight_log("copter", batches=0)
        for seq, rate in enumerate(rates):
            silent = first_is_silent and seq == 0
            _write_batch(log, seq, 5_000_000 + seq * 1_000_000, 120.0, 1024,
                         amplitude=0.0 if silent else 2000.0, rate_hz=rate)
        return log.save(tmp_path / "rates.bin")

    def test_every_batch_goes_into_the_average(self, tmp_path):
        analyzer = _analyzer(self._log(tmp_path, [1000.0, 1001.3, 998.7, 1002.2]))
        peak, _, _ = analyzer._fft_from_batch()
        assert peak == pytest.approx(120.0, abs=3.0)

    def test_a_silent_batch_cannot_stand_for_the_flight(self, tmp_path):
        # Motors not yet spinning. It used to win a group of its own, and a
        # flat spectrum read as no vibration at all. Three loud batches of
        # four average to three quarters of the height.
        path = self._log(tmp_path, [1000.0, 1001.3, 998.7, 1002.2],
                         first_is_silent=True)
        analyzer = _analyzer(path)
        peak, _, _ = analyzer._fft_from_batch()
        assert peak == pytest.approx(120.0, abs=3.0)
        assert np.max(analyzer._gyro_spectrum[1]) == pytest.approx(1500.0, rel=0.1)

    def test_a_different_rate_stays_out(self, tmp_path):
        analyzer = _analyzer(self._log(tmp_path, [1000.0, 1000.5, 999.5, 2000.0]))
        analyzer._fft_from_batch()
        assert analyzer._gyro_spectrum[0][-1] == pytest.approx(500.0, abs=2.0)


class TestAmplitude:
    def test_batch_multiplier_divides(self, tmp_path):
        path = _log_with_batches(tmp_path, 125.0, [1024, 1024], mul=100,
                                 amplitude=2.0)
        analyzer = _analyzer(path)
        analyzer._fft_from_batch()
        freqs, ax, _, _ = analyzer._gyro_spectrum
        assert float(np.max(ax)) == pytest.approx(2.0, rel=0.05)

    def test_large_samples_do_not_wrap(self, tmp_path):
        path = _log_with_batches(tmp_path, 125.0, [1024], mul=30,
                                 amplitude=1000.0)
        analyzer = _analyzer(path)
        analyzer._fft_from_batch()
        assert float(np.max(analyzer._gyro_spectrum[1])) == pytest.approx(
            1000.0, rel=0.05)

    def test_hann_window_gain_is_corrected(self):
        n, rate = 4096, 1000.0
        signal = 10.0 * np.sin(2 * np.pi * 125.0 * np.arange(n) / rate)
        _, amps = do_fft(signal, 1.0 / rate)
        assert float(np.max(amps)) == pytest.approx(10.0, rel=0.02)


class TestFlightBatches:
    """Motor noise is measured on the flight, not on the ground before it."""

    LATE_US = 3_000_000_000  # the log opens 50 minutes after boot

    def _late_log(self, monkeypatch):
        monkeypatch.setattr(binlog, "_T0_US", self.LATE_US)
        return binlog.flight_log("copter", batches=0)

    def test_batch_time_counts_from_the_log_start(self, tmp_path, monkeypatch):
        """TimeUS counts from boot, and armed intervals from the log start."""
        log = self._late_log(monkeypatch)
        _write_batch(log, 0, self.LATE_US + 10_000_000, 120.0, 1024)
        reader = LogReader(log.save(tmp_path / "late.bin"))
        assert reader.isb_batches[0]["time_s"] == pytest.approx(10.0, abs=0.01)
        assert reader.get_armed_intervals()[0][0] == pytest.approx(2.0, abs=0.1)

    def test_a_loud_ground_batch_does_not_stand_for_the_flight(self, tmp_path,
                                                               monkeypatch):
        log = self._late_log(monkeypatch)
        _write_batch(log, 0, self.LATE_US + 1_000_000, 300.0, 1024,
                     amplitude=8000.0)
        for seq in (1, 2):
            _write_batch(log, seq, self.LATE_US + seq * 10_000_000, 120.0, 1024)
        analyzer = _analyzer(log.save(tmp_path / "ground.bin"))
        peak, _, _ = analyzer._fft_from_batch()
        assert peak == pytest.approx(120.0, abs=2.0)

    def test_no_batch_in_flight_gives_no_peak(self, tmp_path, monkeypatch):
        log = self._late_log(monkeypatch)
        _write_batch(log, 0, self.LATE_US + 1_000_000, 300.0, 1024)
        analyzer = _analyzer(log.save(tmp_path / "preflight.bin"))
        peak, _, details = analyzer._fft_from_batch()
        assert peak is None
        assert details == [t("notch_tuning.no_gyro_batch")]

    def test_a_log_with_no_arming_record_still_uses_its_batches(self, tmp_path):
        """No POWR and no events: it may hold a whole flight."""
        analyzer = _analyzer(_log_with_batches(tmp_path, 120.0, [1024] * 3))
        analyzer.reader.get_armed_intervals = lambda: []
        analyzer._armed_intervals = []
        peak, _, _ = analyzer._fft_from_batch()
        assert peak == pytest.approx(120.0, abs=2.0)

    def test_too_little_armed_imu_data_gives_no_peak(self):
        n = 1000
        ts = np.arange(n) * 0.001
        wave = np.sin(2 * np.pi * 120.0 * ts)
        reader = SyntheticReader(
            messages={"IMU": {"_timestamp": ts, "GyrX": wave, "GyrY": wave,
                              "GyrZ": wave}},
            armed_intervals=[(0.0, 0.1)],
        )
        analyzer = NotchTuningAnalyzer(reader, Thresholds(), _copter())
        analyzer._armed_intervals = reader.get_armed_intervals()
        peak, _, details = analyzer._fft_from_imu()
        assert peak is None
        assert details == [t("notch_tuning.imu_too_few")]

    def test_a_log_that_never_armed_is_not_assessed(self):
        reader = SyntheticReader(messages={"RCOU": {"_timestamp": np.arange(9.0)}})
        reader.never_armed = lambda: True
        result = NotchTuningAnalyzer(reader, Thresholds(), _copter()).analyze()
        assert result.status == "info"
        assert result.summary == t("common.never_armed")
        assert not result.param_suggestions


class TestSignificantPeak:
    """The wiki's "significant peak in noise", 10 dB above the floor."""

    FREQS = np.arange(25.0, 501.0)

    @pytest.fixture
    def analyzer(self, tmp_path):
        return _analyzer(_log_with_batches(tmp_path, 120.0, [1024] * 3))

    def _peak(self, analyzer, power):
        amplitude = np.sqrt(np.asarray(power) / 3.0)
        return analyzer._find_dominant_peak(self.FREQS, amplitude, amplitude,
                                            amplitude)

    def _with_peak(self, height):
        power = np.ones(len(self.FREQS))
        power[int(120 - self.FREQS[0])] = height
        return power

    def test_a_clear_peak_is_found(self, analyzer):
        assert self._peak(analyzer, self._with_peak(100.0)) == 120.0

    def test_just_over_the_line_is_a_peak(self, analyzer):
        assert self._peak(analyzer, self._with_peak(10.5)) == 120.0

    def test_just_under_the_line_is_not(self, analyzer):
        assert self._peak(analyzer, self._with_peak(9.5)) is None

    def test_a_floor_rising_to_the_end_of_the_band_is_not_a_peak(self, analyzer):
        """What log_88 looks like: motors at idle, the top near 490 Hz."""
        rng = np.random.default_rng(1)
        rise = 1.0 + 4.0 * ((self.FREQS - 25.0) / 475.0) ** 6
        power = rise * rng.uniform(0.8, 1.2, len(self.FREQS))
        assert self._peak(analyzer, power) is None

    def test_a_silent_spectrum_with_one_tone_is_a_peak(self, analyzer):
        power = self._with_peak(1.0) * 0.0
        power[int(120 - self.FREQS[0])] = 1.0
        assert self._peak(analyzer, power) == 120.0

    def test_noise_batches_get_no_advice(self, tmp_path):
        rng = np.random.default_rng(2)
        log = binlog.flight_log("copter", batches=0)
        for seq in range(4):
            time_us = 5_000_000 + seq * 1_000_000
            log.write("ISBH", time_us, seq, 1, 0, 1, 1024, time_us, 1000.0)
            noise = rng.integers(-500, 500, 1024).tolist()
            for chunk, start in enumerate(range(0, 1024, 32)):
                part = noise[start:start + 32]
                log.write("ISBD", time_us, seq, chunk, part, part, part)
        result = _analyzer(log.save(tmp_path / "noise.bin")).analyze()
        assert t("notch_tuning.no_peak") in result.details
        assert not [s for s in result.param_suggestions
                    if s["param"].startswith("INS_HNTCH")]


class TestAttenuationMeaning:
    """What a filtered spectrum can say about a notch that stays put. One
    that follows the motors is not measured at all: see
    test_notch_tracking.py."""

    NOTCH = {"INS_HNTCH_ENABLE": 1, "INS_HNTCH_FREQ": 120, "INS_HNTCH_BW": 40,
             "INS_HNTCH_MODE": 0, "INS_HNTCH_REF": 0}

    def _details(self, analyzer, peak):
        status, details, _ = analyzer._check_notch_filter("", peak)
        return status, " ".join(details)

    def _flat(self, analyzer):
        freqs = np.linspace(1.0, 500.0, 500)
        flat = np.ones(500)
        analyzer._gyro_spectrum = (freqs, flat, flat, flat)

    def test_pre_filter_batches_are_not_assessed(self, tmp_path):
        path = _log_with_batches(tmp_path, 120.0, [1024] * 3,
                                 params={**self.NOTCH, "INS_LOG_BAT_OPT": 0})
        analyzer = _analyzer(path)
        self._flat(analyzer)
        assert analyzer._measure_notch_attenuation(120, 40) == {
            "method": "pre_filter"}

    def test_post_filter_batches_are_assessed(self, tmp_path):
        path = _log_with_batches(
            tmp_path, 120.0, [1024] * 3,
            params={**self.NOTCH, "INS_LOG_BAT_OPT": 2})
        analyzer = _analyzer(path)
        self._flat(analyzer)
        assert "atten_db" in analyzer._measure_notch_attenuation(120, 40)

    @pytest.mark.parametrize("options", [3, 4, 5, 6, 7])
    def test_pre_and_post_interleaved_is_not_assessed(self, tmp_path, options):
        """Bit 2 alternates pre- and post-filter batches, and so do bits 0
        and 1 together (BatchSampler::doing_post_filter_logging). 3 was
        read as post-filter."""
        path = _log_with_batches(
            tmp_path, 120.0, [1024] * 3,
            params={**self.NOTCH, "INS_LOG_BAT_OPT": options})
        analyzer = _analyzer(path)
        self._flat(analyzer)
        assert analyzer._measure_notch_attenuation(120, 40) == {
            "method": "mixed", "options": options}
        _status, text = self._details(analyzer, 120.0)
        assert t("notch_tuning.atten_mixed", opt=options) in text

    def test_flat_spectrum_is_not_a_failed_notch(self, tmp_path):
        path = _log_with_batches(tmp_path, 120.0, [1024] * 3,
                                 params={**self.NOTCH, "INS_LOG_BAT_OPT": 2})
        analyzer = _analyzer(path)
        self._flat(analyzer)
        status, _ = self._details(analyzer, None)
        assert status != "critical"

    def test_residual_peak_in_band_is_not_a_failed_notch(self, tmp_path):
        """This read critical. A notch on a peak leaves a smaller peak and
        no dip: flown, one that took 97.7% off its peak failed this."""
        path = _log_with_batches(tmp_path, 120.0, [1024] * 3,
                                 params={**self.NOTCH, "INS_LOG_BAT_OPT": 2})
        analyzer = _analyzer(path)
        self._flat(analyzer)
        atten = analyzer._measure_notch_attenuation(120, 40)
        assert atten["atten_db"] < Thresholds().notch_atten_crit  # precondition
        status, text = self._details(analyzer, 120.0)
        assert status == "good"
        assert t("notch_tuning.atten_residual",
                 peak="120", lo="100", hi="140") in text

    def test_visible_dip_is_good_without_any_peak(self, tmp_path):
        path = _log_with_batches(tmp_path, 120.0, [1024] * 3,
                                 params={**self.NOTCH, "INS_LOG_BAT_OPT": 2})
        analyzer = _analyzer(path)
        freqs = np.linspace(1.0, 500.0, 500)
        spectrum = np.ones(500)
        spectrum[(freqs > 110) & (freqs < 130)] = 1e-4
        analyzer._gyro_spectrum = (freqs, spectrum, spectrum, spectrum)
        atten = analyzer._measure_notch_attenuation(120, 40)
        assert atten["atten_db"] >= Thresholds().notch_atten_warn  # precondition
        status, text = self._details(analyzer, None)
        assert status == "good"
        assert t("notch_tuning.atten_good", db=f"{atten['atten_db']:.1f}",
                 converge=Thresholds().notch_atten_converge,
                 method=atten["method"]) in text


class TestNotchAdvice:
    """Each value here was flown: see the constants in notch_tuning.py."""

    def test_bandwidth_is_half_the_centre_frequency(self, tmp_path):
        analyzer = _analyzer(_log_with_batches(tmp_path, 138.0, [1024] * 3))
        assert analyzer._calc_notch_bw(138.0) == 69
        analyzer._measured_peak_bw = 3.0  # the peak's width has no say
        assert analyzer._calc_notch_bw(138.0) == 69

    def test_a_clean_tone_gets_one_notch(self, tmp_path):
        analyzer = _analyzer(_log_with_batches(tmp_path, 138.0, [1024] * 3))
        analyzer._fft_from_batch()
        assert analyzer._recommended_opts == 0

    def test_a_smeared_peak_gets_triple_not_double(self, tmp_path):
        log = binlog.flight_log("copter", batches=0)
        for seq in range(4):
            _write_batch(log, seq, 5_000_000 + seq * 1_000_000, 120.0, 1024,
                         sweep_hz=36.0)
        analyzer = _analyzer(log.save(tmp_path / "wide.bin"))
        analyzer._fft_from_batch()
        assert analyzer._measured_peak_bw >= 5
        assert analyzer._recommended_opts == 16


class TestFltdAdvice:
    @staticmethod
    def _flown(tmp_path, gyro_filter, fltd):
        path = _log_with_batches(
            tmp_path, 138.0, [1024] * 3,
            params={"INS_GYRO_FILTER": gyro_filter, "ATC_RAT_RLL_FLTD": fltd,
                    "ATC_RAT_PIT_FLTD": fltd})
        return _analyzer(path).analyze()

    @staticmethod
    def _suggested(result, param):
        return [s["suggested"] for s in result.param_suggestions
                if s["param"] == param]

    def test_above_half_the_gyro_filter_is_lowered(self, tmp_path):
        result = self._flown(tmp_path, 20, 20)
        assert self._suggested(result, "ATC_RAT_RLL_FLTD") == [10.0]

    def test_below_half_the_gyro_filter_is_not_raised(self, tmp_path):
        result = self._flown(tmp_path, 100, 20)
        assert self._suggested(result, "ATC_RAT_RLL_FLTD") == []
        assert any("INS_GYRO_FILTER/2" in d for d in result.details)


def test_critical_attitude_error_does_not_raise_thr_mix():
    # The wiki raises it only after a good tune, and warns against it on an
    # oscillating aircraft. In SITL 0.7 changed nothing.
    ts = np.linspace(0.0, 60.0, 500)
    reader = SyntheticReader(messages={"ATT": {
        "_timestamp": ts, "Roll": np.full(500, 8.0), "DesRoll": np.zeros(500),
        "Pitch": np.zeros(500), "DesPitch": np.zeros(500),
    }}, armed_intervals=[(0.0, 60.0)])
    analyzer = NotchTuningAnalyzer(reader, Thresholds(), _copter())
    analyzer._armed_intervals = reader.get_armed_intervals()
    result_status, _details = analyzer._check_attitude_stability()
    assert result_status == "critical"
    # The method now hands back no recommendations at all.
    assert len(analyzer._check_attitude_stability()) == 2
