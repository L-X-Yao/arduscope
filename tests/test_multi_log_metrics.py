"""multi_report.extract_metrics against logs in the real on-disk format.

The comparison report grades these numbers against the converge_*
thresholds, so a metric that reads the wrong field turns into a verdict.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import pytest

import batch_report
import binlog
from lib.i18n import t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from multi_report import extract_metrics, generate_html
from tests.test_synthetic_coverage import SyntheticReader

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

T0_US = 2_000_000
TICK_US = 100_000
ARM_S = 10
DISARM_S = 50
END_S = 60


def _ticks():
    """(time_us, seconds, armed) over a log armed from 10 s to 50 s."""
    for i in range(END_S * 10):
        secs = i / 10
        yield T0_US + i * TICK_US, secs, ARM_S <= secs <= DISARM_S


def _with_arm_events(log: binlog.BinLog, time_us: int, secs: float) -> None:
    if secs == ARM_S:
        log.write("EV", time_us, 10)
    if secs == DISARM_S:
        log.write("EV", time_us, 11)


def _modern_vibe_log(path, boot_clips: int, armed_rise: int) -> LogReader:
    """Three IMUs, one Clip counter column, as firmware 4.5 and 4.6 log it."""
    log = binlog.BinLog()
    for time_us, secs, armed in _ticks():
        _with_arm_events(log, time_us, secs)
        progress = min(max(secs - ARM_S, 0), DISARM_S - ARM_S)
        clips = boot_clips + int(armed_rise * progress / (DISARM_S - ARM_S))
        for imu in (0, 1, 2):
            log.write("VIBE", time_us, imu, 5.0, 5.0, 6.0, clips)
    return LogReader(log.save(path))


class TestClipCount:
    def test_counts_clips_from_the_field_real_logs_carry(self, tmp_path):
        reader = _modern_vibe_log(tmp_path / "a.bin", 0, 5000)
        raw = reader.get_messages_as_arrays("VIBE", ["Clip"])["Clip"]
        assert raw.max() == 5000                     # precondition
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_clip"] == 15000          # 3 IMUs x 5000

    def test_clips_before_arming_are_not_flight_clips(self, tmp_path):
        reader = _modern_vibe_log(tmp_path / "a.bin", 3000, 0)
        raw = reader.get_messages_as_arrays("VIBE", ["Clip"])["Clip"]
        assert raw.max() == 3000                     # precondition
        assert extract_metrics(reader, Thresholds())["max_clip"] == 0

    def test_old_firmware_columns_still_count(self, tmp_path):
        log = binlog.BinLog()
        log.declare("VIBE", "QfffIII",
                    "TimeUS,VibeX,VibeY,VibeZ,Clip0,Clip1,Clip2")
        for time_us, secs, armed in _ticks():
            _with_arm_events(log, time_us, secs)
            rise = int((secs - ARM_S) * 100) if armed else 0
            rise = 4000 if secs >= DISARM_S else rise
            log.write("VIBE", time_us, 5.0, 5.0, 6.0, rise, 2 * rise, 0)
        reader = LogReader(log.save(tmp_path / "old.bin"))
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_clip"] > 3 * 3000

    def test_no_clip_column_is_no_data_not_zero(self, tmp_path):
        log = binlog.BinLog()
        log.declare("VIBE", "Qfff", "TimeUS,VibeX,VibeY,VibeZ")
        for time_us, _, _ in _ticks():
            log.write("VIBE", time_us, 5.0, 5.0, 6.0)
        reader = LogReader(log.save(tmp_path / "none.bin"))
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_vibe"] == pytest.approx(6.0)  # precondition
        assert metrics["max_clip"] is None


GROUND_ALT_M = 400.0


def _hover_log(path, *, tho: float, climb_ms: float = 0.0, roll: float = 0.0,
               alt_m: float = 20.0, hover_param: float = 0.35,
               forward_throttle: float = 0.0, ground_tho: float = 0.08,
               params: dict | None = None):
    """QuadPlane: spool up on the ground, then hold a hover at tho."""
    log = binlog.BinLog()
    log.write("MSG", T0_US, "ArduPlane V4.6.3 (abc12345)")
    table = {"Q_ENABLE": 1, "Q_M_THST_HOVER": hover_param, "THR_MIN": 0,
             **(params or {})}
    for name, value in table.items():
        log.write("PARM", T0_US, name, float(value), float(value))
    log.declare("CTUN", "QccccffffBfi",
                "TimeUS,NavRoll,Roll,NavPitch,Pitch,ThO,RdO,ThD,As,AsT,E2T,GU")
    for time_us, secs, armed in _ticks():
        _with_arm_events(log, time_us, secs)
        flying = armed and secs >= ARM_S + 10
        log.write("ATT", time_us, roll, roll, 0.0, 0.0, 0.0, 0.0, 3)
        log.write("CTUN", time_us, 0.0, 0.0, 0.0, 0.0,
                  forward_throttle if flying else 0.0, 0.0, 0.0, 0.0, 0, 0.0,
                  0)
        log.write("QTUN", time_us, tho, 0.0,
                  tho if flying else ground_tho, hover_param, 0.0,
                  alt_m if flying else 0.0, 0.0, 0.0,
                  climb_ms if flying else 0.0, 0.0, 0, 0)
        log.write("POS", time_us, 34.1, 108.9,
                  GROUND_ALT_M + (alt_m if flying else 0.0), 0.0, 0.0)
    return LogReader(log.save(path))


class TestHoverThrust:
    def test_matching_hover_reads_as_matching(self, tmp_path):
        # Thrust 0.35 at the configured 0.35. The ground spool-up at 0.08
        # must not drag the estimate down.
        reader = _hover_log(tmp_path / "a.bin", tho=0.35)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["mean_hover_thrust"] == pytest.approx(0.35, abs=1e-3)
        assert metrics["hover_thr_err"] == pytest.approx(0.0, abs=1e-3)

    def test_wrong_parameter_is_caught(self, tmp_path):
        # The real log_87 case: hover at 0.32 with the parameter left at 0.2.
        reader = _hover_log(tmp_path / "a.bin", tho=0.32, hover_param=0.2)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["hover_thr_err"] == pytest.approx(0.12, abs=1e-3)
        assert metrics["hover_thr_err"] > 2 * Thresholds().converge_hover_thr_err

    def test_quadplane_ignores_a_stale_copter_parameter(self, tmp_path):
        reader = _hover_log(tmp_path / "a.bin", tho=0.35,
                            params={"MOT_THST_HOVER": 0.9})
        assert reader.params["MOT_THST_HOVER"] == pytest.approx(0.9)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["hover_thr_err"] == pytest.approx(0.0, abs=1e-3)

    def test_tailsitter_lift_motor_is_not_a_forward_motor(self, tmp_path):
        reader = _hover_log(tmp_path / "a.bin", tho=0.35,
                            forward_throttle=80.0,
                            params={"Q_TAILSIT_ENABLE": 1})
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["mean_hover_thrust"] == pytest.approx(0.35, abs=1e-3)

    def test_copter_reads_ctun(self, tmp_path):
        log = binlog.BinLog()
        log.write("MSG", T0_US, "ArduCopter V4.6.3 (abc12345)")
        log.write("PARM", T0_US, "FRAME_CLASS", 1.0, 1.0)
        log.write("PARM", T0_US, "MOT_THST_HOVER", 0.25, 0.25)
        for time_us, secs, armed in _ticks():
            _with_arm_events(log, time_us, secs)
            flying = armed and secs >= ARM_S + 10
            log.write("ATT", time_us, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 3)
            log.write("CTUN", time_us, 0.4, 0.0, 0.4 if flying else 0.05,
                      0.25, 0.0, 20.0 if flying else 0.0, 0.0, 0.0, 0.0, 0.0,
                      0.0, 0.0)
        reader = LogReader(log.save(tmp_path / "c.bin"))
        assert reader.is_copter                      # precondition
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["hover_thr_err"] == pytest.approx(0.15, abs=1e-3)

    @pytest.mark.parametrize("change", [
        {"climb_ms": 2.0}, {"roll": 20.0}, {"alt_m": 0.3},
        {"forward_throttle": 80.0},
    ], ids=["climbing", "banked", "not-airborne", "forward-motor-running"])
    def test_outside_the_firmware_hover_gate_is_no_estimate(
            self, tmp_path, change):
        reader = _hover_log(tmp_path / "a.bin", tho=0.6, **change)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["mean_hover_thrust"] is None
        assert metrics["hover_thr_err"] is None

    def test_pure_plane_has_no_hover_thrust(self, tmp_path):
        log = binlog.BinLog()
        log.write("MSG", T0_US, "ArduPlane V4.6.3 (abc12345)")
        log.write("PARM", T0_US, "Q_ENABLE", 0.0, 0.0)
        for time_us, _, _ in _ticks():
            log.write("RCOU", time_us, *([1500] * 14))
        reader = LogReader(log.save(tmp_path / "p.bin"))
        assert reader.is_plane and not reader.is_quadplane   # precondition
        assert extract_metrics(reader, Thresholds())["mean_hover_thrust"] is None


def _ekf_log(path, *, boot=0.1, primary=0.1, backup=0.1, primary_index=0):
    """Two EKF cores, with XKF4 rows as firmware 4.6 writes them."""
    log = binlog.BinLog()
    for time_us, secs, armed in _ticks():
        _with_arm_events(log, time_us, secs)
        for core in (0, 1):
            lane = primary if core == primary_index else backup
            value = lane if armed else boot
            log.write("XKF4", time_us, core, value, 0.05, 0.05, 0.05, 0.0,
                      0.0, 0.0, 0.0, 0, 0, 0, 0, primary_index)
    return LogReader(log.save(path))


class TestEkfVariance:
    def test_boot_transient_is_not_flight_variance(self, tmp_path):
        reader = _ekf_log(tmp_path / "a.bin", boot=2.5)
        sv = reader.get_messages_as_arrays("XKF4", ["SV"])["SV"]
        assert sv.max() == pytest.approx(2.5)        # precondition
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_ekf_var"] == pytest.approx(0.1)

    def test_backup_lane_spike_does_not_count(self, tmp_path):
        reader = _ekf_log(tmp_path / "a.bin", backup=2.5)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_ekf_var"] == pytest.approx(0.1)

    def test_follows_the_primary_core_the_log_names(self, tmp_path):
        # Core 1 is flying the vehicle, so its variance is the real one.
        reader = _ekf_log(tmp_path / "a.bin", primary=2.5, primary_index=1)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_ekf_var"] == pytest.approx(2.5)


class TestVibrationPeak:
    def _log(self, path, *, ground=5.0, other_imu=5.0):
        log = binlog.BinLog()
        for time_us, secs, armed in _ticks():
            _with_arm_events(log, time_us, secs)
            for imu in (0, 1):
                level = other_imu if imu == 1 else 5.0
                value = level if armed else ground
                log.write("VIBE", time_us, imu, value, value, value, 0)
        return LogReader(log.save(path))

    def test_ground_knock_is_not_flight_vibration(self, tmp_path):
        reader = self._log(tmp_path / "a.bin", ground=60.0)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_vibe"] == pytest.approx(5.0)

    def test_other_imu_is_not_the_primary_reading(self, tmp_path):
        reader = self._log(tmp_path / "a.bin", other_imu=60.0)
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["max_vibe"] == pytest.approx(5.0)


def _ekf_verdict(limit_source: Thresholds) -> str:
    """The EKF line of a report on two flights at variance 0.45."""
    metrics = extract_metrics(SyntheticReader(messages={}), limit_source)
    metrics["max_ekf_var"] = 0.45
    report = generate_html(["a.BIN", "b.BIN"], [dict(metrics), dict(metrics)])
    label = t("trend.metric.max_ekf_var")
    return next(line for line in
                re.findall(r'class="trend-text">([^<]+)<', report)
                if label in line)


class TestConvergenceLimits:
    def test_limit_follows_the_aircraft_fs_ekf_thresh(self):
        relaxed = Thresholds.from_params({"FS_EKF_THRESH": 1.0}, "ArduCopter")
        assert relaxed.converge_ekf_var_max == pytest.approx(0.5)
        assert "✅" in _ekf_verdict(relaxed)

    def test_default_limit_still_applies(self):
        assert Thresholds().converge_ekf_var_max == pytest.approx(0.4)
        assert "✅" not in _ekf_verdict(Thresholds())

    def test_batch_metrics_carry_the_limit(self, tmp_path):
        path = binlog.flight_log(
            "copter", params={"FS_EKF_THRESH": 1.0}).save(tmp_path / "a.bin")
        result = batch_report.analyze_one(
            path, str(tmp_path), no_ai=True, no_map=True,
            no_fleet_record=True)
        assert result["error"] is None               # precondition
        limits = result["metrics"]["converge"]
        assert limits["converge_ekf_var_max"] == pytest.approx(0.5)


class TestFlightTime:
    def test_gps_week_becomes_utc_seconds(self, tmp_path):
        path = binlog.flight_log("copter").save(tmp_path / "a.bin")
        metrics = extract_metrics(LogReader(path), Thresholds())
        # GPS week 2300 starts on 2024-02-04, and the log's first fix is
        # 100 s into it (leap seconds ignored, as in the fleet archive).
        assert metrics["flight_time"] == pytest.approx(
            datetime(2024, 2, 4, 0, 1, 40, tzinfo=timezone.utc).timestamp(),
            abs=1.0)

    def test_no_gps_time_is_none(self):
        metrics = extract_metrics(SyntheticReader(messages={}), Thresholds())
        assert metrics["flight_time"] is None


class TestIdentity:
    def test_a_log_says_which_aircraft_it_is(self, tmp_path):
        path = binlog.flight_log("quadplane").save(tmp_path / "a.bin")
        metrics = extract_metrics(LogReader(path), Thresholds())
        assert metrics["serial"] == "001A002B-3030510C-31323334"
        assert metrics["vehicle"] == "ArduPlane"
        assert metrics["firmware"] == "V4.6.3"

    def test_a_renamed_banner_is_still_the_same_family(self, tmp_path):
        log = binlog.BinLog()
        log.write("MSG", T0_US, "Acme-FC V4.6.3 (abc12345)")
        log.write("PARM", T0_US, "Q_ENABLE", 1.0, 1.0)
        reader = LogReader(log.save(tmp_path / "a.bin"))
        assert reader.vehicle_type != "ArduPlane"    # precondition
        assert reader.is_plane                       # precondition
        assert extract_metrics(reader, Thresholds())["vehicle"] == "ArduPlane"

    def test_unknown_identity_is_none(self):
        reader = SyntheticReader(messages={}, vehicle_type="Unknown")
        reader.firmware_version = "Unknown"
        metrics = extract_metrics(reader, Thresholds())
        assert metrics["serial"] is None
        assert metrics["vehicle"] is None
        assert metrics["firmware"] is None


class TestFlew:
    def _log(self, path, climb_m, armed=True):
        log = binlog.BinLog()
        for time_us, secs, _ in _ticks():
            if armed:
                _with_arm_events(log, time_us, secs)
            progress = min(max(secs - ARM_S, 0), DISARM_S - ARM_S)
            log.write("POS", time_us, 34.1, 108.9,
                      GROUND_ALT_M + climb_m * progress / DISARM_S, 0.0, 0.0)
        return LogReader(log.save(path))

    def test_climbing_is_flying(self, tmp_path):
        reader = self._log(tmp_path / "a.bin", 20.0)
        assert extract_metrics(reader, Thresholds())["flew"] is True

    def test_armed_without_climbing_is_a_ground_run(self, tmp_path):
        reader = self._log(tmp_path / "a.bin", 0.0)
        assert reader.get_armed_intervals()          # precondition
        assert extract_metrics(reader, Thresholds())["flew"] is False

    def test_no_arm_event_cannot_be_called_a_ground_run(self, tmp_path):
        reader = self._log(tmp_path / "a.bin", 0.0, armed=False)
        assert reader.get_armed_intervals() == []    # precondition
        assert extract_metrics(reader, Thresholds())["flew"] is None


class TestCliRejection:
    def _run(self, args, cwd):
        return subprocess.run(
            [sys.executable, os.path.join(_ROOT, "multi_report.py"), *args],
            cwd=cwd, capture_output=True, text=True, timeout=120,
            encoding="utf-8")

    def test_file_with_no_flight_data_is_rejected(self, tmp_path):
        good = binlog.flight_log("copter").save(tmp_path / "good.bin")
        zeros = tmp_path / "zeros.BIN"
        zeros.write_bytes(b"\0" * 200_000)
        out = tmp_path / "out.html"
        result = self._run([good, str(zeros), "-o", str(out), "--no-open",
                            "--lang", "en"], tmp_path)
        assert result.returncode == 0, result.stderr
        assert "not an analyzable flight log" in result.stdout
        report = out.read_text(encoding="utf-8")
        table = report[report.index('<table class="comparison-table">'):]
        assert "zeros.BIN" in table                  # still shown, as N/A
        assert "<td>N/A</td>" in table


class TestBatchCompare:
    def test_failed_log_stays_visible_in_the_comparison(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        for name in ("a.bin", "b.bin"):
            binlog.flight_log("copter", seed=len(name)).save(logs / name)
        (logs / "z_corrupt.bin").write_bytes(b"\0" * 200_000)
        out = tmp_path / "out"
        result = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "batch_report.py"),
             str(logs), "-o", str(out), "--compare", "--no-open", "--no-map",
             "--no-ai", "--no-fleet-record", "--lang", "en"],
            capture_output=True, text=True, timeout=240, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        report = (out / "batch_comparison.html").read_text(encoding="utf-8")
        table = report[report.index('<table class="comparison-table">'):]
        assert "z_corrupt.bin" in table

