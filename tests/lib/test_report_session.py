"""The report session owns the post-pipeline order and failure visibility."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arduscope.lib.issues import AnalysisIssue
from arduscope.lib.report_session import AnalysisRequest, ReportSession
from arduscope.lib.results import AnalysisResult


def _result(key: str, status: str = "good") -> AnalysisResult:
    return AnalysisResult(title=key, status=status, summary="ok",
                          registry_key=key)


def _pipeline(*, issues=(), keys=("a",)):
    results = [_result(key) for key in keys]
    return SimpleNamespace(
        results=results,
        results_by_key={r.registry_key: r for r in results},
        issues=list(issues),
    )


@pytest.mark.fast
def test_session_composes_diagnosis_then_history(monkeypatch):
    pipeline = _pipeline()

    def diagnose(_pipeline):
        return _result("diagnosis", "info")

    def archive(_pipeline):
        return _result("history", "info")

    monkeypatch.setattr("arduscope.lib.report_session.run_analysis",
                        lambda *_args, **_kwargs: pipeline)
    outcome = ReportSession(
        AnalysisRequest("x.BIN"), diagnose=diagnose, archive=archive).run()

    assert [r.registry_key for r in outcome.results] == [
        "diagnosis", "a", "history"]
    assert outcome.diagnosis.registry_key == "diagnosis"
    assert outcome.history.registry_key == "history"
    assert outcome.issues == []
    assert outcome.archive_error == ""


@pytest.mark.fast
def test_session_turns_a_diagnosis_error_into_a_failed_card(monkeypatch):
    issue = AnalysisIssue(stage="analyzer_load", message="load failed")
    pipeline = _pipeline(issues=[issue])

    def diagnose(_pipeline):
        raise RuntimeError("diagnosis broke")

    monkeypatch.setattr("arduscope.lib.report_session.run_analysis",
                        lambda *_args, **_kwargs: pipeline)
    outcome = ReportSession(
        AnalysisRequest("x.BIN"), diagnose=diagnose).run()

    assert outcome.issues[0] is issue
    assert outcome.issues[1].stage == "diagnosis"
    failed = outcome.results[0]
    assert failed.failed and failed.registry_key == "diagnosis"


@pytest.mark.fast
def test_session_keeps_an_archive_error_out_of_the_report(monkeypatch):
    pipeline = _pipeline()

    def archive(_pipeline):
        raise RuntimeError("archive broke")

    monkeypatch.setattr("arduscope.lib.report_session.run_analysis",
                        lambda *_args, **_kwargs: pipeline)
    outcome = ReportSession(
        AnalysisRequest("x.BIN"), archive=archive).run()

    assert outcome.archive_error == "archive broke"
    assert [r.registry_key for r in outcome.results] == ["a"]
    assert outcome.issues == []
