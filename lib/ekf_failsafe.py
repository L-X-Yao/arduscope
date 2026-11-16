"""ArduPilot's EKF failsafe test, evaluated on the logged variances.

ArduCopter and ArduPlane use the same test (``ekf_check.cpp``): a
magnetometer variance at or above ``FS_EKF_THRESH`` scores one; a velocity
variance at or above it scores one, and two at twice the threshold;
position fires only with at least one other score, so the test needs two
indicators (or a very large velocity variance). Height is not part of the
decision. The condition must hold for about a second, ten checks at 10 Hz,
before the failsafe fires. Plane runs the check only while the VTOL
position controller is active (``QuadPlane::in_vtol_posvel_mode()``).

QLOITER, QLAND, QRTL and QAUTOTUNE are direct evidence of that controller.
AUTO needs phase evidence too: ``Plane::update_flight_stage`` logs
``STAT.Stage=VTOL`` both for ``in_vtol_auto()`` and for fixed-wing Q assist,
so Stage alone is not enough. ``QTUN.Ast`` records ``assisted_flight``;
a nearby row with its active bit clear separates the two. AUTO stays closed
without both signals or while assist is active. If assist and
``in_vtol_auto()`` overlap, as during a takeoff transition, this conservative
gate can miss a real AUTO VTOL trigger.

GUIDED remains excluded. Its gate also needs ``auto_state.vtol_loiter`` and
``poscontrol.get_state() > QPOS_APPROACH``. Neither state is logged directly;
even with Ast, QTUN does not expose ``guided_takeoff`` or the poscontrol state,
so it cannot prove the conjunction.

A single variance over the threshold, the compass being the common case,
is therefore a health signal, not an ArduPilot failsafe.
"""

from __future__ import annotations

import numpy as np

# Ten failed checks at 10 Hz before Plane/Copter declare the failsafe.
TRIGGER_SECONDS = 1.0

# Direct-mode cases in QuadPlane::in_vtol_posvel_mode(): QLOITER, QLAND,
# QRTL and QAUTOTUNE. AUTO and GUIDED need state beyond the mode number.
PLANE_VTOL_POSVEL_MODES = (19, 20, 21, 22)
_PLANE_AUTO_MODE = 10                 # ArduPlane/mode.h
_FLIGHT_STAGE_VTOL = 2               # AP_FixedWing::FlightStage::VTOL
_QTUN_MATCH_SECONDS = 0.25           # QTUN is 25 Hz; its tail lasts 250 ms
# Plane-4.5.1 writes assisted_flight as a boolean in QTUN.Ast; Plane 4.6+
# writes log_assistance_flags. Bit 0 is the active indication in both.
_QTUN_ASSIST_ACTIVE = 1


def over_threshold(sv, sp, sm, thresh: float) -> np.ndarray:
    """Per-sample boolean: ArduPilot's EKF test says "bad" at this row."""
    sv = np.asarray(sv, dtype=float)
    sp = np.asarray(sp, dtype=float)
    sm = np.asarray(sm, dtype=float)
    count = (sm >= thresh).astype(np.int8)
    count += np.where(sv >= 2.0 * thresh, 2,
                      np.where(sv >= thresh, 1, 0)).astype(np.int8)
    return ((sp >= thresh) & (count >= 1)) | (count >= 2)


def sustained(mask, timestamps, seconds: float = TRIGGER_SECONDS) -> np.ndarray:
    """Per-sample boolean: part of a run that held at least ``seconds``."""
    mask = np.asarray(mask, dtype=bool)
    ts = np.asarray(timestamps, dtype=float)
    held = np.zeros(len(mask), dtype=bool)
    i, n = 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        if ts[j] - ts[i] >= seconds:
            held[i:j + 1] = True
        i = j + 1
    return held


def in_modes(mode_list, timestamps, modes) -> np.ndarray | None:
    """Rows whose last mode change names one of ``modes``.

    None when the log records no mode change: without that evidence the
    caller cannot gate on a mode.
    """
    rows = [m for m in mode_list if getattr(m, "timestamp", None) is not None]
    if not rows:
        return None
    changes = np.array([m.timestamp for m in rows], dtype=float)
    numbers = np.array([m.mode_number for m in rows], dtype=int)
    idx = np.searchsorted(changes, np.asarray(timestamps, dtype=float),
                          side="right") - 1
    mask = np.zeros(len(timestamps), dtype=bool)
    valid = idx >= 0
    mask[valid] = np.isin(numbers[idx[valid]], modes)
    return mask


def plane_posvel_mask(reader, timestamps) -> np.ndarray | None:
    """Plane rows with positive evidence for ``in_vtol_posvel_mode()``.

    Direct Q modes come from MODE. AUTO additionally requires STAT's VTOL
    stage and a nearby QTUN row whose assist-active bit is clear. This is
    the distinction in Plane-4.5.1 ``Plane::update_flight_stage`` and
    ``QuadPlane::Log_Write_QControl_Tuning``. Unknown evidence fails closed.

    None means the log has no usable MODE changes, as in ``in_modes``.
    """
    ts = np.asarray(timestamps, dtype=float)
    direct = in_modes(reader.mode_list, ts, PLANE_VTOL_POSVEL_MODES)
    if direct is None:
        return None
    auto = in_modes(reader.mode_list, ts, (_PLANE_AUTO_MODE,))
    if not auto.any():
        return direct
    if not reader.has_message("STAT") or not reader.has_message("QTUN"):
        return direct

    stat = reader.get_messages_as_arrays("STAT", ["Stage"])
    qtun = reader.get_messages_as_arrays("QTUN", ["Ast"])
    stat_ts = stat["_timestamp"]
    qtun_ts = qtun["_timestamp"]
    if len(stat_ts) == 0 or len(qtun_ts) == 0:
        return direct

    # STAT is written on a stage change. Hold the last known stage, but do
    # not back-fill the first one into an earlier, unknown interval.
    stat_idx = np.searchsorted(stat_ts, ts, side="right") - 1
    stage_known = stat_idx >= 0
    stage_vtol = np.zeros(len(ts), dtype=bool)
    stage_vtol[stage_known] = (
        stat["Stage"][stat_idx[stage_known]] == _FLIGHT_STAGE_VTOL)

    # Match each EKF row to the nearest QTUN sample. A missing/old Ast field
    # is unknown, not evidence that Q assist was inactive. At assist start,
    # the previous Ast can persist for at most about one 25 Hz QTUN interval
    # (40 ms), far below the 1 s hold, so this cannot create a false critical.
    right = np.searchsorted(qtun_ts, ts)
    left = np.clip(right - 1, 0, len(qtun_ts) - 1)
    right = np.clip(right, 0, len(qtun_ts) - 1)
    use_left = np.abs(ts - qtun_ts[left]) <= np.abs(qtun_ts[right] - ts)
    nearest = np.where(use_left, left, right)
    distance = np.minimum(np.abs(ts - qtun_ts[left]),
                          np.abs(qtun_ts[right] - ts))
    ast = np.asarray(qtun["Ast"], dtype=float)
    finite_ast = np.isfinite(ast)
    ast_bits = np.zeros(len(ast), dtype=np.int64)
    ast_bits[finite_ast] = ast[finite_ast].astype(np.int64)
    no_assist = ((distance <= _QTUN_MATCH_SECONDS) & finite_ast[nearest]
                 & ((ast_bits[nearest] & _QTUN_ASSIST_ACTIVE) == 0))

    return direct | (auto & stage_vtol & no_assist)
