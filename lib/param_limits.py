"""Documented ranges of the parameters this project suggests.

Values are the @Range metadata in the ArduPilot source. The firmware does
not enforce them, but a tool should not recommend leaving them.
"""

from __future__ import annotations

import numbers
import re

_RANGES = [
    # libraries/Filter/HarmonicNotchFilter.cpp
    (r"INS_HNT(CH|C2)_FREQ", 10.0, 495.0),
    (r"INS_HNT(CH|C2)_BW", 5.0, 250.0),
    (r"INS_HNT(CH|C2)_REF", 0.0, 1.0),
    # libraries/AC_AttitudeControl/AC_AttitudeControl_Multi.cpp
    (r"(ATC|Q_A)_RAT_(RLL|PIT)_P", 0.01, 0.5),
    (r"(ATC|Q_A)_RAT_(RLL|PIT)_I", 0.01, 2.0),
    (r"(ATC|Q_A)_RAT_(RLL|PIT)_D", 0.0, 0.05),
    (r"(ATC|Q_A)_RAT_(RLL|PIT)_FLTD", 5.0, 100.0),
    (r"(ATC|Q_A)_RAT_YAW_P", 0.10, 2.5),
    (r"(ATC|Q_A)_RAT_YAW_I", 0.01, 1.0),
    (r"(ATC|Q_A)_RAT_YAW_D", 0.0, 0.02),
    (r"(ATC|Q_A)_RAT_YAW_FLTD", 5.0, 50.0),
    (r"(ATC|Q_A)_RAT_(RLL|PIT|YAW)_FF", 0.0, 0.5),
    (r"(ATC|Q_A)_THR_MIX_MAX", 0.5, 0.9),
    # libraries/AP_Motors/AP_MotorsMulticopter.cpp
    (r"(MOT|Q_M)_SPIN_MIN", 0.0, 0.25),
    (r"(MOT|Q_M)_THST_HOVER", 0.125, 0.6875),
    # libraries/APM_Control/AP_{Roll,Pitch,Yaw}Controller.cpp
    (r"(RLL|PTCH|YAW)_RATE_P", 0.08, 0.35),
    (r"(RLL|PTCH|YAW)_RATE_I", 0.01, 0.6),
    (r"(RLL|PTCH|YAW)_RATE_D", 0.001, 0.03),
    (r"(RLL|PTCH|YAW)_RATE_FF", 0.0, 3.0),
    (r"(RLL|PTCH|YAW)_RATE_FLTD", 0.0, 50.0),
    # libraries/AP_TECS/AP_TECS.cpp
    (r"TECS_SPDWEIGHT", 0.0, 2.0),
    (r"TECS_LAND_ARSPD", -1.0, 127.0),
    (r"TECS_PITCH_MAX", 0.0, 45.0),
]
_COMPILED = [(re.compile(pattern + r"\Z"), low, high)
             for pattern, low, high in _RANGES]


def param_range(name: str) -> tuple[float, float] | None:
    """Documented (low, high) of a parameter, None if not in the table."""
    for pattern, low, high in _COMPILED:
        if pattern.match(name):
            return low, high
    return None


def limit_suggestions(suggestions: list[dict]) -> list[dict]:
    """Keep every suggestion inside the parameter's documented range.

    A value past the limit is pulled back to it. If the current value is
    already out of range the user chose that deliberately, and only a jump
    could bring it back, so the suggestion is dropped. So is one that the
    limit reduces to no change.
    """
    kept = []
    for suggestion in suggestions:
        limits = param_range(suggestion["param"])
        value = suggestion["suggested"]
        current = suggestion["current"]
        # numbers.Real, not (int, float): numpy's float32 and int64 are
        # neither, and a value of those types went through unchecked.
        if limits is None or not isinstance(value, numbers.Real):
            kept.append(suggestion)
            continue
        low, high = limits
        if low <= value <= high:
            kept.append(suggestion)
        elif (isinstance(current, numbers.Real) and low <= current <= high
              and min(max(value, low), high) != current):
            kept.append({**suggestion, "suggested": min(max(value, low), high)})
    return kept
