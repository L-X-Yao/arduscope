"""The application boundary around one analyzed flight.

Every entry point runs the same order: pipeline, optional diagnosis, optional
fleet history. This module owns that order and the failure visibility; the
entry points keep only their output and transport concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .i18n import t
from .issues import AnalysisIssue, issue_card
from .pipeline import PipelineResult, run_analysis
from .results import AnalysisResult


@dataclass
class AnalysisRequest:
    filepath: str
    template: object | None = None
    only: set[str] | None = None
    skip: set[str] | None = None
    thresholds_path: str | None = None
    include_gps: bool = True
    on_progress: Callable[[int, str], None] | None = None


@dataclass
class AnalysisOutcome:
    pipeline: PipelineResult
    results: list[AnalysisResult]
    diagnosis: AnalysisResult | None = None
    history: AnalysisResult | None = None
    issues: list[AnalysisIssue] = field(default_factory=list)
    archive_error: str = ""


class ReportSession:
    """Run one pipeline and compose diagnosis and fleet history around it."""

    def __init__(self, request: AnalysisRequest, *,
                 diagnose: Callable[[PipelineResult], AnalysisResult | None] | None = None,
                 archive: Callable[[PipelineResult], AnalysisResult | None] | None = None):
        self.request = request
        self._diagnose = diagnose
        self._archive = archive

    def run(self) -> AnalysisOutcome:
        request = self.request
        pipeline = run_analysis(
            request.filepath,
            template=request.template,
            only=request.only,
            skip=request.skip,
            thresholds_path=request.thresholds_path,
            include_gps=request.include_gps,
            on_progress=request.on_progress,
        )
        results = list(pipeline.results)
        issues = list(pipeline.issues)

        diagnosis = None
        if self._diagnose is not None and pipeline.results_by_key:
            try:
                diagnosis = self._diagnose(pipeline)
            except Exception as e:
                issue = AnalysisIssue(
                    stage="diagnosis",
                    message=t("pipeline.stage_failed_detail", error=e),
                    exception=type(e).__name__,
                )
                issues.append(issue)
                diagnosis = issue_card(
                    issue,
                    "pipeline.stage_failed_title",
                    "pipeline.stage_failed_summary",
                    "diagnosis",
                )
            if diagnosis is not None:
                results.insert(0, diagnosis)

        history = None
        archive_error = ""
        if self._archive is not None:
            try:
                history = self._archive(pipeline)
            except Exception as e:
                archive_error = str(e)
            if history is not None:
                results.append(history)

        return AnalysisOutcome(
            pipeline=pipeline,
            results=results,
            diagnosis=diagnosis,
            history=history,
            issues=issues,
            archive_error=archive_error,
        )
