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
"""

from __future__ import annotations

import argparse
import os
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
# fills mid-scan, so the smaller free space of the two governs.
MIN_FREE_DISK_GB = 10.0
# The 2026-11 gate runs peaked at 7.43 GB under the operator's 12 GB cap
# (measured then, not recorded in the repository). With much less than
# 8 GB available the OOM killer can land mid-corpus, which would read as a
# gate failure rather than a full machine.
MIN_AVAILABLE_MEM_GB = 8.0

# The binaries _launch() in tests/sitl/generate_logs.py flies, under
# <sitl_dir>/build/sitl/bin/.
SITL_BINARIES = ("arducopter", "arduplane")
# The globs tools/baseline_run.py scans by default (--pattern).
CORPUS_PATTERNS = ("*.bin", "*.BIN")


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


def disk_free_bytes(repo_root: str) -> int:
    """Free bytes where the scan writes: the repo root, and the cache disk
    when ARDUSCOPE_LOG_CACHE moves the cache off it."""
    free = shutil.disk_usage(repo_root).free
    cache = os.environ.get("ARDUSCOPE_LOG_CACHE")
    if cache:
        probe = os.path.abspath(cache)
        while not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                probe = ""
                break
            probe = parent
        if probe:
            free = min(free, shutil.disk_usage(probe).free)
    return free


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
               corpora_from_config: bool = True) -> list[str]:
    """Print one PASS/FAIL line per check; return the failed check names.

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

    free = disk_free_bytes(repo_root)
    report(free >= MIN_FREE_DISK_GB * 1024 ** 3, "disk",
           f"{free / 1024 ** 3:.1f} GB free at {repo_root} "
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
