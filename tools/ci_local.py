#!/usr/bin/env python3
"""
Local CI check — runs all quality gates that GitHub Actions would run.

Use before pushing to catch issues early.

Usage:
    python3 tools/ci_local.py          # full check
    python3 tools/ci_local.py --quick  # skip real-log tests
"""

import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(label: str, cmd: list[str], **kwargs) -> bool:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    t0 = time.time()
    result = subprocess.run(cmd, cwd=_ROOT, **kwargs)
    elapsed = time.time() - t0
    ok = result.returncode == 0
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label} ({elapsed:.1f}s)")
    return ok


def main():
    quick = "--quick" in sys.argv
    results = []

    # 1. Lint
    results.append(_run(
        "Project lint",
        [sys.executable, "tools/lint_project.py"],
    ))

    results.append(_run("Ruff", [sys.executable, "-m", "ruff", "check", "."]))

    # 2. Unit tests (no real logs)
    test_cmd = [
        sys.executable, "-m", "pytest", "tests/", "-q",
        "--ignore=tests/integration/test_baseline_snapshots.py",
        "--ignore=tests/sitl/test_sitl_regression.py",
    ]
    if quick:
        # Marker-based selection: the fast tier is the pure-logic
        # subset. (The old -k substring anti-select silently skipped
        # legitimate tests whose NAMES contained "armed"/"notch".)
        test_cmd.extend(["-m", "fast"])
    results.append(_run("Unit tests", test_cmd))

    # 2b. i18n safety + analyzer contracts — these run in GitHub CI,
    # and release.py trusts this script when the remote CI is
    # unavailable, so their absence here let violations reach a release.
    results.append(_run(
        "i18n check (check_i18n.py)",
        [sys.executable, "tools/check_i18n.py"],
    ))
    results.append(_run(
        "Analyzer contracts (check_analyzers.py)",
        [sys.executable, "tools/check_analyzers.py"],
    ))
    results.append(_run(
        "Message field matrix (check_msg_fields.py)",
        [sys.executable, "tools/check_msg_fields.py"],
    ))
    results.append(_run(
        "Parameter names (check_params.py)",
        [sys.executable, "tools/check_params.py"],
    ))
    results.append(_run(
        "Suggestion registry (check_suggestions.py)",
        [sys.executable, "tools/check_suggestions.py"],
    ))

    # 3. Import check
    results.append(_run(
        "Import check (all modules)",
        [sys.executable, "-c",
         "from lib.pipeline import run_analysis; "
         "from web_app import app; "
         "from analyzers import discover_analyzers; "
         "a = discover_analyzers(); "
         f"assert len(a) >= 28, f'Only {{len(a)}} analyzers'; "
         "print(f'  {{len(a)}} analyzers OK')"],
    ))

    # 4. Package build
    results.append(_run(
        "Package build (wheel)",
        [sys.executable, "-m", "build", "--wheel"],
    ))

    # 5. Locale symmetry (quick sanity)
    results.append(_run(
        "Locale key count",
        [sys.executable, "-c",
         "import json; "
         "zh = json.load(open('locales/zh.json', encoding='utf-8')); "
         "en = json.load(open('locales/en.json', encoding='utf-8')); "
         "assert len(zh) == len(en), "
         f"f'zh={{len(zh)}} != en={{len(en)}}'; "
         "print(f'  {{len(zh)}} keys, symmetric')"],
    ))

    # Summary
    n_pass = sum(results)
    n_total = len(results)
    print(f"\n{'='*60}")
    print(f"  Results: {n_pass}/{n_total} passed")
    if n_pass == n_total:
        print("  All checks passed. This run did not cover real-log snapshots,")
        print("  SITL or the corpus: tools/release_gate.py does, before a tag.")
    else:
        print(f"  {n_total - n_pass} check(s) FAILED")
    print(f"{'='*60}")
    sys.exit(0 if n_pass == n_total else 1)


if __name__ == "__main__":
    main()
