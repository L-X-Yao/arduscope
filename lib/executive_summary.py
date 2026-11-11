"""
Executive summary builder for the top layer of reports.

Aggregates the per-analyzer AnalysisResult list into a single
ExecutiveSummary object containing:
  - overall action level (worst across all analyzers)
  - one-line headline (locale-aware)
  - prioritized list of actions (filtered to non-OK levels)
  - basic flight info
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .results import (
    AnalysisResult,
    ACTION_OK,
    ACTION_APPLY_PARAM,
    ACTION_PHYSICAL_CHECK,
    ACTION_RUN_AUTOTUNE,
    ACTION_SAFETY_STOP,
    worst_action_level,
)
from lib.i18n import t
from lib.vehicle_detect import VehicleInfo


_LEVEL_DISPLAY = {
    ACTION_OK: {
        "icon": "✅",
        "i18n_key": "action.ok",
        "css_class": "level-ok",
        "severity": 0,
    },
    ACTION_APPLY_PARAM: {
        "icon": "⚙️",
        "i18n_key": "action.apply_param",
        "css_class": "level-apply-param",
        "severity": 1,
    },
    ACTION_PHYSICAL_CHECK: {
        "icon": "🔧",
        "i18n_key": "action.physical_check",
        "css_class": "level-physical-check",
        "severity": 1,
    },
    ACTION_RUN_AUTOTUNE: {
        "icon": "🎯",
        "i18n_key": "action.run_autotune",
        "css_class": "level-run-autotune",
        "severity": 1,
    },
    ACTION_SAFETY_STOP: {
        "icon": "🚨",
        "i18n_key": "action.safety_stop",
        "css_class": "level-safety-stop",
        "severity": 3,
    },
}


def get_level_display(level: str) -> dict:
    """Return display metadata for an action level (icon, label, css)."""
    raw = _LEVEL_DISPLAY.get(level, _LEVEL_DISPLAY[ACTION_OK])
    return {**raw, "label": t(raw["i18n_key"])}


@dataclass
class ActionItem:
    """
    One action recommendation surfaced in the executive summary.

    One ActionItem corresponds to one AnalysisResult that produced a
    non-OK action_level. Multiple ActionItems are sorted from most
    severe to least severe in the executive summary.
    """
    level: str                  # action_level (e.g. "stop_flying")
    icon: str                   # ✅/📋/☎️/🚨
    title: str                  # Short title — analyzer's title
    plain_summary: str          # Plain-language one-liner
    action_text: str            # Concrete action recommendation
    source_key: str             # registry_key of the source analyzer
    source_status: str          # technical status (good/warning/critical/info)


@dataclass
class ExecutiveSummary:
    """Top-level summary used by html_report.py for the executive summary card."""
    overall_action_level: str   # Worst action level across all results
    overall_status: str         # Worst technical status across all results
    headline: str               # 1-line conclusion in plain Chinese
    actions: list[ActionItem] = field(default_factory=list)
    # Counts by level for the dashboard pills
    counts: dict[str, int] = field(default_factory=dict)
    # Optional flight info reused from existing pipeline
    health_score: int = -1
    flight_duration: float = 0.0
    vehicle_label: str = ""


def _build_headline(level: str, n_actions: int, has_data: bool,
                    n_critical: int = 0, n_failed: int = 0) -> str:
    """Generate the plain-language headline."""
    if not has_data:
        return t("headline.no_data")
    if level == ACTION_SAFETY_STOP:
        return t("headline.safety_stop")
    if n_critical:
        return t("headline.n_critical", n=n_critical, m=n_actions)
    if n_failed:
        return t("headline.incomplete", n=n_failed)
    if level in (ACTION_APPLY_PARAM, ACTION_PHYSICAL_CHECK,
                 ACTION_RUN_AUTOTUNE):
        if n_actions == 1:
            return t("headline.one_action")
        return t("headline.n_actions", n=n_actions)
    return t("headline.all_good")


def _vehicle_label(vehicle_info: Optional[VehicleInfo]) -> str:
    """Build a short vehicle label like 'ArduCopter / CubeOrange (4 motors)'."""
    if vehicle_info is None:
        return ""
    parts = [vehicle_info.vehicle_type]
    if vehicle_info.board_name:
        parts.append(vehicle_info.board_name)
    label = " / ".join(parts)
    if vehicle_info.motor_count > 0:
        label += " " + t("vehicle.motor_count", count=vehicle_info.motor_count)
    return label


def _flatten_results(
    results: list[AnalysisResult],
) -> list[tuple[AnalysisResult, str]]:
    """
    Flatten any sub_results so the executive summary doesn't miss
    nested issues (e.g., pid_tuning publishes per-axis sub_results).

    Returns list of (result, source_key) tuples.
    """
    flat: list[tuple[AnalysisResult, str]] = []
    for r in results:
        key = r.registry_key or r.title
        flat.append((r, key))
        for sub in r.sub_results:
            flat.append((sub, f"{key}/{sub.title}"))
    return flat


def build_executive_summary(
    results: list[AnalysisResult],
    vehicle_info: Optional[VehicleInfo] = None,
    health_score: int = -1,
    flight_duration: float = 0.0,
) -> ExecutiveSummary:
    """
    Aggregate per-analyzer AnalysisResult list into an ExecutiveSummary.

    The summary is intentionally conservative:
      - Never invents new conclusions; only surfaces what analyzers already
        decided.
      - The headline degrades gracefully when there are no results
        ("暂无可分析的飞行数据") instead of claiming "all good".
      - Sub-results (e.g., pid_tuning per-axis) are flattened so a
        critical sub-result still surfaces.
    """
    if not results:
        return ExecutiveSummary(
            overall_action_level=ACTION_OK,
            overall_status="info",
            headline=_build_headline(ACTION_OK, 0, has_data=False),
            actions=[],
            counts={
                ACTION_OK: 0,
                ACTION_APPLY_PARAM: 0,
                ACTION_PHYSICAL_CHECK: 0,
                ACTION_RUN_AUTOTUNE: 0,
                ACTION_SAFETY_STOP: 0,
            },
            health_score=health_score,
            flight_duration=flight_duration,
            vehicle_label=_vehicle_label(vehicle_info),
        )

    flat = _flatten_results(results)

    # Aggregate
    overall_action = worst_action_level(*(r.action_level for r, _ in flat))
    overall_status = "info"
    severity = {"info": 0, "good": 1, "warning": 2, "critical": 3}
    for r, _ in flat:
        if severity.get(r.status, 0) > severity.get(overall_status, 0):
            overall_status = r.status

    # Build action items list (skip OK)
    counts: dict[str, int] = {
        ACTION_OK: 0,
        ACTION_APPLY_PARAM: 0,
        ACTION_PHYSICAL_CHECK: 0,
        ACTION_RUN_AUTOTUNE: 0,
        ACTION_SAFETY_STOP: 0,
    }
    actions: list[ActionItem] = []
    for result, source_key in flat:
        counts[result.action_level] = counts.get(result.action_level, 0) + 1
        if result.action_level == ACTION_OK:
            continue
        plain = result.plain_summary or result.summary
        action_text = result.action_text or t("headline.default_action")
        display = get_level_display(result.action_level)
        actions.append(ActionItem(
            level=result.action_level,
            icon=display["icon"],
            title=result.title,
            plain_summary=plain,
            action_text=action_text,
            source_key=source_key,
            source_status=result.status,
        ))

    # Sort by severity (most severe first), then critical findings ahead
    # of the rest of their level. Original order breaks ties, so the
    # output stays deterministic.
    actions.sort(
        key=lambda a: (-get_level_display(a.level)["severity"],
                       -severity.get(a.source_status, 0)),
    )

    headline = _build_headline(
        overall_action, len(actions), has_data=True,
        n_critical=sum(1 for r in results if r.status == "critical"),
        n_failed=sum(1 for r in results if r.failed),
    )

    return ExecutiveSummary(
        overall_action_level=overall_action,
        overall_status=overall_status,
        headline=headline,
        actions=actions,
        counts=counts,
        health_score=health_score,
        flight_duration=flight_duration,
        vehicle_label=_vehicle_label(vehicle_info),
    )
