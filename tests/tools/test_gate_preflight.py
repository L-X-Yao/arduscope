"""tools/gate_preflight.py: the checks that stop a doomed gate early.

Every check runs against a fake repository root in tmp_path; the machine
measurements (git, disk, memory, the cgroup cap) are stubbed except where a
test exists to exercise them. No test runs the gate or touches the real
corpora.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
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


# Any other filesystem, as a real mount table has before the cgroup one.
PROC_MOUNT = ("112 80 0:59 / /proc rw,nosuid,nodev,noexec,noatime "
              "shared:16 - proc proc rw")
CGROUP_V1_MOUNT = ("31 22 0:27 / /sys/fs/cgroup/memory rw,nosuid,nodev "
                   "- cgroup cgroup memory")


def _holding(tmp_path, relative: str, pid: int) -> None:
    """A cgroup directory under tmp_path whose cgroup.procs names pid."""
    directory = tmp_path / relative
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cgroup.procs").write_text(f"{pid}\n", encoding="ascii")


def _machine(tmp_path, cgroup="/run-1.scope", proc="v2", memory_max="max",
             memory_swap_max="max", named=None, mounts=None, where=None,
             other=(), members=(), strangers=()):
    """A mount table and a proc file naming a cgroup, with the cgroup's two
    files somewhere under tmp_path. Returns the paths of the proc file and
    of the table.

    mounts is [(mount point, mount root)] as the table lists them, the
    default being one cgroup2 mount at <tmp>/cgroup over the whole
    hierarchy. The two files go to `where`, by default the directory that
    mount keeps the cgroup in. proc "v2" names the cgroup on cgroup v2,
    "v1" on v1 only, None writes no file. A limit of None leaves its file
    out; `named` is what the proc file calls the cgroup when that differs
    from where it is. `other` lists more lines of the table.

    `members` are cgroups, by their path under tmp_path, whose cgroup.procs
    names the process running the test, as the reader's own does;
    `strangers` are ones that hold someone else.
    """
    hierarchy = tmp_path / "cgroup"
    places = mounts if mounts is not None else [(str(hierarchy), "/")]
    directory = where if where is not None else hierarchy / cgroup.strip("/")
    directory.mkdir(parents=True, exist_ok=True)
    for relative in members:
        _holding(tmp_path, relative, os.getpid())
    for relative in strangers:
        _holding(tmp_path, relative, 1)
    for name, value in (("memory.max", memory_max),
                        ("memory.swap.max", memory_swap_max)):
        if value is not None:
            (directory / name).write_text(value + "\n", encoding="ascii")
    table = tmp_path / "mountinfo"
    table.write_text("\n".join(
        [PROC_MOUNT, *other]
        + [f"30 22 0:26 {root} {at} rw,nosuid,nodev,noexec,relatime "
           f"shared:4 - cgroup2 cgroup rw,nsdelegate"
           for at, root in places]) + "\n", encoding="ascii")
    path = tmp_path / "self.cgroup"
    if proc == "v2":
        # A cgroup v1 line comes first, as under systemd: the reader has
        # to take the "0::" one.
        path.write_text(f"11:cpu,cpuacct:{named or cgroup}\n"
                        f"0::{named or cgroup}\n", encoding="ascii")
    elif proc == "v1":
        path.write_text("12:memory:/user.slice/1000.slice\n",
                        encoding="ascii")
    return str(path), str(table)


def _read(tmp_path, **kwargs) -> dict:
    named, table = _machine(tmp_path, **kwargs)
    return gp.cgroup_memory_limits(named, table)


class TestCgroupReader:
    """cgroup_memory_limits on files laid out the way cgroup v2 and the
    kernel's mount table write them. Nothing here reads this machine."""

    def test_byte_counts_are_read(self, tmp_path):
        assert _read(tmp_path, memory_max="12884901888",
                     memory_swap_max="0") == {
            "cgroup": "/run-1.scope", "memory_max": 12884901888,
            "memory_swap_max": 0}

    def test_max_is_no_limit(self, tmp_path):
        limits = _read(tmp_path, memory_max="max", memory_swap_max="0")
        assert limits["memory_max"] is None and "error" in limits
        assert ("memory.max is max in "
                + str(tmp_path / "cgroup" / "run-1.scope")
                + ": no memory cap") in limits["error"]
        assert "MemorySwapMax" in limits["error"]  # the way out is named

    def test_an_unlimited_swap_keeps_the_memory_limit(self, tmp_path):
        limits = _read(tmp_path, memory_max="12884901888",
                       memory_swap_max="max")
        assert limits["memory_max"] == 12884901888
        assert limits["memory_swap_max"] is None
        assert "memory.swap.max is max" in limits["error"]

    def test_a_missing_file_is_unreadable(self, tmp_path):
        limits = _read(tmp_path, memory_max="12884901888",
                       memory_swap_max=None)
        assert limits["memory_max"] == 12884901888
        assert limits["memory_swap_max"] is None
        assert ("cannot read memory.swap.max in "
                + str(tmp_path / "cgroup" / "run-1.scope")) in limits["error"]

    def test_a_cgroup_missing_under_the_mount_is_unreadable(self, tmp_path):
        """A container with a namespace of its own names its cgroup by a
        path the mount does not have."""
        limits = _read(tmp_path, named="/system.slice/docker-1.scope",
                       memory_max="1", memory_swap_max="0")
        assert limits["cgroup"] == "/system.slice/docker-1.scope"
        assert limits["memory_max"] is None
        assert "cannot read memory.max in" in limits["error"]

    def test_a_v1_hierarchy_is_not_read(self, tmp_path):
        limits = _read(tmp_path, proc="v1", memory_max="1",
                       memory_swap_max="0")
        assert limits["memory_max"] is None and limits["cgroup"] is None
        assert "no cgroup v2 hierarchy" in limits["error"]

    def test_a_missing_proc_file_is_unreadable(self, tmp_path):
        limits = _read(tmp_path, proc=None, memory_max="1",
                       memory_swap_max="0")
        assert limits["memory_max"] is None
        assert "cannot read" in limits["error"]

    def test_something_other_than_bytes_is_unreadable(self, tmp_path):
        limits = _read(tmp_path, memory_max="12 GB", memory_swap_max="0")
        assert limits["memory_max"] is None
        assert "'12 GB'" in limits["error"] and "not a byte count" in \
            limits["error"]

    def test_a_cgroup_at_the_root_of_the_hierarchy_is_the_mount_point(
            self, tmp_path):
        limits = _read(tmp_path, cgroup="/", memory_max="1",
                       memory_swap_max="0")
        assert limits == {"cgroup": "/", "memory_max": 1, "memory_swap_max": 0}


class TestWhereTheCgroupIs:
    """The cgroup2 filesystem is not always at /sys/fs/cgroup: the reader
    follows /proc/self/mountinfo, and refuses when it cannot."""

    def test_a_hybrid_mount_point_is_followed(self, tmp_path):
        """systemd's hybrid hierarchy mounts cgroup2 at
        /sys/fs/cgroup/unified, next to the v1 controllers."""
        unified = tmp_path / "cgroup" / "unified"
        limits = _read(tmp_path, mounts=[(str(unified), "/")],
                       where=unified / "run-1.scope",
                       other=[CGROUP_V1_MOUNT],
                       memory_max="1", memory_swap_max="0")
        assert limits == {"cgroup": "/run-1.scope", "memory_max": 1,
                          "memory_swap_max": 0}

    def test_a_mount_root_of_its_own_is_where_the_path_starts(self, tmp_path):
        """A container sharing the host's cgroup namespace names its cgroup
        by the host's path; what is mounted is only that subtree."""
        limits = _read(
            tmp_path, cgroup="/system.slice/docker-1.scope/run-1.scope",
            mounts=[(str(tmp_path / "cgroup"), "/system.slice/docker-1.scope")],
            where=tmp_path / "cgroup" / "run-1.scope",
            memory_max="1", memory_swap_max="0")
        assert limits == {
            "cgroup": "/system.slice/docker-1.scope/run-1.scope",
            "memory_max": 1, "memory_swap_max": 0}

    def test_a_mount_root_with_a_trailing_slash_joins_the_same_way(
            self, tmp_path):
        """/machine/ is /machine: the path under it was being cut one
        character short, /machine/run giving /cgrun."""
        mounts = [("/cg", "/machine/")]
        assert gp._cgroup_directory("/machine/run", mounts, 1) == "/cg/run"
        assert gp._cgroup_directory("/machine", mounts, 1) == "/cg"
        hierarchy = tmp_path / "cgroup"
        limits = _read(tmp_path, cgroup="/machine/run-1.scope",
                       mounts=[(str(hierarchy), "/machine/")],
                       where=hierarchy / "run-1.scope",
                       memory_max="1", memory_swap_max="0")
        assert limits == {"cgroup": "/machine/run-1.scope", "memory_max": 1,
                          "memory_swap_max": 0}

    def test_the_mount_rooted_nearest_takes_precedence(self, tmp_path):
        """Of two mounts that both show the cgroup, the one rooted nearer."""
        cgroup = tmp_path / "cgroup"
        tight = cgroup / "tight"
        limits = _read(
            tmp_path, cgroup="/machine/run-1.scope",
            mounts=[(str(cgroup), "/"), (str(tight), "/machine")],
            where=tight / "run-1.scope", memory_max="1", memory_swap_max="0")
        # Under the first mount the cgroup would be at machine/run-1.scope,
        # which was never written: only the second mount answers.
        assert not (cgroup / "machine").exists()
        assert (tight / "run-1.scope" / "memory.max").exists()
        assert limits == {"cgroup": "/machine/run-1.scope", "memory_max": 1,
                          "memory_swap_max": 0}

    def test_a_cgroup_under_no_mount_root_is_unreadable(self, tmp_path):
        limits = _read(tmp_path, cgroup="/elsewhere/run-1.scope",
                       mounts=[(str(tmp_path / "cgroup"), "/machine/slice")],
                       memory_max="1", memory_swap_max="0")
        assert limits["memory_max"] is None
        assert ("/elsewhere/run-1.scope is under none of the mounted cgroup2 "
                in limits["error"])
        assert "shows /machine/slice" in limits["error"]


class TestInheritedFromOutsideTheNamespace:
    """cgroup_namespaces(7), the anomaly it shows. Inside a cgroup namespace,
    /proc/self/cgroup names the process's cgroup from the namespace's root
    down, "/" for the root itself. A cgroup2 mount made before the namespace
    was shows its root as /.., one .. for each level it lies above the
    namespace's root, and the directory at the mount point is an ancestor's.
    The process's cgroup is below it under a name the proc file no longer
    gives, and is told by its cgroup.procs."""

    def test_the_cgroup_holding_the_process_is_found_one_level_down(
            self, tmp_path):
        """The man page's session: /proc/self/cgroup says "/" and mountinfo
        says "/.. /sys/fs/cgroup"."""
        hierarchy = tmp_path / "cgroup"
        limits = _read(tmp_path, cgroup="/",
                       mounts=[(str(hierarchy), "/..")],
                       where=hierarchy / "init.scope",
                       members=["cgroup/init.scope"],
                       strangers=["cgroup/other.scope",
                                  "cgroup/system.slice"],
                       memory_max="12884901888", memory_swap_max="0")
        assert limits == {"cgroup": "/", "memory_max": 12884901888,
                          "memory_swap_max": 0}

    def test_a_cgroup_below_the_namespace_root_is_found_through_the_levels(
            self, tmp_path):
        """Two levels outside, and a sibling that holds the same relative
        path but not this process."""
        hierarchy = tmp_path / "cgroup"
        limits = _read(
            tmp_path, cgroup="/app.slice/run-1.scope",
            mounts=[(str(hierarchy), "/../..")],
            where=hierarchy / "user.slice" / "user-1000.slice" / "app.slice"
            / "run-1.scope",
            members=["cgroup/user.slice/user-1000.slice/app.slice/"
                     "run-1.scope"],
            strangers=["cgroup/system.slice/app.slice/run-1.scope"],
            memory_max="1", memory_swap_max="0")
        assert limits == {"cgroup": "/app.slice/run-1.scope",
                          "memory_max": 1, "memory_swap_max": 0}

    def test_when_no_directory_under_the_mount_holds_the_process(
            self, tmp_path):
        hierarchy = tmp_path / "cgroup"
        limits = _read(tmp_path, cgroup="/", mounts=[(str(hierarchy), "/..")],
                       where=hierarchy / "init.scope",
                       strangers=["cgroup/other.scope"],
                       memory_max="1", memory_swap_max="0")
        assert limits["memory_max"] is None
        error = limits["error"]
        assert ("/ is under none of the mounted cgroup2 filesystems" in error
                and f"under {hierarchy}, whose root is outside this cgroup "
                "namespace, no directory holds this process" in error)

    def test_a_mount_rooted_beside_the_namespace_is_not_searched(
            self, tmp_path):
        """../other is an ancestor's other child: the process's cgroup is
        not under it, whatever its cgroup.procs say."""
        hierarchy = tmp_path / "cgroup"
        limits = _read(tmp_path, cgroup="/",
                       mounts=[(str(hierarchy), "/../other")],
                       where=hierarchy / "run-1.scope",
                       members=["cgroup/run-1.scope"],
                       memory_max="1", memory_swap_max="0")
        assert limits["memory_max"] is None

    def test_a_mount_of_the_namespace_itself_is_preferred(self, tmp_path):
        """A mount rooted in the namespace answers first: the search under
        an inherited one is not made."""
        hierarchy = tmp_path / "cgroup"
        limits = _read(
            tmp_path, cgroup="/run-1.scope",
            mounts=[(str(hierarchy), "/.."), (str(hierarchy / "own"), "/")],
            where=hierarchy / "own" / "run-1.scope",
            memory_max="1", memory_swap_max="0")
        assert limits == {"cgroup": "/run-1.scope", "memory_max": 1,
                          "memory_swap_max": 0}

    def test_the_search_is_bounded(self, tmp_path, monkeypatch):
        """A hierarchy of thousands of cgroups under an inherited mount is
        not walked to the end: the limit is left unread, and said so."""
        monkeypatch.setattr(gp, "MAX_SEARCHED", 2)
        hierarchy = tmp_path / "cgroup"
        _holding(tmp_path, "cgroup/a", os.getpid())
        _holding(tmp_path, "cgroup/b", 1)
        _holding(tmp_path, "cgroup/c", 1)
        named, table = _machine(tmp_path, cgroup="/",
                                mounts=[(str(hierarchy), "/..")],
                                where=hierarchy / "a", memory_max="1",
                                memory_swap_max="0")
        limits = gp.cgroup_memory_limits(named, table)
        assert limits["memory_max"] is None
        assert "no directory holds this process" in limits["error"]

    def test_no_cgroup2_mount_is_not_read(self, tmp_path):
        limits = _read(tmp_path, mounts=[], other=[CGROUP_V1_MOUNT],
                       memory_max="1", memory_swap_max="0")
        assert limits["memory_max"] is None
        assert "no cgroup2 filesystem is mounted (cgroup v1?)" in \
            limits["error"]

    def test_a_space_in_the_mount_point_is_unescaped(self, tmp_path):
        """The kernel writes a space in a mount point as \040."""
        spaced = str(tmp_path / "with space")
        limits = _read(tmp_path, mounts=[(spaced.replace(" ", "\\040"), "/")],
                       where=tmp_path / "with space" / "run-1.scope",
                       memory_max="1", memory_swap_max="0")
        assert limits["memory_max"] == 1

    def test_an_unreadable_mount_table_is_unreadable(self, tmp_path):
        named, _ = _machine(tmp_path)
        limits = gp.cgroup_memory_limits(named, str(tmp_path / "no-table"))
        assert limits["memory_max"] is None
        assert "cannot list the cgroup2 mounts" in limits["error"]

    def test_the_table_is_read_the_way_the_kernel_writes_it(self, tmp_path):
        """Optional fields before the separator, an escaped root, and lines
        of other filesystems, among them a cgroup v1 one."""
        table = tmp_path / "mountinfo"
        table.write_text(
            "25 1 0:21 / /sys rw,nosuid,nodev,noexec shared:5 - sysfs sysfs rw\n"
            "30 22 0:26 /with\\040space /sys/fs/cgroup rw shared:4 master:7 - "
            "cgroup2 cgroup rw\n"
            "31 22 0:27 / /sys/fs/cgroup/memory rw - cgroup cgroup memory\n"
            "32 22 0:28 /docker/abc /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            "garbled\n", encoding="ascii")
        assert gp.cgroup2_mounts(str(table)) == [
            ("/sys/fs/cgroup", "/with space"),
            ("/sys/fs/cgroup", "/docker/abc")]


class TestTheRecordNamesTheCap:
    """environment() is what the gate writes into its result."""

    def test_the_entry_is_the_caps_the_gate_hands_over(self, monkeypatch):
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: pytest.fail(
            "read again"))
        env = gate.environment(version=lambda name: None, caps=dict(CAPPED))
        assert env["cgroup"] == CAPPED

    def test_a_caller_with_no_caps_gets_a_reading_of_its_own(self, monkeypatch):
        """Not how a record is made: the gate reads once and hands it over."""
        monkeypatch.setattr(gp, "cgroup_memory_limits", _uncapped)
        entry = gate.environment(version=lambda name: None)["cgroup"]
        assert entry["memory_max"] is None and "no memory cap" in entry["error"]

    def test_the_preflight_checks_the_caps_it_is_given_and_reads_no_other(
            self, repo, monkeypatch, capsys):
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: pytest.fail(
            "read again"))
        assert gp.run_checks(sitl_dir=str(repo / "ardupilot"),
                             steps=["ci_local"], corpora={},
                             repo_root=str(repo), caps=dict(CAPPED)) == []
        assert "PASS cgroup: memory.max 12.0 GB" in capsys.readouterr().out
        assert "cgroup" in gp.run_checks(
            sitl_dir=str(repo / "ardupilot"), steps=["ci_local"], corpora={},
            repo_root=str(repo), caps=_uncapped())

    def test_the_record_holds_the_reading_the_preflight_checked(
            self, repo, monkeypatch):
        """The caps were read twice, once for the preflight and once for the
        record, and nothing checked the second: a pass record could carry a
        None, or a cap that was lifted in between."""
        readings = iter([dict(CAPPED), _uncapped()])
        monkeypatch.setattr(gp, "cgroup_memory_limits", lambda: next(readings))
        _sitl_binaries(repo)
        monkeypatch.setattr(gate, "REPO_ROOT", str(repo))
        monkeypatch.setattr(gate, "GATE_DIR", str(repo / "gate"))
        monkeypatch.setattr(gate, "ACCEPTED_DIR", str(repo / "gate" / "a"))
        monkeypatch.setattr(gate, "head_commit", lambda: "abc")
        monkeypatch.setattr(gate, "tree_is_dirty", lambda: False)
        monkeypatch.setattr(gate, "_windows_pins", lambda: {})  # not this repo
        monkeypatch.setattr(gate, "RUNNERS", {
            name: (lambda args, log_dir: {"status": "pass"})
            for name in gate.STEPS})
        # The preflight runs for real, on the fake repository: everything
        # it measures is stubbed by the fixture but the caps.
        args = SimpleNamespace(steps=list(gate.STEPS),
                               sitl_dir=str(repo / "ardupilot"),
                               corpora={"field": str(repo / "logs")}, corpus=None)
        assert gate.run_gate(args) == 0
        record = json.loads(Path(gate.result_path("abc")).read_text("utf-8"))
        assert record["status"] == "pass"
        assert record["environment"]["cgroup"] == CAPPED




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
