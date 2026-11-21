"""CSV output that cannot run as a spreadsheet formula."""

from __future__ import annotations

import csv
from typing import IO, Iterable


def _plain_text(cell: str) -> str:
    """Prefix an apostrophe when a spreadsheet would run the text.

    Excel and LibreOffice treat a cell starting with one of these as a
    formula, and firmware strings, board names and file names all come
    from the log, which can come from anyone. Plain numbers pass: -1.5 is
    data, and they are the only legitimate cells starting this way.
    """
    if cell.lstrip()[:1] not in ("=", "+", "-", "@"):
        return cell
    try:
        float(cell)
        return cell
    except ValueError:
        return "'" + cell


class SafeWriter:
    """csv.writer for rows that may hold text taken from a log."""

    def __init__(self, stream: IO[str]):
        self._writer = csv.writer(stream)

    def writerow(self, row: Iterable) -> None:
        self._writer.writerow(
            [_plain_text(c) if isinstance(c, str) else c for c in row])
