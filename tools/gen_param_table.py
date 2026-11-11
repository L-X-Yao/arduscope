#!/usr/bin/env python3
"""
Generate tools/param_table.json from an ArduPilot source tree.

ArduPilot parameter names are assembled at compile time: a vehicle's
var_info table attaches a prefix to an object, that object's group table
may attach another prefix to a sub-object, and the leaf AP_GROUPINFO
supplies the last segment. `MOT_THST_HOVER` is `GOBJECT(motors, "MOT_")`
plus `AP_GROUPINFO("THST_HOVER")`; `EK3_SRC1_POSXY` is three segments.

Rather than resolve which class each prefix points at, this harvests the
two vocabularies separately:

  prefixes  every string a GOBJECT/GGROUP/AP_SUBGROUP* macro attaches
  leaves    every string an AP_GROUPINFO/GSCALAR macro supplies

check_params.py accepts a name that decomposes into zero or more
prefixes followed by a leaf. That is deliberately weaker than full
resolution: it would accept a real prefix glued to a real leaf from an
unrelated class. It is strong enough for the bug it exists to catch —
names that do not exist at all (`THST_HOVER`, `COMPASS_MOT`, a typo) —
and it cannot fail a legitimate name, which matters more in CI.

ArduPilot's own Tools/autotest/param_metadata/param_parse.py resolves
properly, but it needs a build environment and did not complete on this
tree; the harvest below is source-only and takes seconds.

Usage:
    python3 tools/gen_param_table.py --ap-path /path/to/ardupilot

The tree has to be checked out at an ArduPilot release tag, such as
Copter-4.7.2. The table is published with ArduScope, so it holds the
names of upstream releases only: a fork or a working branch adds its own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE_PATH = os.path.join(REPO_ROOT, "tools", "param_table.json")

# Versioned release tags only: ArduCopter-stable and the like move.
_RELEASE_TAG = re.compile(r"(Copter|Plane|Rover|Sub|Tracker|Blimp)-\d+\.\d+\.\d+")

# Leaf segments: the last piece of a parameter name.
_LEAF = re.compile(
    r'\bAP_GROUPINFO(?:_FLAGS|_FRAME|_FLAGS_DEFAULT_POINTER)?\s*\(\s*'
    r'"([A-Z0-9_]+)"')
# Vehicle-level scalars are already complete names.
_SCALAR = re.compile(r'\bG(?:SCALAR|ARRAY)\s*\(\s*[^,]+,\s*"([A-Z0-9_]+)"')
# Prefix segments: attached to a nested object. GOBJECTN puts the prefix in
# its third argument, GOBJECTN(ahrs.EKF3, NavEKF3, "EK3_", NavEKF3), the rest
# in their second.
_PREFIX = re.compile(
    r'\b(?:GOBJECT[A-Z_]*|GGROUP[A-Z_]*|AP_SUBGROUP(?:INFO|PTR|VARPTR)'
    r'[A-Z_]*)\s*\(\s*(?:[^,"]+,\s*){1,2}"([A-Z0-9_]*)"')

_SKIP_DIRS = {".git", "build", "modules", "docs", "Tools"}


def _iter_sources(ap_path: str):
    for root, dirs, names in os.walk(ap_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for n in names:
            if n.endswith((".cpp", ".h")):
                yield os.path.join(root, n)


def harvest(ap_path: str) -> tuple[set[str], set[str], int]:
    prefixes: set[str] = set()
    leaves: set[str] = set()
    n_files = 0
    for path in _iter_sources(ap_path):
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        if "AP_GROUPINFO" not in text and "GSCALAR" not in text \
                and "GOBJECT" not in text:
            continue
        n_files += 1
        leaves.update(_LEAF.findall(text))
        leaves.update(_SCALAR.findall(text))
        prefixes.update(p for p in _PREFIX.findall(text) if p)
    return prefixes, leaves, n_files


def _commit(ap_path: str) -> str:
    try:
        out = subprocess.run(["git", "-C", ap_path, "rev-parse", "--short",
                              "HEAD"], capture_output=True, text=True,
                             timeout=15)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _release_tags(ap_path: str) -> list[str]:
    """The ArduPilot release tags at the tree's HEAD, or []."""
    try:
        out = subprocess.run(["git", "-C", ap_path, "tag", "--points-at",
                              "HEAD"], capture_output=True, text=True,
                             timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(t for t in out.stdout.split() if _RELEASE_TAG.fullmatch(t))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ap-path", required=True,
                    help="ArduPilot source tree checked out at a release tag")
    args = ap.parse_args()

    ap_path = os.path.abspath(args.ap_path)
    if not os.path.isdir(ap_path):
        print(f"not a directory: {ap_path}", file=sys.stderr)
        return 2
    tags = _release_tags(ap_path)
    if not tags:
        print(f"{ap_path} is not at an ArduPilot release tag such as "
              f"Copter-4.7.2. The table ships with ArduScope, so it is built "
              f"from upstream releases only.", file=sys.stderr)
        return 2

    prefixes, leaves, n_files = harvest(ap_path)
    if len(leaves) < 500:
        print(f"only {len(leaves)} leaf names harvested — that is far below "
              f"the ~3000 ArduPilot defines. Refusing to write a table that "
              f"would fail valid names.", file=sys.stderr)
        return 1

    table = {
        "_source": {"commit": _commit(ap_path), "tags": tags,
                    "files_scanned": n_files},
        "prefixes": sorted(prefixes),
        "leaves": sorted(leaves),
    }
    with open(TABLE_PATH, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(table, indent=1, sort_keys=False) + "\n")
    print(f"{len(prefixes)} prefixes, {len(leaves)} leaf names from "
          f"{n_files} files -> {os.path.relpath(TABLE_PATH, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
