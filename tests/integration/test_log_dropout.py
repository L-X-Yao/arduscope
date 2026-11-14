"""
Fast tests for log_health's DSF dropout + LOG_REPLAY bloat detection.

Real-world calibration (2026-07-15 corpus scan): 19/20 customer logs
have Dp=0; the single LOG_REPLAY log dropped 116,017 messages with the
buffer starved to 487 B. Each test asserts its precondition first.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.log_health import LogHealthAnalyzer
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

N = 20


def _vehicle():
    return VehicleInfo(
        vehicle_type="ArduCopter", firmware_version="4.6.3",
        board_name="synthetic", frame_class=1, motor_count=4,
        imu_count=1, is_quadplane=False)


def _pm_healthy():
    return {
        "_timestamp": np.linspace(0.0, 100.0, N),
        "NLon": np.zeros(N),
        "NLoop": np.full(N, 4000.0),
        "MaxT": np.full(N, 2600.0),
        "Mem": np.full(N, 100000.0),
        "Load": np.full(N, 30.0),
    }


def _dsf(dp_final, fmn_min=180000):
    return {
        "_timestamp": np.linspace(0.0, 100.0, N),
        "Dp": np.linspace(0.0, float(dp_final), N),
        "FMn": np.linspace(190000.0, float(fmn_min), N),
    }


def _rfrh():
    return {"_timestamp": np.linspace(0.0, 100.0, N), "TF": np.zeros(N)}


def _run(messages, params=None):
    # armed ends at 90 s < log end 100 s so the abrupt-end forensics
    # (a different log_health check) stays quiet.
    reader = SyntheticReader(messages=messages, params=params or {},
                             armed_intervals=[(0.0, 90.0)])
    return LogHealthAnalyzer(reader, Thresholds(), _vehicle()).analyze()


class TestDsfDropout:

    def test_dropped_messages_warn(self):
        dsf = _dsf(116017, fmn_min=487)
        assert np.nanmax(dsf["Dp"]) >= Thresholds().log_dropped_msgs_warn
        r = _run({"PM": _pm_healthy(), "DSF": dsf})
        assert r.status == "warning"
        assert any("116017" in d for d in r.details)
        assert r.evidence_source           # POSITIONING: source cited
        assert r.action_level == "physical_check"

    def test_zero_drops_silent(self):
        dsf = _dsf(0)
        assert np.nanmax(dsf["Dp"]) == 0   # precondition
        r = _run({"PM": _pm_healthy(), "DSF": dsf})
        assert r.status == "good"
        assert not any("丢弃" in d for d in r.details)

    def test_routine_transient_drops_stay_info(self):
        # Fleet calibration: healthy flights show 0-13 drops at
        # boot/arming. Two dropped messages must not warn.
        dsf = _dsf(2)
        assert 0 < np.nanmax(dsf["Dp"]) < Thresholds().log_dropped_msgs_warn
        r = _run({"PM": _pm_healthy(), "DSF": dsf})
        assert r.status == "good"
        assert any("丢弃" in d for d in r.details)   # factual note kept


class TestLogReplay:

    def test_replay_with_param_suggests_disable(self):
        r = _run({"PM": _pm_healthy(), "DSF": _dsf(0), "RFRH": _rfrh()},
                 params={"LOG_REPLAY": 1})
        assert r.status == "good"          # healthy log, debug flag on
        assert any(s["param"] == "LOG_REPLAY" and s["suggested"] == 0
                   for s in r.param_suggestions)
        assert r.action_level == "apply_param"

    def test_replay_without_param_info_only(self):
        # Replay stream present but LOG_REPLAY param missing from the
        # log (old firmware / stripped params): note it, suggest nothing.
        r = _run({"PM": _pm_healthy(), "RFRH": _rfrh()})
        assert any("LOG_REPLAY" in d for d in r.details)  # precondition
        assert r.param_suggestions == []
        assert r.action_level == "ok"

    def test_dropout_and_replay_combined(self):
        # The real chip天线.BIN shape: replay stream on, buffer starved,
        # 116k messages lost.
        r = _run({"PM": _pm_healthy(), "DSF": _dsf(116017, fmn_min=487),
                  "RFRH": _rfrh()},
                 params={"LOG_REPLAY": 1})
        assert r.status == "warning"
        assert any(s["param"] == "LOG_REPLAY" for s in r.param_suggestions)
        assert r.action_level == "apply_param"
