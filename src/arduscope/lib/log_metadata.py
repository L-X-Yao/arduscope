"""Identity and flight time read from a log, independent of persistence."""

from __future__ import annotations

import datetime
import re

import numpy as np

# Boot banner: "<board-name> XXXXXXXX XXXXXXXX XXXXXXXX" (STM32 UID)
_SERIAL_RE = re.compile(
    r"^(\S+)\s+([0-9A-F]{8})\s+([0-9A-F]{8})\s+([0-9A-F]{8})\s*$")

# GPS epoch for GWk/GMS -> UTC (leap seconds ignored; date precision)
_GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)


def extract_identity(reader) -> tuple[str | None, str | None]:
    """(serial, board_name) from the boot banner, or (None, None)."""
    for tm in getattr(reader, "text_messages", []) or []:
        m = _SERIAL_RE.match(str(tm.text).strip())
        if m:
            serial = f"{m.group(2)}-{m.group(3)}-{m.group(4)}"
            return serial, m.group(1)
    return None, None


def extract_flight_time(reader) -> float | None:
    """UTC seconds since 1970 from the first valid GPS week, else None."""
    if not reader.has_message("GPS"):
        return None
    gps = reader.get_messages_as_arrays("GPS", ["GWk", "GMS"])
    gwk, gms = gps["GWk"], gps["GMS"]
    ok = np.isfinite(gwk) & (gwk > 0) & np.isfinite(gms)
    if not np.any(ok):
        return None
    wk = float(gwk[ok][0])
    ms = float(gms[ok][0])
    dt = _GPS_EPOCH + datetime.timedelta(weeks=wk, milliseconds=ms)
    return dt.timestamp()


def format_flight_time(when: float) -> str:
    """UTC 'YYYY-MM-DD HH:MM' of an extract_flight_time() value."""
    dt = datetime.datetime.fromtimestamp(when, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def extract_flight_date(reader) -> str:
    """UTC 'YYYY-MM-DD HH:MM' from the first valid GPS week, else ''."""
    when = extract_flight_time(reader)
    return format_flight_time(when) if when is not None else ""
