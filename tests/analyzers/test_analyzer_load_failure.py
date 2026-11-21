"""A broken analyzer module must not make the report look complete."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import binlog
import arduscope.analyzers as analyzers
from arduscope.analyzers import load_analyzers
from arduscope.lib.ai_diagnosis import generate_diagnosis
from arduscope.lib.fleet_history import _is_full_run
from arduscope.lib.issues import AnalysisIssue
from arduscope.lib.pipeline import NO_SCORE, run_analysis
from arduscope.lib.results import AnalysisResult
from arduscope.lib.vehicle_detect import VehicleInfo


@pytest.mark.fast
def test_load_analyzers_returns_broken_modules(monkeypatch):
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.endswith(".wind"):
            raise RuntimeError("boom")
        return real_import(name, package)

    monkeypatch.setattr(analyzers.importlib, "import_module", fake_import)
    found, errors = load_analyzers()

    assert [name for name, _error in errors] == ["wind"]
    assert len(found) == 31


@pytest.mark.fast
def test_pipeline_turns_a_load_error_into_a_failed_card(tmp_path, monkeypatch):
    log = binlog.flight_log("copter", duration_s=5).save(tmp_path / "a.BIN")
    monkeypatch.setattr(
        analyzers, "load_analyzers",
        lambda: ([], [("wind", RuntimeError("boom"))]),
    )

    result = run_analysis(str(log))

    assert result.health_score == NO_SCORE
    assert result.issues[0].stage == "analyzer_load"
    assert result.issues[0].analyzer_key == "wind"
    card = next(r for r in result.results if r.registry_key == "analyzer_load")
    assert result.results_by_key["analyzer_load"] is card
    assert card.failed
    assert "本报告不完整" in card.summary


@pytest.mark.fast
def test_diagnosis_says_incomplete_when_one_analyzer_failed_to_load():
    healthy = {
        f"a{i}": AnalysisResult(title=f"a{i}", status="good", summary="ok")
        for i in range(31)
    }
    failed = AnalysisResult(
        title="load", status="info", summary="incomplete",
        action_level="physical_check", failed=True, registry_key="analyzer_load")
    healthy["analyzer_load"] = failed

    diagnosis = generate_diagnosis(
        healthy, VehicleInfo("ArduCopter", "x", "4", 1, 4, 1, False), 100, 10)

    assert diagnosis.status == "info"
    assert "未完成" in diagnosis.summary or "incomplete" in diagnosis.summary.lower()


@pytest.mark.fast
def test_fleet_rejects_an_incomplete_run():
    issue = AnalysisIssue(stage="analyzer_load", message="boom")
    assert not _is_full_run(SimpleNamespace(
        issues=[issue], results=[], reader=None, thresholds=None,
        vehicle_info=None))

    failed = AnalysisResult(
        title="load", status="info", summary="incomplete",
        action_level="physical_check", failed=True, registry_key="analyzer_load")
    assert not _is_full_run(SimpleNamespace(
        issues=[], results=[failed], reader=None, thresholds=None,
        vehicle_info=None))
