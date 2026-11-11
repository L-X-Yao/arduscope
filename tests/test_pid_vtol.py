"""
Fast tests for pid_tuning's phase split (copter / plane / VTOL).

On a QuadPlane, PIDR/PIDP/PIDY log the FIXED-WING rate controllers
(RLL_RATE_*/PTCH_RATE_*) and PIQ* log the VTOL controllers (Q_A_RAT_*);
RATE.*Out is a product of the copter/VTOL attitude controller only.
Before the split, plane-phase data was mapped to Q_A_RAT_* parameters
and gated on the hover controller's RATE output. Each test asserts its
precondition first.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.pid_tuning import PIDTuningAnalyzer
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

N = 400
TS = np.linspace(0.0, 100.0, N)


def _vehicle(vt="ArduCopter", quadplane=False):
    return VehicleInfo(
        vehicle_type=vt, firmware_version="4.6.3", board_name="synthetic",
        frame_class=1, motor_count=4, imu_count=1, is_quadplane=quadplane)


def _pid(tracking="poor", ts=TS):
    """PID message arrays. 'poor' = 70% tracking error, no oscillation."""
    n = len(ts)
    tar = np.sin(2 * np.pi * 0.25 * ts)          # ±1 rad/s target
    act = (0.3 * tar) if tracking == "poor" else (0.98 * tar)
    return {
        "_timestamp": ts,
        "Tar": tar, "Act": act,
        "P": 0.3 * tar, "I": 0.05 * np.ones(n) + 0.001 * np.sin(ts),
        "D": 0.01 * np.ones(n), "FF": 0.02 * np.ones(n),
    }


def _pos_flying():
    return {"_timestamp": TS, "Alt": np.linspace(0.0, 80.0, N)}


def _rate(rout_amp, t0=0.0, t1=100.0, n=N):
    """RATE arrays with |ROut| = rout_amp inside [t0, t1], 0 outside."""
    ts = np.linspace(0.0, 100.0, n)
    out = np.where((ts >= t0) & (ts <= t1),
                   rout_amp * np.sign(np.sin(2 * np.pi * 2.0 * ts)), 0.0)
    return {"_timestamp": ts, "ROut": out, "RDes": np.zeros(n),
            "POut": np.zeros(n), "PDes": np.zeros(n),
            "YOut": np.zeros(n), "YDes": np.zeros(n)}


def _run(messages, params, vt="ArduCopter", quadplane=False):
    reader = SyntheticReader(messages=messages, params=params,
                             vehicle_type=vt)
    an = PIDTuningAnalyzer(reader, Thresholds(),
                           _vehicle(vt=vt, quadplane=quadplane))
    return an.analyze()


def _sub(result, title_frag):
    hits = [s for s in result.sub_results if title_frag in s.title]
    return hits[0] if hits else None


def _names(axis, param: str) -> bool:
    """True if the axis' advice text points at this parameter."""
    return any(param in d for d in axis.details)


class TestPhaseParamNamespace:

    def test_copter_uses_atc_rat(self):
        r = _run({"PIDR": _pid("poor"), "POS": _pos_flying()},
                 params={"ATC_RAT_RLL_P": 0.135, "ATC_RAT_RLL_I": 0.135,
                         "ATC_RAT_RLL_D": 0.0036})
        assert r.sub_results                      # precondition: axis ran
        assert _names(r.sub_results[0], "ATC_RAT_RLL_P")

    def test_vtol_axis_uses_q_a_rat(self):
        r = _run({"PIQR": _pid("poor"), "POS": _pos_flying()},
                 params={"Q_A_RAT_RLL_P": 0.19, "Q_A_RAT_RLL_I": 0.19,
                         "Q_A_RAT_RLL_D": 0.002},
                 vt="ArduPlane", quadplane=True)
        vtol = _sub(r, "旋翼相")
        assert vtol is not None                   # precondition: VTOL axis
        assert _names(vtol, "Q_A_RAT_RLL_P")
        assert not _names(vtol, "ATC_RAT_RLL_P")
        assert not _names(vtol, "RLL_RATE_P")

    def test_plane_phase_uses_rll_rate(self):
        # QuadPlane fixed-wing phase: PIDR must map to RLL_RATE_*, not
        # to the VTOL Q_A_RAT_* namespace (pre-split mismap).
        r = _run({"PIDR": _pid("poor"), "POS": _pos_flying()},
                 params={"RLL_RATE_P": 0.3846, "RLL_RATE_I": 0.3846,
                         "Q_A_RAT_RLL_P": 0.19},
                 vt="ArduPlane", quadplane=True)
        plane = _sub(r, "固定翼相")
        assert plane is not None                  # precondition: plane axis
        assert _names(plane, "RLL_RATE_P")
        assert not _names(plane, "Q_A_RAT_RLL_P")


class TestPhaseRateGate:

    def test_plane_axis_not_judged_by_rate_msg(self):
        # Well-tracking plane axis + saturated hover-controller RATE
        # output: the plane axis must NOT inherit the VTOL verdict.
        th = Thresholds()
        rate = _rate(0.9)
        rms = float(np.sqrt(np.mean(rate["ROut"] ** 2)))
        assert rms >= th.pid_rate_out_crit        # precondition
        r = _run({"PIDR": _pid("good"), "RATE": rate,
                  "POS": _pos_flying()},
                 params={"RLL_RATE_P": 0.3846},
                 vt="ArduPlane", quadplane=True)
        plane = _sub(r, "固定翼相")
        assert plane is not None
        assert plane.status == "good"

    def test_vtol_rate_gate_restricted_to_vtol_windows(self):
        # PIQR (VTOL) exists only in t=[0,30]; the RATE stream shows a
        # saturated controller only in t=[60,90] (plane cruise, e.g.
        # weathervaning artifacts). The VTOL verdict must come from the
        # VTOL window only.
        th = Thresholds()
        piq = _pid("good", ts=np.linspace(0.0, 30.0, N))
        rate = _rate(0.9, t0=60.0, t1=90.0)
        whole_rms = float(np.sqrt(np.mean(rate["ROut"] ** 2)))
        assert whole_rms >= th.pid_rate_out_warn  # precondition: unmasked
        r = _run({"PIQR": piq, "RATE": rate, "POS": _pos_flying()},
                 params={"Q_A_RAT_RLL_P": 0.19},
                 vt="ArduPlane", quadplane=True)
        vtol = _sub(r, "旋翼相")
        assert vtol is not None
        assert vtol.status == "good"

    def test_copter_rate_gate_still_authoritative(self):
        # Unchanged copter behavior: saturated RATE output drives
        # critical even when Tar/Act tracking looks clean.
        th = Thresholds()
        rate = _rate(0.9)
        rms = float(np.sqrt(np.mean(rate["ROut"] ** 2)))
        assert rms >= th.pid_rate_out_crit        # precondition
        r = _run({"PIDR": _pid("good"), "RATE": rate,
                  "POS": _pos_flying()},
                 params={"ATC_RAT_RLL_P": 0.135})
        assert r.status == "critical"


class TestPlaneVtolExclusion:

    def _garbage_pid(self, ts):
        # Hover contamination shape: the plane controller is fed
        # nonzero targets (nav/weathervane) but the surfaces have no
        # authority — Act anti-correlates with Tar.
        n = len(ts)
        tar = np.sin(2 * np.pi * 0.25 * ts)
        return {"_timestamp": ts, "Tar": tar, "Act": -0.5 * tar,
                "P": 0.3 * tar, "I": np.full(n, 0.05),
                "D": np.full(n, 0.01), "FF": np.full(n, 0.02)}

    def test_plane_axis_all_vtol_returns_info(self):
        # PIQ spans the whole flight -> no fixed-wing phase remains.
        piq = _pid("good", ts=TS)
        pidr = self._garbage_pid(TS)
        err = pidr["Tar"] - pidr["Act"]
        ratio = (np.sqrt(np.mean(err**2))
                 / np.sqrt(np.mean(pidr["Tar"]**2)))
        assert ratio >= Thresholds().pid_tracking_crit  # precondition
        r = _run({"PIQR": piq, "PIDR": pidr, "POS": _pos_flying()},
                 params={"Q_A_RAT_RLL_P": 0.19, "RLL_RATE_P": 0.3846},
                 vt="ArduPlane", quadplane=True)
        plane = _sub(r, "固定翼相")
        assert plane is not None
        assert plane.status == "info"
        assert not any(s["param"].startswith("RLL_")
                       for s in r.param_suggestions)

    def test_plane_axis_mode_fallback_excludes_q_modes(self):
        # No PIQ/QTUN logged (reduced LOG_BITMASK): Q-mode intervals
        # from MODE must still cut the hover contamination. QHOVER(18)
        # for t<60 with garbage data, RTL(11) after with clean tracking.
        ts_q = TS[TS < 60.0]
        ts_fw = TS[TS >= 60.0]
        garbage = self._garbage_pid(ts_q)
        tar_fw = np.sin(2 * np.pi * 0.25 * ts_fw)
        pidr = {
            "_timestamp": TS,
            "Tar": np.concatenate([garbage["Tar"], tar_fw]),
            "Act": np.concatenate([garbage["Act"], 0.98 * tar_fw]),
            "P": np.full(N, 0.1), "I": np.full(N, 0.05),
            "D": np.full(N, 0.01), "FF": np.full(N, 0.02),
        }
        err = pidr["Tar"] - pidr["Act"]
        full_ratio = (np.sqrt(np.mean(err**2))
                      / np.sqrt(np.mean(pidr["Tar"]**2)))
        assert full_ratio >= Thresholds().pid_tracking_warn  # precondition
        mode = {"_timestamp": np.array([0.0, 60.0]),
                "Mode": np.array([18.0, 11.0])}
        r = _run({"PIDR": pidr, "MODE": mode, "POS": _pos_flying()},
                 params={"RLL_RATE_P": 0.3846},
                 vt="ArduPlane", quadplane=True)
        plane = _sub(r, "固定翼相")
        assert plane is not None
        assert plane.status == "good"


class TestPscInfo:

    def _psc(self):
        v = 2.0 * np.sin(2 * np.pi * 0.1 * TS)
        return {
            "PSCN": {"_timestamp": TS, "TVN": v, "VN": v * 0.9},
            "PSCE": {"_timestamp": TS, "TVE": v, "VE": v * 0.95},
            "PSCD": {"_timestamp": TS, "TVD": v * 0.2, "VD": v * 0.21},
        }

    def test_psc_reported_as_info(self):
        msgs = {"PIQR": _pid("good"), "POS": _pos_flying(), **self._psc()}
        r = _run(msgs, params={"Q_A_RAT_RLL_P": 0.19},
                 vt="ArduPlane", quadplane=True)
        psc = _sub(r, "位置环")
        assert psc is not None                    # precondition
        assert psc.status == "info"
        assert len(psc.details) == 3
        # info sub-result must not dilute the overall verdict
        assert r.status == "good"

    def test_no_psc_no_subresult(self):
        r = _run({"PIQR": _pid("good"), "POS": _pos_flying()},
                 params={"Q_A_RAT_RLL_P": 0.19},
                 vt="ArduPlane", quadplane=True)
        assert _sub(r, "位置环") is None
