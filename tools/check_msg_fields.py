#!/usr/bin/env python3
"""
Field-matrix guardrail: validate every message/field reference in
analyzers/ and lib/ against tools/msg_field_table.json (generated from
the ArduPilot source tree by gen_msg_field_table.py).

The largest bug class of the 2026-07 hunt was "field renamed upstream →
get_messages_as_arrays returns all-NaN → check silently dead". This
turns that class into a CI failure at the moment the reference is
written.

Checked call shapes (literal arguments only):
    reader.get_messages_as_arrays("MSG", ["F1", "F2"])
    self.read_primary_instance("MSG", ["F1"], "InstCol")
    self.read_primary_imu(["F1"])          # message: IMU
    reader.has_message("MSG") / required_messages = ["MSG"]

Dynamic references (f-strings, variables) are counted and reported but
not validated.

Exit 0 = all references valid; 1 = violations found.
"""

from __future__ import annotations

import ast
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY_POINTS = ("flight_report.py", "multi_report.py", "batch_report.py",
                "fleet_report.py", "web_app.py")
TABLE_PATH = os.path.join(REPO_ROOT, "tools", "msg_field_table.json")

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Fields read for OLD firmware generations that the current AP tree no
# longer defines. Each entry documents why it is legitimate. Anything
# not listed here and not in the table (or the alias layer) fails.
LEGACY_FIELDS: dict[tuple[str, str], str] = {
    ("VIBE", "Clip0"): "pre-4.0 per-accel clip counters (vibration.py fallback)",
    ("VIBE", "Clip1"): "pre-4.0 per-accel clip counters",
    ("VIBE", "Clip2"): "pre-4.0 per-accel clip counters",
}

# Messages synthesized by tools, not by ArduPilot itself.
LEGACY_MESSAGES: dict[str, str] = {}

_FIELD_CALLS = {
    "get_messages_as_arrays": (0, 1, None),   # (msg_idx, fields_idx, inst_idx)
    "read_primary_instance": (0, 1, 2),
}
_MSG_ONLY_CALLS = {"has_message", "get_message_rate"}


def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_file(path: str, table: dict, aliases: set,
              errors: list[str]) -> int:
    """Returns the number of dynamic (unverifiable) references."""
    rel = os.path.relpath(path, REPO_ROOT)
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    dynamic = 0

    def check_msg(msg: str, lineno: int) -> bool:
        if msg in table or msg in LEGACY_MESSAGES:
            return True
        errors.append(f"{rel}:{lineno}  unknown message type \"{msg}\"")
        return False

    def check_field(msg: str, field: str, lineno: int) -> None:
        if field == "_timestamp":
            return
        if field in table.get(msg, ()):
            return
        if (msg, field) in aliases:
            return   # backfilled by LogReader._FIELD_ALIASES
        if (msg, field) in LEGACY_FIELDS:
            return
        errors.append(
            f"{rel}:{lineno}  \"{msg}\" has no field \"{field}\" "
            f"(reads return all-NaN — dead check)")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")

        if name in _MSG_ONLY_CALLS and node.args:
            msg = _const_str(node.args[0])
            if msg is not None:
                check_msg(msg, node.lineno)
            else:
                dynamic += 1

        elif name in _FIELD_CALLS:
            msg_i, fields_i, inst_i = _FIELD_CALLS[name]
            if len(node.args) <= fields_i:
                continue
            msg = _const_str(node.args[msg_i])
            if msg is None:
                dynamic += 1
                continue
            if not check_msg(msg, node.lineno):
                continue
            fields_node = node.args[fields_i]
            if isinstance(fields_node, ast.List):
                for elt in fields_node.elts:
                    f = _const_str(elt)
                    if f is None:
                        dynamic += 1
                    else:
                        check_field(msg, f, elt.lineno)
            else:
                dynamic += 1
            if inst_i is not None and len(node.args) > inst_i:
                inst = _const_str(node.args[inst_i])
                if inst is not None:
                    check_field(msg, inst, node.lineno)

        elif name == "read_primary_imu" and node.args:
            fields_node = node.args[0]
            if isinstance(fields_node, ast.List):
                for elt in fields_node.elts:
                    f = _const_str(elt)
                    if f is None:
                        dynamic += 1
                    else:
                        check_field("IMU", f, elt.lineno)
            else:
                dynamic += 1

        # required_messages / optional_messages assignments are class
        # bodies, not calls — handled below.

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            tnames = {t.id for t in targets if isinstance(t, ast.Name)}
            if not tnames & {"required_messages", "optional_messages"}:
                continue
            value = node.value
            if isinstance(value, ast.List):
                for elt in value.elts:
                    msg = _const_str(elt)
                    if msg is not None:
                        check_msg(msg, elt.lineno)

    return dynamic


def main() -> int:
    with open(TABLE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    table = {k: set(v) for k, v in data["messages"].items()}

    sys.path.insert(0, REPO_ROOT)
    from lib.log_reader import LogReader
    aliases = set(LogReader._FIELD_ALIASES.keys())

    errors: list[str] = []
    dynamic = 0
    n_files = 0
    for d in ("analyzers", "lib"):
        full = os.path.join(REPO_ROOT, d)
        for fname in sorted(os.listdir(full)):
            if fname.endswith(".py"):
                n_files += 1
                dynamic += scan_file(os.path.join(full, fname),
                                     table, aliases, errors)
    # The entry points read log fields too: multi_report once read a field
    # that does not exist, and nothing caught it.
    for fname in ENTRY_POINTS:
        n_files += 1
        dynamic += scan_file(os.path.join(REPO_ROOT, fname),
                             table, aliases, errors)

    src = data.get("_source", {})
    print(f"{BOLD}[Message field matrix]{RESET} "
          f"{len(table)} messages (AP @ {src.get('commit', '?')}), "
          f"{n_files} files scanned, {dynamic} dynamic refs skipped")
    if errors:
        for e in errors:
            print(f"  {RED}FAIL{RESET}  {e}")
        print(f"\n{RED}{BOLD}FAILED{RESET}: {len(errors)} invalid "
              f"field reference(s)")
        return 1
    print(f"  {GREEN}PASS{RESET}  all literal message/field references "
          f"exist in the matrix")
    print(f"\n{GREEN}{BOLD}ALL PASS{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
