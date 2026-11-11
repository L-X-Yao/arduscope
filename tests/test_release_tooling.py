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

import hashlib
import importlib.util
import re
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


REQUIREMENTS = REPO / "win64-requirements.txt"


class TestWindowsPackagePins:
    """Up to 0.9.9 the package took whatever pip found that day, with
    --no-deps, on an embeddable whose download nothing checked. 0.9.10
    pins the Python by its SHA-256 and each library by its wheel's."""

    def test_a_download_that_is_not_the_listed_file_stops_the_build(self):
        mod = _load_build()
        with pytest.raises(RuntimeError, match="python.org lists"):
            mod.check_embed(b"not the embeddable", mod.DEFAULT_PYTHON)

    def test_a_python_with_no_hash_on_record_stops_the_build(self):
        with pytest.raises(RuntimeError, match="No SHA-256 on record"):
            _load_build().check_embed(b"", "3.99.0")

    def test_the_listed_file_passes(self, monkeypatch):
        mod = _load_build()
        monkeypatch.setitem(mod.EMBED_SHA256, "9.9.9",
                            hashlib.sha256(b"zip").hexdigest())
        mod.check_embed(b"zip", "9.9.9")

    def test_the_default_python_has_a_hash(self):
        mod = _load_build()
        assert re.fullmatch(r"[0-9a-f]{64}", mod.EMBED_SHA256[mod.DEFAULT_PYTHON])

    def test_every_pin_carries_a_hash(self):
        text = REQUIREMENTS.read_text().replace("\\\n", " ")
        entries = [line for line in text.splitlines()
                   if line.strip() and not line.startswith("#")]
        pins = _load_build().read_requirements(str(REQUIREMENTS))
        assert len(entries) == len(pins) >= 6
        for entry in entries:
            assert re.match(r"[A-Za-z0-9._-]+==[^\s]+ ", entry), entry
            assert re.search(r"--hash=sha256:[0-9a-f]{64}\b", entry), entry

    def test_the_pins_meet_pyproject(self):
        tomllib = pytest.importorskip("tomllib")
        mod = _load_build()
        with open(REPO / "pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        pins = mod.read_requirements(str(REQUIREMENTS))
        assert mod.check_requirements(pins, pyproject) == []

    def test_a_pin_that_misses_pyproject_is_reported(self):
        pyproject = {"project": {
            "dependencies": ["numpy>=1.24,<3", "plotly>=6.0.0"],
            "optional-dependencies": {"web": ["bottle>=0.13"]}}}
        problems = _load_build().check_requirements(
            {"numpy": "3.0.0", "plotly": "6.1"}, pyproject)
        assert problems == ["numpy==3.0.0 does not satisfy numpy>=1.24,<3",
                            "bottle is not pinned"]

    @staticmethod
    def _site(tmp_path, libraries):
        for folder, requires in libraries.items():
            info = tmp_path / f"{folder}.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: x\n"
                + "".join(f"Requires-Dist: {r}\n" for r in requires))
        return str(tmp_path)

    def test_a_dependency_left_out_stops_the_build(self, tmp_path):
        site = self._site(tmp_path, {
            "plotly-7.1.0": ["narwhals>=1.15.1", "packaging",
                             'numpy>=1.22; extra == "express"'],
            "packaging-26.3": [],
            # Both left out on purpose, see _NOT_BUNDLED.
            "pymavlink-2.4.50": ["lxml", "fastcrc"],
            "Some_Lib-1.0": ['colorama; sys_platform == "win32"',
                             'uvloop; sys_platform == "linux"',
                             'tomli; python_version < "3.11"'],
        })
        assert _load_build().check_closure(site, "3.14.10") == [
            "plotly needs narwhals, which is not pinned",
            "some-lib needs colorama, which is not pinned",
        ]


class TestReleaseWorkflow:
    WORKFLOW = (REPO / ".github" / "workflows" / "release.yml").read_text()

    def test_the_release_is_a_draft(self):
        step = self.WORKFLOW.split("softprops/action-gh-release", 1)[1]
        assert re.search(r"^\s+draft: true$", step, re.M)

    def test_the_runner_python_is_the_bundled_minor(self):
        """.pyc compiled by another minor version does not load."""
        build_job = self.WORKFLOW.split("\n  build:", 1)[1]
        runner = re.search(r'python-version: "([\d.]+)"', build_job).group(1)
        default = _load_build().DEFAULT_PYTHON
        assert runner == ".".join(default.split(".")[:2])

    def test_the_version_is_named_in_one_place(self):
        assert "--python-version" not in self.WORKFLOW

    def test_only_the_build_job_can_write(self):
        """A job without a permissions block of its own takes the
        workflow's, or else the repository default: CodeQL flagged ci."""
        head, jobs = self.WORKFLOW.split("\njobs:", 1)
        assert re.search(r"^permissions:\n  contents: read$", head, re.M)
        ci_job, build_job = jobs.split("\n  build:", 1)
        assert "write" not in ci_job
        assert re.search(r"^      contents: write$", build_job, re.M)

    def test_actions_from_outside_github_are_named_by_commit(self):
        """A tag can be moved to other code; a commit cannot."""
        workflows = (REPO / ".github" / "workflows").glob("*.yml")
        uses = [m.group(1) for path in workflows
                for m in re.finditer(r"uses:\s*(\S+)", path.read_text())]
        outside = [u for u in uses
                   if not u.startswith(("./", "actions/", "github/"))]
        assert outside, "the release action is expected here"
        for action in outside:
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), action


def _load_check():
    spec = importlib.util.spec_from_file_location(
        "wpc_mod", REPO / "tools" / "win_package_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPackageCheck:
    SITE = "arduscope/python/Lib/site-packages/"

    def test_libraries_come_from_their_dist_info(self):
        names = [self.SITE + "numpy-2.5.4.dist-info/METADATA",
                 self.SITE + "numpy/__init__.py",
                 self.SITE + "pip-26.0.dist-info/METADATA",
                 self.SITE + "PyYAML-6.0.dist-info/RECORD"]
        assert _load_check().bundled_libraries(names) == {"numpy": "2.5.4"}

    def test_a_library_off_its_pin_is_named(self):
        differ = _load_check().pin_differences(
            {"numpy": "2.5.4", "plotly": "7.0.0"},
            {"numpy": "2.5.4", "plotly": "7.1.0", "bottle": "0.13.4"})
        assert differ == ["bottle: missing, pinned 0.13.4",
                          "plotly: 7.0.0, pinned 7.1.0"]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "gate_mod", REPO / "tools" / "release_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGateEnvironment:
    """The gate ran on whatever the local environment held, a numpy and a
    plotly older than the ones the Windows package ships, and its record did
    not say so."""

    PINS = _load_build().read_requirements(str(REQUIREMENTS))

    def test_the_record_lists_every_pinned_library(self):
        env = _load_gate().environment(version=lambda name: self.PINS[name])
        assert env["libraries"] == self.PINS
        assert env["off_the_windows_pins"] == []
        assert re.fullmatch(r"\d+\.\d+\.\d+\S*", env["python"])

    def test_a_library_off_its_pin_is_named(self):
        older = {**self.PINS, "numpy": "2.4.6"}
        env = _load_gate().environment(version=lambda name: older[name])
        assert env["off_the_windows_pins"] == ["numpy"]

    def test_a_missing_library_is_named(self):
        from importlib import metadata

        def version(name):
            if name == "bottle":
                raise metadata.PackageNotFoundError(name)
            return self.PINS[name]

        env = _load_gate().environment(version=version)
        assert env["libraries"]["bottle"] is None
        assert env["off_the_windows_pins"] == ["bottle"]
