"""What may leave the machine, and what is stripped before it does.

Every tool result passes through here. The defaults send the analysis
results and the fields the tools need; coordinates, the board serial and
free text stay local unless the user opted in.
"""

from __future__ import annotations

import re

# Message fields that locate the aircraft. Names are compared lowercased,
# so "Lat", "GPSLat" and "Position_Lat" all match the first entry.
_GPS_FIELDS = frozenset({
    "lat", "lng", "lon", "long", "latitude", "longitude",
    "gpslat", "gpslng", "gpslon", "poslat", "poslng",
    "latdeg", "lngdeg",
})

# Boot banner: "<board-name> XXXXXXXX XXXXXXXX XXXXXXXX" (STM32 UID).
_SERIAL_RE = re.compile(
    r"\b([0-9A-F]{8})\s+([0-9A-F]{8})\s+([0-9A-F]{8})\b")

# A coordinate pair written into free text ("35.123, 104.567").
_COORD_RE = re.compile(
    r"-?\d{1,3}\.\d{3,}\s*[,;/]\s*-?\d{1,3}\.\d{3,}")


def is_gps_field(name: str) -> bool:
    """Whether a log field name carries a horizontal position."""
    return str(name).strip().lower().replace("_", "") in _GPS_FIELDS


def redact_text(text: str, *, allow_serial: bool = False,
                allow_text: bool = False) -> str:
    """Free text as it may be sent.

    ``allow_text`` false means the text is not sent at all; the caller
    should not call this in that case. With text allowed, coordinates
    are removed unless ``allow_gps`` is handled by the caller, and the
    board serial is removed unless allowed.
    """
    out = str(text)
    if not allow_serial:
        out = _SERIAL_RE.sub("[serial]", out)
    # Coordinates in free text are always removed: the GPS opt-in covers
    # message fields, and a text message has no schema to bound it.
    out = _COORD_RE.sub("[position]", out)
    return out


def egress_categories(allow_gps: bool, allow_serial: bool,
                      allow_text: bool) -> list[str]:
    """Categories the current configuration may send, for the report."""
    cats = ["results", "parameters", "messages"]
    if allow_gps:
        cats.append("gps")
    if allow_serial:
        cats.append("serial")
    if allow_text:
        cats.append("text")
    return cats
