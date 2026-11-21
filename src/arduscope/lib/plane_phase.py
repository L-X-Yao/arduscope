"""Which log rows an ArduPlane flew on its wing.

The fixed-wing controllers keep logging through phases their numbers say
nothing about: hovering, sitting on the ground, or a stage that runs them with
other settings. STAT holds the firmware's own view of the flight. QTUN and
MODE cover what it leaves out.
"""

from __future__ import annotations

import numpy as np

# ArduPlane VTOL mode numbers (ArduPlane/mode.h): QSTABILIZE=17,
# QHOVER=18, QLOITER=19, QLAND=20, QRTL=21, QAUTOTUNE=22, QACRO=23,
# LOITER_ALT_QLAND=25. THERMAL=24 is fixed-wing.
PLANE_Q_MODES = {17, 18, 19, 20, 21, 22, 23, 25}

# MANUAL=0 and TRAINING=3 are servo passthrough: Plane::stabilize()
# routes them to mode->run() without the rate controllers, so their
# PIDR/PIDP rows carry stale values from the last stabilized mode.
_PLANE_PASSTHROUGH_MODES = {0, 3}

# AP_FixedWing::FlightStage::NORMAL, as logged in STAT.Stage.
FLIGHT_STAGE_NORMAL = 3

# Data-sufficiency floor: a mode-bounce sliver must not carry a verdict.
MIN_FIXED_WING_S = 10.0


def stat_at(reader, ts: np.ndarray, field: str) -> np.ndarray | None:
    """The STAT `field` in force at each of `ts`.

    Returns None when the log has fewer than two STAT rows.
    """
    if not reader.has_message("STAT"):
        return None
    stat = reader.get_messages_as_arrays("STAT", [field])
    s_ts = stat["_timestamp"]
    if len(s_ts) < 2:
        return None
    idx = np.searchsorted(s_ts, ts, side="right") - 1
    return stat[field][np.clip(idx, 0, len(s_ts) - 1)]


def covered_seconds(ts: np.ndarray) -> float:
    """Time spanned by `ts`, leaving out gaps of a second or more."""
    if len(ts) < 2:
        return 0.0
    dt = np.diff(ts)
    return float(np.sum(dt[dt < 1.0]))


def vtol_windows_mask(reader, ts: np.ndarray) -> np.ndarray | None:
    """True where `ts` is NOT under fixed-wing rate control.

    Union of two window sources — neither alone suffices:
    - PIQ*/QTUN stream proximity: the Q controllers log only while
      running, which also catches VTOL segments inside AUTO
      missions. But the stream can pause (observed: QLAND) and
      reduced LOG_BITMASK configs drop it entirely.
    - MODE intervals: every pilot-flown Q mode, plus the
      MANUAL/TRAINING passthrough modes whose PID rows are stale
      (Plane::stabilize skips the rate controllers there).

    Returns None when neither source exists.
    """
    combined: np.ndarray | None = None
    for msg in ("PIQR", "PIQP", "PIQY", "QTUN"):
        if not reader.has_message(msg):
            continue
        ref = reader.get_messages_as_arrays(msg, [])["_timestamp"]
        if len(ref) > 1:
            idx = np.searchsorted(ref, ts)
            prev_gap = np.abs(ts - ref[np.clip(idx - 1, 0, len(ref) - 1)])
            next_gap = np.abs(ref[np.clip(idx, 0, len(ref) - 1)] - ts)
            combined = np.minimum(prev_gap, next_gap) <= 0.5
            break
    if reader.has_message("MODE"):
        mode = reader.get_messages_as_arrays("MODE", ["Mode"])
        mts, mvals = mode["_timestamp"], mode["Mode"]
        if len(mts) > 0:
            in_q = np.zeros(len(ts), dtype=bool)
            edges = np.append(mts, np.inf)
            skip = PLANE_Q_MODES | _PLANE_PASSTHROUGH_MODES
            for i in range(len(mts)):
                mv = mvals[i]
                if np.isfinite(mv) and int(mv) in skip:
                    in_q |= (ts >= edges[i]) & (ts < edges[i + 1])
            combined = in_q if combined is None else (combined | in_q)
    return combined
