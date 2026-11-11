"""Where a log shows the lift motors carrying the aircraft, and at what thrust.

Hover thrust, the notch reference and the motor noise spectrum are all
measured on a flight, and "armed with some throttle" is not one: it takes in
a ground run, where the motors turn at a fraction of hover thrust and the
aircraft never leaves the ground.
"""

from __future__ import annotations

import numpy as np

# Level-hover limits of update_throttle_hover (ArduCopter/Attitude.cpp,
# ArduPlane/quadplane.cpp): the firmware only learns hover inside these.
HOVER_MAX_CLIMB_MS = 0.6
HOVER_MAX_TILT_DEG = 5.0

# Throttle above which the lift motors are taken to be working.
LIFT_THROTTLE = 0.05

# LogEvent ids of Copter's land detector (AP_Logger.h).
_LAND_COMPLETE = 18
_NOT_LANDED = 28


def lift_message(reader) -> str | None:
    """The message that logs lift throttle as 0 to 1, None if there is none.

    QTUN on a QuadPlane, CTUN on a Copter. A fixed-wing Plane's CTUN.ThO is
    the forward motor's, in percent.
    """
    if reader.is_quadplane:
        return "QTUN"
    if reader.is_copter:
        return "CTUN"
    return None


def _off_the_ground(reader, thresholds, ts: np.ndarray,
                    alt: np.ndarray) -> np.ndarray | None:
    """Rows of ts, with their altitude, where the aircraft has left the
    ground. None if the log gives nothing to tell by.

    Copter logs its land detector: NOT_LANDED and LAND_COMPLETE are the
    state update_throttle_hover itself goes by. Plane logs neither, and
    STAT.isFlying is already true during spool-up on the ground, so there
    the altitude is the judge: above where the aircraft stood when it
    armed. The lowest point of the log does not stand in for that. Lift
    rotors in ground effect pull the baro height down by metres
    (EK3_GND_EFF_DZ), and a ground run then reads as a hover 2 m up.
    """
    marks = sorted((event.timestamp, event.event_id)
                   for event in getattr(reader, "events", [])
                   if event.event_id in (_LAND_COMPLETE, _NOT_LANDED))
    if marks:
        # A log that opens in flight has no NOT_LANDED before its landing.
        flying = marks[0][1] == _LAND_COMPLETE
        since = ts[0] if len(ts) else 0.0
        up = np.zeros(len(ts), dtype=bool)
        for when, event_id in marks:
            if flying:
                up |= (ts >= since) & (ts < when)
            flying, since = event_id == _NOT_LANDED, when
        if flying:
            up |= ts >= since
        return up

    known = np.isfinite(alt)
    if not np.any(known):
        return None
    height = np.full(len(ts), np.nan)
    stretches = reader.get_armed_intervals() or [(ts[0], ts[-1])]
    for start, end in stretches:
        rows = (ts >= start) & (ts <= end) & known
        if not np.any(rows):
            continue
        before = np.flatnonzero((ts < start) & known)
        ground = alt[before[-1]] if len(before) else alt[np.flatnonzero(rows)[0]]
        height[rows] = alt[rows] - ground
    with np.errstate(invalid="ignore"):
        return height > thresholds.hover_airborne_alt_m


def airborne(reader, thresholds, ts: np.ndarray) -> np.ndarray | None:
    """Rows of ts where the lift motors carry the aircraft in the air.

    Armed, lift throttle up, and off the ground. A log with nothing to
    tell the ground by is judged on the throttle alone. None when the log
    does not show the lift throttle at all.
    """
    source = lift_message(reader)
    if source is None or not reader.has_message(source):
        return None
    tun = reader.get_messages_as_arrays(source, ["ThO", "Alt"])
    tun_ts = tun["_timestamp"]
    known = np.isfinite(tun["ThO"])
    if np.sum(known) < 2:
        return None
    up = np.interp(ts, tun_ts[known], tun["ThO"][known]) > LIFT_THROTTLE

    flying = _off_the_ground(reader, thresholds, tun_ts, tun["Alt"])
    if flying is not None:
        # Each row takes the state of the throttle row before it.
        at = np.clip(np.searchsorted(tun_ts, ts, side="right") - 1,
                     0, len(tun_ts) - 1)
        up &= flying[at]

    armed = reader.get_armed_intervals()
    if armed:
        inside = np.zeros(len(ts), dtype=bool)
        for start, end in armed:
            inside |= (ts >= start) & (ts <= end)
        up &= inside
    return up


def hover_thrust(reader, thresholds, min_rows: int = 10) -> float | None:
    """Median thrust in level hover, or None if the log has no hover.

    This is ThO, motors->get_throttle(), which is what the firmware learns
    the hover parameter from. RCOU cannot stand in for it: PWM comes after
    thrust_to_actuator() applies expo and the spin range, so a correct
    parameter reads 0.1 to 0.2 away from it.

    The gate is the firmware's own (Copter and QuadPlane
    update_throttle_hover): off the ground, level, and not climbing.
    Manual-throttle modes stay in: level and not climbing takes hover
    thrust whoever holds the stick.
    """
    source = lift_message(reader)
    if (source is None or not reader.has_message(source)
            or not reader.has_message("ATT")):
        return None
    tun = reader.get_messages_as_arrays(source, ["ThO", "Alt", "CRt"])
    att = reader.get_messages_as_arrays("ATT", ["Roll", "Pitch"])
    ts, att_ts = tun["_timestamp"], att["_timestamp"]
    if len(ts) == 0 or len(att_ts) == 0:
        return None
    flying = _off_the_ground(reader, thresholds, ts, tun["Alt"])
    if flying is None:
        return None

    # The firmware asks for throttle above zero. A copter carried about
    # with its motors at 1% is not hovering, though its land detector let
    # go: two such logs read a hover throttle under 0.03.
    nearest = np.minimum(np.searchsorted(att_ts, ts), len(att_ts) - 1)
    hover = ((tun["ThO"] > LIFT_THROTTLE)
             & flying
             & (np.abs(tun["CRt"]) < HOVER_MAX_CLIMB_MS)
             & (np.abs(att["Roll"][nearest]) < HOVER_MAX_TILT_DEG)
             & (np.abs(att["Pitch"][nearest]) < HOVER_MAX_TILT_DEG))

    # The wing carries some of the weight once the forward motor runs.
    # On a tailsitter that motor is the lift motor.
    if (reader.is_quadplane and reader.has_message("CTUN")
            and not reader.get_param("Q_TAILSIT_ENABLE", 0)):
        ctun = reader.get_messages_as_arrays("CTUN", ["ThO"])
        ctun_ts = ctun["_timestamp"]
        if len(ctun_ts) > 0:
            forward = ctun["ThO"][
                np.minimum(np.searchsorted(ctun_ts, ts), len(ctun_ts) - 1)]
            hover &= forward <= max(0, reader.get_param("THR_MIN", 0) + 10)

    thrust = tun["ThO"][hover]
    return float(np.median(thrust)) if len(thrust) > min_rows else None
