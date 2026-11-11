"""Whether a GPS.VZ value was measured.

A driver with no vertical velocity logs VZ as 0, not as missing
(AP_GPS_Backend::fill_3d_velocity). GPA.VV, written next to each GPS row,
says which it was.
"""

from __future__ import annotations

import numpy as np


def vz_measured(reader, ts: np.ndarray, inst: np.ndarray) -> np.ndarray:
    """True on GPS rows (`ts`, `inst`) whose VZ is a measurement.

    A log with no GPA.VV counts as measured: VZ is all it has.
    """
    measured = np.ones(len(ts), dtype=bool)
    if not reader.has_message("GPA"):
        return measured
    gpa = reader.get_messages_as_arrays("GPA", ["I", "VV"])
    gpa_inst = np.nan_to_num(gpa["I"], nan=0).astype(int)
    gps_inst = np.nan_to_num(inst, nan=0).astype(int)
    for instance in np.unique(gps_inst):
        mine = (gpa_inst == instance) & np.isfinite(gpa["VV"])
        if not np.any(mine):
            continue
        rows = gps_inst == instance
        idx = np.searchsorted(gpa["_timestamp"][mine], ts[rows],
                              side="right") - 1
        measured[rows] = gpa["VV"][mine][np.maximum(idx, 0)] > 0
    return measured
