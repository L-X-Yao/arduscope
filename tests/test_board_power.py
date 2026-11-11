"""
Fast tests for the board_power analyzer (POWR/MCU board supply health).

Each test asserts its precondition (the raw signal really contains the
scenario) before the invariant, so none can pass vacuously.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.board_power import BoardPowerAnalyzer
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

N = 200  # 100 s of POWR at 2 Hz


def _vehicle():
    return VehicleInfo(
        vehicle_type="ArduCopter", firmware_version="4.6.3",
        board_name="synthetic", frame_class=1, motor_count=4,
        imu_count=1, is_quadplane=False)


def _powr(vcc, flags=None, vservo=None, accflags=None):
    n = len(vcc)
    if flags is None:
        flags = np.full(n, 1.0)      # BRICK_VALID throughout
    if accflags is None:
        accflags = np.array(flags, dtype=float)
    return {
        "_timestamp": np.linspace(0.0, 100.0, n),
        "Vcc": np.asarray(vcc, dtype=float),
        "VServo": (np.asarray(vservo, dtype=float) if vservo is not None
                   else np.zeros(n)),
        "Flags": np.asarray(flags, dtype=float),
        "AccFlags": np.asarray(accflags, dtype=float),
    }


def _run(messages, armed=None):
    reader = SyntheticReader(messages=messages,
                             armed_intervals=armed)
    if armed is not None and len(armed) == 0:
        reader._armed = []  # bypass the or-default coercion
    return BoardPowerAnalyzer(reader, Thresholds(), _vehicle()).analyze()


class TestVccSag:

    def test_stable_vcc_good(self):
        vcc = np.full(N, 5.05)
        assert np.max(vcc) - np.min(vcc) < 0.15  # precondition
        r = _run({"POWR": _powr(vcc)})
        assert r.status == "good"
        assert r.action_level == "ok"

    def test_sustained_sag_warns(self):
        vcc = np.full(N, 5.05)
        vcc[90:110] = 4.82           # 10 s dip, well above 4.1 V
        sag = np.max(vcc) - np.min(vcc)
        assert sag > Thresholds().board_vcc_sag_warn   # precondition
        assert np.min(vcc) > Thresholds().board_vcc_low_crit
        r = _run({"POWR": _powr(vcc)})
        assert r.status == "warning"
        assert r.action_level == "physical_check"
        assert r.evidence_source            # POSITIONING: source cited

    def test_prearm_step_not_escalated(self):
        # Bench: rail steps 0.3 V when USB unplugs BEFORE arming.
        vcc = np.concatenate([np.full(N // 2, 4.90),
                              np.full(N // 2, 5.20)])
        assert np.max(vcc) - np.min(vcc) > 0.15  # precondition: raw sag
        r = _run({"POWR": _powr(vcc)}, armed=[(60.0, 100.0)])
        assert r.status == "good"    # armed window itself is flat

    def test_below_operating_range_critical(self):
        vcc = np.full(N, 5.0)
        vcc[100:120] = 3.95          # sustained excursion below 4.1 V
        assert np.min(vcc) < Thresholds().board_vcc_low_crit  # precondition
        r = _run({"POWR": _powr(vcc)})
        assert r.status == "critical"
        assert r.evidence_source

    def test_single_sample_spike_filtered(self):
        vcc = np.full(N, 5.0)
        vcc[50] = 4.4                # lone ADC glitch
        assert np.max(vcc) - np.min(vcc) > 0.15  # precondition: raw sag
        r = _run({"POWR": _powr(vcc)})
        assert r.status == "good"    # median-of-3 removes it


class TestPowerFlags:

    def test_brick_loss_while_armed_critical(self):
        vcc = np.full(N, 5.0)
        flags = np.full(N, 1.0)
        flags[120:] = 0.0            # BRICK_VALID drops mid-flight
        assert flags[0] == 1.0 and flags[-1] == 0.0  # precondition
        r = _run({"POWR": _powr(vcc, flags=flags)})
        assert r.status == "critical"
        assert r.evidence_source

    def test_never_brick_valid_no_false_critical(self):
        # USB bench session: BRICK_VALID never set — no loss event.
        vcc = np.full(N, 5.0)
        flags = np.zeros(N)
        assert not np.any(flags.astype(int) & 1)  # precondition
        r = _run({"POWR": _powr(vcc, flags=flags)})
        assert r.status == "good"

    def test_overcurrent_warns(self):
        vcc = np.full(N, 5.0)
        flags = np.full(N, 1.0)
        flags[80:83] = 9.0           # BRICK_VALID | PERIPH_OVERCURRENT
        assert np.any(flags.astype(int) & 8)  # precondition
        r = _run({"POWR": _powr(vcc, flags=flags)})
        assert r.status == "warning"
        assert r.evidence_source

    def test_accflags_only_overcurrent_warns(self):
        # Overcurrent blip between two POWR samples: only the
        # accumulated-flags register catches it.
        vcc = np.full(N, 5.0)
        flags = np.full(N, 1.0)
        acc = np.full(N, 1.0)
        acc[100:] = 17.0             # BRICK_VALID | HIPOWER_OVERCURRENT
        assert not np.any(flags.astype(int) & 24)  # precondition
        assert np.any(acc.astype(int) & 24)
        r = _run({"POWR": _powr(vcc, flags=flags, accflags=acc)})
        assert r.status == "warning"


class TestGroundTestAndData:

    def test_unarmed_log_is_info_not_verdict(self):
        # 0.3 V variation that WOULD warn — but there is no armed
        # window, so no stability verdict is made.
        vcc = np.concatenate([np.full(N // 2, 4.90),
                              np.full(N // 2, 5.20)])
        assert np.max(vcc) - np.min(vcc) > 0.15  # precondition
        r = _run({"POWR": _powr(vcc)}, armed=[])
        assert r.status == "info"

    def test_empty_powr_info(self):
        empty = {"_timestamp": np.array([]), "Vcc": np.array([]),
                 "VServo": np.array([]), "Flags": np.array([]),
                 "AccFlags": np.array([])}
        r = _run({"POWR": empty})
        assert r.status == "info"

    def test_mcu_envelope_in_details(self):
        vcc = np.full(N, 5.0)
        mcu = {
            "_timestamp": np.linspace(0.0, 100.0, N),
            "MTemp": np.linspace(35.0, 52.0, N),
            "MVolt": np.full(N, 3.30),
            "MVmin": np.full(N, 3.28),
            "MVmax": np.full(N, 3.32),
        }
        r = _run({"POWR": _powr(vcc), "MCU": mcu})
        assert r.status == "good"
        assert any("MCU" in d for d in r.details)  # precondition+invariant

    def test_mcu_implausible_temp_flagged_not_printed(self):
        # H7 Rev V AN5312 scaling bug: MTemp logs ~290 "degrees".
        vcc = np.full(N, 5.0)
        mcu = {
            "_timestamp": np.linspace(0.0, 100.0, N),
            "MTemp": np.linspace(289.0, 300.0, N),
            "MVolt": np.full(N, 3.30),
            "MVmin": np.full(N, 3.28),
            "MVmax": np.full(N, 3.32),
        }
        th = Thresholds()
        assert np.max(mcu["MTemp"]) > th.board_mcu_temp_valid_max  # precondition
        r = _run({"POWR": _powr(vcc), "MCU": mcu})
        assert r.status == "good"          # info note, not an alarm
        joined = " ".join(r.details)
        assert "AN5312" in joined          # flagged as broken scaling
        assert "300°C" not in joined       # not presented as a temperature
