"""
Regression tests for the ArduPilot-semantics fix batch (audit §2.2).

Each test first asserts the precondition (that it actually builds the
target scenario) and then asserts the invariant, so the test can never
pass vacuously. Covers:

  - ARSP Health/H field alias (4.1+ renamed Health -> H)
  - failsafe ECode==0 (ERROR_RESOLVED) recovery events not counted
  - failsafe subsystem table (detection subsystems excluded, real
    failsafes included) per AP LogErrorSubsystem
  - failsafe EKF variance filtered to the primary lane (XKF4 C column)
  - summary EVENT_NAMES / ERROR_SUBSYS_NAMES corrected against the
    AP LogEvent / LogErrorSubsystem enums
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.airspeed import AirspeedAnalyzer
from analyzers.failsafe import FailsafeAnalyzer, _FS_SUBSYS_KEYS
from analyzers.summary import EVENT_NAMES, ERROR_SUBSYS_NAMES
from lib.log_reader import ErrorEvent, LogReader
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


def _run_failsafe(reader) -> object:
    return FailsafeAnalyzer(
        reader, Thresholds.from_params(reader.params, reader.vehicle_type),
        _vehicle()).analyze()


# ---------------------------------------------------------------------
# 1. ARSP Health -> H alias (log_reader layer)
# ---------------------------------------------------------------------

class TestAirspeedHealthAlias:

    def test_alias_table_has_arsp_health(self):
        assert ("ARSP", "Health") in LogReader._FIELD_ALIASES
        assert "H" in LogReader._FIELD_ALIASES[("ARSP", "Health")]

    def test_apply_aliases_fills_health_from_h(self):
        reader = LogReader.__new__(LogReader)  # no __init__ / no file
        h_vals = np.array([1.0, 1.0, 0.0, 0.0, 1.0])
        result = {"Health": np.full(5, np.nan), "H": h_vals.copy()}
        # Precondition: this models a 4.1+ log — "Health" is absent
        # (all-NaN) and the real data lives under "H".
        assert np.all(np.isnan(result["Health"]))
        out = reader._apply_aliases("ARSP", ["Health"], result)
        # Invariant: the alias layer backfills Health from H.
        assert not np.all(np.isnan(out["Health"]))
        np.testing.assert_array_equal(out["Health"], h_vals)

    def test_health_zero_triggers_unhealthy(self):
        # 60% of samples unhealthy (Health==0) must escalate to critical.
        n = 50
        ts = np.linspace(0, 50, n)
        health = np.ones(n)
        health[:30] = 0.0
        # Precondition: 60% unhealthy, above the 50% critical gate.
        assert float(np.mean(health == 0) * 100) == pytest.approx(60.0)
        reader = SyntheticReader(
            messages={"ARSP": {
                "_timestamp": ts, "I": np.zeros(n),
                "Airspeed": np.full(n, 20.0),
                "DiffPress": np.full(n, 300.0),
                "Health": health,
            }},
            vehicle_type="ArduPlane",
        )
        res = AirspeedAnalyzer(
            reader, Thresholds.from_params(reader.params, reader.vehicle_type),
            _vehicle()).analyze()
        assert res.status == "critical"


# ---------------------------------------------------------------------
# 2. failsafe ECode==0 recovery filtering
# ---------------------------------------------------------------------

class TestFailsafeRecoveryFilter:

    def test_recovery_only_log_not_critical(self):
        reader = SyntheticReader(messages={})
        reader.errors = [ErrorEvent(timestamp=10.0, subsys=6, ecode=0)]
        # Precondition: the only ERR is a battery-failsafe RECOVERY (ECode 0).
        assert reader.errors[0].ecode == 0
        assert reader.errors[0].subsys in _FS_SUBSYS_KEYS
        res = _run_failsafe(reader)
        # Invariant: a resolved-only record is not a triggered failsafe.
        assert res.status != "critical"

    def test_occurrence_is_critical(self):
        reader = SyntheticReader(messages={})
        reader.errors = [ErrorEvent(timestamp=10.0, subsys=6, ecode=1)]
        # Precondition: battery-failsafe OCCURRENCE (ECode != 0).
        assert reader.errors[0].ecode != 0
        res = _run_failsafe(reader)
        assert res.status == "critical"
        assert res.param_suggestions == []  # no dangerous BATT_LOW_VOLT advice
        assert res.action_level == "physical_check"


# ---------------------------------------------------------------------
# 3. failsafe subsystem table (detection vs failsafe)
# ---------------------------------------------------------------------

class TestFailsafeSubsysTable:

    def test_detection_subsystems_excluded(self):
        # 16 EKFCHECK and 18 BARO are detection subsystems, not failsafes.
        assert 16 not in _FS_SUBSYS_KEYS
        assert 18 not in _FS_SUBSYS_KEYS

    def test_real_failsafes_present(self):
        # Values that were missing before the fix.
        for subsys in (20, 23, 26, 27, 29, 31):
            assert subsys in _FS_SUBSYS_KEYS

    def test_baro_glitch_not_a_failsafe(self):
        reader = SyntheticReader(messages={})
        reader.errors = [ErrorEvent(timestamp=5.0, subsys=18, ecode=2)]
        # Precondition: a BARO glitch (subsys 18), not a failsafe.
        assert reader.errors[0].subsys == 18
        res = _run_failsafe(reader)
        assert res.status != "critical"

    def test_terrain_failsafe_counted(self):
        reader = SyntheticReader(messages={})
        reader.errors = [ErrorEvent(timestamp=5.0, subsys=23, ecode=1)]
        assert reader.errors[0].subsys == 23
        res = _run_failsafe(reader)
        assert res.status == "critical"


# ---------------------------------------------------------------------
# 4. failsafe EKF variance filtered to primary lane (XKF4 C column)
# ---------------------------------------------------------------------

class TestFailsafeEkfPrimaryLane:

    def _reader(self):
        th = Thresholds()
        crit = th.ekf_variance_crit
        n = 100
        ts = np.linspace(0, 100, n)
        core = np.tile([0.0, 1.0], n // 2)
        low = np.full(n, crit * 0.1)
        # Core 1 (backup lane) spikes; core 0 (primary) stays well below.
        sv = np.where(core == 0, crit * 0.1, crit * 3.0)
        reader = SyntheticReader(
            messages={"XKF4": {
                "_timestamp": ts, "C": core,
                "SV": sv, "SP": low, "SH": low, "SM": low,
            }},
            armed_intervals=[(0.0, 100.0)],
        )
        return reader, crit

    def test_backup_lane_spike_not_critical(self):
        reader, crit = self._reader()
        xkf4 = reader.get_messages_as_arrays("XKF4", ["C", "SV"])
        # Precondition: core 1 exceeds the threshold, core 0 does not.
        c1 = xkf4["C"] == 1
        c0 = xkf4["C"] == 0
        assert np.any(c1) and np.any(c0)
        assert np.max(xkf4["SV"][c1]) > crit
        assert np.max(xkf4["SV"][c0]) < crit
        res = _run_failsafe(reader)
        # Invariant: AP evaluates the primary lane only, so a backup-lane
        # variance spike is not a threshold breach.
        assert res.status != "critical"


# ---------------------------------------------------------------------
# 5. summary EVENT_NAMES / ERROR_SUBSYS_NAMES corrected
# ---------------------------------------------------------------------

class TestSummaryEnumTables:

    def test_event_names_match_ap_logevent(self):
        # The mis-map that made routine landings read as flips.
        assert EVENT_NAMES[18] == "Land Complete"
        assert EVENT_NAMES[19] == "Lost GPS"
        assert EVENT_NAMES[21] == "Flip Start"
        assert EVENT_NAMES[62] == "EKF Yaw Reset"
        # ID 16 was a fabricated "Land Complete"; the real enum has no 16.
        assert 16 not in EVENT_NAMES

    def test_error_subsys_names_match_ap_enum(self):
        # 25 = THRUST_LOSS_CHECK — a crash-critical signal formerly shown
        # as "Failsafe Leak".
        assert ERROR_SUBSYS_NAMES[25] == "Thrust Loss Check"
        assert ERROR_SUBSYS_NAMES[27] == "Failsafe Leak"
        assert ERROR_SUBSYS_NAMES[18] == "Baro"
        assert ERROR_SUBSYS_NAMES[23] == "Failsafe Terrain"
        # 27-31 were missing entirely before the fix.
        for subsys in (27, 28, 29, 30, 31):
            assert subsys in ERROR_SUBSYS_NAMES
