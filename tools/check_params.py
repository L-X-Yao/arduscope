#!/usr/bin/env python3
"""
Parameter-name guardrail: validate every ArduPilot parameter this project
names against tools/param_table.json (harvested by gen_param_table.py).

Three places name parameters, and a wrong name fails silently in all
three:

    reader.get_param("NAME", default)   -> returns the default forever,
                                           so the check quietly dies
    {"param": "NAME", ...}              -> ships in the user's .param
                                           table and does nothing
    locale strings                      -> the user is told to change a
                                           parameter that does not exist

The 2026-07-24 AI-diagnosis audit found four of the third kind
(THST_HOVER, COMPASS_MOT, and two instance-blind names). Nothing caught
them, the same way nothing caught PM.NLoop being logged as NL until the
message-field matrix landed.

Names are validated by decomposition: zero or more known prefixes
followed by a known leaf. See gen_param_table.py for why that is weaker
than full resolution and why it is the right trade for CI.

Exit 0 = every name resolves; 1 = at least one does not.
"""

from __future__ import annotations

import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE_PATH = os.path.join(REPO_ROOT, "tools", "param_table.json")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

CODE_DIRS = ("analyzers", "lib")
ENTRY_FILES = ("flight_report.py", "multi_report.py", "batch_report.py",
               "fleet_report.py", "web_app.py")
LOCALE_FILES = ("locales/zh.json", "locales/en.json")

# Strings that look like a parameter but are not one. Each needs a
# reason: this list is the escape hatch, so it has to stay short.
NOT_PARAMS = {
    "EK3_SRC": "prose writes EK3_SRC* — the source family, not one param",
    "AC_PID": "ArduPilot class name, cited as an evidence source",
    "AP_GPS": "ArduPilot library name, cited as an evidence source",
    "AC_ATTITUDE_THRUST_ERROR_ANGLE": "C constant in AC_AttitudeControl",
    "AUTOTUNE_LEVEL_ANGLE_CD": "C constant in AC_AutoTune",
    "GPS_OK_FIX_3D": "AP_GPS method name, cited as an evidence source",
    "MAV_POWER_STATUS": "MAVLink enum",
    "BRICK_VALID": "MAV_POWER_STATUS flag",
    "PERIPH_OVERCURRENT": "MAV_POWER_STATUS flag",
    "ALT_HOLD": "flight mode name",
    "PIT_P": "written as a suffix of {prefix}_PIT_P in the same string",
    "MAX_CM": "written as RNGFND1_TYPE/MAX_CM — a shared-prefix shorthand",
}

# Parameters an older firmware generation defined and the current tree
# no longer does. ArduScope reads logs from every generation, so these
# stay — but each must name its replacement, so the pair is visible.
LEGACY_PARAMS = {
    "WPNAV_RADIUS": "Copter pre-SI (cm); now WP_RADIUS_M (m)",
    "RNGFND1_MAX_CM": "pre-SI (cm); now RNGFND1_MAX (m)",
}

_GET_PARAM = re.compile(r'get_param\(\s*"([A-Z][A-Z0-9_]*)"')
_SUGGESTION = re.compile(r'"param"\s*:\s*"([A-Z][A-Z0-9_]*)"')
# Prose token: at least one underscore, so plain words are not candidates.
_PROSE = re.compile(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b')


def load_table() -> tuple[set[str], set[str]]:
    with open(TABLE_PATH, encoding="utf-8") as fh:
        table = json.load(fh)
    return set(table["prefixes"]), set(table["leaves"])


def resolves(name: str, prefixes: set[str], leaves: set[str],
             _memo: dict | None = None) -> bool:
    """True when name is a chain of prefixes ending in a known leaf."""
    if _memo is None:
        _memo = {}
    if name in _memo:
        return _memo[name]
    if name in leaves:
        _memo[name] = True
        return True
    # A Vector3f parameter is declared once and exposed per axis:
    # AP_GROUPINFO("OFS", ...) is COMPASS_OFS_X / _Y / _Z to the user.
    if name[-2:] in ("_X", "_Y", "_Z") and name[:-2] in leaves:
        _memo[name] = True
        return True
    result = False
    for prefix in prefixes:
        if prefix and name.startswith(prefix) and len(prefix) < len(name):
            if resolves(name[len(prefix):], prefixes, leaves, _memo):
                result = True
                break
    _memo[name] = result
    return result


def _code_files() -> list[str]:
    files = []
    for directory in CODE_DIRS:
        for root, _dirs, names in os.walk(os.path.join(REPO_ROOT, directory)):
            if "__pycache__" in root:
                continue
            files.extend(os.path.join(root, n) for n in names
                         if n.endswith(".py"))
    files.extend(os.path.join(REPO_ROOT, f) for f in ENTRY_FILES)
    return [f for f in files if os.path.isfile(f)]


def collect() -> dict[str, list[tuple[str, int]]]:
    """{param_name: [(where, line), ...]} across code and locales."""
    found: dict[str, list[tuple[str, int]]] = {}

    def add(name: str, where: str, line: int) -> None:
        found.setdefault(name, []).append((where, line))

    for path in _code_files():
        rel = os.path.relpath(path, REPO_ROOT)
        with open(path, encoding="utf-8") as fh:
            for lineno, text in enumerate(fh, 1):
                for pattern in (_GET_PARAM, _SUGGESTION):
                    for name in pattern.findall(text):
                        add(name, rel, lineno)

    for rel in LOCALE_FILES:
        path = os.path.join(REPO_ROOT, rel)
        with open(path, encoding="utf-8") as fh:
            strings = json.load(fh)
        for key, value in strings.items():
            if not isinstance(value, str):
                continue
            for name in _PROSE.findall(value):
                add(name, f"{rel}:{key}", 0)
    return found


def main() -> int:
    print(f"{BOLD}[Parameter name matrix]{RESET}", end=" ")
    prefixes, leaves = load_table()
    with open(TABLE_PATH, encoding="utf-8") as fh:
        commit = json.load(fh)["_source"]["commit"]
    found = collect()
    print(f"{len(leaves)} leaves / {len(prefixes)} prefixes "
          f"(AP @ {commit}), {len(found)} names referenced")

    bad = []
    legacy = []
    for name in sorted(found):
        if name in NOT_PARAMS:
            continue
        if resolves(name, prefixes, leaves):
            continue
        if name in LEGACY_PARAMS:
            legacy.append(name)
            continue
        bad.append(name)

    for name in legacy:
        print(f"  {YELLOW}NOTE{RESET}  {name} — {LEGACY_PARAMS[name]}")

    if bad:
        for name in bad:
            sites = found[name]
            where = ", ".join(f"{w}:{ln}" if ln else w for w, ln in sites[:3])
            if len(sites) > 3:
                where += f" (+{len(sites) - 3} more)"
            print(f"  {RED}FAIL{RESET}  {name} — no such parameter "
                  f"[{where}]")
        print(f"\n{RED}{BOLD}FAILED{RESET}: {len(bad)} unknown parameter "
              f"name(s)")
        return 1

    print(f"  {GREEN}PASS{RESET}  every referenced parameter resolves")
    print(f"\n{GREEN}{BOLD}ALL PASS{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
