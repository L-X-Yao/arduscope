"""
Regression tests for EKFVelDivergenceAnalyzer: per-core EKF-vs-GPS 3D
speed divergence windows (the sprint "GPS 眩晕" signature).
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.ekf_vel_divergence import EKFVelDivergenceAnalyzer
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast


def _vehicle() -> VehicleInfo:
    return VehicleInfo(
        vehicle_type="ArduCopter", firmware_version="4.6.3",
        board_name="synthetic", frame_class=1, motor_count=4,
        imu_count=1, is_quadplane=False,
    )


def _reader(diverge=False, two_cores=False, status=4.0, flying=1.0,
            gps_slice=None):
    """Synthetic 150 s flight.

    status/flying drive the two gates (GPS 3D fix, STAT.isFlying) and are
    dropped from the log entirely when None; gps_slice restricts which
    GPS samples exist at all, for the dropout-coverage case.
    """
    n = 600
    ts = np.linspace(0, 150, n)
    g_ts = ts if gps_slice is None else ts[gps_slice]
    gps = {
        "_timestamp": g_ts, "I": np.zeros(len(g_ts)),
        "Spd": np.full(len(g_ts), 10.0), "VZ": np.full(len(g_ts), -2.0),
    }
    if status is not None:
        gps["Status"] = np.full(len(g_ts), status)
    vn = np.full(n, 10.0)
    if diverge:
        vn[300:340] = np.linspace(10.0, 22.0, 40)   # ~10s runaway to 2.2x
    msgs = {"GPS": gps, "XKF1": {
        "_timestamp": ts, "C": np.zeros(n),
        "VN": vn, "VE": np.zeros(n), "VD": np.full(n, -2.0),
    }}
    if flying is not None:
        msgs["STAT"] = {"_timestamp": ts, "isFlying": np.full(n, flying)}
    if two_cores:
        msgs["XKF1"]["C"] = np.tile([0.0, 1.0], n // 2)
    return SyntheticReader(messages=msgs, armed_intervals=[(0.0, 150.0)])


def _run(reader):
    return EKFVelDivergenceAnalyzer(
        reader, Thresholds.from_params(reader.params, reader.vehicle_type),
        _vehicle()).analyze()


class TestEKFVelDivergence:

    def test_clean_good(self):
        res = _run(_reader())
        assert res.status == "good"
        assert res.action_level == "ok"

    def test_divergence_flagged(self):
        res = _run(_reader(diverge=True))
        assert res.status in ("warning", "critical"), res.status
        assert res.details, "expected at least one window detail"
        joined = " ".join(res.details)
        assert "core 0" in joined, res.details

    def test_divergence_critical_when_deep(self):
        # 2.2x GPS for ~10s: both the 40% and 5s criteria trip
        res = _run(_reader(diverge=True))
        assert res.status == "critical", (res.status, res.summary)

    def test_no_ekf_info(self):
        n = 100
        ts = np.linspace(0, 50, n)
        reader = SyntheticReader(
            messages={"GPS": {"_timestamp": ts, "I": np.zeros(n),
                              "Spd": np.full(n, 5.0),
                              "VZ": np.zeros(n)}},
            armed_intervals=[(0.0, 50.0)])
        res = _run(reader)
        assert res.status == "info"


class TestGradingGates:
    """The 2026-07-24 corpus backtest traced 4 of 9 findings to samples
    that were never comparable in the first place."""

    def test_no_3d_fix_not_graded(self):
        # Status 1 = NO_FIX: Spd is meaningless, so a runaway EKF must
        # not be reported against it.
        res = _run(_reader(diverge=True, status=1.0))
        assert res.status == "info", res.summary

    def test_ground_run_not_graded(self):
        # The firmware never declared the vehicle flying: bench EKF drift.
        res = _run(_reader(diverge=True, flying=0.0))
        assert res.status == "info", res.summary

    def test_dropout_not_bridged(self):
        # GPS stops at t=50s; the divergence sits at t~75-85s. Without
        # the coverage guard np.interp clamps to the last GPS sample and
        # invents a window out of a dropout.
        res = _run(_reader(diverge=True, gps_slice=slice(0, 200)))
        assert res.status == "good", (res.status, res.summary)

    def test_no_fix_stretch_dropped_not_whole_log(self):
        # Real shape of the log_43 false positive: mid-flight the fix
        # drops, Spd reads 0 for 30 s while the EKF keeps flying at
        # 10 m/s. The stretch must be dropped, the flight still graded.
        reader = _reader()
        gps = reader._messages["GPS"]
        bad = (gps["_timestamp"] >= 60.0) & (gps["_timestamp"] <= 90.0)
        gps["Spd"] = np.where(bad, 0.0, gps["Spd"])
        gps["VZ"] = np.where(bad, 0.0, gps["VZ"])
        gps["Status"] = np.where(bad, 1.0, gps["Status"])
        res = _run(reader)
        assert res.status == "good", (res.status, res.summary)

    def test_stale_fix_status_with_no_satellites_dropped(self):
        # log_43 of the corpus holds Status 4 through a stretch where
        # NSats has already fallen to 0: the status field alone lets the
        # dropout through, the satellite count catches it.
        reader = _reader(diverge=True)
        gps = reader._messages["GPS"]
        sats = np.full(len(gps["_timestamp"]), 20.0)
        blind = (gps["_timestamp"] >= 70.0) & (gps["_timestamp"] <= 90.0)
        gps["NSats"] = np.where(blind, 0.0, sats)
        res = _run(reader)
        assert res.status == "good", (res.status, res.summary)

    def test_windows_do_not_merge_across_a_hole(self):
        # The coverage filter leaves holes in c_ts; two brief windows
        # either side of one must not read as a single 60 s window.
        c_ts = np.concatenate([np.arange(0.0, 2.0, 0.1),
                               np.arange(60.0, 62.0, 0.1)])
        v3d = np.full(len(c_ts), 20.0)
        gps_i = np.full(len(c_ts), 10.0)
        wins = EKFVelDivergenceAnalyzer._windows(
            c_ts, v3d, gps_i, np.abs(v3d - gps_i),
            np.ones(len(c_ts), dtype=bool), 0)
        assert len(wins) == 2, wins
        assert all(w[1] - w[0] < 3.0 for w in wins), wins

    def test_gates_absent_when_log_lacks_the_fields(self):
        # No Status column and no STAT: grade anyway rather than
        # silently disabling the check.
        res = _run(_reader(diverge=True, status=None, flying=None))
        assert res.status == "critical", (res.status, res.summary)


def _steady_reader(ekf_speed, gps_speed=20.0, cores=(0,), seconds=3.0):
    """60 s at 4 Hz: every core reads ekf_speed for `seconds`, from t=20."""
    ts = np.arange(0, 60, 0.25)
    run = (ts >= 20.0) & (ts < 20.0 + seconds)
    speed = np.where(run, ekf_speed, gps_speed)
    n = len(ts)
    return SyntheticReader(
        messages={
            "GPS": {"_timestamp": ts, "I": np.zeros(n),
                    "Spd": np.full(n, gps_speed), "VZ": np.zeros(n),
                    "Status": np.full(n, 4.0)},
            "XKF1": {"_timestamp": np.repeat(ts, len(cores)),
                     "C": np.tile(np.array(cores, dtype=float), n),
                     "VN": np.repeat(speed, len(cores)),
                     "VE": np.zeros(n * len(cores)),
                     "VD": np.zeros(n * len(cores))},
        },
        armed_intervals=[(0.0, 60.0)],
    )


class TestDivergenceNumbers:
    def test_cores_diverging_together_count_once(self):
        res = _run(_steady_reader(45.0, cores=(0, 1), seconds=10.0))
        assert res.status == "critical"
        assert len([d for d in res.details if "core" in d]) == 2
        assert "9.8" in res.summary, res.summary

    def test_two_cores_short_of_the_time_limit_stay_warning(self):
        # 3.5 s on each of two cores is 3.5 s of divergence, not the 7 s
        # that would pass CRIT_TOTAL_S. 25 against 20 m/s is a window, but
        # only 25% off GPS, so time alone decides here.
        res = _run(_steady_reader(25.0, cores=(0, 1), seconds=3.5))
        assert res.status == "warning"

    def test_percent_is_off_gps_when_ekf_runs_ahead(self):
        res = _run(_steady_reader(30.0, gps_speed=20.0))
        assert any("50%" in d for d in res.details), res.details
        assert res.status == "critical"       # 50% off GPS is past 40%

    def test_percent_is_off_gps_when_ekf_lags(self):
        res = _run(_steady_reader(10.0, gps_speed=20.0))
        assert any("50%" in d for d in res.details), res.details

    def test_stationary_gps_gives_a_finite_percent(self):
        res = _run(_steady_reader(9.0, gps_speed=0.0))
        assert res.status == "critical"
        assert any("450%" in d for d in res.details), res.details
