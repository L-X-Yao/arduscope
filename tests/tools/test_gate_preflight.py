"""tools/gate_preflight.py: the checks that stop a doomed gate early.

Every check runs against a fake repository root in tmp_path; the machine
measurements (git, disk, memory, the cgroup cap) are stubbed except where a
test exists to exercise them. No test runs the gate or touches the real
corpora.
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

# A run under systemd-run --scope -p MemoryMax=12G -p MemorySwapMax=0.
CAPPED = {"cgroup": "/user.slice/run-1234.scope", "memory_max": 12 * GB,
          "memory_swap_max": 0}


def _uncapped(**overrides) -> dict:
    """The entry for a run that sits under no cap, as the reader writes it."""
    return {"cgroup": "/init.scope", "memory_max": None,
            "memory_swap_max": None,
            "error": "memory.max is max in /sys/fs/cgroup/init.scope: no "
                     "memory cap. Start the gate under one",
            **overrides}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake repository root: clean tree, local_paths.json, one corpus."""
    (tmp_path / "local_paths.json").write_text("{}", encoding="utf-8")
    corpus = tmp_path / "logs"
    corpus.mkdir()
    (corpus / "flight.BIN").write_bytes(b"")
    monkeypatch.setattr(gp, "_tree_state", lambda root: (False, "clean"))
    monkeypatch.setattr(gp.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=100 * GB))
    monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)
    monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: dict(CAPPED))
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
        assert out.count("PASS ") == 7  # tree, local_paths, sitl, corpus,
        # disk, memory, cgroup
        assert "FAIL" not in out

    def test_the_cap_is_checked_whatever_the_steps(self, repo, monkeypatch):
        """It is not a step's need but the run's: a partial run gets it too."""
        monkeypatch.setattr(gp, "cgroup_memory_limits", _uncapped)
        assert "cgroup" in _run(repo, steps=("sitl",))

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

    def test_a_ci_local_run_skips_it(self, repo):
        os.remove(repo / "local_paths.json")
        assert _run(repo, steps=("ci_local",)) == []

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
        monkeypatch.setattr(gp.shutil, "disk_usage",
                            lambda path: SimpleNamespace(free=100 * GB))
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)
        failures = gp.run_checks(sitl_dir=str(tmp_path / "a"),
                                 steps=["ci_local"], corpora={},
                                 repo_root=str(tmp_path))
        assert "tree" in failures
        assert "git" in capsys.readouterr().out

    def test_a_missing_git_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gp.shutil, "disk_usage",
                            lambda path: SimpleNamespace(free=100 * GB))
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
        monkeypatch.setattr(gp.shutil, "disk_usage",
                            lambda path: SimpleNamespace(free=5 * GB))
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
    def _stub_disk(self, monkeypatch, repo, sizes):
        monkeypatch.setattr(gp.shutil, "disk_usage",
                            lambda path: SimpleNamespace(
                                free=sizes.get(os.path.normpath(
                                    os.path.abspath(str(path))),
                                    100 * GB)))
        monkeypatch.setattr(gp, "_tree_state", lambda root: (False, "clean"))
        monkeypatch.setattr(gp, "available_memory_bytes", lambda: 16 * GB)
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: dict(CAPPED))
        return repo

    def test_the_cache_disk_counts_when_it_is_elsewhere(self, tmp_path,
                                                        monkeypatch, capsys):
        repo = self._stub_disk(monkeypatch, tmp_path,
                               {str(tmp_path): 100 * GB,
                                str(tmp_path / "data"): 5 * GB})
        (repo / "data").mkdir()  # the cache's nearest existing ancestor
        monkeypatch.setenv("ARDUSCOPE_LOG_CACHE",
                           str(repo / "data" / "cache" / "not" / "there"))
        assert "disk" in _run(repo, steps=("corpus",))
        assert str(repo / "data") in capsys.readouterr().out  # the binding disk

    def test_a_relative_cache_path_resolves_against_the_repo_root(
            self, tmp_path, monkeypatch, capsys):
        """The gate runs its consumers from the repository root, so a
        relative ARDUSCOPE_LOG_CACHE cannot follow the caller's cwd."""
        repo = self._stub_disk(monkeypatch, tmp_path,
                               {str(tmp_path): 100 * GB,
                                str(tmp_path / "data"): 5 * GB})
        (repo / "data").mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("ARDUSCOPE_LOG_CACHE", "data/cache/not/there")
        assert "disk" in _run(repo, steps=("corpus",))
        assert str(repo / "data") in capsys.readouterr().out

    def test_a_sitl_run_ignores_the_cache_disk(self, tmp_path, monkeypatch):
        """The SITL flights write no parse cache, so an external cache disk
        with room to spare for nothing must not block them."""
        repo = self._stub_disk(monkeypatch, tmp_path,
                               {str(tmp_path): 100 * GB,
                                str(tmp_path / "data"): 5 * GB})
        _sitl_binaries(repo)
        monkeypatch.setenv("ARDUSCOPE_LOG_CACHE",
                           str(repo / "data" / "cache" / "not" / "there"))
        assert _run(repo, steps=("sitl",)) == []

    def test_an_unmeasurable_disk_fails_with_the_path(self, repo,
                                                      monkeypatch, capsys):
        def denied(path):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(gp.shutil, "disk_usage", denied)
        assert "disk" in _run(repo)
        assert str(repo) in capsys.readouterr().out

    def test_without_the_variable_only_the_root_counts(self, repo):
        assert _run(repo) == []


class TestTheTreeCheck:
    """The one check that runs git for real, on a throwaway repository."""

    def test_it_reads_git_status(self, tmp_path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        assert not gp.tree_dirty(str(tmp_path))
        (tmp_path / "stray.txt").write_text("x", encoding="utf-8")
        assert gp.tree_dirty(str(tmp_path))


class TestCgroupCap:
    """The rule is that the gate runs under a memory cap. Nothing refused a
    run without one, and a record could not say what its run sat under."""

    def test_a_finite_cap_passes_and_is_named(self, repo, capsys):
        assert _run(repo) == []
        assert ("PASS cgroup: memory.max 12.0 GB, memory.swap.max 0.0 GB "
                "in /user.slice/run-1234.scope") in capsys.readouterr().out

    def test_no_cap_fails(self, repo, monkeypatch, capsys):
        """memory.max is max: there is no limit to run under."""
        monkeypatch.setattr(gp, "cgroup_memory_limits", _uncapped)
        assert _run(repo) == ["cgroup"]
        assert "FAIL cgroup: memory.max is max" in capsys.readouterr().out

    def test_unlimited_swap_fails_under_a_memory_cap(self, repo, monkeypatch):
        """MemoryMax alone leaves the swap unlimited: the cap covers both."""
        half = _uncapped(memory_max=12 * GB, memory_swap_max=None,
                         error="memory.swap.max is max in /run-1234.scope")
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: half)
        assert _run(repo, steps=("ci_local",)) == ["cgroup"]

    def test_an_unreadable_limit_fails(self, repo, monkeypatch, capsys):
        unreadable = _uncapped(error="cannot read memory.max in "
                                     "/sys/fs/cgroup/run-1.scope: "
                                     "[Errno 13] Permission denied")
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: unreadable)
        assert _run(repo) == ["cgroup"]
        assert "cannot read memory.max" in capsys.readouterr().out

    def test_a_limit_left_unread_fails_even_when_nothing_says_why(
            self, repo, monkeypatch, capsys):
        """The limits decide, not the message that comes with them."""
        silent = {"cgroup": "/run-1.scope", "memory_max": 12 * GB,
                  "memory_swap_max": None}
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: silent)
        assert _run(repo) == ["cgroup"]
        assert "FAIL cgroup: a memory limit was not read" in \
            capsys.readouterr().out


class TestCgroupReader:
    """cgroup_memory_limits on files laid out the way cgroup v2 writes them.
    Nothing here reads this machine's own cgroup."""

    def _machine(self, tmp_path, cgroup="/run-1.scope", proc="v2",
                 memory_max="max", memory_swap_max="max", named=None):
        """A cgroup2 root with one cgroup's two files. proc "v2" names the
        cgroup on cgroup v2, "v1" on v1 only, None writes no file. A limit of
        None leaves its file out; named is what the proc file calls it when
        that differs from where it is."""
        root = tmp_path / "cgroup"
        directory = root / cgroup.strip("/")
        directory.mkdir(parents=True)
        for name, value in (("memory.max", memory_max),
                            ("memory.swap.max", memory_swap_max)):
            if value is not None:
                (directory / name).write_text(value + "\n", encoding="ascii")
        path = tmp_path / "self.cgroup"
        if proc == "v2":
            # A cgroup v1 line comes first, as under systemd: the reader has
            # to take the "0::" one.
            path.write_text(f"11:cpu,cpuacct:{named or cgroup}\n"
                            f"0::{named or cgroup}\n", encoding="ascii")
        elif proc == "v1":
            path.write_text("12:memory:/user.slice/1000.slice\n",
                            encoding="ascii")
        return str(path), str(root)

    def _read(self, tmp_path, **kwargs) -> dict:
        named, root = self._machine(tmp_path, **kwargs)
        return gp.cgroup_memory_limits(named, root)

    def test_byte_counts_are_read(self, tmp_path):
        assert self._read(tmp_path, memory_max="12884901888",
                          memory_swap_max="0") == {
            "cgroup": "/run-1.scope", "memory_max": 12884901888,
            "memory_swap_max": 0}

    def test_max_is_no_limit(self, tmp_path):
        limits = self._read(tmp_path, memory_max="max", memory_swap_max="0")
        assert limits["memory_max"] is None and "error" in limits
        assert ("memory.max is max in "
                + str(tmp_path / "cgroup" / "run-1.scope")
                + ": no memory cap") in limits["error"]
        assert "MemorySwapMax" in limits["error"]  # the way out is named

    def test_an_unlimited_swap_keeps_the_memory_limit(self, tmp_path):
        limits = self._read(tmp_path, memory_max="12884901888",
                            memory_swap_max="max")
        assert limits["memory_max"] == 12884901888
        assert limits["memory_swap_max"] is None
        assert "memory.swap.max is max" in limits["error"]

    def test_a_missing_file_is_unreadable(self, tmp_path):
        limits = self._read(tmp_path, memory_max="12884901888",
                            memory_swap_max=None)
        assert limits["memory_max"] == 12884901888
        assert limits["memory_swap_max"] is None
        assert ("cannot read memory.swap.max in "
                + str(tmp_path / "cgroup" / "run-1.scope")) in limits["error"]

    def test_a_cgroup_missing_under_the_root_is_unreadable(self, tmp_path):
        """A container can name a cgroup by its path on the host."""
        limits = self._read(tmp_path, named="/system.slice/docker-1.scope",
                            memory_max="1", memory_swap_max="0")
        assert limits["cgroup"] == "/system.slice/docker-1.scope"
        assert limits["memory_max"] is None
        assert "cannot read memory.max in" in limits["error"]

    def test_a_v1_hierarchy_is_not_read(self, tmp_path):
        limits = self._read(tmp_path, proc="v1", memory_max="1",
                            memory_swap_max="0")
        assert limits["memory_max"] is None and limits["cgroup"] is None
        assert "no cgroup v2 hierarchy" in limits["error"]

    def test_a_missing_proc_file_is_unreadable(self, tmp_path):
        limits = self._read(tmp_path, proc=None, memory_max="1",
                            memory_swap_max="0")
        assert limits["memory_max"] is None
        assert "cannot read" in limits["error"]

    def test_something_other_than_bytes_is_unreadable(self, tmp_path):
        limits = self._read(tmp_path, memory_max="12 GB", memory_swap_max="0")
        assert limits["memory_max"] is None
        assert "'12 GB'" in limits["error"] and "not a byte count" in \
            limits["error"]

    def test_the_root_is_joined_without_a_double_slash(self, tmp_path):
        """The cgroup at the root of the mount names itself with one slash."""
        named, root = self._machine(tmp_path, cgroup="/", memory_max="1",
                                    memory_swap_max="0")
        limits = gp.cgroup_memory_limits(named, root.rstrip("/"))
        assert limits == {"cgroup": "/", "memory_max": 1, "memory_swap_max": 0}


class TestTheRecordNamesTheCap:
    """environment() is what the gate writes into its result."""

    def test_the_entry_is_the_readers_entry(self, monkeypatch):
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: dict(CAPPED))
        assert gate.environment(version=lambda name: None)["cgroup"] == CAPPED

    def test_a_run_without_a_cap_says_so_in_the_record(self, monkeypatch):
        """The preflight stops such a run first; were one recorded anyway,
        the entry would not pretend to a limit."""
        monkeypatch.setattr(gp, "cgroup_memory_limits", _uncapped)
        entry = gate.environment(version=lambda name: None)["cgroup"]
        assert entry["memory_max"] is None and "no memory cap" in entry["error"]


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
