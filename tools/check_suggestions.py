#!/usr/bin/env python3
"""
Suggestion registry guardrail: every parameter an analyzer can suggest must
be listed in docs/suggestion_evidence.md, with its source and flight record.

A suggestion ends up in a .param file that someone may load on an aircraft.
ArduScope has shipped suggestions that read well and did nothing, or did
harm: the first closed-loop flights removed five of them. Nothing tied a
suggestion to the evidence behind it, so nothing could say which ones had
any. check_params.py proves a name exists; this proves someone answered for
the value.

What it checks:

    code against table    a name an analyzer can suggest needs a row for that
                          analyzer, and a row needs a name in the code
    a flight or a waiver  a row is flown, a log setting, or waived below
    a source or a waiver  a row with source `none` must be waived too

Names are read from the suggestion dicts. A dict with a literal name needs
nothing else. A dict that takes its name from a variable cannot be read, so
it must be declared in DYNAMIC_SITES: a new one fails until it says what it
can produce. `--baseline` runs the same comparison on what a corpus scan
really suggested, which is how a wrong DYNAMIC_SITES entry gets caught.

Usage:
    python3 tools/check_suggestions.py
    python3 tools/check_suggestions.py --baseline scan.json [scan2.json ...]

Exit 0 = code and table agree; 1 = they do not.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYZER_DIR = os.path.join(REPO_ROOT, "analyzers")
REGISTRY_PATH = os.path.join(REPO_ROOT, "docs", "suggestion_evidence.md")

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

STATUSES = ("flown", "not_flown", "log_setting")

# Suggestion dicts that take their name from a variable, as
# "<module>.<function>:<expression>", and what each one can produce.
DYNAMIC_SITES: dict[str, tuple[str, ...]] = {
    "attitude.analyze:autotune_param": (
        "AUTOTUNE_AXES", "Q_AUTOTUNE_AXES"),
    "compass_consistency.analyze:param": (
        "COMPASS_USE", "COMPASS_USE2", "COMPASS_USE3"),
    "motor.analyze:spin_min_name": ("MOT_SPIN_MIN", "Q_M_SPIN_MIN"),
    "notch_tuning._check_notch_filter:fltd_param": (
        "ATC_RAT_RLL_FLTD", "ATC_RAT_PIT_FLTD",
        "Q_A_RAT_RLL_FLTD", "Q_A_RAT_PIT_FLTD"),
    "notch_tuning._check_hover_throttle:hover_param": (
        "MOT_THST_HOVER", "Q_M_THST_HOVER"),
    "notch_tuning._notch_set:param": (
        "INS_HNTCH_ENABLE", "INS_HNTCH_MODE", "INS_HNTCH_FREQ",
        "INS_HNTCH_BW", "INS_HNTCH_REF", "INS_HNTCH_HMNCS", "INS_HNTCH_OPTS",
        "INS_HNTC2_ENABLE", "INS_HNTC2_MODE", "INS_HNTC2_FREQ",
        "INS_HNTC2_BW", "INS_HNTC2_REF", "INS_HNTC2_HMNCS", "INS_HNTC2_OPTS"),
    "pid_tuning._analyze_axis:d_name": (
        "ATC_RAT_RLL_D", "ATC_RAT_PIT_D", "ATC_RAT_YAW_D",
        "Q_A_RAT_RLL_D", "Q_A_RAT_PIT_D", "Q_A_RAT_YAW_D",
        "RLL_RATE_D", "PTCH_RATE_D", "YAW_RATE_D"),
}

# Rows allowed to go without a flight or a source, as "<analyzer>:<first
# pattern>". These predate the rule. This list is the escape hatch, so a new
# suggestion gets here only by someone deciding, in review, that it should
# ship unflown.
NOT_FLOWN_WAIVERS: dict[str, str] = {
    "airspeed:ARSPD_USE": "needs a scenario with a failing pitot",
    "attitude:AUTOTUNE_AXES": "selects what AutoTune runs, not a tuned value",
    "compass_consistency:COMPASS_USE": "needs a scenario with a bad compass",
    "gps:GPS_AUTO_SWITCH": "needs a scenario with a failing second GPS",
    "notch_tuning:INS_HNTC2_*": "no scenario has two noise bands yet",
    "pid_tuning:RLL_RATE_D": "flights so far were on Copter",
}


@dataclass(frozen=True)
class Row:
    patterns: tuple[str, ...]
    analyzer: str
    source: str
    flight: str
    status: str

    @property
    def key(self) -> str:
        return f"{self.analyzer}:{self.patterns[0]}"

    def matches(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in self.patterns)


def parse_registry(text: str) -> tuple[list[Row], list[str]]:
    """Rows of the `## Registry` table, and any line that does not parse."""
    match = re.search(r"^## Registry\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if match is None:
        return [], ["no `## Registry` section"]
    rows, errors = [], []
    for line in match.group(1).splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6:
            errors.append(f"expected 6 cells, got {len(cells)}: {line}")
            continue
        patterns = tuple(re.findall(r"`([^`]+)`", cells[0]))
        rows.append(Row(patterns, cells[1], cells[3], cells[4], cells[5]))
    return rows, errors


class _Sites(ast.NodeVisitor):
    """Suggestion dicts in one module: literal names and variable ones."""

    def __init__(self, module: str):
        self.module = module
        self.names: set[str] = set()
        self.dynamic: set[str] = set()
        self._functions: list[ast.AST] = []

    def _visit_function(self, node):
        self._functions.append(node)
        self.generic_visit(node)
        self._functions.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Dict(self, node):
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "param" in keys and keys & {"suggested", "new"}:
            index = [isinstance(k, ast.Constant) and k.value == "param"
                     for k in node.keys].index(True)
            self._record(node.values[index])
        self.generic_visit(node)

    def _record(self, value: ast.expr) -> None:
        if isinstance(value, ast.Constant):
            self.names.add(value.value)
        elif _is_param_lookup(value):
            pass  # copies a dict this visitor already reads
        elif not self._record_unpacked(value):
            function = self._functions[-1].name if self._functions else ""
            self.dynamic.add(
                f"{self.module}.{function}:{ast.unparse(value)}")

    def _record_unpacked(self, value: ast.expr) -> bool:
        """`for param, old, new in recs`: read the name out of the tuples."""
        if not isinstance(value, ast.Name) or not self._functions:
            return False
        function = self._functions[-1]
        for loop in ast.walk(function):
            if (isinstance(loop, ast.For)
                    and isinstance(loop.target, ast.Tuple)
                    and isinstance(loop.target.elts[0], ast.Name)
                    and loop.target.elts[0].id == value.id):
                width = len(loop.target.elts)
                rows = [n for n in ast.walk(function)
                        if isinstance(n, ast.Tuple) and len(n.elts) == width
                        and isinstance(n.elts[0], ast.Constant)]
                self.names.update(r.elts[0].value for r in rows)
                return bool(rows)
        return False


def _is_param_lookup(value: ast.expr) -> bool:
    return (isinstance(value, ast.Subscript)
            and isinstance(value.slice, ast.Constant)
            and value.slice.value == "param")


def scan_source(module: str, source: str) -> tuple[set[str], set[str]]:
    """(literal names, dynamic site keys) in one analyzer's source."""
    sites = _Sites(module)
    sites.visit(ast.parse(source))
    return sites.names, sites.dynamic


def scan_analyzers(directory: str = ANALYZER_DIR) -> dict[str, tuple]:
    """Module name -> (literal names, dynamic site keys), for modules with
    any suggestion."""
    found = {}
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".py") or filename == "__init__.py":
            continue
        module = filename[:-3]
        with open(os.path.join(directory, filename), encoding="utf-8") as f:
            names, dynamic = scan_source(module, f.read())
        if names or dynamic:
            found[module] = (names, dynamic)
    return found


def check_rows(rows: list[Row], waivers: dict[str, str]) -> list[str]:
    """Each row carries what its status promises."""
    errors = []
    for row in rows:
        if row.status not in STATUSES:
            errors.append(f"{row.key}: status `{row.status}`")
        if not (row.source == "none" or row.source.startswith(("A: ", "D: "))):
            errors.append(f"{row.key}: source must be `A: `, `D: ` or `none`")
        if row.status == "flown" and row.flight in ("", "-"):
            errors.append(f"{row.key}: flown with no run named")
        if row.status != "flown" and row.flight != "-":
            errors.append(f"{row.key}: run named but status {row.status}")
        if row.status == "log_setting" and not all(
                p.startswith("LOG_") for p in row.patterns):
            errors.append(f"{row.key}: only LOG_* is a log setting")
        needs_waiver = row.status == "not_flown" or row.source == "none"
        if needs_waiver and row.key not in waivers:
            errors.append(f"{row.key}: {row.status}, source {row.source}, "
                          f"and no waiver in NOT_FLOWN_WAIVERS")
    needed = {r.key for r in rows
              if r.status == "not_flown" or r.source == "none"}
    errors += [f"{key}: waiver for a row that does not need one"
               for key in sorted(set(waivers) - needed)]
    return errors


def check_code(rows: list[Row], scanned: dict[str, tuple],
               sites: dict[str, tuple[str, ...]]) -> list[str]:
    """Code and table name the same suggestions, analyzer by analyzer."""
    errors = []
    produced: dict[str, set[str]] = {}
    declared = set()
    for module, (names, dynamic) in scanned.items():
        produced.setdefault(module, set()).update(names)
        for site in dynamic:
            declared.add(site)
            if site not in sites:
                errors.append(f"{site}: variable name, declare it in "
                              f"DYNAMIC_SITES")
                continue
            produced[module].update(sites[site])
    errors += [f"{site}: in DYNAMIC_SITES but not in the code"
               for site in sorted(set(sites) - declared)]

    for module, names in sorted(produced.items()):
        own = [r for r in rows if r.analyzer == module]
        errors += [f"{module} suggests {name}: no row in the registry"
                   for name in sorted(names)
                   if not any(r.matches(name) for r in own)]
    for row in rows:
        names = produced.get(row.analyzer, set())
        errors += [f"{row.key}: `{pattern}` matches nothing in the code"
                   for pattern in row.patterns
                   if not fnmatch.filter(names, pattern)]
    return errors


def check_baseline(rows: list[Row], baseline: dict) -> list[str]:
    """What a corpus scan suggested, against the table."""
    errors = set()
    for log in baseline.get("logs", []):
        for analyzer, record in (log.get("analyzers") or {}).items():
            for suggestion in record.get("suggestions") or []:
                name = suggestion.split("=", 1)[0]
                if not any(r.analyzer == analyzer and r.matches(name)
                           for r in rows):
                    errors.add(f"{analyzer} suggested {name} in a scan: "
                               f"no row in the registry")
    return sorted(errors)


def _report(title: str, errors: list[str]) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    if not errors:
        print(f"  {GREEN}PASS{RESET}")
    for error in errors:
        print(f"  {RED}FAIL{RESET}  {error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check docs/suggestion_evidence.md against the code")
    parser.add_argument("--baseline", nargs="*", default=[],
                        help="baseline_run.py output to check as well")
    args = parser.parse_args(argv)

    with open(REGISTRY_PATH, encoding="utf-8") as f:
        rows, errors = parse_registry(f.read())
    errors += check_rows(rows, NOT_FLOWN_WAIVERS)
    _report("Registry rows", errors)
    code_errors = check_code(rows, scan_analyzers(), DYNAMIC_SITES)
    _report("Registry against the code", code_errors)
    errors += code_errors
    for path in args.baseline:
        with open(path, encoding="utf-8") as f:
            scan_errors = check_baseline(rows, json.load(f))
        _report(f"Registry against {os.path.basename(path)}", scan_errors)
        errors += scan_errors

    flown = sum(r.status == "flown" for r in rows)
    print(f"\n  {len(rows)} rows, {flown} flown, "
          f"{len(NOT_FLOWN_WAIVERS)} waived")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
