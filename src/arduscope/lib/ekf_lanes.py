"""Which EKF core is the primary lane, from the PI column of XKF4/NKF4.

AP moves the primary lane between cores in flight, and its failsafe and
navigation use whichever lane is primary at that moment, so the lowest core
number says nothing about the estimate flying the aircraft.
"""

from __future__ import annotations

import numpy as np


def primary_lane_rows(core: np.ndarray, primary: np.ndarray) -> np.ndarray:
    """True on XKF4/NKF4 rows logged by the lane primary at that moment.

    Falls back to the lowest core when the log has no usable PI column.
    """
    core_id = np.nan_to_num(core, nan=-1).astype(int)
    rows = core_id == np.nan_to_num(primary, nan=-2).astype(int)
    if np.any(rows):
        return rows
    return core_id == int(np.nanmin(core))


def primary_core_at(reader, kf4_type: str, ts: np.ndarray):
    """Primary core index at each of `ts`, or None when the log cannot say.

    For messages other than XKF4/NKF4: XKF1 rows carry their own core but
    not the primary one.
    """
    if not reader.has_message(kf4_type):
        return None
    kf4 = reader.get_messages_as_arrays(kf4_type, ["PI"])
    known = np.isfinite(kf4["PI"])
    if not np.any(known):
        return None
    primary = kf4["PI"][known].astype(int)
    idx = np.searchsorted(kf4["_timestamp"][known], ts, side="right") - 1
    return primary[np.clip(idx, 0, len(primary) - 1)]
