"""tools/release_gate.py: what lets a commit be tagged.

The steps run for real in the gate itself. These tests cover what it makes of
them, since a gate that reads a failure as a pass does worse than no gate.
"""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import release_gate as gate  # noqa: E402

pytestmark = pytest.mark.fast


def _scan(status="good", suggestions=(), missing=False, logs=("a.BIN",)):
    record = {"status": status, "action": "ok",
              "n_param_suggestions": len(suggestions),
              "suggestions": list(suggestions), "missing_evidence": missing}
    return {"tool_version": "x", "generated_at": "t",
            "logs": [{"relpath": log, "analyzers": {"gps": dict(record)}}
                     for log in logs]}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "GATE_DIR", str(tmp_path))
    monkeypatch.setattr(gate, "ACCEPTED_DIR", str(tmp_path / "accepted"))
    return tmp_path


class TestPytestCounts:
    @pytest.mark.parametrize("line, expected", [
        ("3041 passed, 114 skipped in 900.10s (0:15:00)",
         {"passed": 3041, "skipped": 114}),
        ("1 failed, 2 passed, 3 warnings in 1.00s",
         {"failed": 1, "passed": 2, "warnings": 3}),
        ("2 errors in 1.00s", {"error": 2}),
        ("no tests ran in 0.01s", {}),
    ])
    def test_summary_line(self, line, expected):
        assert gate.pytest_counts(f"....\n{line}\n") == expected

    def test_the_last_summary_counts(self):
        output = "1 passed in 1.00s\nmore output\n2 failed in 2.00s\n"
        assert gate.pytest_counts(output) == {"failed": 2}


class TestJudgeTests:
    def test_a_green_run_passes(self):
        assert gate.judge_tests(0, "5 passed in 1.00s\n")["status"] == "pass"

    def test_a_failed_run_fails(self):
        assert gate.judge_tests(1, "1 failed in 1.00s\n")["status"] == "fail"

    def test_a_run_that_skipped_everything_fails(self):
        """Tests skip without logs, and exit 0 doing it."""
        assert gate.judge_tests(0, "5 skipped in 1.00s\n")["status"] == "fail"

    def test_output_with_no_summary_fails(self):
        assert gate.judge_tests(0, "crashed\n")["status"] == "fail"


class TestJudgeCorpus:
    def test_the_same_results_pass(self):
        assert gate.judge_corpus(_scan(), _scan())["status"] == "pass"

    def test_no_baseline_needs_review(self):
        assert gate.judge_corpus(None, _scan())["status"] == "review"

    def test_a_status_change_needs_review(self):
        result = gate.judge_corpus(_scan(), _scan(status="critical"))
        assert result["status"] == "review"
        assert result["detail"]["logs_changed"] == 1
        assert result["detail"]["by_analyzer"]["gps"]["escalated"] == 1

    def test_a_changed_suggestion_needs_review(self):
        """Status can stay put while the value someone would load moves."""
        before = _scan(suggestions=["GPS_AUTO_SWITCH=1"])
        after = _scan(suggestions=["GPS_AUTO_SWITCH=2"])
        assert gate.judge_corpus(before, after)["status"] == "review"

    def test_a_log_gone_needs_review(self):
        result = gate.judge_corpus(_scan(logs=("a.BIN", "b.BIN")), _scan())
        assert result["status"] == "review"
        assert result["detail"]["logs_gone"] == 1

    def test_a_new_log_needs_review(self):
        result = gate.judge_corpus(_scan(), _scan(logs=("a.BIN", "b.BIN")))
        assert result["status"] == "review"

    def test_a_finding_with_no_source_fails(self):
        result = gate.judge_corpus(_scan(), _scan(missing=True))
        assert result["status"] == "fail"


class TestOverall:
    def _steps(self, *statuses):
        return {str(i): {"status": s} for i, s in enumerate(statuses)}

    def test_all_steps_passing_is_a_pass(self):
        assert gate.overall(self._steps("pass", "pass")) == "pass"

    def test_review_holds_a_pass_back(self):
        assert gate.overall(self._steps("pass", "review")) == "review"

    def test_a_failure_beats_a_review(self):
        assert gate.overall(self._steps("review", "fail")) == "fail"


class TestPassed:
    def test_no_result(self, dirs):
        assert not gate.passed("abc")

    @pytest.mark.parametrize("status, expected", [
        ("pass", True), ("review", False), ("fail", False),
    ])
    def test_only_pass_counts(self, dirs, status, expected):
        gate.save_result({"commit": "abc", "status": status})
        assert gate.passed("abc") is expected

    def test_another_commit_does_not_count(self, dirs):
        gate.save_result({"commit": "abc", "status": "pass"})
        assert not gate.passed("def")


class TestAccept:
    def _review(self, dirs, status="review"):
        steps = {"corpus": {"status": "review", "detail": {"field": {}}}}
        gate.save_result({"commit": "abc", "status": status, "steps": steps})
        scan = dirs / "abc" / "field.json"
        scan.parent.mkdir()
        scan.write_text(json.dumps(_scan(status="critical")), encoding="utf-8")

    def test_replaces_the_baseline_and_passes(self, dirs):
        self._review(dirs)
        assert gate.accept("abc", "the rule changed on purpose") == 0
        assert gate.passed("abc")
        assert gate.load_result("abc")["accepted"] == "the rule changed on purpose"
        accepted = json.loads((dirs / "accepted" / "field.json").read_text("utf-8"))
        assert accepted["logs"][0]["analyzers"]["gps"]["status"] == "critical"

    def test_needs_a_reason(self, dirs):
        self._review(dirs)
        assert gate.accept("abc", "  ") == 1
        assert not gate.passed("abc")

    def test_only_a_review_can_be_accepted(self, dirs):
        self._review(dirs, status="fail")
        assert gate.accept("abc", "reason") == 1
        assert not gate.passed("abc")

    def test_no_result(self, dirs):
        assert gate.accept("abc", "reason") == 1


class TestRunGate:
    def _args(self, steps=gate.STEPS):
        return SimpleNamespace(steps=list(steps))

    def _fake(self, monkeypatch, status="pass", dirty=(False, False)):
        states = iter(dirty)
        monkeypatch.setattr(gate, "head_commit", lambda: "abc")
        monkeypatch.setattr(gate, "tree_is_dirty", lambda: next(states))
        monkeypatch.setattr(gate, "RUNNERS", {
            name: (lambda args, log_dir: {"status": status})
            for name in gate.STEPS})

    def test_every_step_passing_records_a_pass(self, dirs, monkeypatch):
        self._fake(monkeypatch)
        assert gate.run_gate(self._args()) == 0
        assert gate.passed("abc")

    def test_a_dirty_tree_is_refused(self, dirs, monkeypatch):
        self._fake(monkeypatch, dirty=(True,))
        assert gate.run_gate(self._args()) == 1
        assert gate.load_result("abc") is None

    def test_a_tree_that_changed_during_the_run_fails(self, dirs, monkeypatch):
        self._fake(monkeypatch, dirty=(False, True))
        assert gate.run_gate(self._args()) == 1
        assert gate.load_result("abc")["status"] == "fail"

    def test_a_partial_run_cannot_pass_a_commit(self, dirs, monkeypatch):
        self._fake(monkeypatch)
        assert gate.run_gate(self._args(["corpus"])) == 0
        assert not gate.passed("abc")
        assert gate.load_result("abc") is None

    def test_a_partial_run_leaves_a_full_result_alone(self, dirs, monkeypatch):
        gate.save_result({"commit": "abc", "status": "pass"})
        self._fake(monkeypatch, status="fail")
        assert gate.run_gate(self._args(["corpus"])) == 1
        assert gate.passed("abc")
        partial = json.loads(Path(gate.partial_path("abc")).read_text("utf-8"))
        assert partial["status"] == "fail"

    def test_a_failed_step_fails(self, dirs, monkeypatch):
        self._fake(monkeypatch, status="fail")
        assert gate.run_gate(self._args()) == 1
        assert gate.load_result("abc")["status"] == "fail"


class TestCommandLine:
    def test_check_reports_a_missing_result(self, dirs, monkeypatch, capsys):
        monkeypatch.setattr(gate, "head_commit", lambda: "abc")
        assert gate.main(["--check"]) == 1
        assert "no result" in capsys.readouterr().out

    def test_check_passes_on_a_pass(self, dirs, monkeypatch):
        monkeypatch.setattr(gate, "head_commit", lambda: "abc")
        gate.save_result({"commit": "abc", "status": "pass"})
        assert gate.main(["--check"]) == 0

    def test_an_unknown_step_is_refused(self):
        with pytest.raises(SystemExit):
            gate.main(["--steps", "corpus,typo"])
