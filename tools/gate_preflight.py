#!/usr/bin/env python3
"""Fail fast before the release gate, in seconds instead of half an hour.

A gate run costs about thirty minutes and most of a machine: the corpora are
scanned into a parse cache and the SITL scenarios are flown. A missing SITL
binary or an emptied corpus surfaces twenty minutes in, after the machine is
committed. tools/release_gate.py runs this at the top of run_gate(); it also
runs standalone:

    python3 tools/gate_preflight.py [--sitl-dir DIR] [--steps a,b] \\
        [--corpus NAME=PATH]

Every check prints one PASS or FAIL line and the exit code is 0 only when
all of them pass. There is no --skip flag, on purpose: the gate has none
either, and an escape hatch here would be used exactly once it should not
be. A partial run checks only what its steps need: a missing SITL binary
never blocks a corpus scan, and local_paths.json is required only when a
step reads it -- snapshots, or a corpus named in the file rather than given
with --corpus.

The run also has to sit under a finite cgroup memory cap, on cgroup v2: the
gate's record names the cap it ran under, and a run without one is refused.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_paths  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keep in step with tools/release_gate.py; test_gate_preflight pins the two.
STEPS = ("ci_local", "snapshots", "sitl", "corpus")
DEFAULT_SITL_DIR = "../ardupilot"

# A corpus scan writes its parse cache (ARDUSCOPE_LOG_CACHE, by default
# .log_cache/ at the repository root) plus its reports; the cache alone is
# ~56 GB. When the variable points at another disk, that is the disk that
# fills mid-scan, so when a cache-writing step runs the smaller free space
# of the two disks governs.
MIN_FREE_DISK_GB = 10.0
# The 2026-11 gate runs peaked at 7.43 GB under the operator's 12 GB cap
# (measured then, not recorded in the repository). With much less than
# 8 GB available the OOM killer can land mid-corpus, which would read as a
# gate failure rather than a full machine.
MIN_AVAILABLE_MEM_GB = 8.0

# MemAvailable says what the machine has spare. Only the process's own cgroup
# says what this run may take before the OOM killer comes for it, and a run
# without a cap has no such number. cgroup v2 names the cgroup in
# /proc/self/cgroup as "0::/path"; where that is on the filesystem is a
# question for /proc/self/mountinfo, since the cgroup2 filesystem is not
# always mounted at /sys/fs/cgroup. Its limits are files in the cgroup's
# directory: a byte count, or "max" for no limit.
PROC_CGROUP = "/proc/self/cgroup"
PROC_MOUNTS = "/proc/self/mountinfo"
CGROUP_LIMITS = (("memory_max", "memory.max"),
                 ("memory_swap_max", "memory.swap.max"))
# A mount root of /.., or of so many of them: the mount was made outside the
# process's cgroup namespace, and is rooted above the namespace's root.
OUTSIDE = re.compile(r"(?:/\.\.)+")
# What may be looked at under such a mount for the process's cgroup: one
# count for every directory opened and every directory found in it, over
# all the levels and all the mounts. Once it is spent the search is given
# up, and the limit left unread.
MAX_SEARCHED = 4096

# The binaries _launch() in tests/sitl/generate_logs.py flies, under
# <sitl_dir>/build/sitl/bin/.
SITL_BINARIES = ("arducopter", "arduplane")
# The globs tools/baseline_run.py scans by default (--pattern).
CORPUS_PATTERNS = ("*.bin", "*.BIN")
# The steps whose consumers write the parse cache: the local CI suite and
# the snapshot tests load logs through conftest's fixtures with
# disk_cache=True, and the corpus step passes --disk-cache. The SITL
# flights read their logs with a plain LogReader (lib/pipeline.py) and
# write nothing to it.
CACHE_STEPS = ("ci_local", "snapshots", "corpus")


def tree_dirty(repo_root: str) -> bool | None:
    """Whether git reports changes; None when git cannot answer."""
    return _tree_state(repo_root)[0]


def _tree_state(repo_root: str) -> tuple[bool | None, str]:
    """(dirty, detail); dirty is None when git could not be asked."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"],
                             cwd=repo_root, capture_output=True, text=True)
    except OSError as e:
        return None, str(e)
    if out.returncode != 0:
        return None, (out.stderr.strip() or f"git exited {out.returncode}")
    dirty = bool(out.stdout.strip())
    return dirty, ("clean" if not dirty
                   else "uncommitted changes; a result names a commit")


def disk_free_bytes(repo_root: str,
                    cache_too: bool = True) -> tuple[int, str]:
    """(free bytes, the path they were measured on).

    The repository root always counts; cache_too also measures the disk
    ARDUSCOPE_LOG_CACHE points at -- relative values resolved against
    repo_root, where the gate's consumers run, at the nearest existing
    ancestor -- and reports whichever is smaller. Raises OSError naming
    the path that could not be measured.
    """
    try:
        free = shutil.disk_usage(repo_root).free
    except OSError as e:
        raise OSError(f"{repo_root}: {e}") from None
    free_path = repo_root
    cache = os.environ.get("ARDUSCOPE_LOG_CACHE")
    if cache_too and cache:
        probe = cache if os.path.isabs(cache) else os.path.join(repo_root,
                                                                 cache)
        probe = os.path.normpath(probe)
        while not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                probe = ""
                break
            probe = parent
        if probe:
            try:
                cached = shutil.disk_usage(probe).free
            except OSError as e:
                raise OSError(f"{probe}: {e}") from None
            if cached < free:
                free, free_path = cached, probe
    return free, str(free_path)


def available_memory_bytes() -> int | None:
    """MemAvailable from /proc/meminfo, or None when it cannot be read."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _unescaped(field: str) -> str:
    """A mountinfo field as a path: the kernel writes a space as \040."""
    return re.sub(r"\\([0-7]{3})", lambda found: chr(int(found.group(1), 8)),
                  field)


def cgroup2_mounts(proc_mounts: str = PROC_MOUNTS) -> list[tuple[str, str]]:
    """(mount point, mount root) of every cgroup2 filesystem mounted, in the
    order the kernel lists them. Raises OSError when they cannot be read.

    A mountinfo line is "id parent major:minor root mount-point options
    [optional...] - fstype source super-options", so the root and the mount
    point come before the options and the fstype after the separator.
    """
    try:
        with open(proc_mounts, encoding="ascii", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise OSError(f"{proc_mounts}: {e}") from None
    mounts = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 7 or "-" not in fields:
            continue
        if fields[fields.index("-") + 1] != "cgroup2":
            continue
        mounts.append((_unescaped(fields[4]), _unescaped(fields[3])))
    return mounts


class _Spent(Exception):
    """The budget is used up: the search ends, under every mount at once."""


class _Budget:
    """What may be looked at under mounts inherited from outside the cgroup
    namespace.

    One count for every directory opened and every directory found in one,
    over all the levels and all the mounts; it is kept while they are
    listed, not once a level has been listed in full. A cgroup's files are
    passed over without counting: they are a fixed few dozen, and are never
    opened.

    `left` is what remains. With nothing left no entry is asked for any
    more, so the directories looked at never pass the budget, not even by
    the one entry that would have spent it.
    """

    def __init__(self, left: int):
        self.left = left
        self.stopped = False

    def take(self) -> None:
        """Count one more: a directory opened. With nothing left, the
        search is stopped."""
        if self.left <= 0:
            self.stopped = True
            raise _Spent
        self.left -= 1


def _holds(directory: str, pid: int) -> bool:
    """Whether the cgroup's cgroup.procs names the process. A process is
    in one cgroup of a hierarchy, so that tells which one is its own."""
    try:
        with open(os.path.join(directory, "cgroup.procs"), encoding="ascii",
                  errors="replace") as f:
            return str(pid) in f.read().split()
    except OSError:
        return False


def _searched(directory: str, levels: int, below: str, pid: int,
              budget: _Budget) -> str | None:
    """The process's cgroup `levels` below `directory`, or None when no
    directory there holds it.

    This is for a mount inherited from outside the process's cgroup
    namespace. Such a mount was made before the namespace was, and its
    root lies `levels` above the namespace's root: /proc/self/mountinfo
    shows that root as /.. (cgroup_namespaces(7)), and the directory at
    the mount point is an ancestor's. /proc/self/cgroup names the
    process's cgroup from the namespace's root down, `below`, so the
    levels in between are hidden and have to be looked for: the cgroup is
    under the one directory `levels` down where the cgroup.procs of what
    lies below it names the process.

    One directory is opened at a time, and its entries asked for one by
    one while the budget lasts, so that is what bounds the work: no level
    is ever listed in full to be counted afterwards.
    """
    if levels == 0:
        candidate = directory if below == "/" else directory + below
        return candidate if _holds(candidate, pid) else None
    budget.take()  # opening the directory
    try:
        with os.scandir(directory) as entries:
            while budget.left:
                try:
                    entry = next(entries)
                except StopIteration:
                    return None
                if not entry.is_dir():
                    continue
                budget.left -= 1  # a directory under this one
                found = _searched(entry.path, levels - 1, below, pid,
                                  budget)
                if found is not None:
                    return found
    except OSError:
        return None
    budget.stopped = True  # directories may remain here, not looked at
    return None


def _cgroup_directory(path: str, mounts: list[tuple[str, str]], pid: int,
                      budget: _Budget | None = None) -> str | None:
    """Where the process's cgroup is on the filesystem, or None when no
    cgroup2 mount shows it.

    A mount shows the part of the hierarchy under its mount root. The root
    is / on a workstation, where systemd-run --scope lands in a scope of a
    path of its own. In a container the root can be a path, and that
    subtree, not the hierarchy's root, is what sits at the mount point.
    Under a cgroup namespace an inherited mount has a root of /.., which
    stands for so many levels above the namespace's root: _searched().

    A mount rooted nearest is taken first: of the mounts that show the
    cgroup, its root is the longest.
    """
    budget = budget if budget is not None else _Budget(MAX_SEARCHED)
    mounts = [(point, root.rstrip("/") or "/") for point, root in mounts]
    showing = [(point, root) for point, root in mounts
               if root == "/" or path == root or path.startswith(root + "/")]
    if showing:
        point, root = max(showing, key=lambda mount: len(mount[1]))
        # Under a root of / the path keeps its leading slash, and names the
        # cgroup under the mount point; under any other root it is shortened.
        return point + (path if root == "/" else path[len(root):])
    for point, root in mounts:
        if OUTSIDE.fullmatch(root):
            try:
                found = _searched(point, root.count("/.."), path, pid,
                                  budget)
            except _Spent:
                return None
            if found is not None:
                return found
    return None


def cgroup_memory_limits(proc_cgroup: str = PROC_CGROUP,
                         proc_mounts: str = PROC_MOUNTS) -> dict:
    """The memory caps on this process's own cgroup, or why they are unknown.

    The dict is the gate record's entry as it stands:

        {"cgroup": "/user.slice/user-1000.slice/user@1000.service/"
                   "app.slice/run-1234.scope",
         "memory_max": 12884901888, "memory_swap_max": 0}

    A limit that is missing, unreadable or "max" is None and "error" says
    which and why; nothing here turns an unknown into a number. Only the
    process's own cgroup is read: a cap on an ancestor does not count, since
    the documented command puts the limit on the scope the gate runs in.
    """
    limits = {"cgroup": None, "memory_max": None, "memory_swap_max": None}
    # errors="replace": a path that is not ASCII then names no directory
    # and fails below, as unreadable, rather than raising here.
    try:
        with open(proc_cgroup, encoding="ascii", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {**limits, "error": f"cannot read {proc_cgroup}: {e}"}
    path = next((line[3:] for line in lines if line.startswith("0::")), None)
    if path is None:
        return {**limits, "error": (
            f"{proc_cgroup} names no cgroup v2 hierarchy (cgroup v1?), so "
            f"its memory limit is not read here")}
    limits["cgroup"] = path
    try:
        mounts = cgroup2_mounts(proc_mounts)
    except OSError as e:
        return {**limits, "error": f"cannot list the cgroup2 mounts: {e}"}
    if not mounts:
        return {**limits, "error": (
            f"no cgroup2 filesystem is mounted (cgroup v1?), so the limit "
            f"of {path} is not read here")}
    budget = _Budget(MAX_SEARCHED)
    directory = _cgroup_directory(path, mounts, os.getpid(), budget)
    if directory is None:
        shown = "; ".join(f"{at} shows {of}" for at, of in mounts)
        outside = [at for at, root in mounts if OUTSIDE.fullmatch(root)]
        hidden = ""
        if outside:
            where = f"under {', '.join(outside)}, whose root is outside this "
            hidden = (where + f"cgroup namespace, the search for this "
                      f"process's cgroup was given up after {MAX_SEARCHED} "
                      f"directories looked at"
                      if budget.stopped else
                      where + "cgroup namespace, no directory holds this "
                      "process")
            hidden = "; " + hidden
        return {**limits, "error": (
            f"{path} is under none of the mounted cgroup2 filesystems "
            f"({shown}){hidden}, so its limit is not read here")}
    for key, name in CGROUP_LIMITS:
        try:
            with open(os.path.join(directory, name), encoding="ascii",
                      errors="replace") as f:
                value = f.read().strip()
        except OSError as e:
            return {**limits,
                    "error": f"cannot read {name} in {directory}: {e}"}
        if value == "max":
            return {**limits, "error": (
                f"{name} is max in {directory}: no memory cap. Start the "
                f"gate under one, for example: systemd-run --user --scope "
                f"-p MemoryMax=12G -p MemorySwapMax=0 -- python3 "
                f"tools/release_gate.py")}
        try:
            limits[key] = int(value)
        except ValueError:
            return {**limits, "error": (
                f"{name} is {value!r} in {directory}, not a byte count")}
    return limits


def sitl_bin_dir(sitl_dir: str, repo_root: str) -> str:
    """Where generate_logs.py looks, resolved the way the gate resolves it."""
    root = sitl_dir if os.path.isabs(sitl_dir) else os.path.join(repo_root,
                                                                 sitl_dir)
    return os.path.normpath(os.path.join(root, "build", "sitl", "bin"))


def _corpus_has_log(root: str) -> bool:
    for pattern in CORPUS_PATTERNS:
        for _ in Path(root).rglob(pattern):
            return True
    return False


def run_checks(sitl_dir: str, steps: list[str], corpora: dict[str, str],
               repo_root: str | None = None,
               corpora_from_config: bool = True,
               caps: dict | None = None) -> list[str]:
    """Print one PASS/FAIL line per check; return the failed check names.

    caps are the cgroup memory limits as already read; without them they
    are read here. The gate reads them once and hands the same dict to its
    record, so a record cannot hold a second reading that was never checked.

    corpora_from_config says whether `corpora` came from local_paths.json
    (the gate without --corpus) or from a --corpus override: only in the
    first case does the corpus step need the file to exist.
    """
    repo_root = repo_root or REPO_ROOT
    failures: list[str] = []

    def report(ok: bool, name: str, detail: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    dirty, detail = _tree_state(repo_root)
    if dirty is None:
        report(False, "tree", f"git could not check: {detail}")
    else:
        report(not dirty, "tree", detail)

    needs_paths = "snapshots" in steps or ("corpus" in steps
                                           and corpora_from_config)
    if needs_paths:
        paths_file = os.path.join(repo_root, "local_paths.json")
        if os.path.isfile(paths_file):
            report(True, "local_paths", paths_file)
        else:
            report(False, "local_paths",
                   f"{paths_file} is missing; snapshots and file-named "
                   f"corpora take their logs from it")

    if "sitl" in steps:
        bin_dir = sitl_bin_dir(sitl_dir, repo_root)
        missing = [name for name in SITL_BINARIES
                   if not (os.path.isfile(os.path.join(bin_dir, name))
                           and os.access(os.path.join(bin_dir, name), os.X_OK))]
        if missing:
            report(False, "sitl",
                   f"{', '.join(missing)} not executable in {bin_dir} "
                   f"(build with ./waf copter plane)")
        else:
            report(True, "sitl", f"both binaries executable in {bin_dir}")

    if "corpus" in steps:
        if not corpora:
            report(False, "corpus", "none named in local_paths.json")
        for name, path in sorted(corpora.items()):
            root = path if os.path.isabs(path) else os.path.join(repo_root,
                                                                 path)
            if not os.path.isdir(root):
                report(False, f"corpus {name}", f"{root} is not a directory")
            elif _corpus_has_log(root):
                report(True, f"corpus {name}", f"logs under {root}")
            else:
                report(False, f"corpus {name}",
                       f"no {CORPUS_PATTERNS[0]} or {CORPUS_PATTERNS[1]} "
                       f"under {root}")

    try:
        free, measured_on = disk_free_bytes(
            repo_root, cache_too=bool(set(steps) & set(CACHE_STEPS)))
    except OSError as e:
        report(False, "disk", f"could not measure free space: {e}")
    else:
        report(free >= MIN_FREE_DISK_GB * 1024 ** 3, "disk",
               f"{free / 1024 ** 3:.1f} GB free at {measured_on} "
               f"(need {MIN_FREE_DISK_GB:.0f})")

    available = available_memory_bytes()
    if available is None:
        report(False, "memory",
               "could not read MemAvailable from /proc/meminfo; "
               "refusing to guess")
    else:
        report(available >= MIN_AVAILABLE_MEM_GB * 1024 ** 3, "memory",
               f"{available / 1024 ** 3:.1f} GB available "
               f"(need {MIN_AVAILABLE_MEM_GB:.0f})")

    if caps is None:
        caps = cgroup_memory_limits()
    if caps["memory_max"] is None or caps["memory_swap_max"] is None:
        report(False, "cgroup",
               caps.get("error") or "a memory limit was not read")
    else:
        report(True, "cgroup",
               f"memory.max {caps['memory_max'] / 1024 ** 3:.1f} GB, "
               f"memory.swap.max {caps['memory_swap_max'] / 1024 ** 3:.1f} GB "
               f"in {caps['cgroup']}")

    return failures


def _corpus_arg(value: str) -> tuple[str, str]:
    # Same shape as tools/release_gate.py's --corpus.
    name, _, path = value.partition("=")
    if not name or not path:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    return name, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check what the release gate needs, before it runs")
    parser.add_argument("--sitl-dir", default=DEFAULT_SITL_DIR)
    parser.add_argument("--steps", default=",".join(STEPS),
                        help=f"comma list, default {','.join(STEPS)}")
    parser.add_argument("--corpus", action="append", type=_corpus_arg,
                        metavar="NAME=PATH",
                        help="replaces the corpora in local_paths.json")
    args = parser.parse_args(argv)

    steps = [s for s in args.steps.split(",") if s]
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        parser.error(f"unknown steps: {', '.join(unknown)}")
    corpora = dict(args.corpus) if args.corpus else local_paths.corpora()
    failures = run_checks(sitl_dir=args.sitl_dir, steps=steps,
                          corpora=corpora,
                          corpora_from_config=not args.corpus)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
