#!/usr/bin/env python3
"""
Generate tools/msg_field_table.json from an ArduPilot source tree.

The table maps every logged message type to the union of its field
names, harvested from BOTH definition styles:
  - static LogStructure tables:  { ..., "NAME", "fmt", "lbl1,lbl2", ... }
  - runtime Write() calls:       AP::logger().Write("NAME", "lbl1,lbl2", "fmt", ...)

check_msg_fields.py validates every analyzer field reference against
this table, turning the "field renamed upstream → silent all-NaN dead
check" bug class (the largest class of the 2026-07 bug hunt) into a CI
failure.

Usage:
    python3 tools/gen_msg_field_table.py --ap-path /path/to/ardupilot

The tree has to be checked out at an ArduPilot release tag, such as
Copter-4.7.2. The table is published with ArduScope, so it holds the
messages of upstream releases only: a fork or a working branch adds its own.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE_PATH = os.path.join(REPO_ROOT, "tools", "msg_field_table.json")

# Versioned release tags only: ArduCopter-stable and the like move.
_RELEASE_TAG = re.compile(r"(Copter|Plane|Rover|Sub|Tracker|Blimp)-\d+\.\d+\.\d+")


def _release_tags(ap_path: str) -> list[str]:
    """The ArduPilot release tags at the tree's HEAD, or []."""
    try:
        out = subprocess.run(["git", "-C", ap_path, "tag", "--points-at",
                              "HEAD"], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(t for t in out.stdout.split() if _RELEASE_TAG.fullmatch(t))

# Three consecutive C string literals — the shape shared by both
# LogStructure entries (name, fmt, labels) and Write() calls
# (name, labels, fmt). Which of s2/s3 is the label list is decided by
# content: labels contain commas / TimeUS, formats never contain commas.
_TRIPLET = re.compile(
    r'"([A-Z][A-Z0-9_]{1,4})"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"')

_LABELS = re.compile(r'^[A-Za-z0-9_]+(,[A-Za-z0-9_]+)+$')

# Label lists shared through macros, e.g.
#   #define PID_LABELS "TimeUS,Tar,Act,Err,P,I,D,FF,..."
#   { LOG_PIQR_MSG, sizeof(log_PID), "PIQR", PID_FMT, PID_LABELS, ... }
_DEFINE = re.compile(r'#\s*define\s+(\w+)\s+"([^"]+)"')
_MACRO_TRIPLET = re.compile(
    r'"([A-Z][A-Z0-9_]{1,4})"\s*,\s*([A-Za-z_]\w+)\s*,\s*([A-Za-z_]\w+)')
# Mixed forms: "PSCN", PSCx_FMT, "TimeUS,DPN,..."  /  "X", "fmt", MACRO
_MIXED_LIT_LABELS = re.compile(
    r'"([A-Z][A-Z0-9_]{1,4})"\s*,\s*[A-Za-z_]\w+\s*,\s*"([^"]+)"')
_MIXED_MACRO_LABELS = re.compile(
    r'"([A-Z][A-Z0-9_]{1,4})"\s*,\s*"[^",]*"\s*,\s*([A-Za-z_]\w+)')


# C joins string literals that stand next to each other, and a backslash
# carries a macro onto the next line. BARO is written both ways:
#   "BARO", \
#   "Q" "B" "f" ..., \
#   "TimeUS," "I," "Alt," ...
# so it was not in the table, and a check of BARO.Alt called it unknown.
_CONTINUATION = re.compile(r'\\[ \t]*\n')
_ADJACENT_LITERALS = re.compile(r'"\s+"')


def _as_the_compiler_reads_it(text: str) -> str:
    return _ADJACENT_LITERALS.sub("", _CONTINUATION.sub("\n", text))


def _pick_labels(s2: str, s3: str) -> str | None:
    if _LABELS.match(s2) and ("," in s2):
        return s2
    if _LABELS.match(s3) and ("," in s3):
        return s3
    return None


def harvest(ap_path: str) -> dict[str, list[str]]:
    table: dict[str, set[str]] = {}
    patterns = [
        os.path.join(ap_path, "libraries", "**", "*.h"),
        os.path.join(ap_path, "libraries", "**", "*.cpp"),
        os.path.join(ap_path, "ArduPlane", "*.cpp"),
        os.path.join(ap_path, "ArduPlane", "*.h"),
        os.path.join(ap_path, "ArduCopter", "*.cpp"),
        os.path.join(ap_path, "ArduCopter", "*.h"),
    ]
    files: list[str] = []
    for pat in patterns:
        files.extend(glob.glob(pat, recursive=True))

    texts: list[str] = []
    macros: dict[str, str] = {}
    for path in files:
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        text = _as_the_compiler_reads_it(text)
        texts.append(text)
        for dm in _DEFINE.finditer(text):
            macros[dm.group(1)] = dm.group(2)

    def _add(name: str, labels: str) -> None:
        fields = labels.split(",")
        # Every real log message starts with TimeUS (or TimeMS on
        # ancient formats) — anything else is a false triplet match.
        if fields[0] not in ("TimeUS", "TimeMS", "SampleUS"):
            return
        table.setdefault(name, set()).update(fields)

    for text in texts:
        for m in _TRIPLET.finditer(text):
            name, s2, s3 = m.groups()
            labels = _pick_labels(s2, s3)
            if labels is not None:
                _add(name, labels)
        # Macro-labeled entries: resolve both identifiers, keep the one
        # whose macro value is a label list.
        for m in _MACRO_TRIPLET.finditer(text):
            name, id2, id3 = m.groups()
            s2 = macros.get(id2, "")
            s3 = macros.get(id3, "")
            labels = _pick_labels(s2, s3)
            if labels is not None:
                _add(name, labels)
        # Mixed literal/macro forms ("PSCN", PSCx_FMT, "TimeUS,...")
        for m in _MIXED_LIT_LABELS.finditer(text):
            name, labels = m.groups()
            if _LABELS.match(labels):
                _add(name, labels)
        for m in _MIXED_MACRO_LABELS.finditer(text):
            name, ident = m.groups()
            labels = macros.get(ident, "")
            if _LABELS.match(labels):
                _add(name, labels)

    return {k: sorted(v) for k, v in sorted(table.items())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--ap-path", required=True,
                    help="ArduPilot source tree checked out at a release tag")
    args = ap.parse_args()

    ap_path = os.path.abspath(os.path.join(REPO_ROOT, args.ap_path)) \
        if not os.path.isabs(args.ap_path) else args.ap_path
    if not os.path.isdir(os.path.join(ap_path, "libraries")):
        sys.exit(f"Not an ArduPilot tree: {ap_path}")
    tags = _release_tags(ap_path)
    if not tags:
        sys.exit(f"{ap_path} is not at an ArduPilot release tag such as "
                 f"Copter-4.7.2. The table ships with ArduScope, so it is "
                 f"built from upstream releases only.")

    table = harvest(ap_path)
    if len(table) < 50:
        sys.exit(f"Suspiciously small harvest ({len(table)} messages) — "
                 f"refusing to overwrite the table.")

    commit = "?"
    r = subprocess.run(["git", "-C", ap_path, "rev-parse", "--short", "HEAD"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        commit = r.stdout.strip()

    out = {
        "_source": {"commit": commit, "tags": tags},
        "messages": table,
    }
    with open(TABLE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write("\n")
    n_fields = sum(len(v) for v in table.values())
    print(f"Wrote {len(table)} messages / {n_fields} fields "
          f"(AP @ {commit}) to {os.path.relpath(TABLE_PATH, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
