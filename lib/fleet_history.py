"""
Fleet history archive: per-airframe health records across flights.

Design: docs/fleet_history_design.md. The airframe key is the STM32
96-bit hardware UID that ArduPilot prints in its boot banner
("CubeOrange 001A002B 3030510C 31323334"), so every log
self-identifies its board. Each analyzed log stores one `flights` row
(deduplicated by file sha1) in an sqlite database living next to the
report directory. The history is DISPLAY-ONLY: it stores numbers the
analyzers already computed or trivial reader-level aggregates, and the
report section it feeds carries no verdicts (POSITIONING: no
threshold-free alarms).

Public API:
    record(db_path, pipeline_result, log_path) -> (serial, n_flights)
    NotArchived                                raised by record()
    backfill(db_path, sources, ...)            -> stats dict
    query_history(db_path, serial, limit)      -> list[dict]
    build_history_result(rows, serial)         -> AnalysisResult | None
    extract_identity(reader)                   -> (serial, board) | (None, None)
"""

from __future__ import annotations

import datetime
import gc
import hashlib
import json
import os
import re
import sqlite3

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from lib.results import AnalysisResult
from lib.thresholds import Thresholds

# Boot banner: "<board-name> XXXXXXXX XXXXXXXX XXXXXXXX" (STM32 UID)
_SERIAL_RE = re.compile(
    r"^(\S+)\s+([0-9A-F]{8})\s+([0-9A-F]{8})\s+([0-9A-F]{8})\s*$")

# GPS epoch for GWk/GMS -> UTC (leap seconds ignored — date precision)
_GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)

UNKNOWN_SERIAL = "unknown"


class NotArchived(Exception):
    """This run cannot stand for the flight, so nothing was stored."""


class AliasTaken(ValueError):
    """The alias is already another airframe's serial or alias."""


# ---------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------

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


def _armed_mask(reader, ts):
    armed = reader.get_armed_intervals()
    if not armed:
        return np.ones(len(ts), dtype=bool)
    mask = np.zeros(len(ts), dtype=bool)
    for s, e in armed:
        mask |= (ts >= s) & (ts <= e)
    return mask


def _safe(v) -> float | None:
    return round(float(v), 3) if v is not None and np.isfinite(v) else None


def extract_metrics(pipeline_result) -> dict:
    """Compact per-flight metric set (display-only aggregates)."""
    reader = pipeline_result.reader
    m: dict = {
        "statuses": {r.registry_key: r.status
                     for r in pipeline_result.results if r.registry_key},
    }

    # Vibration: primary-IMU VIBE peaks
    if reader.has_message("VIBE"):
        vibe = reader.get_messages_as_arrays(
            "VIBE", ["IMU", "VibeX", "VibeY", "VibeZ"])
        inst = vibe.get("IMU")
        sel = np.ones(len(vibe["_timestamp"]), dtype=bool)
        if inst is not None and np.any(np.isfinite(inst)):
            sel = np.nan_to_num(inst, nan=-1) == np.nanmin(inst)
        for ax in ("VibeX", "VibeY", "VibeZ"):
            vals = vibe[ax][sel]
            vals = vals[np.isfinite(vals)]
            m[f"vibe_max_{ax[-1].lower()}"] = (
                _safe(np.max(vals)) if len(vals) else None)

    # Battery: primary instance start/min voltage
    if reader.has_message("BAT"):
        bat = reader.get_messages_as_arrays("BAT", ["Inst", "Volt"])
        inst = bat.get("Inst")
        sel = np.ones(len(bat["_timestamp"]), dtype=bool)
        if inst is not None and np.any(np.isfinite(inst)):
            sel = np.nan_to_num(inst, nan=-1) == np.nanmin(inst)
        volt = bat["Volt"][sel]
        good = np.isfinite(volt) & (volt > 0)
        volt, ts = volt[good], bat["_timestamp"][sel][good]
        if len(volt):
            m["batt_v_start"] = _safe(volt[0])
            # Armed only: when power is pulled the rail decays through
            # 20 V and below before logging stops, and a whole-log minimum
            # records that instead of the battery. With no armed window
            # there is no telling the two apart, so no minimum is kept.
            armed_volt = volt[_armed_mask(reader, ts)]
            if reader.get_armed_intervals() and len(armed_volt):
                m["batt_v_min"] = _safe(np.min(armed_volt))

    # Board supply: armed-window Vcc sag (median-of-3 smoothed)
    if reader.has_message("POWR"):
        powr = reader.get_messages_as_arrays("POWR", ["Vcc"])
        ts, vcc = powr["_timestamp"], powr["Vcc"]
        ok = np.isfinite(vcc) & (vcc > 0)
        ts, vcc = ts[ok], vcc[ok]
        if len(vcc) >= 3:
            sm = np.median(np.stack([vcc[:-2], vcc[1:-1], vcc[2:]]), axis=0)
            sel = _armed_mask(reader, ts[1:-1])
            if int(np.sum(sel)) >= 10:
                m["vcc_sag"] = _safe(np.max(sm[sel]) - np.min(sm[sel]))

    # Attitude authority: armed-window RATE output RMS per axis
    if reader.has_message("RATE"):
        rate = reader.get_messages_as_arrays(
            "RATE", ["ROut", "POut", "YOut"])
        rts = rate["_timestamp"]
        sel = _armed_mask(reader, rts)
        for field, key in (("ROut", "rate_rms_r"), ("POut", "rate_rms_p"),
                           ("YOut", "rate_rms_y")):
            vals = rate[field][sel]
            vals = vals[np.isfinite(vals)]
            m[key] = (_safe(np.sqrt(np.mean(vals ** 2)))
                      if len(vals) > 10 else None)

    return m


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS airframes (
    serial TEXT PRIMARY KEY,
    alias TEXT DEFAULT '',
    board TEXT DEFAULT '',
    first_seen TEXT DEFAULT '',
    last_seen TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    serial TEXT NOT NULL REFERENCES airframes(serial),
    log_sha1 TEXT UNIQUE NOT NULL,
    log_name TEXT DEFAULT '',
    flight_date TEXT DEFAULT '',
    duration_s REAL DEFAULT 0,
    health_score INTEGER DEFAULT -1,
    fw_version TEXT DEFAULT '',
    fw_hash TEXT DEFAULT '',
    metrics_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_flights_serial
    ON flights(serial, flight_date);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _file_sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_full_run(pipeline_result) -> bool:
    """True if every analyzer that applies ran, at the log's own thresholds.

    Decided from the result, so no entry point has to remember to say.
    """
    from analyzers import discover_analyzers  # heavy, and imports lib
    reader = pipeline_result.reader
    standard = Thresholds.from_params(
        reader.params, reader.canonical_vehicle_type)
    if pipeline_result.thresholds != standard:
        return False
    return all(
        key in pipeline_result.results_by_key
        or not cls(reader, standard, pipeline_result.vehicle_info).can_run()
        for key, cls in discover_analyzers())


def record(db_path: str, pipeline_result, log_path: str,
           log_name: str | None = None) -> tuple[str, int]:
    """Archive one analyzed flight. Returns (serial, n_flights).

    log_name is the name to show, when log_path is a working copy.

    A flight is stored once, by file hash, so the first run stays as its
    score for good. Raises NotArchived for anything but a full standard
    run: --only summary would otherwise fix a flight at 100 and turn the
    real 87 away later.
    """
    if not _is_full_run(pipeline_result):
        raise NotArchived(t("fleet.partial_run"))
    reader = pipeline_result.reader
    serial, board = extract_identity(reader)
    if serial is None:
        serial, board = UNKNOWN_SERIAL, ""

    flight_date = extract_flight_date(reader)
    t0, t1 = reader.time_range
    duration = max(0.0, t1 - t0)
    fw = getattr(reader, "firmware_version", "") or ""
    metrics = extract_metrics(pipeline_result)

    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO airframes(serial, board, first_seen, last_seen)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(serial) DO UPDATE SET"
                "   last_seen=excluded.last_seen,"
                "   board=CASE WHEN excluded.board != ''"
                "         THEN excluded.board ELSE airframes.board END",
                (serial, board, flight_date, flight_date))
            conn.execute(
                "INSERT OR IGNORE INTO flights"
                " (serial, log_sha1, log_name, flight_date, duration_s,"
                "  health_score, fw_version, metrics_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (serial, _file_sha1(log_path),
                 log_name or os.path.basename(log_path), flight_date, duration,
                 int(pipeline_result.health_score), fw,
                 json.dumps(metrics, ensure_ascii=False)))
        n = conn.execute(
            "SELECT COUNT(*) FROM flights WHERE serial=?",
            (serial,)).fetchone()[0]
    finally:
        conn.close()
    return serial, int(n)


def _archived_sha1s(db_path: str) -> set[str]:
    if not os.path.isfile(db_path):
        return set()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        return {r[0] for r in conn.execute("SELECT log_sha1 FROM flights")}
    finally:
        conn.close()


def iter_log_files(sources: list[str]) -> list[str]:
    """Every .BIN/.bin under the given files or directories, sorted."""
    files: list[str] = []
    for src in sources:
        if os.path.isfile(src):
            files.append(src)
            continue
        for root, _dirs, names in os.walk(src):
            files.extend(os.path.join(root, n) for n in names
                         if n.upper().endswith(".BIN"))
    return sorted(files)


def backfill(db_path: str, sources: list[str],
             analyze=None, progress=None) -> dict:
    """Analyze and archive every log under `sources` (files or dirs).

    Logs whose sha1 is already archived are skipped WITHOUT re-analysis,
    so re-runs are cheap and safe. Failures (unreadable/non-log .bin)
    are isolated per file. `analyze(path)` defaults to
    lib.pipeline.run_analysis; `progress(i, n, path, event, detail)` is
    called per file with event in {"record", "skip", "fail"} and detail
    = serial / "" / error text.

    Returns {"total", "recorded", "skipped", "failed"} counts.
    """
    if analyze is None:
        from lib.pipeline import run_analysis as analyze  # heavy; lazy
    files = iter_log_files(sources)
    seen = _archived_sha1s(db_path)
    stats = {"total": len(files), "recorded": 0, "skipped": 0, "failed": 0}
    for i, path in enumerate(files, 1):
        try:
            sha1 = _file_sha1(path)
            if sha1 in seen:
                stats["skipped"] += 1
                if progress:
                    progress(i, len(files), path, "skip", "")
                continue
            result = analyze(path)
            serial, _n = record(db_path, result, path)
            seen.add(sha1)
            del result
            stats["recorded"] += 1
            if progress:
                progress(i, len(files), path, "record", serial)
        except Exception as exc:  # noqa: BLE001 — isolate per-file failures
            stats["failed"] += 1
            if progress:
                progress(i, len(files), path, "fail",
                         f"{type(exc).__name__}: {exc}")
        gc.collect()  # large-log arrays; keep long runs memory-bounded
    return stats


# Newest first, over a table alias. A flight with no GPS fix has an empty
# date, which alone would sort it as the oldest there is, so the newest
# indoor flight fell off its own history card. With no date to go on, it
# stays where it was archived: right after the flights stored before it.
_NEWEST_FIRST = """
    COALESCE(NULLIF({f}.flight_date, ''),
             (SELECT MAX(earlier.flight_date) FROM flights earlier
               WHERE earlier.serial={f}.serial AND earlier.id<{f}.id
                 AND earlier.flight_date != ''),
             '') DESC, {f}.id DESC"""


def query_history(db_path: str, serial: str,
                  limit: int = 20) -> list[dict]:
    """Most recent `limit` flights for a serial, oldest first."""
    if not os.path.isfile(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        rows = conn.execute(
            "SELECT f.log_name, f.flight_date, f.duration_s, f.health_score,"
            "       f.fw_version, f.metrics_json"
            " FROM flights f WHERE f.serial=?"
            f" ORDER BY {_NEWEST_FIRST.format(f='f')} LIMIT ?",
            (serial, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for name, date, dur, score, fw, mj in reversed(rows):
        try:
            metrics = json.loads(mj)
        except (TypeError, ValueError):
            metrics = {}
        out.append({"log_name": name, "flight_date": date,
                    "duration_s": dur, "health_score": score,
                    "fw_version": fw, "metrics": metrics})
    return out


def list_airframes(db_path: str) -> list[dict]:
    if not os.path.isfile(db_path):
        return []
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        rows = conn.execute(
            "SELECT a.serial, a.alias, a.board, COUNT(f.id), MAX(f.flight_date),"
            "       (SELECT f2.health_score FROM flights f2"
            "         WHERE f2.serial=a.serial"
            f"        ORDER BY {_NEWEST_FIRST.format(f='f2')} LIMIT 1)"
            " FROM airframes a LEFT JOIN flights f ON f.serial=a.serial"
            " GROUP BY a.serial ORDER BY MAX(f.flight_date) DESC").fetchall()
    finally:
        conn.close()
    return [{"serial": s, "alias": al or "", "board": b or "",
             "n_flights": n, "last_date": d or "",
             "last_score": sc if sc is not None else -1}
            for s, al, b, n, d, sc in rows]


def resolve_serial(db_path: str, key: str) -> str | None:
    """Map a serial / alias / unique serial prefix to the full serial.

    A real serial wins over any alias, and two airframes sharing an alias
    resolve to neither. set_alias() refuses both, but older archives may
    hold them, and a guess here shows one airframe's history as another's.
    """
    frames = list_airframes(db_path)
    if any(f["serial"] == key for f in frames):
        return key
    named = [f["serial"] for f in frames if f["alias"] and f["alias"] == key]
    if named:
        return named[0] if len(named) == 1 else None
    hits = [f["serial"] for f in frames if f["serial"].startswith(key)]
    return hits[0] if len(hits) == 1 else None


def set_alias(db_path: str, serial: str, alias: str) -> bool:
    """Name an airframe, or clear its name. False if the serial is unknown.

    Raises AliasTaken if the alias already means a different airframe.
    """
    conn = _connect(db_path)
    try:
        taken = alias and conn.execute(
            "SELECT serial FROM airframes"
            " WHERE serial != ? AND (serial = ? OR alias = ?)",
            (serial, alias, alias)).fetchone()
        if taken:
            raise AliasTaken(t("fleet.alias_taken", alias=alias,
                               serial=taken[0]))
        with conn:
            cur = conn.execute(
                "UPDATE airframes SET alias=? WHERE serial=?",
                (alias, serial))
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Presentation (an info AnalysisResult appended to the report)
# ---------------------------------------------------------------------

def _trend_figure(rows: list[dict]) -> go.Figure:
    # One slot per flight, labelled with its date. With the dates themselves
    # on a category axis, flights logged in the same minute shared a tick
    # and their points piled up on one spot.
    slots = list(range(len(rows)))
    labels = [r["flight_date"] or r["log_name"] for r in rows]
    names = [r["log_name"] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=slots, y=[r["health_score"] for r in rows], text=names,
        hoverinfo="text+y+name",
        mode="lines+markers", name=t("fleet.trace_score"),
        line=dict(color="#1f77b4", width=2)))
    vibe = [max(filter(None, (r["metrics"].get("vibe_max_x"),
                              r["metrics"].get("vibe_max_y"),
                              r["metrics"].get("vibe_max_z"))), default=None)
            for r in rows]
    if any(v is not None for v in vibe):
        fig.add_trace(go.Scatter(
            x=slots, y=vibe, text=names, hoverinfo="text+y+name",
            mode="lines+markers",
            name=t("fleet.trace_vibe"), yaxis="y2",
            line=dict(color="#ff7f0e", width=1.5)))
    fig.update_layout(
        title=dict(text=t("fleet.fig_trend"), font=dict(size=14)),
        height=350,
        margin=dict(l=60, r=60, t=50, b=70),
        template="plotly_white",
        xaxis=dict(tickmode="array", tickvals=slots, ticktext=labels),
        yaxis=dict(title=t("fleet.axis_score"), range=[0, 105]),
        yaxis2=dict(title=t("fleet.axis_vibe"), overlaying="y",
                    side="right"),
        font=dict(family="PingFang SC, Microsoft YaHei, "
                         "Noto Sans SC, sans-serif", size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
    )
    return fig


def build_history_result(rows: list[dict],
                         serial: str) -> AnalysisResult | None:
    """Info-level history card for the report tail; None if <2 flights."""
    if len(rows) < 2:
        return None
    details = []
    for r in rows:
        mets = r["metrics"]

        def _fmt(key, suffix=""):
            v = mets.get(key)
            return f"{v}{suffix}" if v is not None else "-"

        details.append(t(
            "fleet.flight_line",
            date=r["flight_date"] or "-",
            name=r["log_name"],
            score=r["health_score"],
            dur=f"{(r['duration_s'] or 0) / 60:.0f}",
            vibe=_fmt("vibe_max_x"),
            vmin=_fmt("batt_v_min", "V"),
            sag=_fmt("vcc_sag", "V"),
        ))
    return AnalysisResult(
        title=t("fleet.title", serial=serial, n=len(rows)),
        status="info",
        summary=t("fleet.summary", n=len(rows),
                  first=rows[0]["flight_date"] or "-",
                  last=rows[-1]["flight_date"] or "-"),
        details=details,
        figures=[_trend_figure(rows)],
        action_level="ok",
    )
