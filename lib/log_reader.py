"""
LogReader: pymavlink wrapper for ArduPilot .BIN log files.

Single-pass architecture:
  - prescan + data preload in ONE file read
  - Metadata (params, modes, errors, events) collected alongside message data
  - get_messages_as_arrays() returns from cache (zero I/O after init)
"""

import hashlib
import os
import pickle  # noqa: S403 - cache is self-generated from local .BIN files
from array import array
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import numpy as np
from pymavlink import DFReader as _dfreader
from pymavlink import mavutil
from pymavlink.DFReader import DFReader

# Disk cache directory for parsed log data. Test-only feature; override
# with ARDUSCOPE_LOG_CACHE to share one cache between checkouts.
_CACHE_DIR = os.environ.get(
    "ARDUSCOPE_LOG_CACHE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 ".log_cache"))

# Bump whenever parsing changes what ends up in the cache. Part of the
# cache key, so a stale entry can never mask a parser regression.
_CACHE_VERSION = 3

# Set while armed in POWR.Safety (AP_Logger::Write_Power).
_POWR_ARMED_BIT = 4

# Every DataFlash message opens with these two bytes, and its length is one
# byte of its FMT record, so none is over 255 long (AP_Logger/LogStructure.h:
# HEAD_BYTE1, HEAD_BYTE2, log_Format.length).
_HEAD1, _HEAD2 = 0xA3, 0x95
_MAX_MESSAGE = 255

# The extensions pymavlink reads as a binary DataFlash log.
_BINARY_SUFFIXES = (".bin", ".px4log")

# How much of a log may be something else and still be read. pymavlink walks
# such bytes one at a time, in Python, on every pass over the file: 1.2 s a
# megabyte here, and one analysis makes up to seven passes. None of the 560
# corpus logs has a single such byte.
_MAX_NOT_LOG_BYTES = 1 << 20

_SCAN_BLOCK = 8 << 20


class UnreadableLog(ValueError):
    """A .BIN that cannot be read as a DataFlash log, with the reason.

    not_log_bytes of the file's size bytes cannot be messages, the first of
    them at first_offset. Or pymavlink gave up on the format records, and
    reason is what it said.
    """

    def __init__(self, message: str, *, size: int = 0, not_log_bytes: int = 0,
                 first_offset: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.size = size
        self.not_log_bytes = not_log_bytes
        self.first_offset = first_offset
        self.reason = reason


def _say_nothing(*_args, **_kwargs) -> None:
    """Stands in for print() inside pymavlink's DFReader module.

    It prints a line for every byte it cannot place: 64 KB of zeros in a
    log came to 262,000 lines on the console, and 5 MB to 36 million and
    a minute and a half. What matters of it is counted by not_log_bytes().
    """


_dfreader.print = _say_nothing


def not_log_bytes(path: str) -> tuple[int, int]:
    """(bytes of the file that cannot be log data, offset of the first).

    Past 255 bytes from one message header with no next one, the bytes
    belong to no message. That takes in a run of zeros from a failed copy
    or an incomplete download, text, and nearly all of any other kind of
    file. It cannot see a wrong value inside a message: the format has no
    checksum.
    """
    size = os.path.getsize(path)
    total, first = 0, -1
    last_head = -_MAX_MESSAGE     # so the bytes before the first header count
    with open(path, "rb") as handle:
        offset, carry = 0, b""
        while True:
            block = handle.read(_SCAN_BLOCK)
            if not block:
                break
            data = np.frombuffer(carry + block, dtype=np.uint8)
            heads = (np.flatnonzero((data[:-1] == _HEAD1)
                                    & (data[1:] == _HEAD2))
                     + (offset - len(carry)))
            if len(heads):
                over = np.diff(heads, prepend=last_head) - _MAX_MESSAGE
                bad = over > 0
                if np.any(bad):
                    total += int(np.sum(over[bad]))
                    if first < 0:
                        at = int(np.argmax(bad))
                        first = int(heads[at] - over[at])
                last_head = int(heads[-1])
            offset += len(block)
            carry = block[-1:]
    tail = size - (last_head + _MAX_MESSAGE)
    if tail > 0:
        total += tail
        if first < 0:
            first = last_head + _MAX_MESSAGE
    return total, max(first, 0)


def _refuse_what_is_not_a_log(path: str) -> None:
    """Raise UnreadableLog before pymavlink spends minutes on the file."""
    if not path.lower().endswith(_BINARY_SUFFIXES):
        return
    size = os.path.getsize(path)
    not_log, first = not_log_bytes(path)
    if not_log * 2 > size:
        raise UnreadableLog(
            f"Not a DataFlash log, {not_log} of its {size} bytes are not "
            f"log data: {path}",
            size=size, not_log_bytes=not_log, first_offset=first)
    if not_log > _MAX_NOT_LOG_BYTES:
        raise UnreadableLog(
            f"Damaged log, {not_log / 1e6:.1f} MB of it is not log data, "
            f"from byte {first}: {path}",
            size=size, not_log_bytes=not_log, first_offset=first)


def _close_quietly(resource) -> None:
    try:
        resource.close()
    except (AttributeError, BufferError, OSError, ValueError):
        pass


def _forget(mlog) -> None:
    """Drop pymavlink's own reference to a reader that is done with.

    mavutil keeps the last connection in a module global. For a log that
    is its index, an offset for every message: most of a gigabyte for an
    813 MB log. It stayed until the next log's index had been built beside
    it, so a web server peaked 1.7 GB higher on its third large log than
    on its first.
    """
    if getattr(mavutil, "mavfile_global", None) is mlog:
        mavutil.mavfile_global = None


def _release_failed_reader(error: BaseException) -> None:
    """Let go of the file held by a reader whose constructor raised.

    DFReader opens and maps the file before it parses anything, so a bad
    FMT record leaves no reader to close, and a reference cycle holds the
    file until the next garbage collection. The reader is still reachable
    through the traceback, and it may be only half built.
    """
    frame = error.__traceback__
    while frame is not None:
        reader = frame.tb_frame.f_locals.get("self")
        if isinstance(reader, DFReader):
            _close_quietly(getattr(reader, "data_map", None))
            _close_quietly(getattr(reader, "filehandle", None))
        frame = frame.tb_next


@contextmanager
def _open_log(path: str) -> Iterator:
    """Open a DataFlash log and always release its file handle and mmap.

    Windows cannot delete a file that is still open, so an unclosed reader
    leaves every web upload behind.

    PYMAVLINK_FAST_INDEX=0 selects pymavlink's Python indexer. The compiled
    one calls C exit() on a malformed FMT record, which ends the process
    past any except clause: one corrupt upload would take down the web
    server, and one corrupt log a whole batch. The Python indexer raises
    ordinary exceptions and benchmarks no slower on real logs. DFReader
    reads this variable at construction, so it is set right here.
    """
    os.environ["PYMAVLINK_FAST_INDEX"] = "0"
    try:
        mlog = mavutil.mavlink_connection(path)
    except BaseException as e:
        _release_failed_reader(e)
        # The constructor reads every FMT, UNIT and MULT record, and trips
        # over a damaged one in a dozen ways: a bare Exception, struct.error,
        # AttributeError, TypeError. The user was told "smp_cnt" or "GMS".
        if (isinstance(e, Exception)
                and not isinstance(e, (OSError, MemoryError))):
            reason = f"{type(e).__name__}: {e}"
            raise UnreadableLog(
                f"The log's format records cannot be read ({reason}): "
                f"{path}", reason=reason) from e
        raise
    # pymavlink picks its reader by file extension, and anything but .bin
    # or .log gets the telemetry log reader. That one reads any bytes at
    # all as a stream of bad packets with dates of its own: an HTML report
    # came out as a flight of 130,000 years with a health score of 90.
    if not isinstance(mlog, DFReader):
        _close_quietly(mlog)
        _forget(mlog)
        raise ValueError(f"Not a DataFlash log (.BIN or .log): {path}")
    try:
        yield mlog
    finally:
        _close_quietly(mlog)
        _forget(mlog)


@dataclass
class ModeChange:
    """A flight mode change event."""
    timestamp: float     # seconds since log start
    mode_name: str
    mode_number: int


@dataclass
class ErrorEvent:
    """An error event from the ERR message."""
    timestamp: float
    subsys: int
    ecode: int


@dataclass
class Event:
    """An event from the EV message."""
    timestamp: float
    event_id: int


@dataclass
class TextMessage:
    """A text message from the MSG message."""
    timestamp: float
    text: str


# Below this span a log cannot hold even one record of the 1 Hz messages,
# so there is nothing to base a conclusion on.
_MIN_LOG_SPAN_S = 1.0

# Message types that are metadata-only (not useful for analysis data)
_META_TYPES = frozenset({
    'FMT', 'FMTU', 'MULT', 'ISBD', 'ISBH', 'UNIT',
    'PARM', 'MODE', 'ERR', 'EV', 'MSG',
})


class LogReader:
    """
    Wraps pymavlink to provide convenient access to ArduPilot .BIN logs.

    Usage:
        reader = LogReader("00000285.BIN")
        # Or with preload for faster analysis:
        reader = LogReader("00000285.BIN",
                           preload_types=["ATT", "GPS", "BAT"])
        data = reader.get_messages_as_arrays("ATT", ["Roll", "Pitch", "Yaw"])
    """

    def __init__(self, filepath: str,
                 preload_types: list[str] | None = None,
                 disk_cache: bool = False):
        """
        Args:
            filepath: Path to .BIN log file
            preload_types: Message types to preload in the same pass as
                          metadata scan.  If provided, these types are cached
                          immediately (single-pass). If None, only metadata
                          is collected (two-pass: prescan now, data later).
            disk_cache: If True, cache ALL parsed data (metadata + message
                       arrays) to disk.  Subsequent reads of the same file
                       skip pymavlink entirely (~100x faster).  Useful for
                       repeated test runs.
        """
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"Log file not found: {filepath}")
        if os.path.getsize(filepath) == 0:
            raise ValueError(f"Log file is empty: {filepath}")
        self.filepath = os.path.abspath(filepath)
        self._disk_cache = disk_cache

        # Metadata populated during pre-scan
        self.params: dict[str, float] = {}
        self.mode_list: list[ModeChange] = []
        self.errors: list[ErrorEvent] = []
        self.events: list[Event] = []
        self.text_messages: list[TextMessage] = []
        self.available_messages: set[str] = set()
        self.vehicle_type: str = "Unknown"
        self.firmware_version: str = "Unknown"
        self.board_name: str = "Unknown"
        self.time_range: tuple[float, float] = (0.0, 0.0)

        # Message cache: msg_type -> {field_name: np.ndarray}
        self._msg_cache: dict[str, dict[str, np.ndarray]] = {}
        # Track which msg_types had ALL fields captured (preload or
        # preload_all) vs those populated by partial-field queries.
        # Used by get_messages_as_arrays to decide whether missing
        # fields are "truly missing from log" or "need to re-read".
        self._fully_cached: set[str] = set()
        # Batch-sampler data: ISBH sequence number -> header + raw samples
        self.isb_batches: dict[int, dict] = {}

        # Try disk cache first (only when disk_cache is enabled)
        if disk_cache and self._load_disk_cache():
            return
        self._prescan(preload_types, want_all=disk_cache)
        if disk_cache:
            self._save_disk_cache()

    # ------------------------------------------------------------------
    # Single-pass scan: metadata + optional data preload
    # ------------------------------------------------------------------

    def _prescan(self, preload_types: list[str] | None = None,
                 want_all: bool = False):
        """
        Read entire log in ONE pass to collect:
        1. Metadata: params, modes, errors, events, text messages
        2. Available message types
        3. Batch-sampler (ISBH/ISBD) data
        4. (Optional) Full field data for preload_types, or for every
           non-metadata type when want_all is set
        """
        _refuse_what_is_not_a_log(self.filepath)
        with _open_log(self.filepath) as mlog:
            self._scan(mlog, preload_types, want_all)

    def _scan(self, mlog, preload_types: list[str] | None,
              want_all: bool) -> None:
        """Single-pass body of _prescan; mlog is closed by the caller."""
        # Determine which types to preload data for
        want_data: set[str] = set()
        if preload_types:
            want_data = set(preload_types)

        # Raw data collection: msg_type -> {'_timestamp': [...], field: [...]}
        # array('d') holds 8 bytes per value. A list of Python floats costs
        # 32, which put peak memory at about 22x the log size.
        raw: dict[str, dict[str, array]] = {}

        first_timestamp = None
        last_timestamp = None

        while True:
            m = mlog.recv_match()
            if m is None:
                break

            msg_type = m.get_type()
            self.available_messages.add(msg_type)

            # Batch-sampler data is collected here so the notch analyzer
            # does not need a second pass over the file.
            # A batch row without these fields is of a layout this does not
            # know, or its FMT record is damaged. That costs the spectrum,
            # not the log: it used to end the read with "smp_cnt".
            if msg_type == 'ISBH':
                try:
                    self.isb_batches[m.N] = {
                        "type": m.type,
                        "instance": getattr(m, 'instance', 0),
                        "mul": m.mul,
                        "smp_rate": m.smp_rate,
                        "smp_cnt": m.smp_cnt,
                        # Seconds since the log's first row, like events
                        # and every message array. TimeUS counts from boot,
                        # and a log can open long after that.
                        "time_s": self._relative_ts(
                            getattr(m, '_timestamp', None), first_timestamp),
                        "x": array('h'), "y": array('h'), "z": array('h'),
                    }
                except (AttributeError, TypeError):
                    pass
                continue
            if msg_type == 'ISBD':
                try:
                    batch = self.isb_batches.get(m.N)
                    if batch is not None:
                        batch["x"].extend(m.x)
                        batch["y"].extend(m.y)
                        batch["z"].extend(m.z)
                except (AttributeError, TypeError, OverflowError):
                    self.isb_batches.pop(getattr(m, 'N', None), None)
                continue

            # Skip format-only types entirely
            if msg_type in ('FMT', 'FMTU', 'MULT', 'UNIT'):
                continue

            # Track timestamps
            ts = getattr(m, '_timestamp', None)
            if ts is not None:
                if first_timestamp is None:
                    first_timestamp = ts
                last_timestamp = ts

            # ---- Metadata extraction ----
            if msg_type == 'PARM':
                name = getattr(m, 'Name', None)
                value = getattr(m, 'Value', None)
                if name is not None and value is not None:
                    self.params[name] = value
                continue

            elif msg_type == 'MODE':
                mode_name = getattr(m, 'Mode', '')
                mode_num = getattr(m, 'ModeNum', getattr(m, 'Mode', 0))
                t = self._relative_ts(ts, first_timestamp)
                if isinstance(mode_name, (int, float)):
                    mode_num = int(mode_name)
                    mode_name = str(mode_num)
                try:
                    mode_number = int(mode_num)
                except (TypeError, ValueError):
                    mode_number = 0
                self.mode_list.append(ModeChange(
                    timestamp=t, mode_name=str(mode_name),
                    mode_number=mode_number,
                ))
                continue

            elif msg_type == 'ERR':
                subsys = getattr(m, 'Subsys', 0)
                ecode = getattr(m, 'ECode', 0)
                t = self._relative_ts(ts, first_timestamp)
                self.errors.append(ErrorEvent(
                    timestamp=t, subsys=subsys, ecode=ecode))
                continue

            elif msg_type == 'EV':
                eid = getattr(m, 'Id', 0)
                t = self._relative_ts(ts, first_timestamp)
                self.events.append(Event(timestamp=t, event_id=eid))
                continue

            elif msg_type == 'MSG':
                text = getattr(m, 'Message', '')
                t = self._relative_ts(ts, first_timestamp)
                self.text_messages.append(TextMessage(
                    timestamp=t, text=text))
                self._parse_firmware_msg(text)
                continue

            # ---- Data preload (only for wanted types) ----
            if not want_all and msg_type not in want_data:
                continue

            bucket = raw.get(msg_type)
            if bucket is None:
                # First message of this type: discover fields
                bucket = {'_timestamp': array('d')}
                fieldnames = m.get_fieldnames()
                for fn in fieldnames:
                    bucket[fn] = array('d')
                raw[msg_type] = bucket

            if ts is None or first_timestamp is None:
                # Skip messages before any timestamp has been seen —
                # relative timing would otherwise be wrong.
                continue
            bucket['_timestamp'].append(ts - first_timestamp)

            for fn in bucket:
                if fn == '_timestamp':
                    continue
                val = getattr(m, fn, None)
                try:
                    bucket[fn].append(float(val))
                except (TypeError, ValueError):
                    bucket[fn].append(float('nan'))

        # Also grab params from mlog.params
        if hasattr(mlog, 'params') and mlog.params:
            for k, v in mlog.params.items():
                if k not in self.params:
                    self.params[k] = v

        # Finalise time range
        if first_timestamp is not None and last_timestamp is not None:
            self.time_range = (first_timestamp, last_timestamp)

        # If no MODE messages found, try flightmode_list from pymavlink
        if not self.mode_list:
            try:
                with _open_log(self.filepath) as mlog2:
                    fm_list = mlog2.flightmode_list()
                for mode_name, t_start, t_end in fm_list:
                    rel = t_start - first_timestamp if first_timestamp else 0
                    self.mode_list.append(ModeChange(
                        timestamp=rel, mode_name=mode_name, mode_number=0
                    ))
            except Exception:
                pass

        # Convert raw lists to numpy arrays and store in cache.
        # Prescan captures EVERY field of the message (see field
        # discovery at bucket['_timestamp'] = [] + for fn in fieldnames),
        # so these entries are fully cached.
        self._store_columns(raw)

    def _store_columns(self, raw: dict[str, dict[str, array]]) -> None:
        """Move raw columns into the numpy cache, freeing them as it goes.

        Every field of each type was captured, so entries count as fully
        cached: a later partial-field query must not fill NaN for a field
        the log really has.
        """
        for msg_type in list(raw):
            columns = raw.pop(msg_type)
            self._msg_cache[msg_type] = {
                k: np.array(v, dtype=np.float64) for k, v in columns.items()
            }
            self._fully_cached.add(msg_type)

    def _parse_firmware_msg(self, text: str):
        """Extract vehicle type and version from firmware MSG lines."""
        text_lower = text.lower()
        if 'arducopter' in text_lower or 'arduplane' in text_lower or \
           'ardurover' in text_lower or 'ardusub' in text_lower:
            parts = text.split()
            if len(parts) >= 2:
                self.vehicle_type = parts[0]
                self.firmware_version = parts[1]

    @staticmethod
    def _relative_ts(absolute_ts: Optional[float],
                     first_ts: Optional[float]) -> float:
        """Convert absolute timestamp to seconds since log start."""
        if absolute_ts is None or first_ts is None:
            return 0.0
        return absolute_ts - first_ts

    # ------------------------------------------------------------------
    # Bulk preload (for types not loaded during prescan)
    # ------------------------------------------------------------------

    def preload_all(self, msg_types: list[str] | None = None):
        """
        Read the log file and cache requested message types.
        Skips types already in cache (from prescan preload).

        Args:
            msg_types: types to preload. If None, loads all non-meta types.
        """
        if msg_types is None:
            msg_types = [t for t in self.available_messages
                         if t not in _META_TYPES]

        wanted = set(t for t in msg_types
                     if t in self.available_messages
                     and t not in self._msg_cache)
        if not wanted:
            return

        raw: dict[str, dict[str, array]] = {}
        for t in wanted:
            raw[t] = {'_timestamp': array('d')}

        first_ts = self.time_range[0] if self.time_range[0] != 0.0 else None
        with _open_log(self.filepath) as mlog:
            while True:
                m = mlog.recv_match(type=wanted)
                if m is None:
                    break

                bucket = raw.get(m.get_type())
                if bucket is None:
                    continue

                ts = getattr(m, '_timestamp', None)
                if ts is None:
                    continue
                bucket['_timestamp'].append(ts - (first_ts or 0.0))

                if len(bucket) == 1:
                    for fn in m.get_fieldnames():
                        bucket[fn] = array('d')
                for fn in bucket:
                    if fn == '_timestamp':
                        continue
                    val = getattr(m, fn, None)
                    try:
                        bucket[fn].append(float(val))
                    except (TypeError, ValueError):
                        bucket[fn].append(float('nan'))

        self._store_columns(raw)

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    # Field name aliases: maps (msg_type, preferred_name) -> [fallback_names].
    # ArduPilot renames fields between firmware versions; this layer lets
    # analyzers use a single canonical name without version checks.
    _FIELD_ALIASES: dict[tuple[str, str], list[str]] = {
        ("TECS", "hgt"): ["h"],
        ("TECS", "spd"): ["sp"],
        ("TECS", "h"):   ["hgt"],
        ("TECS", "sp"):  ["spd"],
        # ArduPilot 4.1+ renamed the ARSP health field "Health" -> "H".
        # Without this alias the airspeed health check reads all-NaN and
        # never fires (silent false negative on a failing pitot).
        ("ARSP", "Health"): ["H"],
        # RFND logs its instance column as "Instance" (LogStructure.h),
        # not "I" — without this, multi-rangefinder instance separation
        # reads all-NaN and silently merges every sensor.
        ("RFND", "I"): ["Instance"],
        # BAT logs remaining percentage as "RemPct", not "Rem" — the old
        # name is all-NaN on every real log so the remaining-% detail and
        # figure never render.
        ("BAT", "Rem"): ["RemPct"],
        # PM renamed the total-loop-count column "NLoop" -> "NL"
        # (current LogStructure.h). Without this alias the long-loop
        # CPU-pressure check reads all-NaN on modern logs and silently
        # never fires (caught by tools/check_msg_fields.py).
        ("PM", "NLoop"): ["NL"],
        # ARSP renamed the primary-sensor flag "Primary" -> "Pri".
        ("ARSP", "Primary"): ["Pri"],
    }

    def get_messages_as_arrays(self, msg_type: str,
                               fields: list[str]) -> dict[str, np.ndarray]:
        """
        Return requested fields for msg_type as numpy arrays.
        Uses cache if available; reads file only as fallback.
        Fields not found in the log are filled with NaN arrays.
        Applies field name aliases for cross-version compatibility.
        """
        cached = self._msg_cache.get(msg_type)
        if cached is not None:
            missing = [f for f in fields if f not in cached]
            if not missing:
                return {f: cached[f] for f in ['_timestamp'] + fields
                        if f in cached}

            # Some fields missing. Two cases:
            # (a) msg_type is in _fully_cached → every field the log
            #     actually contains was captured (preload_all reads via
            #     get_fieldnames, so any missing field truly doesn't
            #     exist in the log) → return NaN arrays.
            # (b) cache was populated by a partial-field query → missing
            #     fields may well exist in the log, just weren't
            #     captured. Re-read the entire msg_type via preload_all
            #     so the cache becomes genuinely complete; a union-
            #     re-read with only the currently-requested fields would
            #     silently strand any future query for an unrequested
            #     field behind the _fully_cached guard (historical bug:
            #     attitude reads RATE[R,P,Y...], then pid_tuning reads
            #     RATE[ROut], re-read union misses POut/YOut, then
            #     Pitch/Yaw axis queries return all-NaN).
            if msg_type in self._fully_cached:
                n = len(cached.get('_timestamp', []))
                nan_arr = np.full(n, np.nan)
                result = {'_timestamp': cached['_timestamp']}
                for f in fields:
                    result[f] = cached[f] if f in cached else nan_arr
                for f in fields:
                    for alt in self._FIELD_ALIASES.get((msg_type, f), []):
                        if alt in cached:
                            result[alt] = cached[alt]
                return self._apply_aliases(msg_type, fields, result)

            # Partial-cache miss — delete and re-read all fields.
            del self._msg_cache[msg_type]
            self.preload_all([msg_type])
            cached = self._msg_cache.get(msg_type, {})
            n = len(cached.get('_timestamp', []))
            nan_arr = np.full(n, np.nan)
            result = {'_timestamp': cached.get('_timestamp', np.array([]))}
            for f in fields:
                result[f] = cached[f] if f in cached else nan_arr
                for alt in self._FIELD_ALIASES.get((msg_type, f), []):
                    if alt in cached:
                        result[alt] = cached[alt]
            return self._apply_aliases(msg_type, fields, result)

        # Cache miss: first read. Pull requested fields + aliases.
        all_fields = list(fields)
        for f in fields:
            for alt in self._FIELD_ALIASES.get((msg_type, f), []):
                if alt not in all_fields:
                    all_fields.append(alt)
        data = self._read_msg_type(msg_type, all_fields)
        self._msg_cache[msg_type] = data

        result = {f: data[f] for f in ['_timestamp'] + all_fields if f in data}
        return self._apply_aliases(msg_type, fields, result)

    def _apply_aliases(self, msg_type: str, requested: list[str],
                       result: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Fill missing requested fields from aliases if available."""
        for f in requested:
            if f in result and not np.all(np.isnan(result[f])):
                continue
            for alt in self._FIELD_ALIASES.get((msg_type, f), []):
                if alt in result and not np.all(np.isnan(result[alt])):
                    result[f] = result[alt]
                    break
        return result

    def _read_msg_type(self, msg_type: str,
                       fields: list[str]) -> dict[str, np.ndarray]:
        """Single-pass read of all instances of msg_type from the file."""
        first_ts = self.time_range[0] if self.time_range[0] != 0.0 else None
        if first_ts is None:
            with _open_log(self.filepath) as mlog:
                m0 = mlog.recv_match()
                if m0 is not None:
                    first_ts = getattr(m0, '_timestamp', 0.0)

        columns: dict[str, array] = {'_timestamp': array('d')}
        for f in fields:
            columns[f] = array('d')

        with _open_log(self.filepath) as mlog:
            while True:
                m = mlog.recv_match(type=msg_type)
                if m is None:
                    break

                ts = getattr(m, '_timestamp', None)
                if ts is None:
                    continue
                columns['_timestamp'].append(ts - (first_ts or 0.0))

                for f in fields:
                    val = getattr(m, f, None)
                    try:
                        columns[f].append(float(val) if val is not None
                                          else float('nan'))
                    except (TypeError, ValueError):
                        # Non-numeric field (string, bytes) -> NaN placeholder
                        columns[f].append(float('nan'))

        return {k: np.array(v, dtype=np.float64) for k, v in columns.items()}

    def get_param(self, name: str, default: float = 0.0) -> float:
        """Get a parameter value from the log, with default fallback."""
        return self.params.get(name, default)

    def has_message(self, msg_type: str) -> bool:
        """Check if a message type exists in the log."""
        return msg_type in self.available_messages

    def get_armed_intervals(self) -> list[tuple[float, float]]:
        """Return list of (start, end) intervals where vehicle was armed.

        ARMED/DISARMED events are exact, so they lead. The logger can open
        after ARMED was sent, which leaves a DISARMED with no partner or
        no event at all, and the armed bit in POWR.Safety fills that hole.
        Only for a run the log opened into, though: a motor test sets the
        same bit without arming, and with the logger already running a
        real arming would have written ARMED.
        """
        events = [ev for ev in self.events if ev.event_id in (10, 11)]
        if not events:
            return self._armed_at_log_open()
        intervals = []
        arm_start = None
        for ev in events:
            if ev.event_id == 10:
                arm_start = ev.timestamp
            elif arm_start is not None:
                intervals.append((arm_start, ev.timestamp))
                arm_start = None
            elif ev is events[0]:
                intervals.append((self._armed_since(ev.timestamp),
                                  ev.timestamp))
        if arm_start is not None:
            duration = self.get_flight_duration()
            intervals.append((arm_start, duration))
        return intervals

    def never_armed(self) -> bool:
        """True when the log shows the vehicle stayed disarmed throughout.

        An empty get_armed_intervals() does not say that on its own: a log
        with no POWR.Safety and no events returns it too, and such a log
        may hold a whole flight.
        """
        return (not any(ev.event_id in (10, 11) for ev in self.events)
                and self._powr_armed_runs() is not None
                and not self._armed_at_log_open())

    def _armed_at_log_open(self) -> list[tuple[float, float]]:
        """The armed run the log opened into, if there is one."""
        runs = self._powr_armed_runs()
        if not runs:
            return []
        first_row = self.get_messages_as_arrays("POWR", [])["_timestamp"][0]
        return runs[:1] if runs[0][0] == first_row else []

    def _armed_since(self, disarm_ts: float) -> float:
        """Start of the armed run closed by a DISARMED with no ARMED."""
        starts = [start for start, _ in self._powr_armed_runs() or []
                  if start <= disarm_ts]
        return starts[-1] if starts else 0.0

    def _powr_armed_runs(self) -> Optional[list[tuple[float, float]]]:
        """Armed runs from POWR.Safety, or None when the log has no such
        column (SITL, no battery logging, firmware before the field)."""
        if not self.has_message("POWR"):
            return None
        powr = self.get_messages_as_arrays("POWR", ["Safety"])
        known = np.isfinite(powr["Safety"])
        if not np.any(known):
            return None
        ts = powr["_timestamp"][known]
        armed = (powr["Safety"][known].astype(int) & _POWR_ARMED_BIT) != 0
        edges = np.flatnonzero(armed[1:] != armed[:-1]) + 1
        bounds = np.concatenate(([0], edges, [len(armed)]))
        return [(float(ts[a]), float(ts[b - 1]))
                for a, b in zip(bounds[:-1], bounds[1:]) if armed[a]]

    def has_flight_data(self) -> bool:
        """True if the log holds data messages over a usable time span.

        pymavlink parses arbitrary bytes into an empty log without error,
        and any log with one stray record into a near-empty one.
        """
        return (bool(self.available_messages - _META_TYPES)
                and self.get_flight_duration() >= _MIN_LOG_SPAN_S)

    def get_flight_duration(self) -> float:
        """Return total log duration in seconds."""
        if self.time_range[0] == 0.0 and self.time_range[1] == 0.0:
            return 0.0
        return self.time_range[1] - self.time_range[0]

    # ------------------------------------------------------------------
    # Vehicle type properties
    # ------------------------------------------------------------------

    # Vendor forks rename the firmware banner (e.g. "Acme-FC"),
    # so the type string alone can miss. The parameter table is
    # vehicle-specific in ArduPilot (each vehicle compiles its own
    # Parameters.cpp), which makes namespace markers authoritative:
    # FRAME_CLASS exists only on Copter, Q_ENABLE only on Plane.

    @property
    def is_copter(self) -> bool:
        if 'Copter' in self.vehicle_type:
            return True
        if any(k in self.vehicle_type for k in ('Plane', 'Rover', 'Sub')):
            return False
        return 'FRAME_CLASS' in self.params

    @property
    def is_plane(self) -> bool:
        if 'Plane' in self.vehicle_type:
            return True
        if any(k in self.vehicle_type for k in ('Copter', 'Rover', 'Sub')):
            return False
        return 'Q_ENABLE' in self.params

    @property
    def is_quadplane(self) -> bool:
        """True if ArduPlane with the Q (VTOL) subsystem enabled.

        Q_ENABLE: 0=disabled, 1=enabled, 2=enabled + VTOL AUTO. Both 1
        and 2 are QuadPlanes, so test >= 1 (not == 1).
        """
        return self.is_plane and self.get_param('Q_ENABLE', 0) >= 1

    @property
    def canonical_vehicle_type(self) -> str:
        """vehicle_type with the canonical ArduPilot family appended when
        a vendor-renamed firmware banner lacks it.

        Vendor forks rename the banner (e.g. "Acme-FC"), so the raw
        vehicle_type may contain no 'Copter'/'Plane' substring even when
        is_copter/is_plane resolve correctly via the param-namespace
        fallback. Downstream substring checks (Thresholds.from_params,
        analyzer can_run gates) need the family in the string; this
        property is the single source of that augmentation.
        """
        vt = self.vehicle_type
        if self.is_copter and 'Copter' not in vt:
            return f"{vt} (ArduCopter)"
        if self.is_plane and 'Plane' not in vt:
            return f"{vt} (ArduPlane)"
        return vt

    # ------------------------------------------------------------------
    # Disk cache: avoid re-parsing unchanged .BIN files
    # ------------------------------------------------------------------

    def _cache_key(self) -> str:
        """Generate cache key from cache version, path, size and mtime."""
        stat = os.stat(self.filepath)
        raw = (f"{_CACHE_VERSION}:{self.filepath}:{stat.st_size}:"
               f"{stat.st_mtime_ns}")
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()

    def _cache_path(self) -> str:
        return os.path.join(_CACHE_DIR, self._cache_key() + ".pkl")

    def _load_disk_cache(self) -> bool:
        """Try to restore metadata (and optionally data) from disk cache."""
        path = self._cache_path()
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)  # noqa: S301
            self.params = data["params"]
            self.mode_list = data["mode_list"]
            self.errors = data["errors"]
            self.events = data["events"]
            self.text_messages = data["text_messages"]
            self.available_messages = data["available_messages"]
            self.vehicle_type = data["vehicle_type"]
            self.firmware_version = data["firmware_version"]
            self.board_name = data["board_name"]
            self.time_range = data["time_range"]
            self.isb_batches = data["isb_batches"]
            if "msg_cache" in data:
                self._msg_cache = data["msg_cache"]
            if "fully_cached" in data:
                self._fully_cached = set(data["fully_cached"])
            return True
        except Exception:
            return False

    def _save_disk_cache(self):
        """Save metadata (and optionally all message data) to disk cache."""
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            data = {
                "params": self.params,
                "mode_list": self.mode_list,
                "errors": self.errors,
                "events": self.events,
                "text_messages": self.text_messages,
                "available_messages": self.available_messages,
                "vehicle_type": self.vehicle_type,
                "firmware_version": self.firmware_version,
                "board_name": self.board_name,
                "time_range": self.time_range,
                "isb_batches": self.isb_batches,
            }
            if self._disk_cache and self._msg_cache:
                data["msg_cache"] = self._msg_cache
                data["fully_cached"] = sorted(self._fully_cached)
            with open(self._cache_path(), "wb") as f:
                pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass  # Cache write failure is non-fatal
