#!/usr/bin/env python3
"""Fail fast before the release gate, in seconds instead of half an hour.

A gate run costs about thirty minutes and most of a machine: the corpora are
scanned into a parse cache and the SITL scenarios are flown. A missing SITL
binary or an emptied corpus surfaces twenty minutes in, after the machine is
committed. tools/release_gate.py runs this at the top of run_gate(); it also
runs standalone:

    python3 tools/gate_preflight.py [--sitl-dir DIR] [--steps a,b]

Every check prints one PASS or FAIL line and the exit code is 0 only when
all of them pass. There is no --skip flag, on purpose: the gate has none
either, and an escape hatch here would be used exactly once it should not
be. A partial run checks only what its steps need -- a missing SITL binary
never blocks a corpus scan.
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

# The parse cache alone is ~56 GB and a corpus scan writes more of it plus
# its reports; a disk that fills mid-corpus leaves a baseline nobody can
# trust. Checked at the repository root, where that cache lives.
MIN_FREE_DISK_GB = 10.0
# The 2026-11 gate runs peaked at 7.43 GB under the operator's 12 GB cap
# (measured then, not recorded in the repository). Less than 8 GB available
# and the OOM killer lands mid-corpus, which reads as a gate failure rather
# than a full machine.
MIN_AVAILABLE_MEM_GB = 8.0

# The binaries _launch() in tests/sitl/generate_logs.py flies, under
# <sitl_dir>/build/sitl/bin/.
SITL_BINARIES = ("arducopter", "arduplane")
# The globs tools/baseline_run.py scans by default (--pattern).
CORPUS_PATTERNS = ("*.bin", "*.BIN")


def tree_dirty(repo_root: str) -> bool:
    out = subprocess.run(["git", "status", "--porcelain"],
                         cwd=repo_root, capture_output=True, text=True)
    return bool(out.stdout.strip())


def disk_free_bytes(repo_root: str) -> int:
    return shutil.disk_usage(repo_root).free


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
               repo_root: str | None = None) -> list[str]:
    """Print one PASS/FAIL line per check; return the FAIL lines."""
    repo_root = repo_root or REPO_ROOT
    failures: list[str] = []

    def report(ok: bool, name: str, detail: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    if tree_dirty(repo_root):
        report(False, "tree", "uncommitted changes; a result names a commit")
    else:
        report(True, "tree", "clean")

    paths_file = os.path.join(repo_root, "local_paths.json")
    if os.path.isfile(paths_file):
        report(True, "local_paths", paths_file)
    else:
        report(False, "local_paths",
               f"{paths_file} is missing; the gate takes its corpora from it")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check what the release gate needs, before it runs")
    parser.add_argument("--sitl-dir", default=DEFAULT_SITL_DIR)
    parser.add_argument("--steps", default=",".join(STEPS),
                        help=f"comma list, default {','.join(STEPS)}")
    args = parser.parse_args(argv)

    steps = [s for s in args.steps.split(",") if s]
    unknown = sorted(set(steps) - set(STEPS))
    if unknown:
        parser.error(f"unknown steps: {', '.join(unknown)}")
    failures = run_checks(sitl_dir=args.sitl_dir, steps=steps,
                          corpora=local_paths.corpora())
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
