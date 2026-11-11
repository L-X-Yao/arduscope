"""
AnalysisResult dataclass and action/confidence constants.

Separated from analyzers/__init__.py to break the circular dependency
between lib/ and analyzers/. Both layers import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import plotly.graph_objects as go

from .param_limits import limit_suggestions


ACTION_OK = "ok"
ACTION_APPLY_PARAM = "apply_param"
ACTION_PHYSICAL_CHECK = "physical_check"
ACTION_RUN_AUTOTUNE = "run_autotune"
ACTION_SAFETY_STOP = "safety_stop"

VALID_ACTION_LEVELS = (
    ACTION_OK,
    ACTION_APPLY_PARAM,
    ACTION_PHYSICAL_CHECK,
    ACTION_RUN_AUTOTUNE,
    ACTION_SAFETY_STOP,
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

VALID_CONFIDENCE_LEVELS = (
    CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW,
)


def derive_default_action_level(status: str) -> str:
    return {
        "good": ACTION_OK,
        "info": ACTION_OK,
        "warning": ACTION_PHYSICAL_CHECK,
        "critical": ACTION_PHYSICAL_CHECK,
    }.get(status, ACTION_OK)


_ACTION_SEVERITY = {
    ACTION_OK: 0,
    ACTION_APPLY_PARAM: 1,
    ACTION_PHYSICAL_CHECK: 1,
    ACTION_RUN_AUTOTUNE: 1,
    ACTION_SAFETY_STOP: 3,
}


def worst_action_level(*levels: str) -> str:
    worst = ACTION_OK
    for lvl in levels:
        if _ACTION_SEVERITY.get(lvl, 0) > _ACTION_SEVERITY.get(worst, 0):
            worst = lvl
    return worst


@dataclass
class AnalysisResult:
    title: str
    status: str = "info"
    summary: str = ""
    details: list[str] = field(default_factory=list)
    figures: list[go.Figure] = field(default_factory=list)
    sub_results: list["AnalysisResult"] = field(default_factory=list)
    confidence: str = CONFIDENCE_HIGH
    action_level: Optional[str] = None
    action_text: str = ""
    plain_summary: str = ""
    evidence: list[dict] = field(default_factory=list)
    evidence_source: str = ""
    param_suggestions: list[dict] = field(default_factory=list)
    registry_key: str = ""
    # The analyzer raised, so this card holds no conclusion at all. Kept
    # apart from status: a failure is neither "info" nor a finding, and
    # must not score, trigger a cross-rule, or read as all clear.
    failed: bool = False
    # Which engine wrote an AI diagnosis card: "rule", "anthropic", "openai"
    provider: str = ""

    def __post_init__(self):
        # Default action_level: when the caller didn't specify, derive from
        # whether there's an actionable parameter fix. Non-empty
        # param_suggestions → apply_param (so the invariant always holds by
        # default); otherwise fall back to the status-based default.
        if self.action_level is None:
            if self.param_suggestions:
                self.action_level = ACTION_APPLY_PARAM
            else:
                self.action_level = derive_default_action_level(self.status)
        # Invariant (CLAUDE.md): non-empty param_suggestions implies an
        # actionable parameter fix, so action_level must be apply_param.
        # safety_stop (severity 3) is allowed because grounding the vehicle
        # takes precedence over applying a tuning parameter mid-flight.
        if self.param_suggestions and self.action_level not in (
                ACTION_APPLY_PARAM, ACTION_SAFETY_STOP):
            raise AssertionError(
                f"AnalysisResult invariant violation in "
                f"{self.registry_key or self.title!r}: "
                f"{len(self.param_suggestions)} param_suggestion(s) "
                f"but action_level={self.action_level!r} "
                f"(must be {ACTION_APPLY_PARAM!r} or {ACTION_SAFETY_STOP!r})"
            )
        # Structural invariant: every suggestion must be exportable to
        # the unified .param table — a dict carrying all four fields.
        for s in self.param_suggestions:
            if (not isinstance(s, dict)
                    or not s.get("param")
                    or "current" not in s
                    or "suggested" not in s
                    or "reason" not in s):
                raise AssertionError(
                    f"AnalysisResult malformed param_suggestion in "
                    f"{self.registry_key or self.title!r}: {s!r} "
                    f"(need param/current/suggested/reason)"
                )
        # Every suggestion passes through here, so this is where the
        # documented ranges are enforced.
        had_suggestions = bool(self.param_suggestions)
        self.param_suggestions = limit_suggestions(self.param_suggestions)
        if (had_suggestions and not self.param_suggestions
                and self.action_level == ACTION_APPLY_PARAM):
            self.action_level = derive_default_action_level(self.status)
