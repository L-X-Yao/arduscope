"""ArduPilot's EKF failsafe test, evaluated on the logged variances.

ArduCopter and ArduPlane use the same test (``ekf_check.cpp``): a
magnetometer variance at or above ``FS_EKF_THRESH`` scores one; a velocity
variance at or above it scores one, and two at twice the threshold;
position fires only with at least one other score, so the test needs two
indicators (or a very large velocity variance). Height is not part of the
decision. The condition must hold for about a second, ten checks at 10 Hz,
before the failsafe fires. Plane runs the check only while the VTOL
position controller is active (``QuadPlane::in_vtol_posvel_mode()``).

A single variance over the threshold, the compass being the common case,
is therefore a health signal, not an ArduPilot failsafe.
"""

from __future__ import annotations

import numpy as np

# Ten failed checks at 10 Hz before Plane/Copter declare the failsafe.
TRIGGER_SECONDS = 1.0

# The Plane modes for which quadplane.in_vtol_posvel_mode() is true
# without also needing auto_state.vtol_loiter: QLOITER, QLAND, QRTL and
# QAUTOTUNE. Guided flight is left out because the log does not say
# whether it was loitering on the VTOL controller.
PLANE_VTOL_POSVEL_MODES = (19, 20, 21, 22)


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
