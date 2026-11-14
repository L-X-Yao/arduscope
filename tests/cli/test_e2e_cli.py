"""
End-to-end CLI integration tests.

Unit tests cover individual analyzers and lib modules, but nobody
previously ran the full `flight_report.py log.bin -o out.html` pipeline
in the test suite. These tests invoke the CLI as a subprocess and
verify that the generated artifacts are valid, catching integration
issues that unit tests miss.

Tests that need real log files skip cleanly via pytest.skip when the
log is absent (consistent with conftest.py's handling), so they work
in both local and CI environments.
"""

from __future__ import annotations

from tests._repo import REPO_ROOT

import os
import subprocess
import sys

import pytest

from conftest import LOG_QUADPLANE_FLIGHT


_SCRIPT_DIR = str(REPO_ROOT)
_FLIGHT_REPORT = os.path.join(_SCRIPT_DIR, "flight_report.py")
_MULTI_REPORT = os.path.join(_SCRIPT_DIR, "multi_report.py")


def _run_cli(args: list[str], cwd: str = _SCRIPT_DIR,
             timeout: int = 120) -> subprocess.CompletedProcess:
    """Run a log_analysis CLI script as a subprocess."""
    return subprocess.run(
        [sys.executable] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestFlightReportCli:
    def test_help_exits_cleanly(self):
        """`flight_report.py --help` should exit 0 with usage text."""
        result = _run_cli(["flight_report.py", "--help"])
        assert result.returncode == 0
        assert "ArduScope" in result.stdout or "usage" in result.stdout.lower()

    def test_list_templates_lists_all(self):
        """--list-templates must print every registered template name."""
        result = _run_cli(["flight_report.py", "--list-templates"])
        assert result.returncode == 0, \
            f"--list-templates failed: {result.stderr}"
        # All shipped templates should appear in output
        for name in ("quadplane-accept", "tuning-compare",
                     "crash-diagnosis", "battery-aging",
                     "copter-factory", "tuning"):
            assert name in result.stdout, \
                f"Template {name} missing from output"

    def test_no_logfile_errors(self):
        """Running without a log file should fail with a clear error."""
        result = _run_cli(["flight_report.py"])
        assert result.returncode != 0
        # Should mention the missing argument
        combined = result.stdout + result.stderr
        assert "logfile" in combined.lower() or "argument" in combined.lower()

    def test_missing_logfile_errors(self):
        """Pointing at a non-existent log should exit 1 with an error."""
        result = _run_cli([
            "flight_report.py", "/nonexistent/fake.bin",
            "--no-open",
        ])
        assert result.returncode != 0
        assert "not found" in result.stderr.lower() or \
               "not found" in result.stdout.lower() or \
               "错误" in result.stderr or "错误" in result.stdout

    def test_full_pipeline_generates_html(self, tmp_path):
        """
        The full pipeline: read log → analyze → generate HTML report.
        This exercises flight_report.py + analyzers + lib/html_report.py
        + lib/ai_diagnosis.py end to end — the one path no single unit
        test touches.
        """
        if not os.path.isfile(LOG_QUADPLANE_FLIGHT):
            pytest.skip(f"Log file not found: {LOG_QUADPLANE_FLIGHT}")

        out_html = tmp_path / "report.html"
        result = _run_cli([
            "flight_report.py",
            LOG_QUADPLANE_FLIGHT,
            "-o", str(out_html),
            "--no-open",
            "--no-ai",  # skip AI diagnosis to keep runtime low
        ])

        assert result.returncode == 0, \
            f"flight_report.py failed: stdout={result.stdout}\n" \
            f"stderr={result.stderr}"
        assert out_html.exists(), "HTML report was not written"

        content = out_html.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        assert "</html>" in content
        # Report should reference the log filename
        assert os.path.basename(LOG_QUADPLANE_FLIGHT) in content
        # At least one analyzer's output should be present
        assert "飞行概览" in content or "summary" in content.lower()

    def test_pipeline_with_template_works(self, tmp_path):
        """--template tuning should also produce a valid HTML report."""
        if not os.path.isfile(LOG_QUADPLANE_FLIGHT):
            pytest.skip(f"Log file not found: {LOG_QUADPLANE_FLIGHT}")

        out_html = tmp_path / "tuning_report.html"
        result = _run_cli([
            "flight_report.py",
            LOG_QUADPLANE_FLIGHT,
            "-o", str(out_html),
            "--no-open",
            "--no-ai",
            "--template", "tuning",
        ])

        assert result.returncode == 0, \
            f"template pipeline failed: {result.stderr}"
        assert out_html.exists()
        content = out_html.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")

    def test_invalid_template_errors(self):
        """Unknown template should be a hard error, not a warning."""
        result = _run_cli([
            "flight_report.py",
            "/tmp/fake.bin",
            "--template", "not-a-real-template",
        ])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        # Should mention the unknown template (Chinese or English)
        assert "template" in combined.lower() or "模板" in combined


class TestFlightReportEdgeCases:
    """Edge case robustness — the tool must never crash on bad input."""

    def test_empty_file_clean_error(self, tmp_path):
        empty = tmp_path / "empty.BIN"
        empty.write_bytes(b"")
        result = _run_cli([
            "flight_report.py", str(empty),
            "-o", str(tmp_path / "out.html"), "--no-open", "--no-ai",
        ])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "empty" in combined.lower() or "空" in combined

    def test_garbage_file_does_not_crash(self, tmp_path):
        garbage = tmp_path / "garbage.BIN"
        garbage.write_bytes(b"\x00" * 200)
        result = _run_cli([
            "flight_report.py", str(garbage),
            "-o", str(tmp_path / "out.html"), "--no-open", "--no-ai",
        ])
        # May succeed (empty report) or fail, but must NOT raise an
        # unhandled exception (traceback in stderr).
        if result.returncode != 0:
            assert "Traceback" not in result.stderr

    def test_param_export_includes_all_suggestions(self, tmp_path):
        """The .param file must include param_suggestions from ALL analyzers."""
        if not os.path.isfile(LOG_QUADPLANE_FLIGHT):
            pytest.skip(f"Log file not found: {LOG_QUADPLANE_FLIGHT}")
        out_html = tmp_path / "report.html"
        result = _run_cli([
            "flight_report.py", LOG_QUADPLANE_FLIGHT,
            "-o", str(out_html), "--no-open", "--no-ai",
        ])
        assert result.returncode == 0
        param_file = tmp_path / "report.param"
        # Log 82 (notch disabled, known issues) always yields
        # suggestions, so the export must exist — the old
        # `if exists():` gate passed with the feature deleted.
        assert param_file.exists(), ".param export missing"
        content = param_file.read_text()
        assert "Auto-generated" in content
        value_lines = [ln for ln in content.splitlines()
                       if ln.strip() and not ln.lstrip().startswith("#")
                       and "," in ln]
        assert value_lines, ".param export contains no parameter rows"


class TestMultiReportCli:
    def test_help_exits_cleanly(self):
        result = _run_cli(["multi_report.py", "--help"])
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or \
               "多" in result.stdout

    def test_no_logs_errors(self):
        """No log file arguments should fail clearly."""
        result = _run_cli(["multi_report.py"])
        assert result.returncode != 0

    def test_single_log_does_not_crash(self, tmp_path):
        """multi_report with only 1 log should not crash."""
        if not os.path.isfile(LOG_QUADPLANE_FLIGHT):
            pytest.skip(f"Log file not found: {LOG_QUADPLANE_FLIGHT}")
        out_html = tmp_path / "multi.html"
        result = _run_cli([
            "multi_report.py", LOG_QUADPLANE_FLIGHT,
            "-o", str(out_html), "--no-open",
        ])
        assert result.returncode == 0
        assert out_html.exists()
