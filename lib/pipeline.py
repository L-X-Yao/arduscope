"""
Unified analysis pipeline.

Single implementation of: log read → vehicle detect → threshold build →
analyzer loop → health score → conflict detection → GPS track extraction.

All entry points (flight_report, web_app, batch_report) call run_analysis()
instead of duplicating the pipeline.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .i18n import t
from .log_reader import LogReader, UnreadableLog
from .param_conflict import detect_param_conflicts
from .results import ACTION_OK, ACTION_PHYSICAL_CHECK, AnalysisResult
from .thresholds import Thresholds
from .vehicle_detect import VehicleInfo, detect_vehicle


# Every report colours the score the same way: 80 and up green, 50 and up
# amber, below that red. Averaged over about 30 analyzers one critical
# costs only 3 points, so a critical finding must pull the score into the
# red band itself or the badge contradicts the report.
CRITICAL_SCORE_CAP = 49

NO_SCORE = -1


class NotAFlightLog(ValueError):
    """The file holds no flight data to analyze, or cannot be read."""


def read_flight_log(filepath: str,
                    preload_types: list[str] | None = None) -> LogReader:
    """The log, or NotAFlightLog saying in the user's language why not."""
    name = os.path.basename(filepath)
    try:
        reader = LogReader(filepath, preload_types=preload_types)
    except UnreadableLog as e:
        raise NotAFlightLog(_unreadable_reason(name, e)) from e
    if not reader.has_flight_data():
        raise NotAFlightLog(t("pipeline.not_a_flight_log", name=name))
    return reader


def _unreadable_reason(name: str, error: UnreadableLog) -> str:
    """The reader's refusal, in the user's language."""
    if error.reason:
        return t("pipeline.damaged_format", name=name, reason=error.reason)
    if error.first_offset == 0:
        # It does not even open as a log does.
        return t("pipeline.not_a_flight_log", name=name)
    return t("pipeline.damaged_not_log", name=name,
             mb=f"{error.not_log_bytes / 1e6:.1f}",
             offset=error.first_offset)


def _collect_preload_types() -> list[str]:
    """Auto-collect all message types needed by registered analyzers."""
    from analyzers import discover_analyzers
    types: set[str] = set()
    for _key, cls in discover_analyzers():
        types.update(cls.required_messages)
        types.update(getattr(cls, "optional_messages", []))
    types.update(["ATT", "RATE", "RCOU", "BAT", "GPS", "POS"])
    return sorted(types)


PRELOAD_MESSAGE_TYPES: list[str] | None = None


def _get_preload_types() -> list[str]:
    global PRELOAD_MESSAGE_TYPES
    if PRELOAD_MESSAGE_TYPES is None:
        PRELOAD_MESSAGE_TYPES = _collect_preload_types()
    return PRELOAD_MESSAGE_TYPES


def compute_health_score(results: list[AnalysisResult]) -> int:
    """Mean of analyzer scores, capped once anything is critical.

    Failed analyzers are left out: they say nothing about the flight.
    NO_SCORE means nothing was analyzed, which is not a score of zero.
    """
    scored = [r for r in results if not r.failed]
    if not scored:
        return NO_SCORE
    status_scores = {"good": 100, "info": 80, "warning": 40, "critical": 0}
    total = sum(status_scores.get(r.status, 50) for r in scored)
    score = round(total / len(scored))
    if any(r.status == "critical" for r in scored):
        return min(score, CRITICAL_SCORE_CAP)
    return score


def extract_gps_track(reader: LogReader) -> dict | None:
    if not reader.has_message("GPS"):
        return None
    gps = reader.get_messages_as_arrays("GPS", ["I", "Lat", "Lng", "Alt"])
    ts = gps["_timestamp"]
    if len(ts) < 10:
        return None
    inst = gps["I"]
    mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
    if not np.any(mask):
        mask = np.ones(len(ts), dtype=bool)
    ts_sel = ts[mask]
    lat_sel = gps["Lat"][mask]
    lng_sel = gps["Lng"][mask]
    alt_sel = gps["Alt"][mask]
    valid = (
        ~np.isnan(lat_sel) & ~np.isnan(lng_sel)
        & (lat_sel != 0) & (lng_sel != 0)
    )
    if np.sum(valid) < 10:
        return None
    ts_v, lat_v = ts_sel[valid], lat_sel[valid]
    lng_v, alt_v = lng_sel[valid], alt_sel[valid]
    if len(ts_v) > 2000:
        step = len(ts_v) // 2000
        ts_v = ts_v[::step]
        lat_v = lat_v[::step]
        lng_v = lng_v[::step]
        alt_v = alt_v[::step]
    return {
        "lat": lat_v.tolist(),
        "lng": lng_v.tolist(),
        "alt": alt_v.tolist(),
        "time_s": ts_v.tolist(),
    }


def _template_notice(template, defaults: dict, thresholds: Thresholds,
                     ) -> AnalysisResult:
    """Card that names the template and every threshold it changed.

    A finding's citation quotes the default limit, so a warning raised by
    a tightened threshold contradicts its own evidence unless the report
    declares the template. It sits outside the score and the action list.
    """
    details = [t("template.notice_change", name=key, default=default,
                 value=getattr(thresholds, key))
               for key, default in defaults.items()]
    if template.analyzers:
        details.append(t("template.notice_analyzers",
                         keys=", ".join(template.analyzers)))
    return AnalysisResult(
        title=t("template.notice_title", name=template.display_name),
        status="info",
        summary=(t("template.notice_summary", n=len(defaults)) if defaults
                 else t("template.notice_summary_plain")),
        details=details,
        action_level=ACTION_OK,
    )


@dataclass
class PipelineResult:
    reader: LogReader
    vehicle_info: VehicleInfo
    thresholds: Thresholds
    results: list[AnalysisResult]
    results_by_key: dict[str, AnalysisResult]
    health_score: int
    conflicts: list[dict]
    gps_track: Optional[dict]


def run_analysis(
    filepath: str,
    *,
    template=None,
    only: set[str] | None = None,
    skip: set[str] | None = None,
    thresholds_path: str | None = None,
    include_gps: bool = True,
    on_progress=None,
) -> PipelineResult:
    """Run the full analysis pipeline on a single .BIN file.

    Args:
        filepath: path to .BIN log file
        template: optional Template object (from lib/templates.py)
        only: if set, run only these analyzer keys
        skip: if set, skip these analyzer keys
        thresholds_path: optional JSON path for threshold overrides
        include_gps: extract GPS track for map
        on_progress: optional callback(pct: int, msg: str)
    """
    from analyzers import discover_analyzers

    def _progress(pct: int, msg: str = ""):
        if on_progress:
            on_progress(pct, msg)

    _progress(5, t("cli.stage_read"))
    reader = read_flight_log(filepath, _get_preload_types())

    _progress(15, t("cli.stage_detect"))
    vehicle_info = detect_vehicle(reader)
    thresholds = Thresholds.from_params(
        reader.params, reader.canonical_vehicle_type)

    template_defaults: dict = {}
    if template and template.thresholds:
        for k, v in template.thresholds.items():
            if hasattr(thresholds, k):
                template_defaults[k] = getattr(thresholds, k)
                setattr(thresholds, k, type(getattr(thresholds, k))(v))

    if thresholds_path:
        thresholds.load_overrides(thresholds_path)

    # Build analyzer list
    registry = discover_analyzers()
    template_only = set(template.analyzers) if template and template.analyzers else set()
    analyzers_to_run = []
    for key, cls in registry:
        if template_only and key not in template_only:
            continue
        if only and key not in only:
            continue
        if skip and key in skip:
            continue
        analyzers_to_run.append((key, cls))

    # Run analyzers
    results: list[AnalysisResult] = []
    results_by_key: dict[str, AnalysisResult] = {}
    total = len(analyzers_to_run)

    for i, (key, cls) in enumerate(analyzers_to_run):
        pct = 15 + int(60 * (i + 1) / total)
        _progress(pct, t("web.analyzing", key=key))

        analyzer = cls(reader, thresholds, vehicle_info)
        if analyzer.can_run():
            try:
                result = analyzer.analyze()
            except Exception as e:
                print(f"[warn] analyzer {key} failed: {e}", file=sys.stderr)
                result = AnalysisResult(
                    title=t("pipeline.analyzer_error_title", key=key),
                    status="info",
                    summary=t("pipeline.analyzer_error_summary", error=e),
                    action_level=ACTION_PHYSICAL_CHECK,
                    action_text=t("pipeline.analyzer_error_action"),
                    failed=True,
                )
            result.registry_key = key
            # POSITIONING contract: a top-level warning/critical must cite
            # its source. Sub-results are exempt, parents carry it. A miss
            # is a bug in the analyzer, but its finding is still real, so
            # keep it and say the source is missing. tools/baseline_run.py
            # records these as errors, which is where the bug gets caught.
            if (result.status in ("warning", "critical")
                    and not result.evidence_source):
                print(f"[warn] analyzer {key}: {result.status} result "
                      f"without evidence_source", file=sys.stderr)
                result.evidence_source = t("pipeline.evidence_missing")
            results.append(result)
            results_by_key[key] = result

    health_score = compute_health_score(results)
    conflicts = detect_param_conflicts(results)
    if template:
        results.insert(0, _template_notice(template, template_defaults,
                                           thresholds))

    gps_track = None
    if include_gps:
        try:
            gps_track = extract_gps_track(reader)
        except Exception as e:
            print(f"[warn] GPS track extraction failed: {e}", file=sys.stderr)

    _progress(95, t("cli.stage_done"))

    return PipelineResult(
        reader=reader,
        vehicle_info=vehicle_info,
        thresholds=thresholds,
        results=results,
        results_by_key=results_by_key,
        health_score=health_score,
        conflicts=conflicts,
        gps_track=gps_track,
    )
