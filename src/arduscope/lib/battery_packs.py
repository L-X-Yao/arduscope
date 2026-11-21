"""Which BAT instances draw current, and what they add up to.

A BAT instance is not always a pack. BATTn_MONITOR=3 reads voltage only, and
AP logs its current as NaN: fleet logs use that for rails at 2.5, 7 and 24 V.
BATTn_MONITOR=10 is the sum of other monitors, so adding it to them counts
every amp twice.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# AP_BattMonitor_Params: "Sum Of Selected Monitors".
_SUM_MONITOR = 10

# AP_BattMonitor var_info: BATT_, BATT2_ to BATT9_, then BATTA_ to BATTG_.
_PARAM_PREFIXES = ["BATT", *(f"BATT{c}" for c in "23456789ABCDEFG")]


@dataclass
class Pack:
    """Rows of one BAT instance that measures current."""
    instance: int
    ts: np.ndarray
    volt: np.ndarray
    curr: np.ndarray
    curr_tot: np.ndarray


def _is_sum_monitor(reader, instance: int) -> bool:
    if instance >= len(_PARAM_PREFIXES):
        return False
    monitor = reader.get_param(f"{_PARAM_PREFIXES[instance]}_MONITOR", 0)
    return int(monitor) == _SUM_MONITOR


def read_packs(reader) -> list[Pack]:
    """BAT instances with a current reading, lowest instance first.

    A Sum monitor is left out, unless it holds the only reading. Empty when
    nothing in the log measures current.
    """
    bat = reader.get_messages_as_arrays(
        "BAT", ["Inst", "Volt", "Curr", "CurrTot"])
    inst = bat["Inst"]
    known = np.unique(inst[~np.isnan(inst)])
    # A log with no Inst column holds a single battery.
    rows = ([(int(i), inst == i) for i in known] if len(known)
            else [(0, np.ones(len(inst), dtype=bool))])

    packs = [Pack(i, bat["_timestamp"][m], bat["Volt"][m], bat["Curr"][m],
                  bat["CurrTot"][m]) for i, m in rows]
    measured = [p for p in packs if np.any(np.isfinite(p.curr))]
    return ([p for p in measured
             if not _is_sum_monitor(reader, p.instance)] or measured)


def sum_over_packs(packs: list[Pack], values: list[np.ndarray]) -> np.ndarray:
    """Add one array per pack, on the first pack's time base."""
    base = packs[0].ts
    total = np.array(values[0], dtype=float)
    for pack, value in zip(packs[1:], values[1:]):
        good = np.isfinite(pack.ts) & np.isfinite(value)
        if np.sum(good) >= 2:
            total += np.interp(base, pack.ts[good], value[good])
    return total
