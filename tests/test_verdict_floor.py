"""Floors under every verdict: what must never read as healthy."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import binlog
from analyzers.motor import MotorAnalyzer
from lib.ai_diagnosis import generate_diagnosis
from lib.executive_summary import build_executive_summary
from lib.i18n import t
from lib.pipeline import (
    CRITICAL_SCORE_CAP,
    NO_SCORE,
    NotAFlightLog,
    compute_health_score,
    run_analysis,
)
from lib.results import AnalysisResult

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _result(status: str, **kwargs) -> AnalysisResult:
    return AnalysisResult(title=status, status=status,
                          evidence_source="src", **kwargs)


@pytest.fixture(scope="module")
def copter_log(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("bin") / "copter.bin"
    return binlog.flight_log("copter").save(path)


class TestNotAFlightLog:
    @pytest.mark.parametrize("payload", [
        bytes(4096),
        b"\xff" * 4096,
        b"this is a text file, not a flight log\n" * 100,
        bytes(range(256)) * 16,
    ], ids=["zeros", "ones", "text", "pattern"])
    def test_rejected(self, tmp_path, payload):
        path = tmp_path / "file.bin"
        path.write_bytes(payload)
        with pytest.raises(NotAFlightLog):
            run_analysis(str(path))

    def test_stray_records_are_rejected_too(self, tmp_path):
        log = binlog.BinLog()
        log.write("MSG", 1000, "ArduCopter V4.6.3 (abc12345)")
        log.write("ATT", 2000, 0, 0, 0, 0, 0, 0, 0)
        with pytest.raises(NotAFlightLog):
            run_analysis(log.save(tmp_path / "stray.bin"))

    def test_cli_fails_and_writes_nothing(self, tmp_path):
        path = tmp_path / "file.bin"
        path.write_bytes(bytes(4096))
        report = tmp_path / "out.html"
        done = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "flight_report.py"),
             str(path), "--no-open", "-o", str(report)],
            capture_output=True, text=True, timeout=120)
        assert done.returncode != 0
        assert not report.exists()
        assert not (tmp_path / "fleet.db").exists()


class TestScoreCap:
    def test_one_critical_lands_in_the_red_band(self):
        results = [_result("good") for _ in range(30)] + [_result("critical")]
        assert compute_health_score(results) == CRITICAL_SCORE_CAP

    def test_without_critical_the_mean_stands(self):
        assert compute_health_score([_result("good"), _result("warning")]) == 70

    def test_low_mean_is_not_raised_to_the_cap(self):
        assert compute_health_score([_result("critical")]) == 0

    def test_failed_analyzers_do_not_score(self):
        failed = AnalysisResult(title="x", failed=True)
        assert compute_health_score([_result("good"), failed]) == 100

    def test_nothing_analyzed_has_no_score(self):
        assert compute_health_score([]) == NO_SCORE
        assert compute_health_score(
            [AnalysisResult(title="x", failed=True)]) == NO_SCORE


class TestFailedAnalyzer:
    @pytest.fixture
    def pipeline(self, copter_log, monkeypatch):
        def boom(self):
            raise ValueError("synthetic failure")

        monkeypatch.setattr(MotorAnalyzer, "analyze", boom)
        return run_analysis(copter_log)

    def test_marked_failed_and_needs_a_person(self, pipeline):
        card = pipeline.results_by_key["motor"]
        assert card.failed
        assert card.status == "info"
        assert card.action_level == "physical_check"
        assert card.action_text
        assert "synthetic failure" in card.summary

    def test_no_all_clear_headline(self, pipeline):
        summary = build_executive_summary(pipeline.results)
        assert summary.headline == t("headline.incomplete", n=1)
        assert summary.headline != t("headline.all_good")
        assert any(a.source_key == "motor" for a in summary.actions)

    def test_ai_card_does_not_claim_all_normal(self, pipeline):
        card = generate_diagnosis(
            pipeline.results_by_key, pipeline.vehicle_info,
            pipeline.health_score, 30.0)
        assert card.status == "info"
        assert card.summary == t(
            "ai.summary.incomplete", score=pipeline.health_score, n_failed=1,
            n_ok=len(pipeline.results) - 1)
        assert any(pipeline.results_by_key["motor"].title in d
                   for d in card.details)


def test_missing_citation_keeps_the_finding(copter_log, monkeypatch, capsys):
    def uncited(self):
        return AnalysisResult(title="motor", status="critical")

    monkeypatch.setattr(MotorAnalyzer, "analyze", uncited)
    card = run_analysis(copter_log).results_by_key["motor"]
    assert card.status == "critical"
    assert not card.failed
    assert card.evidence_source == t("pipeline.evidence_missing")
    assert "without evidence_source" in capsys.readouterr().err


class TestExecutiveSummary:
    def test_critical_is_named_in_the_headline(self):
        results = [_result("critical", action_level="physical_check"),
                   _result("warning", action_level="physical_check")]
        summary = build_executive_summary(results)
        assert summary.headline == t("headline.n_critical", n=1, m=2)

    def test_critical_sorts_ahead_of_warning_in_its_level(self):
        results = [_result("warning", action_level="physical_check"),
                   _result("critical", action_level="physical_check")]
        summary = build_executive_summary(results)
        assert [a.source_status for a in summary.actions] == [
            "critical", "warning"]
