"""Upload-name and memory-budget helpers for the web app.

Kept out of web_app.py so the limits can be tested without starting a
server. The stateful HTTP layer stays there.
"""

from __future__ import annotations

import os
import re
import threading
from collections import deque

# Peak memory of one analysis, fitted above the peak of a server that had
# analysed the same log four times running: 0.23 GB for a 5 MB log, 0.43 GB
# for 49 MB, 0.97 GB for 229 MB, 3.30 GB for 663 MB and 4.13 GB for 813 MB.
# Past the base that is up to 4.8 bytes for each byte of log. The first
# analysis in a new process peaks a tenth to a third lower, and the Copter
# and Plane logs measured take less for each byte than these QuadPlane logs.
ANALYSIS_BASE_BYTES = 200 * 1024 * 1024
ANALYSIS_PEAK_PER_LOG_BYTE = 6

# Characters a Windows file name cannot hold, and control characters.
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# upload_<id>_ goes before an upload's name and <id>_..._report.html around
# its stem, and a file name holds 255 bytes at most.
MAX_NAME_BYTES = 200


def _clean_upload_name(raw: str) -> str:
    """The name an upload is stored, listed and reported under.

    It comes from the request and goes into two file names. A NUL in it
    raised past the code that frees the upload's slot, and a colon made
    Windows write the log into a stream of an empty file that stayed
    behind.
    """
    raw = raw.encode('utf-8', 'ignore').decode('utf-8')
    name = _UNSAFE_NAME_CHARS.sub('_', os.path.basename(raw.replace('\\', '/')))
    stem, ext = os.path.splitext(name)
    room = max(MAX_NAME_BYTES - len(ext.encode('utf-8')), 0)
    return stem.encode('utf-8')[:room].decode('utf-8', 'ignore') + ext


def _estimated_peak_bytes(log_bytes: int) -> int:
    return ANALYSIS_BASE_BYTES + ANALYSIS_PEAK_PER_LOG_BYTE * log_bytes


class _MemoryBudget:
    """Admit analyses in arrival order while their memory fits.

    Memory follows log size, so counting analyses says nothing: three
    logs at the upload limit are put at 19 GB, three small ones under 1 GB.
    Arrival order keeps a stream of small logs from starving a large one.
    An analysis over the whole budget still runs, once it is alone.
    """

    def __init__(self, limit: int):
        self._limit = limit
        self._used = 0
        self._waiting: deque[object] = deque()
        self._changed = threading.Condition()

    def acquire(self, cost: int) -> None:
        ticket = object()
        with self._changed:
            self._waiting.append(ticket)
            while (self._waiting[0] is not ticket
                   or (self._used and self._used + cost > self._limit)):
                self._changed.wait()
            self._waiting.popleft()
            self._used += cost
            self._changed.notify_all()

    def release(self, cost: int) -> None:
        with self._changed:
            self._used -= cost
            self._changed.notify_all()
