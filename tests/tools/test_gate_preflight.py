"""tools/gate_preflight.py: the checks that stop a doomed gate early.

Every check runs against a fake repository root in tmp_path; the machine
measurements (git, disk, memory) are stubbed except where a test exists to
exercise them. No test runs the gate or touches the real corpora.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

from tests._repo import TOOLS_DIR

import pytest

sys.path.insert(0, str(TOOLS_DIR))

import gate_preflight as gp  # noqa: E402
import release_gate as gate  # noqa: E402

pytestmark = pytest.mark.fast

GB = 1024 ** 3


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repository root: clean tree, local_paths.json, one corpus."""
    (tmp_path / "local_paths.json").write_text("{}", encoding="utf-8")
    corpus = tmp_path / "logs"
    corpus.mkdir()
    (corpus / "flight.BIN").write_bytes(b"")
    monkeypatch.setattr(gp, "_tree_state", lambda root: (False, "clean"))
    monkeypatch.setattr(gp, "disk_free_bytes", lambda root: 100 * GB)
    monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)
    monkeypatch.delenv("ARDUSCOPE_LOG_CACHE", raising=False)
    return tmp_path


def _sitl_binaries(tmp_path) -> str:
    bin_dir = tmp_path / "ardupilot" / "build" / "sitl" / "bin"
    bin_dir.mkdir(parents=True)
    for name in gp.SITL_BINARIES:
        path = bin_dir / name
        path.write_bytes(b"")
        path.chmod(0o755)
    return str(bin_dir)


def _run(repo, steps=("ci_local",), corpora=None, sitl_dir=None,
         from_config=False):
    return gp.run_checks(
        sitl_dir=sitl_dir if sitl_dir is not None else str(repo / "ardupilot"),
        steps=list(steps),
        corpora={"field": str(repo / "logs")}
        if corpora is None else corpora,
        repo_root=str(repo),
        corpora_from_config=from_config)


class TestChecks:

    def test_all_present_passes(self, repo, capsys):
        _sitl_binaries(repo)
        assert _run(repo, steps=gp.STEPS, from_config=True) == []
        out = capsys.readouterr().out
        assert out.count("PASS ") == 6  # tree, local_paths, sitl, corpus,
        # disk, memory
        assert "FAIL" not in out

    def test_a_missing_sitl_binary_fails(self, repo, capsys):
        failures = _run(repo, steps=("sitl",), sitl_dir=str(repo / "ardupilot"))
        assert "sitl" in failures
        out = capsys.readouterr().out
        assert "arducopter" in out and "arduplane" in out

    def test_a_sitl_binary_without_the_executable_bit_fails(self, repo):
        path = repo / "ardupilot" / "build" / "sitl" / "bin" / "arducopter"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        path.chmod(0o644)
        failures = _run(repo, steps=("sitl",), sitl_dir=str(repo / "ardupilot"))
        assert "sitl" in failures

    def test_a_missing_sitl_binary_never_blocks_a_corpus_run(self, repo,
                                                             capsys):
        """A partial run checks only what its steps need."""
        failures = _run(repo, steps=("corpus",),
                        sitl_dir=str(repo / "ardupilot"))
        assert failures == []
        out = capsys.readouterr().out
        assert "FAIL" not in out and "sitl:" not in out


class TestLocalPaths:
    """local_paths.json is only needed when a step reads it: snapshots, or
    a corpus taken from the file rather than a --corpus override."""

    def test_a_snapshots_run_needs_it(self, repo):
        os.remove(repo / "local_paths.json")
        assert "local_paths" in _run(repo, steps=("snapshots",))

    def test_a_corpus_run_from_the_config_needs_it(self, repo):
        os.remove(repo / "local_paths.json")
        failures = _run(repo, steps=("corpus",), from_config=True)
        assert failures == ["local_paths"]

    def test_a_corpus_run_with_an_override_skips_it(self, repo):
        os.remove(repo / "local_paths.json")
        assert _run(repo, steps=("corpus",)) == []

    def test_a_sitl_run_skips_it(self, repo):
        _sitl_binaries(repo)
        os.remove(repo / "local_paths.json")
        assert _run(repo, steps=("sitl",)) == []

    def test_a_config_corpus_with_the_file_present_passes(self, repo):
        assert _run(repo, steps=("corpus",), from_config=True) == []

    def test_a_missing_local_paths_file_fails(self, repo):
        os.remove(repo / "local_paths.json")
        assert "local_paths" in _run(repo, steps=("snapshots",))


class TestCorpora:

    def test_a_missing_corpus_directory_fails(self, repo):
        failures = _run(repo, steps=("corpus",),
                        corpora={"field": str(repo / "nowhere")})
        assert "corpus field" in failures

    def test_an_empty_corpus_fails(self, repo):
        empty = repo / "empty"
        empty.mkdir()
        failures = _run(repo, steps=("corpus",), corpora={"field": str(empty)})
        assert "corpus field" in failures

    def test_a_corpus_without_a_matching_name_fails(self, repo):
        (repo / "logs" / "notes.txt").write_text("x", encoding="utf-8")
        os.remove(repo / "logs" / "flight.BIN")
        failures = _run(repo, steps=("corpus",))
        assert "corpus field" in failures

    def test_a_lowercase_bin_log_is_found(self, repo):
        os.remove(repo / "logs" / "flight.BIN")
        (repo / "logs" / "deep" / "flight.bin").parent.mkdir()
        (repo / "logs" / "deep" / "flight.bin").write_bytes(b"")
        assert _run(repo, steps=("corpus",)) == []

    def test_a_corpus_step_with_none_named_fails(self, repo):
        failures = _run(repo, steps=("corpus",), corpora={})
        assert "corpus" in failures


class TestFailClosed:
    """A check that cannot run is a FAIL, never a pass or a traceback."""

    def test_a_directory_that_is_not_a_repository_fails(self, tmp_path,
                                                        monkeypatch, capsys):
        monkeypatch.setattr(gp, "disk_free_bytes", lambda root: 100 * GB)
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)
        failures = gp.run_checks(sitl_dir=str(tmp_path / "a"),
                                 steps=["ci_local"], corpora={},
                                 repo_root=str(tmp_path))
        assert "tree" in failures
        assert "git" in capsys.readouterr().out

    def test_a_missing_git_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gp, "disk_free_bytes", lambda root: 100 * GB)
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)

        def no_git(command, **kwargs):
            raise OSError("git: not found")

        monkeypatch.setattr(gp.subprocess, "run", no_git)
        failures = gp.run_checks(sitl_dir=str(tmp_path / "a"),
                                 steps=["ci_local"], corpora={},
                                 repo_root=str(tmp_path))
        assert "tree" in failures

    def test_tree_dirty_returns_none_outside_a_repository(self, tmp_path):
        assert gp.tree_dirty(str(tmp_path)) is None

    def test_low_disk_fails(self, repo, monkeypatch):
        monkeypatch.setattr(gp, "disk_free_bytes", lambda root: 5 * GB)
        assert "disk" in _run(repo)

    def test_low_memory_fails(self, repo, monkeypatch):
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: 2 * GB)
        assert "memory" in _run(repo)

    def test_unreadable_memory_fails_instead_of_passing(self, repo,
                                                        monkeypatch, capsys):
        """A machine that hides MemAvailable is not a machine with memory."""
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: None)
        assert "memory" in _run(repo)
        assert "MemAvailable" in capsys.readouterr().out


class TestDisk:
    def _fake_disk(self, monkeypatch, repo, sizes):
        monkeypatch.setattr(gp.shutil, "disk_usage",
                            lambda path: SimpleNamespace(
                                free=sizes.get(os.path.abspath(str(path)),
                                               100 * GB)))
        monkeypatch.setattr(gp, "_tree_state", lambda root: (False, "clean"))
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)
        return repo

    def test_the_cache_disk_counts_when_it_is_elsewhere(self, tmp_path,
                                                        monkeypatch):
        repo = self._fake_disk(monkeypatch, tmp_path,
                               {str(tmp_path): 100 * GB,
                                str(tmp_path / "data"): 5 * GB})
        (repo / "data").mkdir()  # the cache's nearest existing ancestor
        monkeypatch.setenv("ARDUSCOPE_LOG_CACHE",
                           str(repo / "data" / "cache" / "not" / "there"))
        assert "disk" in _run(repo)

    def test_without_the_variable_only_the_root_counts(self, repo):
        assert _run(repo) == []


class TestTheTreeCheck:
    """The one check that runs git for real, on a throwaway repository."""

    def test_it_reads_git_status(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        assert not gp.tree_dirty(str(tmp_path))
        (tmp_path / "stray.txt").write_text("x", encoding="utf-8")
        assert gp.tree_dirty(str(tmp_path))


class TestWiring:
    def test_the_steps_match_the_gate(self):
        assert gp.STEPS == gate.STEPS

    def test_the_default_sitl_dir_matches_the_gate(self):
        assert gp.DEFAULT_SITL_DIR == gate.DEFAULT_SITL_DIR


class TestCommandLine:

    def test_passing_checks_exit_zero(self, repo, monkeypatch, capsys):
        monkeypatch.setattr(gp, "REPO_ROOT", str(repo))
        monkeypatch.setattr(gp.local_paths, "corpora",
                            lambda: {"field": str(repo / "logs")})
        assert gp.main(["--steps", "ci_local"]) == 0
        assert "FAIL" not in capsys.readouterr().out

    def test_a_failing_check_exits_one(self, repo, monkeypatch):
        monkeypatch.setattr(gp, "REPO_ROOT", str(repo))
        empty = repo / "empty"
        empty.mkdir()
        monkeypatch.setattr(gp.local_paths, "corpora",
                            lambda: {"field": str(empty)})
        assert gp.main(["--steps", "corpus"]) == 1

    def test_a_corpus_override_passes_without_local_paths(self, repo,
                                                          monkeypatch):
        os.remove(repo / "local_paths.json")
        monkeypatch.setattr(gp, "REPO_ROOT", str(repo))
        monkeypatch.setattr(gp.local_paths, "corpora", lambda: {})
        assert gp.main(["--steps", "corpus",
                        "--corpus", f"field={repo / 'logs'}"]) == 0

    def test_an_unknown_step_is_refused(self):
        with pytest.raises(SystemExit):
            gp.main(["--steps", "corpus,typo"])
