#!/usr/bin/env python3
"""
Release gate: what has to hold before a tag, in one command, with the result
written down against the commit it ran on.

GitHub CI has no flight logs and no SITL: at v0.9.3 it ran 1239 of 3155 tests.
The defects that shipped in 0.9.x went straight past it. The FFT batch grouping
and the D gain rule passed every automated check, and were found by a corpus
scan and by flying the suggestion. Those were steps someone had to remember.
Here they are one command, and tools/release.py will not tag a commit without a
pass from it.

Steps:
    ci_local    tools/ci_local.py
    snapshots   tests/test_baseline_snapshots.py on the real logs
    sitl        fly the scenarios, then tests/test_sitl_regression.py
    corpus      baseline_run.py on each corpus, against the accepted baseline,
                then check_suggestions.py on what it suggested

Results, in reports/release-gate/<commit>.json:
    pass        tools/release.py will tag this commit
    review      corpus results moved. That may be the point of the release,
                so nothing here can judge it: read the diff, then --accept
    fail        a step failed

A run with --steps goes to <commit>.partial.json: it says something about a
change in progress, and cannot pass a commit or replace a full result.

The corpus logs stay on this machine and so do the results, which is why the
gate cannot run in CI and why the record is a file, not a status check. The
corpora are named in local_paths.json, see tools/local_paths.py, or with
--corpus.

Usage:
    python3 tools/release_gate.py
    python3 tools/release_gate.py --steps corpus
    python3 tools/release_gate.py --accept "why the changes are right"
    python3 tools/release_gate.py --check

Exit 0 = pass; 1 = anything else.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import baseline_diff  # noqa: E402
import check_suggestions  # noqa: E402
import local_paths  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_DIR = os.path.join(REPO_ROOT, "reports", "release-gate")
ACCEPTED_DIR = os.path.join(GATE_DIR, "accepted")

STEPS = ("ci_local", "snapshots", "sitl", "corpus")
DEFAULT_SITL_DIR = "../ardupilot"
# A rebuild of the SITL binaries takes this lock exclusively, so flights hold
# it shared. A machine with other SITL users names the lock they share in
# local_paths.json.
SITL_LOCK = local_paths.get("sitl_lock", "/tmp/arduscope-sitl.lock")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def head_commit() -> str:
    return _git("rev-parse", "HEAD")


def tree_is_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


def result_path(commit: str) -> str:
    return os.path.join(GATE_DIR, f"{commit}.json")


def partial_path(commit: str) -> str:
    return os.path.join(GATE_DIR, f"{commit}.partial.json")


def scan_path(commit: str, corpus: str) -> str:
    return os.path.join(GATE_DIR, commit, f"{corpus}.json")


def load_result(commit: str) -> dict | None:
    try:
        with open(result_path(commit), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_result(result: dict, path: str | None = None) -> None:
    os.makedirs(GATE_DIR, exist_ok=True)
    with open(path or result_path(result["commit"]), "w",
              encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")


def passed(commit: str) -> bool:
    """Whether this commit may be tagged."""
    result = load_result(commit)
    return result is not None and result["status"] == "pass"


def pytest_counts(output: str) -> dict[str, int]:
    """Counts from pytest's summary line, the last one in the output."""
    summaries = re.findall(r"^.*\b\d+ \w+.* in [\d.]+s.*$", output, re.M)
    if not summaries:
        return {}
    return {word.rstrip("s") if word == "errors" else word: int(number)
            for number, word in re.findall(r"(\d+) (\w+)", summaries[-1])}


def judge_tests(returncode: int, output: str) -> dict:
    """A run that skipped everything proves nothing, so it is a failure."""
    counts = pytest_counts(output)
    ran = counts.get("passed", 0)
    status = "pass" if returncode == 0 and ran > 0 else "fail"
    return {"status": status, "detail": counts or "no pytest summary"}


def judge_corpus(accepted: dict | None, scan: dict) -> dict:
    """What one corpus scan means for the gate."""
    silent = sorted({f"{log['relpath']}: {key}"
                     for log in scan.get("logs", [])
                     for key, record in (log.get("analyzers") or {}).items()
                     if record.get("missing_evidence")})
    if silent:
        return {"status": "fail", "detail": {"missing_evidence": silent[:20]}}
    if accepted is None:
        return {"status": "review", "detail": "no accepted baseline"}
    diff = baseline_diff._compare(accepted, scan)
    detail = {"logs": diff["n_common_logs"],
              "logs_changed": len(diff["per_log_changes"]),
              "logs_gone": len(diff["only_before"]),
              "logs_new": len(diff["only_after"]),
              "by_analyzer": {
                  key: {"escalated": rec["escalated"],
                        "deescalated": rec["deescalated"],
                        "changes": len(rec["changes"])}
                  for key, rec in sorted(diff["per_analyzer"].items())
                  if rec["changes"]}}
    moved = (detail["logs_changed"] or detail["logs_gone"]
             or detail["logs_new"])
    return {"status": "review" if moved else "pass", "detail": detail}


def overall(steps: dict[str, dict]) -> str:
    statuses = {step["status"] for step in steps.values()}
    if "fail" in statuses:
        return "fail"
    return "review" if "review" in statuses else "pass"


def _run(name: str, command: list[str], log_dir: str) -> tuple[int, str]:
    """Run a command with its output in <log_dir>/<name>.log."""
    path = os.path.join(log_dir, f"{name}.log")
    with open(path, "w", encoding="utf-8") as log:
        code = subprocess.run(command, cwd=REPO_ROOT, stdout=log,
                              stderr=subprocess.STDOUT).returncode
    with open(path, encoding="utf-8", errors="replace") as f:
        return code, f.read()


def step_ci_local(args, log_dir: str) -> dict:
    code, output = _run("ci_local", [sys.executable, "tools/ci_local.py"],
                        log_dir)
    found = re.search(r"Results: (\d+/\d+) passed", output)
    return {"status": "pass" if code == 0 else "fail",
            "detail": found.group(1) if found else "no summary"}


def step_snapshots(args, log_dir: str) -> dict:
    code, output = _run("snapshots", [
        sys.executable, "-m", "pytest", "tests/test_baseline_snapshots.py",
        "-q", "--tb=short"], log_dir)
    return judge_tests(code, output)


def step_sitl(args, log_dir: str) -> dict:
    code, output = _run("sitl_flights", [
        "flock", "-s", SITL_LOCK, sys.executable,
        "tests/sitl/generate_logs.py", "--sitl-dir", args.sitl_dir], log_dir)
    if code != 0:
        flown = re.search(r"Generated \d+ / \d+ logs", output)
        return {"status": "fail",
                "detail": flown.group(0) if flown else "no flight ran"}
    code, output = _run("sitl_regression", [
        sys.executable, "-m", "pytest", "tests/test_sitl_regression.py",
        "-q", "--tb=short"], log_dir)
    return judge_tests(code, output)


def step_corpus(args, log_dir: str) -> dict:
    results = {}
    for name, root in args.corpora.items():
        scan_file = os.path.join(log_dir, f"{name}.json")
        code, _ = _run(f"corpus_{name}", [
            sys.executable, "tools/baseline_run.py", root, "-o", scan_file,
            "--jobs", str(args.jobs), "--disk-cache"], log_dir)
        if code != 0:
            results[name] = {"status": "fail", "detail": "scan did not finish"}
            continue
        with open(scan_file, encoding="utf-8") as f:
            scan = json.load(f)
        results[name] = judge_corpus(_accepted(name), scan)
        rows, _ = check_suggestions.parse_registry(_registry_text())
        unlisted = check_suggestions.check_baseline(rows, scan)
        if unlisted:
            results[name] = {"status": "fail",
                             "detail": {"not_in_registry": unlisted}}
    statuses = {r["status"] for r in results.values()}
    status = ("fail" if "fail" in statuses
              else "review" if "review" in statuses else "pass")
    return {"status": status, "detail": results}


def _registry_text() -> str:
    with open(check_suggestions.REGISTRY_PATH, encoding="utf-8") as f:
        return f.read()


def _accepted(name: str) -> dict | None:
    try:
        path = os.path.join(ACCEPTED_DIR, f"{name}.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


RUNNERS = {"ci_local": step_ci_local, "snapshots": step_snapshots,
           "sitl": step_sitl, "corpus": step_corpus}


def run_gate(args) -> int:
    if tree_is_dirty():
        print("Working tree is dirty. A result has to name a commit.")
        return 1
    commit = head_commit()
    log_dir = os.path.join(GATE_DIR, commit)
    os.makedirs(log_dir, exist_ok=True)
    started = datetime.datetime.now().isoformat(timespec="seconds")

    steps: dict[str, dict] = {}
    for name in args.steps:
        print(f"[{name}] running", flush=True)
        t0 = time.time()
        steps[name] = RUNNERS[name](args, log_dir)
        steps[name]["seconds"] = round(time.time() - t0)
        print(f"[{name}] {steps[name]['status']} "
              f"({steps[name]['seconds']} s)", flush=True)

    if head_commit() != commit or tree_is_dirty():
        steps["tree"] = {"status": "fail",
                         "detail": "the tree changed while the gate ran"}
    result = {
        "commit": commit,
        "started": started,
        "finished": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": overall(steps),
        "steps": steps,
    }
    complete = set(args.steps) == set(STEPS)
    path = result_path(commit) if complete else partial_path(commit)
    save_result(result, path)
    print(f"\n{commit[:10]}: {result['status']}"
          f"{'' if complete else ' on a partial run'}, "
          f"see {os.path.relpath(path, REPO_ROOT)}")
    _print_review(commit, steps.get("corpus"))
    if complete and result["status"] == "review":
        print("Then: python3 tools/release_gate.py --accept \"why they are right\"")
    return 0 if result["status"] == "pass" else 1


def _print_review(commit: str, corpus: dict | None) -> None:
    """How to read the corpus changes, which a person has to do."""
    if corpus is None or corpus["status"] != "review":
        return
    for name, outcome in corpus["detail"].items():
        if outcome["status"] == "review":
            accepted = os.path.join(ACCEPTED_DIR, f"{name}.json")
            print(f"python3 tools/baseline_diff.py "
                  f"{os.path.relpath(accepted, REPO_ROOT)} "
                  f"{os.path.relpath(scan_path(commit, name), REPO_ROOT)}")


def accept(commit: str, reason: str) -> int:
    """Make this commit's scans the baseline, and its result a pass."""
    result = load_result(commit)
    if result is None or result["status"] != "review":
        found = result["status"] if result else "no result"
        print(f"Nothing to accept for {commit[:10]}: {found}")
        return 1
    if not reason.strip():
        print("--accept needs a reason.")
        return 1
    os.makedirs(ACCEPTED_DIR, exist_ok=True)
    for name in result["steps"]["corpus"]["detail"]:
        shutil.copyfile(scan_path(commit, name),
                        os.path.join(ACCEPTED_DIR, f"{name}.json"))
    result["status"] = "pass"
    result["accepted"] = reason
    save_result(result)
    print(f"{commit[:10]}: pass, baselines replaced")
    return 0


def _corpus_arg(value: str) -> tuple[str, str]:
    name, _, path = value.partition("=")
    if not name or not path:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    return name, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ArduScope release gate")
    parser.add_argument("--steps", default=",".join(STEPS),
                        help=f"comma list, default {','.join(STEPS)}")
    parser.add_argument("--accept", metavar="REASON",
                        help="accept the corpus changes on this commit")
    parser.add_argument("--check", action="store_true",
                        help="report this commit's result, run nothing")
    parser.add_argument("--corpus", action="append", type=_corpus_arg,
                        metavar="NAME=PATH",
                        help="replaces the corpora in local_paths.json")
    parser.add_argument("--sitl-dir", default=DEFAULT_SITL_DIR)
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args(argv)

    if args.accept is not None:
        return accept(head_commit(), args.accept)
    if args.check:
        result = load_result(head_commit())
        print(result["status"] if result else "no result")
        return 0 if passed(head_commit()) else 1

    args.steps = [s for s in args.steps.split(",") if s]
    unknown = sorted(set(args.steps) - set(STEPS))
    if unknown:
        parser.error(f"unknown steps: {', '.join(unknown)}")
    args.corpora = dict(args.corpus) if args.corpus else local_paths.corpora()
    if "corpus" in args.steps and not args.corpora:
        parser.error("no corpus to scan: name them in local_paths.json "
                     "or with --corpus NAME=PATH")
    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
