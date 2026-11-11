"""
Synthetic-data unit tests to cover analyzers that the real-log smoke
suite never exercises.

Why this exists: every reference log we own is from QuadPlanes
that have neither RFND data nor populated BAT.Curr.  So:

  - analyzers/rangefinder.py  → 11% line coverage (hits the early
                                 "RFND empty" return only)
  - analyzers/power.py        → 29% line coverage (hits the
                                 "no current data" return only)

Both analyzers are real production code that runs the moment a
an ArduCopter multirotor (with MT-06 + ESC current sensor) sends a log,
but until then they're dark in the smoke suite.

Rather than synthesizing a full ArduPilot binary, we feed a
SyntheticReader (a duck-typed LogReader replacement) into the analyzer
constructors and exercise the analyze() body directly with crafted
numpy arrays.

This catches:
  - import / signature drift (analyzer calls a method that no longer
    exists on LogReader)
  - branch logic regressions (e.g. a future "fix" breaks the
    cumulative energy integration on logs without CurrTot)
  - threshold check direction errors

It does NOT catch issues with the .bin file parser path — the smoke
tests still own that.
"""

from __future__ import annotations

import numpy as np

from analyzers.attitude import AttitudeAnalyzer
from analyzers.battery import BatteryAnalyzer
from analyzers.power import PowerAnalyzer
from analyzers.rangefinder import RangefinderAnalyzer
from analyzers.vibration import VibrationAnalyzer
from lib.i18n import t as _t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo


class SyntheticReader:
    """Minimal duck-typed LogReader for unit tests.

    Carries a dict of pre-built message data ({msg_type: {field: ndarray}})
    and a params dict.  Implements only the methods analyzers actually
    call (verified via grep at writing time)."""

    def __init__(self, messages: dict, params: dict | None = None,
                 armed_intervals: list | None = None,
                 vehicle_type: str = "ArduCopter"):
        self._messages = messages
        self.params = params or {}
        self.vehicle_type = vehicle_type
        self.firmware_version = "4.6.3"
        self.board_name = "synthetic"
        self.time_range = (0.0, 100.0)
        self._armed = armed_intervals or [(0.0, 100.0)]
        self.available_messages = set(messages.keys())
        self.filepath = "/synthetic"
        self.errors = []
        self.mode_list = []
        self.events = []
        self.text_messages = []

    @property
    def is_copter(self) -> bool:
        # Mirrors LogReader INCLUDING the param-namespace fallback for
        # vendor-renamed firmware banners (FRAME_CLASS is Copter-only,
        # Q_ENABLE is Plane-only). A pure-substring mirror here made
        # synthetic tests validate pre-fix behavior.
        if 'Copter' in self.vehicle_type:
            return True
        if any(k in self.vehicle_type for k in ('Plane', 'Rover', 'Sub')):
            return False
        return 'FRAME_CLASS' in self.params

    @property
    def is_plane(self) -> bool:
        if 'Plane' in self.vehicle_type:
            return True
        if any(k in self.vehicle_type for k in ('Copter', 'Rover', 'Sub')):
            return False
        return 'Q_ENABLE' in self.params

    @property
    def is_quadplane(self) -> bool:
        # Q_ENABLE: 1=enabled, 2=VTOL AUTO — both are QuadPlanes.
        return self.is_plane and self.params.get('Q_ENABLE', 0) >= 1

    @property
    def canonical_vehicle_type(self) -> str:
        # Mirrors LogReader.canonical_vehicle_type.
        vt = self.vehicle_type
        if self.is_copter and 'Copter' not in vt:
            return f"{vt} (ArduCopter)"
        if self.is_plane and 'Plane' not in vt:
            return f"{vt} (ArduPlane)"
        return vt

    def has_message(self, msg_type: str) -> bool:
        return msg_type in self._messages

    def get_messages_as_arrays(self, msg_type: str,
                                fields: list) -> dict:
        msg = self._messages.get(msg_type, {})
        ts = msg.get("_timestamp", np.array([]))
        result = {"_timestamp": ts}
        for f in fields:
            if f in msg:
                result[f] = msg[f]
            else:
                result[f] = np.full(len(ts), np.nan)
        return result

    def get_param(self, name: str, default: float = 0.0) -> float:
        return self.params.get(name, default)

    def get_armed_intervals(self) -> list:
        return self._armed

    def never_armed(self) -> bool:
        return False

    def get_flight_duration(self) -> float:
        return self.time_range[1] - self.time_range[0]


def _vehicle(motors: int = 4, vt: str = "ArduCopter") -> VehicleInfo:
    return VehicleInfo(
        vehicle_type=vt,
        firmware_version="4.6.3",
        board_name="synthetic",
        frame_class=1,
        motor_count=motors,
        imu_count=2,
        is_quadplane=False,
    )


def _thresholds(reader: SyntheticReader) -> Thresholds:
    return Thresholds.from_params(reader.params, reader.vehicle_type)


# =====================================================================
# RangefinderAnalyzer
# =====================================================================

class TestRangefinderSynthetic:
    """Cover the rangefinder.analyze() body the smoke suite never reaches."""

    def _build_reader(self, dist_arr, stat_arr, inst_arr=None,
                      params=None) -> SyntheticReader:
        n = len(dist_arr)
        if inst_arr is None:
            inst_arr = np.zeros(n)
        return SyntheticReader(
            messages={
                "RFND": {
                    "_timestamp": np.linspace(0, 60, n),
                    "I": np.asarray(inst_arr, dtype=float),
                    "Dist": np.asarray(dist_arr, dtype=float),
                    "Stat": np.asarray(stat_arr, dtype=float),
                    "Orient": np.full(n, 25.0),  # downward
                },
            },
            params=params or {"RNGFND1_MAX_CM": 700},
        )

    def test_good_quality_rangefinder(self):
        """Healthy MT-06 — most readings status=4 (Good)."""
        n = 200
        dist = np.linspace(0.5, 5.0, n)
        stat = np.full(n, 4.0)
        reader = self._build_reader(dist, stat)
        analyzer = RangefinderAnalyzer(reader, _thresholds(reader),
                                       _vehicle())
        assert analyzer.can_run()
        result = analyzer.analyze()
        assert result.status in ("good", "info")
        assert _t("status.good") in result.summary
        assert any("%" in d and "/" in d for d in result.details)
        # Should produce both the distance figure and the status figure
        assert len(result.figures) >= 2

    def test_low_quality_rangefinder_warns(self):
        """Most readings status<4 → warning."""
        n = 200
        dist = np.linspace(0.5, 5.0, n)
        # Only 30% good → quality_pct < 50 → warning
        stat = np.zeros(n)
        stat[: int(n * 0.3)] = 4.0
        reader = self._build_reader(dist, stat)
        analyzer = RangefinderAnalyzer(reader, _thresholds(reader),
                                       _vehicle())
        result = analyzer.analyze()
        assert result.status == "warning"
        assert any("50%" in d for d in result.details)

    def test_old_firmware_no_stat_field(self):
        """Stat field all NaN (older firmware) — should not crash."""
        n = 100
        dist = np.linspace(0.3, 4.0, n)
        stat = np.full(n, np.nan)
        reader = self._build_reader(dist, stat)
        result = RangefinderAnalyzer(reader, _thresholds(reader),
                                     _vehicle()).analyze()
        assert any("Stat" in d or "NaN" in d or _t("rangefinder.stat_unavailable", label="").strip() in d for d in result.details)

    def test_empty_rfnd_returns_info(self):
        """0 RFND samples → info (early return)."""
        reader = SyntheticReader(
            messages={"RFND": {"_timestamp": np.array([])}},
        )
        result = RangefinderAnalyzer(reader, _thresholds(reader),
                                     _vehicle()).analyze()
        assert result.status == "info"
        assert "RFND" in result.summary


# =====================================================================
# PowerAnalyzer
# =====================================================================

class TestPowerSynthetic:
    """Cover the power.analyze() body the smoke suite never reaches."""

    def _build_reader(self, *, with_curr_tot: bool = True,
                      with_gps: bool = False, n: int = 600,
                      duration_s: float = 600.0) -> SyntheticReader:
        # Simulate a 10-minute hover at 6S, ~25A average current
        ts = np.linspace(0, duration_s, n)
        volt = np.full(n, 24.0) - np.linspace(0, 1.0, n)  # 24 → 23 V
        curr = np.full(n, 25.0) + 2 * np.sin(np.linspace(0, 20, n))
        # CurrTot in mAh: 25A × duration_h
        curr_tot = (curr.cumsum() / curr.size) * (duration_s / 3600.0) * 1000.0
        if not with_curr_tot:
            curr_tot = np.full(n, np.nan)
        msgs = {
            "BAT": {
                "_timestamp": ts,
                "Inst": np.zeros(n),
                "Volt": volt,
                "Curr": curr,
                "CurrTot": curr_tot,
            },
        }
        if with_gps:
            # 1 km flight at constant 5 m/s
            msgs["GPS"] = {
                "_timestamp": np.linspace(0, duration_s, 200),
                "I": np.zeros(200),
                "Lat": np.full(200, 39.9),
                "Lng": np.full(200, 116.4),
                "Spd": np.full(200, 5.0),
            }
        return SyntheticReader(messages=msgs)

    def test_with_curr_tot_uses_mAh_path(self):
        reader = self._build_reader(with_curr_tot=True)
        result = PowerAnalyzer(reader, _thresholds(reader),
                                _vehicle()).analyze()
        assert result.status in ("good", "info")
        assert any("Wh" in d for d in result.details)
        assert any("W" in d and ":" in d for d in result.details)
        assert "Wh" in result.summary

    def test_without_curr_tot_falls_back_to_integration(self):
        reader = self._build_reader(with_curr_tot=False)
        result = PowerAnalyzer(reader, _thresholds(reader),
                                _vehicle()).analyze()
        assert result.status in ("good", "info")
        assert any("Wh" in d for d in result.details)

    def test_no_current_returns_info(self):
        """All current zero → info early return."""
        n = 100
        reader = SyntheticReader(messages={
            "BAT": {
                "_timestamp": np.linspace(0, 60, n),
                "Inst": np.zeros(n),
                "Volt": np.full(n, 24.0),
                "Curr": np.zeros(n),
                "CurrTot": np.zeros(n),
            },
        })
        result = PowerAnalyzer(reader, _thresholds(reader),
                                _vehicle()).analyze()
        assert result.status == "info"
        assert _t("power.no_current") in result.summary

    def test_with_gps_computes_efficiency(self):
        """Wh/km efficiency calculation with GPS data present."""
        reader = self._build_reader(with_curr_tot=True, with_gps=True)
        result = PowerAnalyzer(reader, _thresholds(reader),
                                _vehicle()).analyze()
        # 5 m/s × 600 s = 3 km flight, with ~150 Wh consumed
        # → efficiency around 50 Wh/km
        wh_km_lines = [d for d in result.details if "Wh/km" in d]
        assert wh_km_lines, "Should report Wh/km when GPS data present"
        # Sanity check the value is in a plausible range. The regex
        # must match — an `if m:` gate here silently skipped the value
        # assertion when the report format changed.
        import re
        m = re.search(r"([\d.]+)\s*Wh/km", wh_km_lines[0])
        assert m is not None, f"no Wh/km value in {wh_km_lines[0]!r}"
        wh_km = float(m.group(1))
        assert 10 < wh_km < 500


# =====================================================================
# BatteryAnalyzer
# =====================================================================

class TestBatterySynthetic:
    """Cover battery analyzer edge cases including NaN and multi-instance."""

    def _build_reader(self, *, n=200, volt_range=(24.0, 22.5),
                      curr=25.0, capacity=5000.0,
                      instances=None, params=None,
                      armed=None) -> SyntheticReader:
        if instances is None:
            instances = [0]
        ts = np.linspace(0, 600, n)
        all_inst = np.concatenate([np.full(n, i) for i in instances])
        all_ts = np.tile(ts, len(instances))
        all_volt = np.tile(np.linspace(volt_range[0], volt_range[1], n),
                           len(instances))
        all_curr = np.full(n * len(instances), curr)
        # Rem goes 100% → 20%
        all_rem = np.tile(np.linspace(100, 20, n), len(instances))
        all_currtot = np.tile(
            np.linspace(0, capacity * 0.8, n), len(instances)
        )

        default_params = {
            "BATT_LOW_VOLT": 21.0,
            "BATT_CRT_VOLT": 20.0,
            "BATT_LOW_MAH": 0,
            "BATT_CRT_MAH": 0,
            "BATT_CAPACITY": capacity,
        }
        if params:
            default_params.update(params)

        return SyntheticReader(
            messages={
                "BAT": {
                    "_timestamp": all_ts,
                    "Inst": all_inst.astype(float),
                    "Volt": all_volt,
                    "Curr": all_curr,
                    "CurrTot": all_currtot,
                    "Rem": all_rem,
                },
            },
            params=default_params,
            armed_intervals=armed or [(0.0, 600.0)],
        )

    def test_healthy_battery(self):
        """Normal 6S flight — good status."""
        reader = self._build_reader(volt_range=(24.0, 22.0))
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert result.status == "good"
        assert any("22.00V" in d for d in result.details)

    def test_voltage_below_low_warns(self):
        """Voltage drops below BATT_LOW_VOLT → warning."""
        reader = self._build_reader(volt_range=(24.0, 20.5))
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert result.status == "warning"
        assert any("BAT" in d and "V" in d and "<" in d for d in result.details)

    def test_voltage_below_crt_critical(self):
        """Voltage drops below BATT_CRT_VOLT → critical."""
        reader = self._build_reader(volt_range=(24.0, 19.5))
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert result.status == "critical"
        assert any("BAT" in d and "V" in d and "<" in d for d in result.details)

    def test_all_nan_voltage_skips_instance(self):
        """All NaN voltage → skip instance, don't report false 'good'."""
        n = 100
        reader = SyntheticReader(
            messages={
                "BAT": {
                    "_timestamp": np.linspace(0, 60, n),
                    "Inst": np.zeros(n),
                    "Volt": np.full(n, np.nan),
                    "Curr": np.full(n, np.nan),
                    "CurrTot": np.full(n, np.nan),
                    "Rem": np.full(n, np.nan),
                },
            },
            params={"BATT_LOW_VOLT": 21.0, "BATT_CRT_VOLT": 20.0},
        )
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert any("NaN" in d for d in result.details)

    def test_backfill_hint_analog_monitor(self):
        """Analog V+I monitor with real consumption → backfill hint
        appears, citing current coefficient and consumed mAh, without
        degrading status."""
        reader = self._build_reader(params={
            "BATT_MONITOR": 4, "BATT_AMP_PERVLT": 40.2,
        })
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        hints = [d for d in result.details if "40.2" in d]
        assert hints, "expected backfill hint citing AMP_PERVLT"
        assert "4000" in hints[0]          # consumed = 80% of 5000
        assert "BATT_AMP_PERVLT" in hints[0]
        assert result.status == "good"

    def test_backfill_hint_needs_consumption(self):
        """Consumption under the 300 mAh floor → refill ratio is
        charger-resolution noise, no hint."""
        reader = self._build_reader(capacity=250.0, params={
            "BATT_MONITOR": 4, "BATT_AMP_PERVLT": 40.2,
        })  # CurrTot tops out at 200 mAh
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert not [d for d in result.details if "40.2" in d]

    def test_backfill_hint_analog_only(self):
        """Factory-calibrated monitor (SMBus=7) → no backfill hint."""
        reader = self._build_reader(params={
            "BATT_MONITOR": 7, "BATT_AMP_PERVLT": 40.2,
        })
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert not [d for d in result.details if "40.2" in d]

    def test_multi_instance_worst_status(self):
        """Two instances: BAT0 healthy, BAT1 critical → overall critical."""
        n = 100
        ts = np.linspace(0, 300, n)
        # Instance 0: healthy 24V
        # Instance 1: critically low 19V
        all_ts = np.concatenate([ts, ts])
        all_inst = np.concatenate([np.zeros(n), np.ones(n)])
        all_volt = np.concatenate([np.full(n, 24.0), np.full(n, 19.0)])
        all_curr = np.full(2 * n, 10.0)
        all_rem = np.full(2 * n, 50.0)
        all_currtot = np.full(2 * n, 2000.0)

        reader = SyntheticReader(
            messages={
                "BAT": {
                    "_timestamp": all_ts,
                    "Inst": all_inst,
                    "Volt": all_volt,
                    "Curr": all_curr,
                    "CurrTot": all_currtot,
                    "Rem": all_rem,
                },
            },
            params={
                "BATT_LOW_VOLT": 21.0,
                "BATT_CRT_VOLT": 20.0,
            },
        )
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert result.status == "critical"

    def test_param_suggestions_when_set_too_high(self):
        """If BATT_LOW_VOLT > max observed voltage → suggest lowering."""
        reader = self._build_reader(
            volt_range=(24.0, 22.0),
            params={"BATT_LOW_VOLT": 25.0, "BATT_CRT_VOLT": 24.5},
        )
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        # Should warn because min_volt (22.0) < low_volt (25.0)
        assert result.status in ("warning", "critical")

    def test_low_volt_near_max_is_flagged_without_a_value(self):
        """BATT_LOW_VOLT misconfigured but voltage never dropped below it.
        The note must appear; no value is suggested, as none has a source."""
        # max_volt=25.2, min_volt=24.0, BATT_LOW_VOLT=24.0
        # 24.0 > 25.2*0.95=23.94 → note fires
        # 24.0 < 24.0 is False → no warning → status stays "good"
        reader = self._build_reader(
            volt_range=(25.2, 24.0),
            params={
                "BATT_LOW_VOLT": 24.0,
                "BATT_CRT_VOLT": 22.0,
                "BATT_LOW_MAH": 0,
                "BATT_CRT_MAH": 0,
            },
        )
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert result.status == "good"
        assert result.param_suggestions == []
        assert any("BATT_LOW_VOLT" in d for d in result.details)

    def test_warning_with_low_volt_near_max_needs_a_person(self):
        """BATT_LOW_VOLT too close to max AND triggered: warning, no value."""
        # max_volt=25.2, min_volt=23.8 < BATT_LOW_VOLT=24.0 → warning
        # 24.0 > 25.2*0.95=23.94 → note fires
        reader = self._build_reader(
            volt_range=(25.2, 23.8),
            params={
                "BATT_LOW_VOLT": 24.0,
                "BATT_CRT_VOLT": 22.0,
                "BATT_LOW_MAH": 0,
                "BATT_CRT_MAH": 0,
            },
        )
        result = BatteryAnalyzer(reader, _thresholds(reader),
                                  _vehicle()).analyze()
        assert result.status == "warning"
        assert result.param_suggestions == []
        assert result.action_level == "physical_check"


# =====================================================================
# VibrationAnalyzer
# =====================================================================

class TestVibrationSynthetic:
    """Cover vibration analyzer thresholds, clips, and NaN handling."""

    def _build_reader(self, *, n=500, max_vibe=10.0, clips=(0, 0, 0),
                      params=None) -> SyntheticReader:
        ts = np.linspace(0, 300, n)
        # Deterministic: base at 60% of max, single spike to max
        vibe_x = np.full(n, max_vibe * 0.6)
        vibe_x[n // 2] = max_vibe
        vibe_y = np.full(n, max_vibe * 0.4)
        vibe_z = np.full(n, max_vibe * 0.3)
        return SyntheticReader(
            messages={
                "VIBE": {
                    "_timestamp": ts,
                    "VibeX": vibe_x,
                    "VibeY": vibe_y,
                    "VibeZ": vibe_z,
                    # Cumulative counters, climbing through the flight
                    "Clip0": np.linspace(0.0, float(clips[0]), n),
                    "Clip1": np.linspace(0.0, float(clips[1]), n),
                    "Clip2": np.linspace(0.0, float(clips[2]), n),
                },
            },
            params=params or {"INS_HNTCH_ENABLE": 1},
            armed_intervals=[(0.0, 300.0)],
        )

    def test_low_vibration_good(self):
        """Max vibe 10 m/s² < warn threshold (15) → good."""
        reader = self._build_reader(max_vibe=10.0)
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        assert result.status == "good"
        assert result.action_level == "ok"

    def test_vibration_warning(self):
        """Max vibe 20 m/s² > warn (15) but < crit (30) → warning."""
        reader = self._build_reader(max_vibe=20.0)
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        assert result.status == "warning"
        assert result.action_level == "physical_check"

    def test_vibration_critical(self):
        """Max vibe 35 m/s² > crit (30) → critical."""
        reader = self._build_reader(max_vibe=35.0)
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        assert result.status == "critical"
        assert result.action_level == "physical_check"

    def test_clip_count_escalates_status(self):
        """High clip count adds warning even if RMS is OK."""
        reader = self._build_reader(max_vibe=8.0, clips=(500, 300, 0))
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        # Clips > threshold should escalate
        assert result.status in ("warning", "critical")

    def test_clip_excess_with_high_vibe_needs_a_physical_check(self):
        """Clips are counted before INS_ACCEL_FILTER, so no filter value can
        help: high vibration plus clipping stays a physical check."""
        # max_vibe=25 > warn_thresh(15) → warning, action=physical_check
        reader = self._build_reader(max_vibe=25.0, clips=(1000, 0, 0))
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        assert result.status in ("warning", "critical")
        assert result.param_suggestions == []
        assert result.action_level == "physical_check"

    def test_clip_excess_with_low_vibe_needs_a_physical_check(self):
        """Vibe is GOOD (max_vibe < warn_thresh) but clip count exceeds
        threshold: the warning must not be left with action_level 'ok'.

        Also assert summary is refreshed in the clip-excess block — the
        original code only updated plain_summary/action_text, leaving
        summary as 'vibration normal' next to a yellow warning badge."""
        # max_vibe=5 < warn_thresh(15) → status="good", action="ok"
        # clips=(1000,0,0) > clip_warn → status promoted to "warning"
        reader = self._build_reader(max_vibe=5.0, clips=(1000, 0, 0))
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        assert result.status == "warning"
        assert result.param_suggestions == []
        assert result.action_level == "physical_check"
        # Summary must reflect clip excess, not the stale "vibration normal"
        # text inherited from the good branch.
        assert "正常" not in result.summary or "Clip" in result.summary, (
            f"summary leaked 'normal' from good branch after clip escalation: "
            f"{result.summary!r}"
        )

    def test_all_nan_vibe_no_crash(self):
        """All NaN vibration data → should not crash."""
        n = 100
        reader = SyntheticReader(
            messages={
                "VIBE": {
                    "_timestamp": np.linspace(0, 60, n),
                    "VibeX": np.full(n, np.nan),
                    "VibeY": np.full(n, np.nan),
                    "VibeZ": np.full(n, np.nan),
                    "Clip0": np.full(n, np.nan),
                    "Clip1": np.full(n, np.nan),
                    "Clip2": np.full(n, np.nan),
                },
            },
            params={"INS_HNTCH_ENABLE": 1},
        )
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        # Should not crash; status depends on 0.0 fallback from _safe_nanmax
        assert result.status in ("good", "info")

    def test_notch_disabled_note(self):
        """When HNTCH not enabled, plain_summary mentions it."""
        reader = self._build_reader(max_vibe=8.0,
                                     params={"INS_HNTCH_ENABLE": 0})
        result = VibrationAnalyzer(reader, _thresholds(reader),
                                    _vehicle()).analyze()
        assert result.status == "good"
        assert "m/s" in result.plain_summary


# =====================================================================
# AttitudeAnalyzer
# =====================================================================

class TestAttitudeSynthetic:
    """Cover attitude analyzer: RMS calculation, armed filtering, edge cases."""

    def _build_reader(self, *, n=300, roll_error=2.0, pitch_error=2.0,
                      armed=None, params=None) -> SyntheticReader:
        ts = np.linspace(0, 120, n)
        des_roll = np.zeros(n)
        roll = np.random.normal(0, roll_error, n)
        des_pitch = np.zeros(n)
        pitch = np.random.normal(0, pitch_error, n)
        des_yaw = np.full(n, 90.0)
        yaw = np.full(n, 90.0) + np.random.normal(0, 1.0, n)

        return SyntheticReader(
            messages={
                "ATT": {
                    "_timestamp": ts,
                    "DesRoll": des_roll,
                    "Roll": roll,
                    "DesPitch": des_pitch,
                    "Pitch": pitch,
                    "DesYaw": des_yaw,
                    "Yaw": yaw,
                },
            },
            params=params or {"ATC_RAT_RLL_P": 0.135, "ATC_RAT_PIT_P": 0.135},
            armed_intervals=armed or [(0.0, 120.0)],
        )

    def test_good_attitude(self):
        """Low RMS error (1 deg) → good status (warn threshold=2.0)."""
        reader = self._build_reader(roll_error=1.0, pitch_error=1.0)
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "good"
        assert any("RMS" in d for d in result.details)

    def test_warning_attitude(self):
        """Moderate RMS error (~3 deg) → warning (warn=2.0, crit=5.0)."""
        reader = self._build_reader(roll_error=3.0, pitch_error=3.0)
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "warning"

    def test_critical_attitude(self):
        """High RMS error (~8 deg) → critical (crit=5.0)."""
        reader = self._build_reader(roll_error=8.0, pitch_error=8.0)
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status == "critical"

    def test_not_armed_graceful(self):
        """No armed intervals → should not crash."""
        reader = self._build_reader(armed=[])
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        # With no armed data, analyzer may use full data or return info
        assert result.status in ("good", "info", "warning", "critical")

    def test_autotune_routing_axes_unset(self):
        """Bad attitude with AUTOTUNE_AXES at 0 (or default 7): no
        param to fix — must route to run_autotune with no suggestion.
        (The old test gated its assertion on `if param_suggestions:`,
        which both hid this routing and could never fail.)"""
        reader = self._build_reader(
            roll_error=8.0, pitch_error=8.0,
            params={"ATC_RAT_RLL_P": 0.135, "ATC_RAT_PIT_P": 0.135,
                    "AUTOTUNE_AXES": 0},
        )
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status in ("warning", "critical")  # precondition
        assert result.param_suggestions == []
        assert result.action_level == "run_autotune"

    def test_autotune_suggestion_partial_axes(self):
        """Bad attitude + AUTOTUNE_AXES left at a partial bitmask →
        suggest restoring the full-axes value (7), apply_param."""
        reader = self._build_reader(
            roll_error=8.0, pitch_error=8.0,
            params={"ATC_RAT_RLL_P": 0.135, "ATC_RAT_PIT_P": 0.135,
                    "AUTOTUNE_AXES": 3},
        )
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        assert result.status in ("warning", "critical")  # precondition
        assert result.param_suggestions, "autotune suggestion missing"
        params_suggested = [s["param"] for s in result.param_suggestions]
        assert "AUTOTUNE_AXES" in params_suggested
        assert result.action_level == "apply_param"

    def test_nan_yaw_shows_na_not_nan_literal(self):
        """When DesYaw/Yaw is absent (NaN from SyntheticReader), yaw_rms
        becomes NaN. The details must show 'N/A', not the literal string
        'nan', which previously leaked through f'{nan:.2f}'."""
        n = 200
        ts = np.linspace(0, 100, n)
        reader = SyntheticReader(
            messages={
                "ATT": {
                    "_timestamp": ts,
                    "DesRoll": np.zeros(n),
                    "Roll": np.random.normal(0, 1.0, n),
                    "DesPitch": np.zeros(n),
                    "Pitch": np.random.normal(0, 1.0, n),
                    # DesYaw and Yaw intentionally omitted → NaN arrays
                },
            },
            params={"ATC_RAT_RLL_P": 0.135},
            armed_intervals=[(0.0, 100.0)],
        )
        result = AttitudeAnalyzer(reader, _thresholds(reader),
                                   _vehicle()).analyze()
        # Roll/pitch data IS present in the fixture, so the analyzer
        # must produce a real verdict — the old `if info: return`
        # early-exit let the nan-literal assertion be bypassed.
        assert result.status != "info", (
            "attitude returned info despite roll/pitch data present")
        # The literal string "nan" must not appear in any detail line
        for detail in result.details:
            assert "nan" not in detail.lower() or "N/A" in detail, (
                f"Detail shows literal 'nan': {detail!r}"
            )
