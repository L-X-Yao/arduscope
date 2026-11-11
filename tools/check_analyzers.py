#!/usr/bin/env python3
"""
ArduScope analyzer contract checker.

Validates that every analyzer subclass satisfies the BaseAnalyzer contract:
  1. registry_key is set and unique
  2. required_messages is a non-empty list (except summary which uses prescan)
  3. sort_order is explicitly set
  4. analyze() method exists
  5. evidence_source is provided for warning/critical results (POSITIONING.md)

Usage:
    python3 tools/check_analyzers.py

Exit 0 if all checks pass, 1 if any fail.
"""

from __future__ import annotations

import ast
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}[{title}]{RESET}")


def check_contracts() -> list[str]:
    section("Analyzer contracts")
    errors = []
    analyzer_dir = os.path.join(PROJECT_ROOT, "analyzers")
    seen_keys: dict[str, str] = {}

    for fname in sorted(os.listdir(analyzer_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        filepath = os.path.join(analyzer_dir, fname)
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            errors.append(f"{fname}: SyntaxError")
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [
                b.id if isinstance(b, ast.Name) else
                b.attr if isinstance(b, ast.Attribute) else ""
                for b in node.bases
            ]
            if "BaseAnalyzer" not in bases:
                continue

            cls_name = node.name
            has_registry_key = False
            has_required_messages = False
            required_messages_empty = False
            has_custom_can_run = False
            has_sort_order = False
            has_analyze = False
            registry_key_val = ""

            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name):
                            if target.id == "registry_key":
                                has_registry_key = True
                                if isinstance(item.value, ast.Constant):
                                    registry_key_val = item.value.value
                            elif target.id == "required_messages":
                                has_required_messages = True
                                if (isinstance(item.value, ast.List)
                                        and not item.value.elts):
                                    required_messages_empty = True
                            elif target.id == "sort_order":
                                has_sort_order = True
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    if item.target.id == "registry_key":
                        has_registry_key = True
                        if item.value and isinstance(item.value, ast.Constant):
                            registry_key_val = item.value.value
                    elif item.target.id == "required_messages":
                        has_required_messages = True
                        if (isinstance(item.value, ast.List)
                                and not item.value.elts):
                            required_messages_empty = True
                    elif item.target.id == "sort_order":
                        has_sort_order = True
                elif isinstance(item, ast.FunctionDef):
                    if item.name == "analyze":
                        has_analyze = True
                    elif item.name == "can_run":
                        has_custom_can_run = True

            if not has_registry_key or not registry_key_val:
                errors.append(f"{fname}:{cls_name} — missing registry_key")
            elif registry_key_val in seen_keys:
                errors.append(
                    f"{fname}:{cls_name} — duplicate registry_key "
                    f"'{registry_key_val}' (also in {seen_keys[registry_key_val]})"
                )
            else:
                seen_keys[registry_key_val] = fname

            if not has_required_messages:
                errors.append(f"{fname}:{cls_name} — missing required_messages")
            elif required_messages_empty and not has_custom_can_run:
                # An empty required_messages with the inherited can_run
                # means "runs on every log" — almost always an omission.
                errors.append(
                    f"{fname}:{cls_name} — required_messages is empty and "
                    f"can_run() is not overridden (runs unconditionally)")

            if not has_sort_order:
                errors.append(f"{fname}:{cls_name} — missing sort_order")

            if not has_analyze:
                errors.append(f"{fname}:{cls_name} — missing analyze() method")

    if errors:
        for e in errors:
            fail(e)
    else:
        ok(f"All {len(seen_keys)} analyzers satisfy contract")

    return errors


def check_evidence_source() -> list[str]:
    section("Evidence source coverage")
    errors: list[str] = []
    analyzer_dir = os.path.join(PROJECT_ROOT, "analyzers")

    for fname in sorted(os.listdir(analyzer_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        filepath = os.path.join(analyzer_dir, fname)
        with open(filepath, encoding="utf-8") as f:
            source = f.read()

        has_warning_or_critical = bool(
            re.search(r'status\s*=\s*["\'](?:warning|critical)["\']', source)
            or re.search(r'worst_status\s*\(', source)
        )

        has_evidence = _sets_evidence_source(ast.parse(source))

        if has_warning_or_critical and not has_evidence:
            errors.append(f"{fname} — can produce warning/critical but never sets evidence_source")

    if errors:
        for e in errors:
            fail(e)
    else:
        ok("All analyzers with warning/critical results provide evidence_source")

    return errors


def _sets_evidence_source(tree: ast.AST) -> bool:
    """True if code assigns or passes evidence_source.

    The bare word is not enough: a comment or a docstring holding it would
    satisfy a text search.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "evidence_source":
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for target in targets:
                name = getattr(target, "id", getattr(target, "attr", ""))
                if name == "evidence_source":
                    return True
    return False


def main() -> int:
    contract_errors = check_contracts()
    evidence_errors = check_evidence_source()

    total = len(contract_errors) + len(evidence_errors)
    print()
    if total:
        print(f"{RED}{BOLD}FAILED{RESET}: {total} error(s) found")
        return 1
    else:
        print(f"{GREEN}{BOLD}ALL PASS{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
