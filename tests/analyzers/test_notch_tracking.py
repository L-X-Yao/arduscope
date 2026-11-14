"""Notch advice that makes the notch follow the motors.

Flown through climbs and descents in SITL, the set suggested before left the
notch at one frequency: INS_HNTCH_REF stayed at 0, which turns tracking off
in every mode. Gyro noise in the motor band read 1.75 deg/s with it, 0.48 with
REF at 1, and 0.39 with FREQ at the low end of the ESC frequency as well.
"""

from __future__ import annotations

import numpy as np
import pytest

import binlog
from analyzers.notch_tuning import NotchTuningAnalyzer
from lib.i18n import t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo, detect_vehicle
from tests.test_synthetic_coverage import SyntheticReader

ESC_FORMAT = ("QBffffcfcf",
              "TimeUS,Instance,RPM,RawRPM,Volt,Curr,Temp,CTot,MotTemp,Err")


def _vehicle(kind: str = "ArduCopter") -> VehicleInfo:
    return VehicleInfo(vehicle_type=kind, firmware_version="4.6.3",
                       board_name="s", frame_class=1, motor_count=4,
                       imu_count=1, is_quadplane=False)


def _reader(esc_hz=None, flying=(10.0, 90.0), throttle=0.35, params=None,
            vehicle="ArduCopter", silent=(), reporting=1.0,
            throttle_message="CTUN") -> SyntheticReader:
    """100 s of log: on the ground, in the air over `flying`, down again.

    esc_hz is one frequency or a function of time. Each of four ESCs
    reports it in the air and 0 on the ground. `silent` ESCs report 0
    throughout, and `reporting` is the share of airborne rows on which
    ESC 0 reports at all.
    """
    n = 1000
    ts = np.linspace(0.0, 100.0, n)
    up = (ts >= flying[0]) & (ts <= flying[1])
    messages = {
        throttle_message: {"_timestamp": ts,
                           "ThO": np.where(up, throttle, 0.0)},
        "RCOU": {"_timestamp": ts, "C1": np.full(n, 1500.0)},
    }
    if esc_hz is not None:
        rows = np.repeat(ts, 4)
        instance = np.tile(np.arange(4.0), n)
        hz = esc_hz(rows) if callable(esc_hz) else np.full(len(rows), esc_hz)
        rpm = np.where(np.repeat(up, 4), hz * 60.0, 0.0)
        for i in silent:
            rpm[instance == i] = 0.0
        first = np.flatnonzero((instance == 0) & np.repeat(up, 4))
        rpm[first[int(len(first) * reporting):]] = 0.0
        messages["ESC"] = {"_timestamp": rows, "Instance": instance,
                           "RPM": rpm}
    return SyntheticReader(messages=messages, params=params or {},
                           vehicle_type=vehicle,
                           armed_intervals=[(flying[0] - 5, flying[1] + 5)])


def _analyzer(reader, hover: float = 0.35) -> NotchTuningAnalyzer:
    analyzer = NotchTuningAnalyzer(reader, Thresholds(), _vehicle())
    analyzer._armed_intervals = reader.get_armed_intervals()
    analyzer._actual_hover_throttle = hover
    analyzer._recommended_opts = 0
    analyzer._measured_peak_bw = None
    analyzer._gyro_spectrum = None
    return analyzer


def _sweep(ts):
    """100 Hz at the start of the log, 200 Hz at its end."""
    return 100.0 + ts


def _values(rows: list[dict]) -> dict:
    return {row["param"]: row["new"] for row in rows}


def _tone_batch(time_s: float, tone_hz: float, amplitude: float = 2000.0,
                samples: int = 1024, rate_hz: float = 1000.0) -> dict:
    wave = (amplitude * np.sin(
        2 * np.pi * tone_hz * np.arange(samples) / rate_hz)).astype(int)
    return {"type": 1, "instance": 0, "mul": 1, "smp_rate": rate_hz,
            "smp_cnt": samples, "time_s": time_s,
            "x": wave.tolist(), "y": wave.tolist(), "z": wave.tolist()}


class TestSpectrumWhereTheMotorsCarry:
    """The wiki pairs the noise frequency at the hover throttle with that
    throttle, so the spectrum takes the rows the hover throttle is measured
    on. Armed alone let in the idle before takeoff and a QuadPlane's cruise:
    22 of 210 corpus logs with ESC RPM peaked under the motors' range in
    flight, 20 of them where the motors idle on the ground."""

    def _spectrum_peak(self, reader, batches):
        reader.isb_batches = dict(enumerate(batches))
        return _analyzer(reader)._fft_from_batch()[0]

    def test_idle_before_takeoff_does_not_stand_for_the_flight(self):
        batches = [_tone_batch(6.0, 50.0, amplitude=8000.0),
                   _tone_batch(30.0, 140.0), _tone_batch(60.0, 140.0)]
        unknown = _reader()
        del unknown._messages["CTUN"]
        assert self._spectrum_peak(unknown, batches) == pytest.approx(
            50.0, abs=2.0)  # precondition: armed at 6 s, and the loudest

        assert self._spectrum_peak(_reader(), batches) == pytest.approx(
            140.0, abs=2.0)

    def test_quadplane_cruise_does_not_stand_for_the_hover(self):
        """Lift motors stopped, forward motor running: not what a notch
        that follows the lift throttle is for."""
        reader = _reader(vehicle="ArduPlane", params={"Q_ENABLE": 1},
                         throttle_message="QTUN", flying=(10.0, 30.0))
        reader._armed = [(5.0, 95.0)]
        batches = [_tone_batch(20.0, 83.0)] + [
            _tone_batch(float(at), 31.0, amplitude=6000.0)
            for at in range(40, 90, 5)]

        assert self._spectrum_peak(reader, batches) == pytest.approx(
            83.0, abs=2.0)

    def test_no_batch_with_the_throttle_up_gives_no_peak(self):
        analyzer = _analyzer(_reader())
        analyzer.reader.isb_batches = {0: _tone_batch(6.0, 50.0)}

        peak, _figures, details = analyzer._fft_from_batch()

        assert peak is None
        assert details == [t("notch_tuning.no_gyro_batch")]

    def test_log_without_its_throttle_keeps_the_armed_batches(self):
        reader = _reader()
        del reader._messages["CTUN"]
        batches = [_tone_batch(2.0, 300.0, amplitude=8000.0),
                   _tone_batch(30.0, 140.0)]

        assert self._spectrum_peak(reader, batches) == pytest.approx(
            140.0, abs=2.0)


class TestEscInFlight:
    """Whether ESC telemetry can drive a notch is a question about the
    flight. Asked of the whole log, a long wait on the ground with RPM at 0
    read as telemetry that drops out."""

    def test_ground_time_does_not_disqualify(self):
        reader = _reader(esc_hz=140.0, flying=(40.0, 60.0))
        rpm = reader.get_messages_as_arrays("ESC", ["RPM"])["RPM"]
        assert np.mean(rpm > 0) < 0.30  # precondition: the old rule's limit

        assert _analyzer(reader)._esc_flight_hz() == pytest.approx(
            (140.0, 140.0, 140.0))

    def test_low_end_median_and_high_end_of_the_flight(self):
        low, median, high = _analyzer(_reader(esc_hz=_sweep))._esc_flight_hz()

        assert low == pytest.approx(114.0, abs=1.0)
        assert median == pytest.approx(150.0, abs=1.0)
        assert high == pytest.approx(186.0, abs=1.0)

    def test_esc_that_stays_silent_disqualifies(self):
        reader = _reader(esc_hz=140.0, silent=(2,))

        assert _analyzer(reader)._esc_flight_hz() is None

    def test_esc_that_drops_out_disqualifies(self):
        assert _analyzer(_reader(
            esc_hz=140.0, reporting=0.2))._esc_flight_hz() is None
        assert _analyzer(_reader(
            esc_hz=140.0, reporting=0.5))._esc_flight_hz() is not None

    def test_no_flight_gives_no_answer(self):
        reader = _reader(esc_hz=140.0, throttle=0.03)

        assert _analyzer(reader)._esc_flight_hz() is None

    def test_quadplane_reads_its_vtol_throttle(self):
        reader = _reader(esc_hz=140.0, vehicle="ArduPlane",
                         params={"Q_ENABLE": 1}, throttle_message="QTUN")

        assert _analyzer(reader)._esc_flight_hz() is not None

    def test_fixed_wing_has_no_hover_to_go_by(self):
        reader = _reader(esc_hz=140.0, vehicle="ArduPlane", throttle=45.0)

        assert _analyzer(reader)._flying(np.array([50.0])) is None
        assert _analyzer(reader)._esc_flight_hz() is None


class TestEscNotchFreq:
    """The wiki sets FREQ below the hover frequency and above
    INS_GYRO_FILTER: under ESC tracking it is the lowest frequency the
    notch follows."""

    def test_freq_is_the_low_end_of_the_flight(self):
        freq, low, median, _high = _analyzer(
            _reader(esc_hz=_sweep))._esc_notch_freq()

        assert freq == int(low)
        assert freq < median

    def test_gyro_filter_is_the_floor(self):
        reader = _reader(esc_hz=_sweep, params={"INS_GYRO_FILTER": 130})
        low = _analyzer(reader)._esc_flight_hz()[0]
        assert low < 130  # precondition

        assert _analyzer(reader)._esc_notch_freq()[0] == 131

    def test_no_room_under_the_hover_frequency(self):
        reader = _reader(esc_hz=_sweep, params={"INS_GYRO_FILTER": 160})
        assert _analyzer(reader)._esc_flight_hz() is not None  # precondition

        assert _analyzer(reader)._esc_notch_freq() is None


class TestNotchSet:
    def test_esc_set_follows_the_motors(self):
        details: list[str] = []
        analyzer = _analyzer(_reader(esc_hz=_sweep))
        freq = analyzer._esc_notch_freq()[0]

        rows = analyzer._notch_set("INS_HNTCH", 300.0, details)

        assert _values(rows) == {
            "INS_HNTCH_ENABLE": "1", "INS_HNTCH_MODE": "3",
            "INS_HNTCH_FREQ": str(freq), "INS_HNTCH_BW": str(round(freq / 2)),
            "INS_HNTCH_REF": "1", "INS_HNTCH_HMNCS": "3",
            "INS_HNTCH_OPTS": "0"}
        assert freq != 300  # the spectrum's peak has no say in it

    def test_throttle_set_takes_the_measured_hover_however_low(self):
        """REF was 0.50 for any hover throttle under 0.2, which leaves the
        notch at FREQ until the throttle passes half."""
        analyzer = _analyzer(_reader(), hover=0.08)

        rows = analyzer._notch_set("INS_HNTCH", 140.0, [])

        assert _values(rows) == {
            "INS_HNTCH_ENABLE": "1", "INS_HNTCH_MODE": "1",
            "INS_HNTCH_FREQ": "140", "INS_HNTCH_BW": "70",
            "INS_HNTCH_REF": "0.08", "INS_HNTCH_HMNCS": "3",
            "INS_HNTCH_OPTS": "0"}

    def test_reliable_esc_wins_over_throttle(self):
        analyzer = _analyzer(_reader(esc_hz=140.0), hover=0.35)

        assert _values(analyzer._notch_set(
            "INS_HNTCH", 140.0, []))["INS_HNTCH_MODE"] == "3"

    @pytest.mark.parametrize("hover", [0.0, -1.0])
    def test_no_hover_and_no_esc_gives_no_set(self, hover):
        """0 is a log with no hover in it, -1 a fixed-wing only Plane, which
        cannot use throttle tracking at all."""
        details: list[str] = []
        analyzer = _analyzer(_reader(), hover=hover)

        assert analyzer._notch_set("INS_HNTCH", 140.0, details) == []
        assert details == [t("notch_tuning.no_notch_source")]

    def test_second_notch_takes_its_own_names(self):
        rows = _analyzer(_reader(esc_hz=140.0))._notch_set(
            "INS_HNTC2", 140.0, [])

        assert rows[0]["param"] == "INS_HNTC2_ENABLE"
        assert all(row["param"].startswith("INS_HNTC2_") for row in rows)
        assert len(rows) == 7

    @pytest.mark.parametrize("esc_hz", [None, _sweep])
    def test_rows_carry_a_priority_the_table_can_sort(self, esc_hz):
        """The table sorts by these labels and shows them. The high end of
        the ESC range once landed in this column."""
        rows = _analyzer(_reader(esc_hz=esc_hz))._notch_set(
            "INS_HNTCH", 140.0, [])
        high = t("notch_tuning.priority_high")
        mid = t("notch_tuning.priority_mid")

        assert len(rows) == 7  # precondition
        assert {row["param"][len("INS_HNTCH_"):]: row["priority"]
                for row in rows} == {
            "ENABLE": high, "MODE": high, "FREQ": high, "REF": high,
            "BW": mid, "HMNCS": mid, "OPTS": mid}

    def test_only_enable_has_a_current_value(self):
        rows = _analyzer(_reader(esc_hz=140.0))._notch_set(
            "INS_HNTCH", 140.0, [])

        assert {row["param"]: row["old"] for row in rows
                if row["old"] != "-"} == {"INS_HNTCH_ENABLE": "0"}


class TestPeakAgainstEsc:
    """The notch follows the motors, so the report says where the
    spectrum's peak sits against them. The spectrum is a mean over the
    flight: through climbs and descents it peaked at 119 Hz for motors
    whose median was 136 Hz, and that is still the motors."""

    RANGE = {"lo": "105", "hi": "187"}

    @pytest.mark.parametrize("peak, key, extra", [
        (119.0, "notch_tuning.esc_peak_match", {}),
        (136.0, "notch_tuning.esc_peak_match", {}),
        (200.0, "notch_tuning.esc_peak_match", {}),
        (93.0, "notch_tuning.esc_peak_match", {}),
        (300.0, "notch_tuning.esc_peak_harmonic", {"k": 2}),
        (66.0, "notch_tuning.esc_peak_other", {}),
        (700.0, "notch_tuning.esc_peak_other", {}),
    ])
    def test_wording(self, peak, key, extra):
        assert NotchTuningAnalyzer._peak_against_esc(
            peak, 105.0, 187.0) == t(key, peak=f"{peak:.0f}", **self.RANGE,
                                     **extra)

    def test_third_harmonic_is_outside_the_suggested_mask(self):
        """D板: 412 Hz on motors the ESCs put at 139 Hz."""
        text = NotchTuningAnalyzer._peak_against_esc(412.0, 130.0, 146.0)

        assert text == (t("notch_tuning.esc_peak_harmonic",
                          peak="412", lo="130", hi="146", k=3)
                        + t("notch_tuning.esc_third_harmonic"))

    def test_peak_between_harmonics_is_not_the_motors(self):
        assert NotchTuningAnalyzer._peak_against_esc(
            200.0, 130.0, 146.0) == t("notch_tuning.esc_peak_other",
                                      peak="200", lo="130", hi="146")


class TestEnabledNotch:
    STATIC = {"INS_HNTCH_ENABLE": 1, "INS_HNTCH_MODE": 0, "INS_HNTCH_REF": 0,
              "INS_HNTCH_FREQ": 120, "INS_HNTCH_BW": 40}

    def _check(self, analyzer, peak):
        status, details, recs = analyzer._check_notch_filter("", peak)
        return status, details, recs

    def _flat(self, analyzer):
        freqs = np.linspace(1.0, 500.0, 500)
        flat = np.ones(500)
        analyzer._gyro_spectrum = (freqs, flat, flat, flat)

    @pytest.mark.parametrize("mode", [2, 3, 5])
    def test_tracking_mode_without_reference_is_fixed(self, mode):
        """AP_Vehicle::update_dynamic_notch returns at REF 0 before it
        looks at the mode."""
        analyzer = _analyzer(_reader(params={
            **self.STATIC, "INS_HNTCH_MODE": mode}))

        status, details, recs = self._check(analyzer, 120.0)

        assert status == "warning"
        assert any("INS_HNTCH_REF=0" in line for line in details)
        assert _values(recs) == {"INS_HNTCH_REF": "1"}
        assert not any(t("notch_tuning.dynamic_mode", desc="")[:6] in line
                       for line in details)

    def test_throttle_mode_without_reference_gets_the_hover(self):
        """The check used to stop at a hover throttle of 0.2."""
        analyzer = _analyzer(_reader(params={
            **self.STATIC, "INS_HNTCH_MODE": 1}), hover=0.08)

        status, details, recs = self._check(analyzer, 120.0)

        assert status == "warning"
        assert _values(recs)["INS_HNTCH_REF"] == "0.08"

    def test_notch_that_tracks_is_not_graded(self):
        analyzer = _analyzer(_reader(params={
            **self.STATIC, "INS_HNTCH_MODE": 3, "INS_HNTCH_REF": 1}))
        self._flat(analyzer)

        status, details, recs = self._check(analyzer, 120.0)

        assert status == "good"
        assert t("notch_tuning.atten_tracking") in details
        assert recs == []

    def test_peak_left_in_a_fixed_notch_is_reported_not_graded(self):
        """Flown: a notch that took 97.7% off its peak left a peak 10 dB
        over the floor, and no dip. That read critical."""
        analyzer = _analyzer(_reader(params=self.STATIC), hover=0.0)
        self._flat(analyzer)
        atten = analyzer._measure_notch_attenuation(120, 40)
        assert atten["atten_db"] < Thresholds().notch_atten_crit  # precondition

        status, details, _recs = self._check(analyzer, 120.0)

        assert status == "good"
        assert t("notch_tuning.atten_residual",
                 peak="120", lo="100", hi="140") in details

    def test_fixed_notch_on_the_motors_is_offered_esc_tracking(self):
        """ESC tracking turns FREQ into a lower limit, so the value the
        fixed notch was told to move to must not go out with it."""
        analyzer = _analyzer(_reader(
            esc_hz=_sweep, params={**self.STATIC, "INS_HNTCH_FREQ": 100}))
        freq = analyzer._esc_notch_freq()[0]

        _status, details, recs = self._check(analyzer, 140.0)

        assert t("notch_tuning.freq_drift", freq="100", peak="140",
                 diff="40") in details  # precondition: FREQ was to move
        assert [r["new"] for r in recs
                if r["param"] == "INS_HNTCH_FREQ"] == [str(freq)]
        assert _values(recs) == {
            "INS_HNTCH_MODE": "3", "INS_HNTCH_REF": "1",
            "INS_HNTCH_FREQ": str(freq), "INS_HNTCH_BW": str(round(freq / 2))}

    def test_esc_notch_that_cannot_follow_down_is_lowered(self):
        """Flown: FREQ 119 left 0.93 deg/s of rate error, 105 left 0.41."""
        analyzer = _analyzer(_reader(esc_hz=_sweep, params={
            **self.STATIC, "INS_HNTCH_MODE": 3, "INS_HNTCH_REF": 1,
            "INS_HNTCH_FREQ": 150, "INS_HNTCH_BW": 50}))
        freq = analyzer._esc_notch_freq()[0]
        assert freq < 150  # precondition

        status, details, recs = self._check(analyzer, None)

        assert status == "warning"
        assert _values(recs) == {"INS_HNTCH_FREQ": str(freq),
                                 "INS_HNTCH_BW": str(round(50 * freq / 150))}

    def test_esc_notch_under_the_flight_is_left_alone(self):
        analyzer = _analyzer(_reader(esc_hz=_sweep, params={
            **self.STATIC, "INS_HNTCH_MODE": 3, "INS_HNTCH_REF": 1,
            "INS_HNTCH_FREQ": 80, "INS_HNTCH_BW": 40}))
        assert analyzer._esc_notch_freq()[0] > 80  # precondition

        status, _details, recs = self._check(analyzer, None)

        assert (status, recs) == ("good", [])

    def test_fixed_notch_without_esc_is_offered_throttle_tracking(self):
        analyzer = _analyzer(_reader(params=self.STATIC), hover=0.12)

        _status, _details, recs = self._check(analyzer, 120.0)

        assert _values(recs) == {"INS_HNTCH_MODE": "1",
                                 "INS_HNTCH_REF": "0.12"}


def _hover_reader(throttle: float, height: float, climb: float = 0.0,
                  roll: float = 0.0, params=None) -> SyntheticReader:
    """60 s armed at one throttle, height, climb rate and bank, with a
    138 Hz tone on the gyros."""
    n = 1500
    ts = np.linspace(0.0, 60.0, n)
    fast = np.arange(60_000) / 1000.0
    tone = np.sin(2 * np.pi * 138.0 * fast)
    alt = np.full(n, height)
    alt[0] = 0.0  # the log opens on the ground
    return SyntheticReader(
        messages={
            "CTUN": {"_timestamp": ts, "ThO": np.full(n, throttle),
                     "Alt": alt, "CRt": np.full(n, climb)},
            "ATT": {"_timestamp": ts, "Roll": np.full(n, roll),
                    "Pitch": np.zeros(n), "DesRoll": np.full(n, roll),
                    "DesPitch": np.zeros(n)},
            "RCOU": {"_timestamp": ts, "C1": np.full(n, 1500.0)},
            "IMU": {"_timestamp": fast, "I": np.zeros(len(fast)),
                    "GyrX": tone, "GyrY": tone, "GyrZ": tone},
        },
        params={"MOT_THST_HOVER": 0.34, **(params or {})},
        armed_intervals=[(0.0, 60.0)])


class TestHoverIsMeasuredInTheAir:
    """The hover throttle was the median over every armed row above 5%
    throttle, so a ground run was a hover: one QuadPlane sat on the ground at
    16% and read "hover throttle severely off" for an aircraft that hovers
    at 34%, with Q_M_THST_HOVER 0.16 to set."""

    def _analyze(self, reader):
        return NotchTuningAnalyzer(reader, Thresholds(), _vehicle()).analyze()

    def test_ground_run_is_not_a_hover(self):
        reader = _hover_reader(throttle=0.16, height=0.1)
        thresholds = Thresholds()
        assert abs(0.16 - 0.34) > thresholds.notch_hover_error_crit  # precondition

        result = self._analyze(reader)

        assert result.status == "info"
        assert result.param_suggestions == []
        assert t("notch_tuning.hover_too_short") in result.details
        assert result.summary == t("notch_tuning.summary_no_spectrum")

    def test_low_hover_is_a_hover(self):
        """Hover tests are flown as low as 1.7 m."""
        result = self._analyze(_hover_reader(throttle=0.34, height=1.7))

        suggested = {s["param"]: s["suggested"]
                     for s in result.param_suggestions}
        assert suggested["INS_HNTCH_REF"] == 0.34
        assert suggested["INS_HNTCH_FREQ"] == pytest.approx(138.0, abs=2.0)
        assert not any("THST_HOVER" in name for name in suggested)

    def test_hover_far_from_the_parameter_is_still_critical(self):
        result = self._analyze(_hover_reader(throttle=0.60, height=10.0))

        assert result.status == "critical"
        assert {s["param"]: s["suggested"] for s in result.param_suggestions
                }["MOT_THST_HOVER"] == 0.6

    @pytest.mark.parametrize("climb, roll", [(2.0, 0.0), (0.0, 20.0)])
    def test_climb_or_bank_is_not_a_hover(self, climb, roll):
        """The firmware learns the parameter in level hover only, and a
        climb at 60% throttle is not a hover at 60%."""
        analyzer = NotchTuningAnalyzer(
            _hover_reader(throttle=0.60, height=10.0, climb=climb, roll=roll),
            Thresholds(), _vehicle())

        assert analyzer._compute_hover_throttle("") == 0.0

    def test_spectrum_without_a_peak_is_still_a_pass(self):
        reader = _hover_reader(throttle=0.34, height=10.0)
        rng = np.random.default_rng(3)
        for axis in ("GyrX", "GyrY", "GyrZ"):
            reader._messages["IMU"][axis] = rng.normal(0.0, 1.0, 60_000)

        result = self._analyze(reader)

        assert t("notch_tuning.notch_disabled_no_fft") in result.details
        assert result.summary == t("notch_tuning.summary_pass")


def _flight_log(tmp_path, esc_hz=None, tone_hz=138.0):
    """A copter flight with a gyro tone, and ESC telemetry if asked."""
    log = binlog.flight_log("copter", batches=3, tone_hz=tone_hz)
    if esc_hz is not None:
        log.declare("ESC", *ESC_FORMAT)
        for tick in range(1000):
            time_us = 2_000_000 + tick * 40_000
            armed = 50 <= tick < 950
            for instance in range(4):
                rpm = esc_hz(tick * 0.04) * 60.0 if armed else 0.0
                log.write("ESC", time_us, instance, rpm, rpm, 24.0, 5.0,
                          30.0, 0.0, 30.0, 0.0)
    return log.save(tmp_path / "flight.bin")


def _analyze(path):
    reader = LogReader(path, preload_types=["IMU", "RCOU", "RATE", "CTUN",
                                            "ESC", "VIBE", "ATT"])
    return NotchTuningAnalyzer(
        reader, Thresholds.from_params(reader.params,
                                       reader.canonical_vehicle_type),
        detect_vehicle(reader)).analyze()


class TestWholeFlight:
    def test_flight_with_esc_telemetry_gets_a_notch_that_tracks(self, tmp_path):
        result = _analyze(_flight_log(
            tmp_path, esc_hz=lambda secs: 120.0 + secs))

        suggested = {s["param"]: s["suggested"]
                     for s in result.param_suggestions}
        assert suggested["INS_HNTCH_ENABLE"] == 1.0
        assert suggested["INS_HNTCH_MODE"] == 3.0
        assert suggested["INS_HNTCH_REF"] == 1.0
        assert 120.0 <= suggested["INS_HNTCH_FREQ"] < 140.0
        assert suggested["INS_HNTCH_BW"] == round(
            suggested["INS_HNTCH_FREQ"] / 2)
        assert result.action_level == "apply_param"

    def test_flight_without_esc_gets_throttle_tracking(self, tmp_path):
        result = _analyze(_flight_log(tmp_path))

        suggested = {s["param"]: s["suggested"]
                     for s in result.param_suggestions}
        assert suggested["INS_HNTCH_MODE"] == 1.0
        assert suggested["INS_HNTCH_REF"] == 0.35
        assert suggested["INS_HNTCH_FREQ"] == pytest.approx(138.0, abs=2.0)

    def test_peak_without_a_flight_to_go_by_is_not_a_pass(self, tmp_path,
                                                          monkeypatch):
        """45 corpus logs had a peak, no hover throttle and no usable ESC
        RPM. Each got a full throttle-tracking set with REF at 0.50."""
        monkeypatch.setattr(NotchTuningAnalyzer, "_compute_hover_throttle",
                            lambda self, q_prefix: 0.0)

        result = _analyze(_flight_log(tmp_path))

        assert any(t("notch_tuning.notch_disabled_info", freq=freq)
                   in result.details for freq in ("138", "139")
                   )  # precondition: there is a peak to act on
        assert [s for s in result.param_suggestions
                if s["param"].startswith("INS_HNT")] == []
        assert t("notch_tuning.no_notch_source") in result.details
        assert result.summary == t("notch_tuning.summary_no_source")
        assert result.status == "info"
        assert result.action_level == "ok"
