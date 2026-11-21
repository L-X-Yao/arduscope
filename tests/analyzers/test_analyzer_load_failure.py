"""A broken analyzer module must not make the report look complete."""

from __future__ import annotations

import importlib

import pytest

import binlog
import arduscope.analyzers as analyzers
from arduscope.analyzers import load_analyzers
from arduscope.lib.pipeline import NO_SCORE, run_analysis


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
    assert card.failed
    assert "本报告不完整" in card.summary
