"""tools/win_package_check.py: what counts as a passing Windows package.

The check itself needs real Windows and runs after a release. What it makes of
the driver's report can be tested anywhere.
"""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import win_package_check as check  # noqa: E402

pytestmark = pytest.mark.fast


def _line(case, status="done", **fields):
    return {"case": case, "status": status, "uploads_left": [],
            "server_alive": True, **fields}


def _good() -> list[dict]:
    return [_line(name, expected or "done")
            for name, expected in check.UPLOADS.items()]


def _failed(checks: list[dict]) -> list[str]:
    return [c["check"] for c in checks if not c["ok"]]


class TestUploads:
    def test_a_good_run_passes(self):
        checks = check.judge_uploads(_good())
        assert len(checks) == len(check.UPLOADS)
        assert not _failed(checks)

    def test_a_good_log_must_analyze(self):
        lines = [_line("normal.BIN", "error") if line["case"] == "normal.BIN"
                 else line for line in _good()]
        assert _failed(check.judge_uploads(lines)) == ["upload normal.BIN"]

    def test_random_bytes_must_be_refused(self):
        lines = [_line("random.BIN", "done") if line["case"] == "random.BIN"
                 else line for line in _good()]
        assert _failed(check.judge_uploads(lines)) == ["upload random.BIN"]

    @pytest.mark.parametrize("status", ["done", "error"])
    def test_a_damaged_log_can_go_either_way(self, status):
        lines = [_line("flipped.BIN", status) if line["case"] == "flipped.BIN"
                 else line for line in _good()]
        assert not _failed(check.judge_uploads(lines))

    def test_a_file_left_behind_fails(self):
        """The defect that only real Windows can show."""
        lines = [dict(line, uploads_left=["upload_x.BIN"])
                 if line["case"] == "flipped.BIN" else line for line in _good()]
        assert _failed(check.judge_uploads(lines)) == ["upload flipped.BIN"]

    def test_a_dead_server_fails(self):
        lines = [dict(line, server_alive=False)
                 if line["case"] == "head7.BIN" else line for line in _good()]
        assert _failed(check.judge_uploads(lines)) == ["upload head7.BIN"]

    def test_a_job_that_never_ended_fails(self):
        lines = [_line("truncated.BIN", "server_died")
                 if line["case"] == "truncated.BIN" else line
                 for line in _good()]
        assert _failed(check.judge_uploads(lines)) == ["upload truncated.BIN"]

    def test_an_upload_with_no_result_fails(self):
        lines = [line for line in _good() if line["case"] != "head7.BIN"]
        assert _failed(check.judge_uploads(lines)) == ["upload head7.BIN"]


class TestModules:
    def test_package_names(self):
        names = ["arduscope/log_analysis/lib/results.pyc",
                 "arduscope/log_analysis/analyzers/gps.pyc",
                 "arduscope/python/python.exe",
                 "arduscope/python/python312.zip"]
        assert check.package_modules(names) == {"lib/results", "analyzers/gps"}

    def test_source_names(self):
        modules = check.source_modules()
        assert {"lib/results", "analyzers/gps"} <= modules


class TestDriver:
    def test_uses_the_standard_library_only(self):
        """It runs under the package's embedded Python."""
        tree = ast.parse(Path(check.DRIVER).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        assert imported <= set(sys.stdlib_module_names)
