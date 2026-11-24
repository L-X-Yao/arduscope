"""Which attitude-controller rules apply to a vehicle, and on which samples.

The 30 deg hard limit (`AC_ATTITUDE_THRUST_ERROR_ANGLE`) and the autotune
relaxation ceilings come from `AC_AttitudeControl`, the multirotor attitude
controller. ArduPlane's fixed-wing controller does not use them, so a
fixed-wing segment is graded by neither. A QuadPlane is under the Q (Copter)
controller only in its VTOL phases, so the Copter rules apply there alone.
"""

from __future__ import annotations

import numpy as np

from .plane_phase import PLANE_Q_MODES


def scope_grade_mask(reader, vehicle, t: np.ndarray) -> tuple[np.ndarray, str]:
    """Return (samples to grade, scope) for the Copter attitude rules.

    scope is one of "copter", "vtol", "vtol_unknown_phase", "fixed_wing" or
    "unknown":
    - copter: every sample;
    - vtol: QuadPlane samples inside a VTOL window;
    - vtol_unknown_phase: a QuadPlane log with no phase source at all. An
      empty mask: the log cannot say which time was under the Q controller,
      so the Copter rules are not graded rather than applied to a fixed-wing
      segment by default;
    - fixed_wing: an empty mask. The caller keeps the metrics and figures and
      does not grade a Copter critical or warning;
    - unknown: an empty mask; the caller stops rather than guess.
    """
    if vehicle.is_copter and not vehicle.is_quadplane:
        return np.ones(len(t), dtype=bool), "copter"
    if vehicle.is_quadplane:
        mask = _vtol_samples(reader, t)
        if mask is None:
            return np.zeros(len(t), dtype=bool), "vtol_unknown_phase"
        return mask, "vtol"
    if vehicle.is_plane:
        return np.zeros(len(t), dtype=bool), "fixed_wing"
    return np.zeros(len(t), dtype=bool), "unknown"


def _vtol_samples(reader, t: np.ndarray) -> np.ndarray | None:
    """QuadPlane samples under the Q controller.

    Two sources, as in plane_phase.vtol_windows_mask: the Q controller's own
    log stream (it logs only while running) and the Q modes.

    This is deliberately NOT that helper, and the two differ in one way:
    vtol_windows_mask also counts MANUAL and TRAINING. It exists to clean up
    the fixed-wing PID analysis, where those modes leave stale PID rows that
    must be dropped from the fixed-wing set, so it folds them into "not
    fixed-wing". Here the question is the opposite one, "is the Copter
    controller running", and in MANUAL/TRAINING no attitude controller runs
    at all, so neither the Copter nor the fixed-wing rule describes them.
    Folding them in would grade servo passthrough by the 30 deg limit.

    The shared part is kept as a separate copy on purpose: changing
    vtol_windows_mask would change which rows pid_tuning analyses.
    """
    combined: np.ndarray | None = None
    for msg in ("PIQR", "PIQP", "PIQY", "QTUN"):
        if not reader.has_message(msg):
            continue
        ref = reader.get_messages_as_arrays(msg, [])["_timestamp"]
        if len(ref) > 1:
            idx = np.searchsorted(ref, t)
            prev_gap = np.abs(t - ref[np.clip(idx - 1, 0, len(ref) - 1)])
            next_gap = np.abs(ref[np.clip(idx, 0, len(ref) - 1)] - t)
            combined = np.minimum(prev_gap, next_gap) <= 0.5
            break
    if reader.has_message("MODE"):
        mode = reader.get_messages_as_arrays("MODE", ["Mode"])
        mts, mvals = mode["_timestamp"], mode["Mode"]
        if len(mts) > 0:
            in_q = np.zeros(len(t), dtype=bool)
            edges = np.append(mts, np.inf)
            for i in range(len(mts)):
                mv = mvals[i]
                if np.isfinite(mv) and int(mv) in PLANE_Q_MODES:
                    in_q |= (t >= edges[i]) & (t < edges[i + 1])
            combined = in_q if combined is None else (combined | in_q)
    return combined
