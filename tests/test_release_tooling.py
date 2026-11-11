"""
Regression tests for the release tooling and the commit-msg hook that
gates it.

release.py used to commit with a bare "release: v..." subject, which the
commit-msg hook rejects (release is not in the conventional-type
whitelist) — so the script died mid-run. It now also refuses a commit
without a pass from tools/release_gate.py, and releases the notes written
under "## [Unreleased]" rather than generating them.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "tools" / "commit-msg-hook.sh"


def _run_hook(subject: str, tmp_path) -> int:
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(subject + "\n")
    return subprocess.run(
        ["bash", str(HOOK), str(msg)], capture_output=True
    ).returncode


def _load_release():
    spec = importlib.util.spec_from_file_location(
        "release_mod", REPO / "tools" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top-level only; main() is __name__-guarded
    return mod


class TestCommitMsgHook:

    def test_accepts_release_commit(self, tmp_path):
        # The subject release.py now emits must pass the hook.
        assert _run_hook("chore: release v0.5.0", tmp_path) == 0

    def test_rejects_bare_release_prefix(self, tmp_path):
        # The subject release.py *used* to emit — proves the bug was real
        # (and that this test would have caught it).
        assert _run_hook("release: v0.5.0", tmp_path) != 0


class TestReleasePy:

    def test_commit_uses_chore_prefix(self):
        src = (REPO / "tools" / "release.py").read_text()
        assert 'f"chore: release v{new_ver}"' in src
        assert 'f"release: v{new_ver}"' not in src

    CHANGELOG = (
        "# Changelog\n\n"
        "## [Unreleased]\n\n### Fixed\n- new\n\n"
        "## [0.4.0] - 2026-07-15\n\n### Fixed\n- old, see `[Unreleased]`\n"
    )

    def test_unreleased_becomes_the_version(self):
        out = _load_release()._date_unreleased(
            self.CHANGELOG, "0.5.0", "2026-08-01")
        assert "## [Unreleased]" not in out
        assert out.index("## [0.5.0] - 2026-08-01") < out.index("- new")
        assert out.index("- new") < out.index("## [0.4.0]")

    def test_older_sections_stay_as_they_are(self):
        out = _load_release()._date_unreleased(
            self.CHANGELOG, "0.5.0", "2026-08-01")
        assert "- old, see `[Unreleased]`" in out

    def test_no_unreleased_section_stops_the_release(self):
        content = "# Changelog\n\n## [0.4.0] - 2026-07-15\n\n- old\n"
        with pytest.raises(SystemExit):
            _load_release()._date_unreleased(content, "0.5.0", "2026-08-01")

    def test_an_empty_section_stops_the_release(self):
        content = "# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-07-15\n"
        with pytest.raises(SystemExit):
            _load_release()._date_unreleased(content, "0.5.0", "2026-08-01")

    def test_no_passing_gate_stops_the_release(self, monkeypatch):
        """Before the version moves or anything is committed."""
        mod = _load_release()
        calls = []
        monkeypatch.setattr(sys, "argv", ["release.py", "patch"])
        monkeypatch.setattr(mod.subprocess, "run", _clean_repo)
        monkeypatch.setattr(mod.release_gate, "head_commit", lambda: "abc")
        monkeypatch.setattr(mod.release_gate, "passed", lambda commit: False)
        monkeypatch.setattr(mod, "_current_version", lambda: "0.9.3")
        monkeypatch.setattr(mod, "_update_version_file", calls.append)
        with pytest.raises(SystemExit, match="No passing gate"):
            mod.main()
        assert not calls


def _clean_repo(command, **kwargs):
    """git on a clean tree, on main, where the new tag does not exist."""
    return SimpleNamespace(returncode=int("--verify" in command), stdout="main")


def _load_build():
    spec = importlib.util.spec_from_file_location(
        "bwp_mod", REPO / "build_windows_package.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestWindowsBuildTooling:
    """.pyc bytecode is minor-version-locked; compiling with an interpreter
    that does not match the bundled embeddable ships a package that dies
    with 'bad magic number' on Windows. _get_compiler must fail loudly
    rather than fall back to a mismatched interpreter."""

    def test_get_compiler_rejects_missing_version(self):
        mod = _load_build()
        # No interpreter provides Python 3.99 -> must raise, not silently
        # return the host interpreter and ship a bad-magic-number .pyc.
        with pytest.raises(RuntimeError, match="magic number"):
            mod._get_compiler("3.99.0", "/tmp")

    def test_get_compiler_accepts_host_version(self):
        import sys
        mod = _load_build()
        host_full = "%d.%d.0" % sys.version_info[:2]
        # The host's own minor version always has a matching interpreter.
        assert mod._get_compiler(host_full, "/tmp")
