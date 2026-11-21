"""Which output channels drive motors, and reading any output channel.

Output numbers are configuration, not convention: motors can sit on any
SERVOn. On the fleet's OctaQuads the eight rotors are outputs 1-4 and
10-13, with the pusher on 5 and control surfaces on 6-8. Assuming C1..Cn
reads those as rotors and misses the real ones.
"""

from __future__ import annotations

import numpy as np

# SERVOn_FUNCTION to motor number (SRV_Channel.h, k_motor1..k_motor32)
_MOTOR_FUNCTIONS = {
    **{33 + i: 1 + i for i in range(8)},
    **{82 + i: 9 + i for i in range(4)},
    **{160 + i: 13 + i for i in range(20)},
}
_MAX_OUTPUTS = 32

# RCOU logs outputs 1-14, RCO2 15-18, RCO3 19-32 (AP_Logger/LogFile.cpp)
_OUTPUT_MESSAGES = (("RCOU", 1, 14), ("RCO2", 15, 18), ("RCO3", 19, 32))
OUTPUT_MESSAGE_TYPES = [name for name, _, _ in _OUTPUT_MESSAGES]


def channels_with_function(reader, functions) -> list[tuple[str, int]]:
    """(channel, function) of every output set to one of the functions."""
    found = []
    for output in range(1, _MAX_OUTPUTS + 1):
        function = int(reader.get_param(f"SERVO{output}_FUNCTION", 0))
        if function in functions:
            found.append((f"C{output}", function))
    return found


def motor_channels(reader) -> list[str]:
    """Channels assigned to motors, in motor order. Empty if none are."""
    found = channels_with_function(reader, _MOTOR_FUNCTIONS)
    found.sort(key=lambda item: _MOTOR_FUNCTIONS[item[1]])
    return [channel for channel, _ in found]


def read_outputs(reader, channels: list[str]) -> dict[str, np.ndarray]:
    """PWM of any output channels, on the RCOU time base.

    Channels above 14 live in RCO2/RCO3. Those are logged right after
    RCOU, so the nearest following sample is the same output frame. A
    channel the log does not carry comes back as NaN.
    """
    wanted: dict[str, list[str]] = {}
    for channel in channels:
        number = int(channel[1:])
        for message, first, last in _OUTPUT_MESSAGES:
            if first <= number <= last:
                wanted.setdefault(message, []).append(channel)

    base = reader.get_messages_as_arrays("RCOU", wanted.get("RCOU", []))
    times = base.get("_timestamp", np.array([]))
    out = {"_timestamp": times}
    for channel in wanted.get("RCOU", []):
        out[channel] = base[channel]

    for message, _, _ in _OUTPUT_MESSAGES[1:]:
        if message not in wanted:
            continue
        data = (reader.get_messages_as_arrays(message, wanted[message])
                if reader.has_message(message) else {})
        extra_times = data.get("_timestamp", np.array([]))
        for channel in wanted[message]:
            if len(extra_times) == 0:
                out[channel] = np.full(len(times), np.nan)
            else:
                index = np.searchsorted(extra_times, times)
                index = np.minimum(index, len(extra_times) - 1)
                out[channel] = data[channel][index]
    return out
