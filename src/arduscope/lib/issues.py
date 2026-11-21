"""Structured analysis issues and their report card."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .i18n import t
from .results import ACTION_PHYSICAL_CHECK, AnalysisResult


@dataclass(frozen=True)
class AnalysisIssue:
    """A stage that failed or degraded, kept apart from analyzer verdicts."""

    stage: str
    message: str
    severity: str = "warning"
    analyzer_key: str = ""
    exception: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def issue_card(issue: AnalysisIssue, title_key: str, summary_key: str,
               registry_key: str) -> AnalysisResult:
    """A failed card that makes the report say it is incomplete."""
    return AnalysisResult(
        title=t(title_key),
        status="info",
        summary=t(summary_key, stage=issue.stage),
        details=[issue.message],
        action_level=ACTION_PHYSICAL_CHECK,
        failed=True,
        registry_key=registry_key,
    )
