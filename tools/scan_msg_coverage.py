#!/usr/bin/env python3
"""
Message-coverage scanner: which .BIN message types exist in a log corpus,
and which of them the analyzers actually consume.

Every message type that appears in real logs but is referenced nowhere in
analyzers/ or lib/ is a diagnostic dimension the tool is blind to. This
script produces the raw inventory behind docs/msg_coverage.md.

Usage:
    python3 tools/scan_msg_coverage.py <logfile-or-dir> [...]
    python3 tools/scan_msg_coverage.py --group customer /path/logs1 \
                                       --group sitl /path/logs2

Output: a Markdown table (one row per message type) with per-group
log-prevalence and row counts, plus the source files that reference the
type. Consumption detection is a heuristic: any quoted ALL-CAPS token in
analyzers/*.py or lib/*.py counts as a reference, so a type named in a
comment string would count too — verify before acting on a single row.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# AP_Logger bookkeeping/format machinery — present in every log by
# construction, consumed by the parser itself, never analyzer material.
BOOKKEEPING = {"FMT", "FMTU", "UNIT", "MULT", "PARM", "MSG", "FILE",
               "VER", "DMS"}


def collect_logs(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    found = []
    for pat in ("*.bin", "*.BIN"):
        found.extend(glob.glob(os.path.join(path, "**", pat),
                               recursive=True))
    return sorted(set(found))


def scan_log(path: str) -> dict[str, int]:
    """Return {msg_type: row_count} for one .BIN log."""
    from pymavlink import DFReader
    log = DFReader.DFReader_binary(path)
    counts: dict[str, int] = {}
    try:
        for mid, name in log.id_to_name.items():
            c = log.counts[mid] if mid < len(log.counts) else 0
            if c > 0:
                counts[name] = counts.get(name, 0) + c
    except AttributeError:
        # Old pymavlink without the offset index: full iterate.
        while True:
            m = log.recv_msg()
            if m is None:
                break
            counts[m.get_type()] = counts.get(m.get_type(), 0) + 1
    return counts


def scan_references() -> dict[str, set[str]]:
    """Map ALL-CAPS quoted tokens -> set of repo files referencing them."""
    refs: dict[str, set[str]] = {}
    pattern = re.compile(r'["\']([A-Z][A-Z0-9_]{1,5})["\']')
    for rel_dir in ("analyzers", "lib"):
        for path in glob.glob(os.path.join(REPO_ROOT, rel_dir, "*.py")):
            rel = os.path.relpath(path, REPO_ROOT)
            text = open(path, encoding="utf-8").read()
            for token in pattern.findall(text):
                refs.setdefault(token, set()).add(rel)
    return refs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("paths", nargs="*",
                        help="log files or directories (group: all)")
    parser.add_argument("--group", nargs="+", action="append", default=[],
                        metavar=("LABEL", "PATH"),
                        help="named corpus group: LABEL PATH [PATH ...]")
    args = parser.parse_args()

    groups: dict[str, list[str]] = {}
    for entry in args.group:
        if len(entry) < 2:
            parser.error("--group needs LABEL followed by at least one PATH")
        label, paths = entry[0], entry[1:]
        groups.setdefault(label, [])
        for p in paths:
            groups[label].extend(collect_logs(p))
    if args.paths:
        groups.setdefault("all", [])
        for p in args.paths:
            groups["all"].extend(collect_logs(p))
    if not any(groups.values()):
        parser.error("no logs found")

    # {msgtype: {label: [n_logs, total_rows]}}
    agg: dict[str, dict[str, list[int]]] = {}
    n_logs: dict[str, int] = {}
    for label, files in groups.items():
        n_logs[label] = 0
        for f in files:
            try:
                counts = scan_log(f)
            except Exception as e:  # unreadable/corrupt log
                print(f"SKIP {f}: {e}", file=sys.stderr)
                continue
            n_logs[label] += 1
            for name, c in counts.items():
                slot = agg.setdefault(name, {}).setdefault(label, [0, 0])
                slot[0] += 1
                slot[1] += c

    refs = scan_references()
    labels = list(groups.keys())

    header = "| Type |"
    sep = "|------|"
    for lb in labels:
        header += f" {lb} logs (of {n_logs[lb]}) | {lb} rows |"
        sep += "------|------|"
    header += " Referenced in |"
    sep += "------|"
    print(header)
    print(sep)

    def sort_key(item):
        name, per = item
        first = per.get(labels[0], [0, 0])
        return (-first[0], -first[1], name)

    for name, per in sorted(agg.items(), key=sort_key):
        if name in BOOKKEEPING:
            continue
        row = f"| {name} |"
        for lb in labels:
            got = per.get(lb, [0, 0])
            row += f" {got[0]} | {got[1]} |"
        consumers = sorted(refs.get(name, ()))
        row += f" {', '.join(consumers) if consumers else '—'} |"
        print(row)

    total = len([t for t in agg if t not in BOOKKEEPING])
    unused = len([t for t in agg
                  if t not in BOOKKEEPING and not refs.get(t)])
    print(f"\n{total} message types (bookkeeping excluded), "
          f"{unused} unreferenced.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
