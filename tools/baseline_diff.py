#!/usr/bin/env python3
"""
Compare two baseline JSON files (as produced by baseline_run.py) and
report per-analyzer and per-log status changes.

Purpose:
  - Validate analyzer changes against real flight logs before pushing.
  - Catch unintended sensitivity shifts (e.g. fixing one analyzer but
    breaking another by side effect).
  - Document the impact of a change in human-readable form for commit
    messages and analysis notes.

Usage:
    python3 tools/baseline_diff.py before.json after.json
    python3 tools/baseline_diff.py before.json after.json --json

Status severity order (for "direction" classification):
    error < skip < info < good < warning < critical
Higher index = more severe.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

SEVERITY = {
    "error": -1,
    "skip": 0,
    "info": 1,
    "good": 2,
    "warning": 3,
    "critical": 4,
}


def _severity_delta(old: str, new: str) -> str:
    """Classify status change direction."""
    o = SEVERITY.get(old, 1)
    n = SEVERITY.get(new, 1)
    if n > o:
        return "escalated"
    if n < o:
        return "deescalated"
    return "same"


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _index_by_log(data: dict) -> dict:
    """Map relpath -> log entry for O(1) lookup."""
    return {log["relpath"]: log for log in data.get("logs", [])}


def _compare(before: dict, after: dict) -> dict:
    """Return a structured diff."""
    before_by_log = _index_by_log(before)
    after_by_log = _index_by_log(after)

    only_before = set(before_by_log) - set(after_by_log)
    only_after = set(after_by_log) - set(before_by_log)
    common = set(before_by_log) & set(after_by_log)

    # Per-analyzer status count changes
    per_analyzer = defaultdict(lambda: {
        "before_counts": defaultdict(int),
        "after_counts": defaultdict(int),
        "escalated": 0,
        "deescalated": 0,
        "unchanged": 0,
        "changes": [],  # list of (log, old_status, new_status)
    })

    # Per-log: list of analyzers whose status changed
    per_log_changes = []

    for rel in sorted(common):
        b = before_by_log[rel]
        a = after_by_log[rel]
        b_ana = b.get("analyzers", {})
        a_ana = a.get("analyzers", {})

        log_changes = []
        for ana_key in sorted(set(b_ana) | set(a_ana)):
            b_rec = b_ana.get(ana_key, {})
            a_rec = a_ana.get(ana_key, {})
            b_status = b_rec.get("status", "missing")
            a_status = a_rec.get("status", "missing")

            per_analyzer[ana_key]["before_counts"][b_status] += 1
            per_analyzer[ana_key]["after_counts"][a_status] += 1

            # Compare the full recorded signature, not just status:
            # apply_param→ok or param_suggestions 3→0 is a regression
            # even when the status column stays identical.
            b_sig = (b_status, b_rec.get("action"),
                     b_rec.get("n_param_suggestions"),
                     b_rec.get("suggestions"))
            a_sig = (a_status, a_rec.get("action"),
                     a_rec.get("n_param_suggestions"),
                     a_rec.get("suggestions"))
            if b_sig == a_sig:
                per_analyzer[ana_key]["unchanged"] += 1
                continue

            if b_status != a_status:
                direction = _severity_delta(b_status, a_status)
            else:
                direction = "changed"  # action/param-count-only change
            if direction == "escalated":
                per_analyzer[ana_key]["escalated"] += 1
            elif direction == "deescalated":
                per_analyzer[ana_key]["deescalated"] += 1

            change = {
                "log": rel,
                "before": b_status,
                "after": a_status,
                "before_action": b_rec.get("action"),
                "after_action": a_rec.get("action"),
                "before_params": b_rec.get("n_param_suggestions"),
                "after_params": a_rec.get("n_param_suggestions"),
                "before_suggestions": b_rec.get("suggestions"),
                "after_suggestions": a_rec.get("suggestions"),
                "direction": direction,
            }
            per_analyzer[ana_key]["changes"].append(change)
            log_changes.append(dict(change, analyzer=ana_key))

        if log_changes:
            per_log_changes.append({
                "log": rel,
                "changes": log_changes,
            })

    return {
        "before_version": before.get("tool_version", "?"),
        "after_version": after.get("tool_version", "?"),
        "before_time": before.get("generated_at", "?"),
        "after_time": after.get("generated_at", "?"),
        "n_common_logs": len(common),
        "only_before": sorted(only_before),
        "only_after": sorted(only_after),
        "per_analyzer": {k: dict(v, before_counts=dict(v["before_counts"]),
                                  after_counts=dict(v["after_counts"]))
                         for k, v in per_analyzer.items()},
        "per_log_changes": per_log_changes,
    }


def _print_human(diff: dict):
    print(f"Before: {diff['before_version']}  @ {diff['before_time']}")
    print(f"After:  {diff['after_version']}  @ {diff['after_time']}")
    print(f"共同日志数: {diff['n_common_logs']}")
    if diff["only_before"]:
        print(f"仅 before 有 ({len(diff['only_before'])}): "
              f"{', '.join(diff['only_before'][:3])}...")
    if diff["only_after"]:
        print(f"仅 after 有 ({len(diff['only_after'])}): "
              f"{', '.join(diff['only_after'][:3])}...")

    # Analyzer-level summary sorted by absolute change magnitude
    print("\n" + "=" * 78)
    print("每分析器变化（按变化绝对值排序）：")
    print("=" * 78)
    ana_items = []
    for ana, d in diff["per_analyzer"].items():
        total_changed = d["escalated"] + d["deescalated"]
        ana_items.append((total_changed, ana, d))
    ana_items.sort(reverse=True)

    print(f"{'analyzer':18} {'↑升':>4} {'↓降':>4} {'同':>4}  before→after (warn+crit)")
    print("-" * 78)
    for total, ana, d in ana_items:
        b_wc = (d["before_counts"].get("warning", 0)
                + d["before_counts"].get("critical", 0))
        a_wc = (d["after_counts"].get("warning", 0)
                + d["after_counts"].get("critical", 0))
        indicator = "  "
        if a_wc < b_wc:
            indicator = "↓↓" if b_wc - a_wc > 5 else "↓ "
        elif a_wc > b_wc:
            indicator = "↑↑" if a_wc - b_wc > 5 else "↑ "
        print(f"{ana:18} {d['escalated']:>4} {d['deescalated']:>4} "
              f"{d['unchanged']:>4}  {b_wc:>2}→{a_wc:<2} {indicator}")

    # Per-log detail for analyzers that actually changed
    print("\n" + "=" * 78)
    print("每日志具体变化：")
    print("=" * 78)
    if not diff["per_log_changes"]:
        print("所有日志所有分析器状态无变化。")
    for entry in diff["per_log_changes"]:
        print(f"\n{entry['log']}:")
        for ch in entry["changes"]:
            arrow = "→"
            if ch["direction"] == "escalated":
                arrow = "↑"
            elif ch["direction"] == "deescalated":
                arrow = "↓"
            extra = ""
            if ch.get("before_action") != ch.get("after_action"):
                extra += (f"  action {ch.get('before_action')}"
                          f"→{ch.get('after_action')}")
            if ch.get("before_params") != ch.get("after_params"):
                extra += (f"  params {ch.get('before_params')}"
                          f"→{ch.get('after_params')}")
            elif ch.get("before_suggestions") != ch.get("after_suggestions"):
                extra += "  suggested values changed"
            print(f"  {ch['analyzer']:18} "
                  f"{ch['before']:>8} {arrow} {ch['after']}{extra}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before", help="前一次 baseline JSON")
    ap.add_argument("after", help="当前 baseline JSON")
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON 而非人类可读文本")
    args = ap.parse_args()

    before = _load(args.before)
    after = _load(args.after)
    diff = _compare(before, after)

    if args.json:
        json.dump(diff, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_human(diff)

    # Gate semantics for automation: non-zero exit when anything
    # changed (an always-0 exit makes this useless as a CI gate).
    has_changes = bool(diff["per_log_changes"]
                       or diff["only_before"] or diff["only_after"])
    sys.exit(1 if has_changes else 0)


if __name__ == "__main__":
    main()
