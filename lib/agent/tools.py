"""Read-only tools the online agent may call.

Every tool returns JSON with a hard size limit. The agent sees parsed
data only: it never opens the .BIN itself and cannot ask for a field the
egress policy strips.
"""

from __future__ import annotations

import html as _html
import inspect
import json
import re

import numpy as np

from lib.fft_helpers import do_fft, find_peaks
from lib.i18n import t
from lib.thresholds import Thresholds

from .config import AgentConfig
from .egress import is_gps_field, redact_text

MAX_RESULT_BYTES = 8 * 1024
MAX_TOTAL_BYTES = 120 * 1024
MAX_POINTS = 500
MAX_ROWS = 200
MAX_FIELDS = 20


def _text_snippets(reader, limit: int = 200, min_len: int = 6) -> list[str]:
    """STATUSTEXT strings, normalized as the detail lines carry them.

    One analyzer (``summary``) quotes the first few STATUSTEXT messages in
    its details. Those lines are free text, so they follow the same opt-in
    as ``get_events(kind="text")``: see ``_tool_get_result``.
    """
    snippets = []
    for message in (getattr(reader, "text_messages", None) or [])[:limit]:
        raw = _html.unescape(str(getattr(message, "text", "") or ""))
        text = re.sub(r"\s+", " ", raw).strip()
        if len(text) >= min_len:
            snippets.append(text)
    return snippets


def _num(value, digits: int = 6):
    """A JSON-safe number: NaN and infinity become null."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    rounded = round(number, digits)
    return int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded


def _json_size(payload) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _shrink(payload) -> bool:
    """Halve one list in place. True when something was shortened."""
    if isinstance(payload, list):
        if len(payload) > 1:
            del payload[len(payload) // 2:]
            return True
        return False
    if isinstance(payload, dict):
        for key in ("points", "rows", "items", "peaks", "values",
                    "available", "available_fields"):
            if isinstance(payload.get(key), list) and len(payload[key]) > 1:
                del payload[key][len(payload[key]) // 2:]
                return True
        for key in ("params",):
            value = payload.get(key)
            if isinstance(value, dict) and len(value) > 1:
                payload[key] = dict(list(value.items())[:len(value) // 2])
                return True
        for value in payload.values():
            if _shrink(value):
                return True
    return False


TOOL_SPECS = [
    {
        "name": "get_vehicle",
        "description": "Vehicle type, board name, firmware, motor and IMU "
                       "counts, flight duration. No serial number.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_results",
        "description": "All offline analyzer results: key, title, status, "
                       "summary and action level. Start here.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_result",
        "description": "One analyzer result in full: details, evidence, "
                       "parameter suggestions and the evidence source.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string",
                        "description": "registry_key from list_results"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "get_messages",
        "description": "Downsampled time series from one log message type. "
                       "Call without fields first to list the field names.",
        "parameters": {
            "type": "object",
            "properties": {
                "msg_type": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "t0": {"type": "number", "description": "start seconds"},
                "t1": {"type": "number", "description": "end seconds"},
                "max_points": {"type": "integer", "default": 200},
            },
            "required": ["msg_type"],
        },
    },
    {
        "name": "get_params",
        "description": "Parameter values by exact names or by name prefix.",
        "parameters": {
            "type": "object",
            "properties": {
                "names": {"type": "array", "items": {"type": "string"}},
                "prefix": {"type": "string"},
            },
        },
    },
    {
        "name": "get_events",
        "description": "Mode changes, ERR events, EV events or STATUSTEXT "
                       "messages in a time range.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["modes", "errors", "events", "text"]},
                "t0": {"type": "number"},
                "t1": {"type": "number"},
            },
            "required": ["kind"],
        },
    },
    {
        "name": "get_spectrum",
        "description": "FFT peaks for one numeric message field.",
        "parameters": {
            "type": "object",
            "properties": {
                "msg_type": {"type": "string"},
                "field": {"type": "string"},
                "t0": {"type": "number"},
                "t1": {"type": "number"},
                "top_n": {"type": "integer", "default": 5},
            },
            "required": ["msg_type", "field"],
        },
    },
    {
        "name": "get_threshold",
        "description": "A threshold value and the source note beside it.",
        "parameters": {
            "type": "object",
            "properties": {"field": {"type": "string"}},
            "required": ["field"],
        },
    },
]


class ToolBox:
    """Dispatches tool calls and enforces the size budgets."""

    def __init__(self, pipeline, config: AgentConfig):
        self.pipeline = pipeline
        self.config = config
        self.total_bytes = 0
        self.exhausted = False

    # -- dispatch ------------------------------------------------------

    def call(self, name: str, arguments: dict) -> dict:
        if self.exhausted:
            return {"error": t("agent.error_tool_budget")}
        handler = getattr(self, "_tool_" + name, None)
        if handler is None:
            return {"error": t("agent.error_unknown_tool", name=name)}
        try:
            payload = handler(**(arguments or {}))
        except TypeError as e:
            return {"error": t("agent.error_bad_arguments", error=e)}
        except Exception as e:  # noqa: BLE001 - a tool must not stop the loop
            return {"error": f"{type(e).__name__}: {e}"}

        while _json_size(payload) > MAX_RESULT_BYTES and _shrink(payload):
            pass
        size = _json_size(payload)
        if size > MAX_RESULT_BYTES:
            payload = {"error": t("agent.error_result_too_large")}
            size = _json_size(payload)
        self.total_bytes += size
        if self.total_bytes > MAX_TOTAL_BYTES:
            self.exhausted = True
            return {"error": t("agent.error_tool_budget")}
        return payload

    # -- tools ---------------------------------------------------------

    def _tool_get_vehicle(self) -> dict:
        info = self.pipeline.vehicle_info
        return {
            "vehicle_type": info.vehicle_type,
            "board_name": info.board_name,
            "firmware_version": info.firmware_version,
            "frame_class": info.frame_class,
            "motor_count": info.motor_count,
            "imu_count": info.imu_count,
            "is_quadplane": info.is_quadplane,
            "flight_duration_s": _num(
                self.pipeline.reader.get_flight_duration(), 1),
        }

    def _tool_list_results(self) -> dict:
        items = []
        for key, result in self.pipeline.results_by_key.items():
            items.append({
                "key": key,
                "title": result.title,
                "status": result.status,
                "summary": result.summary,
                "action_level": result.action_level,
                "failed": bool(result.failed),
            })
        return {"count": len(items), "results": items}

    def _tool_get_result(self, key: str) -> dict:
        result = self.pipeline.results_by_key.get(key)
        if result is None:
            return {"error": t("agent.error_unknown_result", key=key),
                    "keys": list(self.pipeline.results_by_key)}
        details = []
        for block in result.details:
            text = _html.unescape(re.sub(r"<[^>]+>", " ", str(block)))
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                details.append(redact_text(
                    text, allow_serial=self.config.allow_serial,
                    allow_text=True))
        redacted = 0
        if not self.config.allow_text:
            snippets = _text_snippets(self.pipeline.reader)
            if snippets:
                kept = [line for line in details
                        if not any(s in line for s in snippets)]
                redacted = len(details) - len(kept)
                details = kept
        payload = {
            "key": key,
            "title": result.title,
            "status": result.status,
            "summary": result.summary,
            "details": details,
            "evidence": result.evidence,
            "param_suggestions": result.param_suggestions,
            "evidence_source": result.evidence_source,
            "confidence": result.confidence,
            "action_level": result.action_level,
            "failed": bool(result.failed),
        }
        if redacted:
            payload["redacted_text_lines"] = redacted
            payload["egress_note"] = t("agent.note_text_redacted")
        return payload

    def _tool_get_messages(self, msg_type: str, fields: list | None = None,
                           t0: float | None = None,
                           t1: float | None = None,
                           max_points: int = 200) -> dict:
        reader = self.pipeline.reader
        if not reader.has_message(msg_type):
            available = sorted(getattr(reader, "available_messages", set()))
            return {"error": t("agent.error_unknown_message", name=msg_type),
                    "available": available[:60]}

        if not fields:
            if not hasattr(reader, "preload_all"):
                return {"error": t("agent.note_choose_fields")}
            if msg_type not in getattr(reader, "_fully_cached", set()):
                reader.preload_all([msg_type])
            cached = getattr(reader, "_msg_cache", {}).get(msg_type, {})
            names = sorted(k for k in cached if k != "_timestamp")
            return {"msg_type": msg_type,
                    "available_fields": names[:MAX_FIELDS],
                    "note": t("agent.note_choose_fields")}

        if isinstance(fields, str):
            fields = [fields]
        fields = [str(f) for f in fields][:MAX_FIELDS]
        blocked = [f for f in fields if is_gps_field(f)
                   and not self.config.allow_gps]
        wanted = [f for f in fields if f not in blocked]
        if not wanted:
            return {"error": t("agent.error_egress_gps"),
                    "redacted_fields": blocked}

        data = reader.get_messages_as_arrays(msg_type, wanted)
        ts = np.asarray(data.get("_timestamp", []), dtype=float)
        keep = np.ones(len(ts), dtype=bool)
        if t0 is not None:
            keep &= ts >= float(t0)
        if t1 is not None:
            keep &= ts <= float(t1)
        idx = np.flatnonzero(keep)
        total = int(len(idx))
        cap = max(2, min(int(max_points or 200), MAX_POINTS))
        if total > cap:
            idx = idx[np.linspace(0, total - 1, cap).astype(int)]

        points = []
        for i in idx:
            row = {"t": _num(ts[i], 3)}
            for field in wanted:
                row[field] = _num(np.asarray(data[field])[i])
            points.append(row)
        payload = {
            "msg_type": msg_type,
            "n_total": total,
            "points": points,
        }
        if blocked:
            payload["redacted_fields"] = blocked
            payload["egress_note"] = t("agent.note_gps_redacted")
        return payload

    def _tool_get_params(self, names: list | None = None,
                         prefix: str = "") -> dict:
        params = getattr(self.pipeline.reader, "params", {}) or {}
        if isinstance(names, str):
            names = [names]
        if names:
            selected = {str(n): params[str(n)] for n in names
                        if str(n) in params}
            missing = [str(n) for n in names if str(n) not in params]
            payload = {"params": selected, "missing": missing}
        else:
            prefix = str(prefix or "")
            selected = {k: v for k, v in params.items()
                        if k.startswith(prefix)}
            payload = {"prefix": prefix, "matched": len(selected),
                       "params": dict(list(selected.items())[:MAX_ROWS])}
            if len(selected) > MAX_ROWS:
                payload["truncated"] = True
        return payload

    def _tool_get_events(self, kind: str, t0: float | None = None,
                         t1: float | None = None) -> dict:
        kind = str(kind or "").lower()
        reader = self.pipeline.reader
        rows = []
        source = {
            "modes": getattr(reader, "mode_list", []),
            "errors": getattr(reader, "errors", []),
            "events": getattr(reader, "events", []),
            "text": getattr(reader, "text_messages", []),
        }.get(kind)
        if source is None:
            return {"error": t("agent.error_unknown_event_kind", kind=kind)}

        matched = 0
        for item in source:
            stamp = float(getattr(item, "timestamp", 0.0))
            if t0 is not None and stamp < float(t0):
                continue
            if t1 is not None and stamp > float(t1):
                continue
            matched += 1
            if kind == "modes":
                rows.append({"t": _num(stamp, 3),
                             "mode": getattr(item, "mode_name", ""),
                             "number": getattr(item, "mode_number", None)})
            elif kind == "errors":
                rows.append({"t": _num(stamp, 3),
                             "subsys": getattr(item, "subsys", None),
                             "ecode": getattr(item, "ecode", None)})
            elif kind == "events":
                rows.append({"t": _num(stamp, 3),
                             "event_id": getattr(item, "event_id", None)})
            else:
                if not self.config.allow_text:
                    continue
                rows.append({
                    "t": _num(stamp, 3),
                    "text": redact_text(item.text,
                                        allow_serial=self.config.allow_serial,
                                        allow_text=True),
                })
        payload = {"kind": kind, "count": matched,
                   "rows": rows[:MAX_ROWS]}
        if matched > MAX_ROWS:
            payload["truncated"] = True
        if kind == "text" and not self.config.allow_text:
            payload["sent"] = False
            payload["egress_note"] = t("agent.note_text_redacted")
        return payload

    def _tool_get_spectrum(self, msg_type: str, field: str,
                           t0: float | None = None,
                           t1: float | None = None,
                           top_n: int = 5) -> dict:
        if is_gps_field(field) and not self.config.allow_gps:
            return {"error": t("agent.error_egress_gps")}
        reader = self.pipeline.reader
        if not reader.has_message(msg_type):
            return {"error": t("agent.error_unknown_message", name=msg_type)}
        data = reader.get_messages_as_arrays(msg_type, [field])
        ts = np.asarray(data.get("_timestamp", []), dtype=float)
        values = np.asarray(data.get(field, []), dtype=float)
        keep = np.isfinite(values) & np.isfinite(ts)
        if t0 is not None:
            keep &= ts >= float(t0)
        if t1 is not None:
            keep &= ts <= float(t1)
        ts, values = ts[keep], values[keep]
        if len(values) < 64:
            return {"error": t("agent.error_spectrum_short",
                               count=len(values))}
        # One contiguous window keeps the frequency bins meaningful. A
        # whole flight is not needed for a peak, and a huge FFT is slow.
        if len(values) > 20000:
            start = max(0, (len(values) - 20000) // 2)
            ts, values = ts[start:start + 20000], values[start:start + 20000]
        dt = float(np.median(np.diff(ts)))
        if not np.isfinite(dt) or dt <= 0:
            return {"error": t("agent.error_spectrum_timebase")}
        freqs, amps = do_fft(values, dt)
        peaks = find_peaks(freqs, amps, min_freq=0.5,
                           n_peaks=max(1, min(int(top_n or 5), 20)))
        return {
            "msg_type": msg_type,
            "field": field,
            "sample_rate_hz": _num(1.0 / dt, 2),
            "n_samples": int(len(values)),
            "peaks": [{"freq_hz": _num(f, 2), "amplitude": _num(a, 4)}
                      for f, a in peaks],
        }

    def _tool_get_threshold(self, field: str) -> dict:
        thresholds = self.pipeline.thresholds
        if not hasattr(thresholds, field):
            names = [f.name for f in Thresholds.__dataclass_fields__.values()]
            return {"error": t("agent.error_unknown_threshold", field=field),
                    "names": names[:60]}
        source = _threshold_source(field)
        return {"field": field,
                "value": _num(getattr(thresholds, field)),
                "source": source}


_THRESHOLD_SOURCE_RE = re.compile(
    r"^\s*(\w+):\s*[^=\n]+=\s*[^#\n]*#\s*(.+)$", re.MULTILINE)


def _threshold_source(field: str) -> str:
    """The inline source comment beside a Thresholds field."""
    try:
        source = inspect.getsource(Thresholds)
    except OSError:
        return ""
    for match in _THRESHOLD_SOURCE_RE.finditer(source):
        if match.group(1) == field:
            return match.group(2).strip()
    return ""
