"""
Crash-forensics pattern tests (synthetic, no .BIN needed).

Covers the three incident signatures added 2026-07:
  1. Abrupt log end while armed (power-path interruption) — log_health
  2. Collective throttle collapse + attitude limit cycle — throttle_collapse
  3. Throttle deadzone never crossed (THR_DZ forensics) — rc_input
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.log_health import LogHealthAnalyzer
from analyzers.rc_input import RCInputAnalyzer
from analyzers.throttle_collapse import ThrottleCollapseAnalyzer
from lib.i18n import t as _t
from lib.log_reader import Event, ModeChange
from tests.test_synthetic_coverage import SyntheticReader, _thresholds, _vehicle

pytestmark = pytest.mark.fast


def _pm_block(n: int, t_end: float) -> dict:
    """Healthy PM stream so log_health's CPU sections stay quiet."""
    return {
        "_timestamp": np.linspace(0, t_end, n),
        "NLon": np.zeros(n),
        "NLoop": np.full(n, 400.0),
        "MaxT": np.full(n, 3000.0),
        "Mem": np.full(n, 100000.0),
        "Load": np.full(n, 30.0),
    }


# =====================================================================
# 1. Abrupt log end while armed
# =====================================================================

class TestAbruptLogEnd:

    def _reader(self, *, armed_end: float, alt_m: float,
                volt_last: float) -> SyntheticReader:
        n = 101
        ts = np.linspace(0, 100, n)
        reader = SyntheticReader(
            messages={
                "PM": _pm_block(n, 100),
                "CTUN": {"_timestamp": ts, "Alt": np.full(n, alt_m)},
                "BAT": {
                    "_timestamp": ts,
                    "Volt": np.linspace(23.0, volt_last, n),
                    "Curr": np.full(n, 15.0),
                },
            },
            params={"BATT_LOW_VOLT": 21.0},
            armed_intervals=[(5.0, armed_end)],
        )
        reader.time_range = (0.0, 100.0)
        return reader

    def test_airborne_cutoff_is_critical(self):
        reader = self._reader(armed_end=100.0, alt_m=30.0, volt_last=22.5)
        result = LogHealthAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "critical"
        assert result.action_level == "physical_check"
        assert _t("log_health.abrupt_volt_healthy") in result.details
        # power forensics must not push LOG_BITMASK suggestions
        assert result.param_suggestions == []

    def test_sagging_voltage_notes_exhaustion(self):
        reader = self._reader(armed_end=100.0, alt_m=30.0, volt_last=19.0)
        result = LogHealthAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "critical"
        assert _t("log_health.abrupt_volt_sagging") in result.details

    def test_ground_cutoff_is_warning(self):
        reader = self._reader(armed_end=100.0, alt_m=0.5, volt_last=22.5)
        result = LogHealthAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "warning"

    def test_clean_disarm_not_flagged(self):
        reader = self._reader(armed_end=90.0, alt_m=30.0, volt_last=22.5)
        result = LogHealthAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "good"
        assert _t("log_health.abrupt_volt_healthy") not in result.details


# =====================================================================
# 2. Throttle collapse + limit cycle
# =====================================================================

class TestThrottleCollapse:

    def _reader(self, *, tho_floor: bool, osc: bool, mode_num: int = 2,
                alt_m: float = 12.0, climb_spike: bool = True,
                vt: str = "ArduCopter") -> SyntheticReader:
        fs = 10.0
        ts = np.arange(0, 60, 1 / fs)
        n = len(ts)

        alt = np.full(n, alt_m)
        if climb_spike:
            # baro false climb: +8 m between 19.5 and 21 s
            spike = (ts >= 19.5) & (ts <= 21.0)
            alt[spike] += np.linspace(0, 8, int(np.sum(spike)))
            alt[ts > 21.0] += 8.0

        tho = np.full(n, 0.45)
        if tho_floor:
            pinned = (ts >= 21.0) & (ts <= 24.0)
            tho[pinned] = 0.01
            # Nothing holds the vehicle up at the floor.
            alt[pinned] -= 3.0 * (ts[pinned] - 21.0)

        roll = np.full(n, 2.0)
        if osc:
            w = (ts >= 24.0) & (ts <= 30.0)
            roll[w] = 25.0 * np.sin(2 * np.pi * 1.5 * ts[w])

        reader = SyntheticReader(
            messages={
                "CTUN": {"_timestamp": ts, "ThO": tho, "Alt": alt},
                "ATT": {"_timestamp": ts, "Roll": roll,
                        "Pitch": np.full(n, 1.0)},
                "RCOU": {
                    "_timestamp": ts,
                    **{c: np.where((ts >= 21.0) & (ts <= 24.0), 1010.0,
                                   1500.0)
                       for c in ("C1", "C2", "C3", "C4")},
                },
                # A real single-mode flight still logs a MODE message; the
                # analyzer keys off its presence to tell a genuine mode from
                # the no-MODE flightmode_list fallback.
                "MODE": {"_timestamp": np.array([0.0]),
                         "ModeNum": np.array([float(mode_num)])},
            },
            params={"MOT_SPIN_MIN": 0.15, "MOT_PWM_MIN": 1000},
            armed_intervals=[(0.0, 60.0)],
            vehicle_type=vt,
        )
        reader.time_range = (0.0, 60.0)
        reader.mode_list = [ModeChange(0.0, "MODE", mode_num)]
        reader.events = [Event(1.0, 28)]        # NOT_LANDED
        return reader

    def test_collapse_with_limit_cycle_is_critical(self):
        reader = self._reader(tho_floor=True, osc=True)
        result = ThrottleCollapseAnalyzer(reader, _thresholds(reader),
                                          _vehicle()).analyze()
        assert result.status == "critical"
        assert result.action_level == "physical_check"
        # 1.5 Hz at 10 Hz sampling → frequency must be named (not the
        # aliased no-number variant); crossing-time quantization at
        # 10 Hz allows ~±10% on the estimate
        import re
        freqs = [float(m.group(1)) for d in result.details
                 for m in [re.search(r"(\d+\.\d+)\s*Hz", d)] if m]
        assert freqs and 1.3 <= freqs[0] <= 1.7, freqs
        assert any("m/s" in d for d in result.details)      # climb spike
        assert any("%" in d for d in result.details)        # RCOU floor pin

    def test_collapse_without_oscillation_is_warning(self):
        reader = self._reader(tho_floor=True, osc=False)
        result = ThrottleCollapseAnalyzer(reader, _thresholds(reader),
                                          _vehicle()).analyze()
        assert result.status == "warning"

    def test_manual_mode_not_flagged(self):
        # STABILIZE (0): pilot chopping throttle is intentional
        reader = self._reader(tho_floor=True, osc=True, mode_num=0)
        result = ThrottleCollapseAnalyzer(reader, _thresholds(reader),
                                          _vehicle()).analyze()
        assert result.status == "good"

    def test_normal_descent_not_flagged(self):
        reader = self._reader(tho_floor=False, osc=False)
        result = ThrottleCollapseAnalyzer(reader, _thresholds(reader),
                                          _vehicle()).analyze()
        assert result.status == "good"

    def test_near_ground_not_flagged(self):
        # ThO at floor below the airborne gate = landing, not collapse
        reader = self._reader(tho_floor=True, osc=False, alt_m=1.0,
                              climb_spike=False)
        result = ThrottleCollapseAnalyzer(reader, _thresholds(reader),
                                          _vehicle()).analyze()
        assert result.status == "good"

    def test_plane_skipped(self):
        reader = self._reader(tho_floor=True, osc=True, vt="ArduPlane")
        result = ThrottleCollapseAnalyzer(reader, _thresholds(reader),
                                          _vehicle(vt="ArduPlane")).analyze()
        assert result.status == "info"


# =====================================================================
# 3. Throttle deadzone never crossed
# =====================================================================

class TestDeadzoneForensics:

    def _reader(self, c3: np.ndarray, mode_num: int = 2) -> SyntheticReader:
        n = len(c3)
        ts = np.linspace(0, 60, n)
        channels = {c: np.full(n, 1500.0) for c in
                    ("C1", "C2", "C4", "C5")}
        reader = SyntheticReader(
            messages={
                "RCIN": {"_timestamp": ts, "C3": c3, **channels},
            },
            params={"THR_DZ": 100, "RC3_MIN": 1100, "RC3_MAX": 1900},
            armed_intervals=[(0.0, 60.0)],
        )
        reader.time_range = (0.0, 60.0)
        reader.mode_list = [ModeChange(0.0, "MODE", mode_num)]
        return reader

    def test_slow_push_never_crossing_band_top_noted(self):
        # band top = 1580 PWM; pilot slowly raises 1150 → 1560 over 60 s
        # — pushing up the whole time, positive climb command never
        # issued (the H2 "vehicle ignores my throttle" signature)
        c3 = np.linspace(1150.0, 1560.0, 301)
        reader = self._reader(c3)
        result = RCInputAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert result.status == "good"          # info-only, never a warning
        assert any("THR_DZ" in d for d in result.details)

    def test_hover_wiggle_inside_band_not_noted(self):
        # wiggling around mid-stick with no net rise = normal ALT_HOLD
        # hovering, not a thwarted climb input
        ts = np.linspace(0, 60, 301)
        c3 = 1500.0 + 30.0 * np.sin(0.5 * ts)
        reader = self._reader(c3)
        result = RCInputAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert not any("THR_DZ" in d for d in result.details)

    def test_parked_stick_not_noted(self):
        c3 = np.full(301, 1500.0)
        reader = self._reader(c3)
        result = RCInputAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert not any("THR_DZ" in d for d in result.details)

    def test_quick_climb_crossing_band_not_noted(self):
        # stick raised past the band top within ~2 s → run too short,
        # the climb command went positive almost immediately
        ts = np.linspace(0, 60, 301)
        c3 = np.where(ts < 2.0, 1150.0 + ts * 250.0, 1700.0)
        reader = self._reader(c3)
        result = RCInputAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert not any("THR_DZ" in d for d in result.details)

    def test_stabilize_mode_not_noted(self):
        ts = np.linspace(0, 60, 301)
        c3 = 1500.0 + 30.0 * np.sin(0.5 * ts)
        reader = self._reader(c3, mode_num=0)
        result = RCInputAnalyzer(reader, _thresholds(reader),
                                 _vehicle()).analyze()
        assert not any("THR_DZ" in d for d in result.details)
