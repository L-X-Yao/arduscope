#!/usr/bin/env python3
"""
ArduScope i18n safety checker.

Checks:
  1. Placeholder safety  — t() kwargs vs template {placeholder} names
  2. Orphaned locale keys — keys in JSON but never referenced in code
  3. Hardcoded strings    — analyzer fields that bypass t()

Usage:
    python3 tools/check_i18n.py

Exit 0 if all checks pass, 1 if any errors found.
Warnings (extra kwargs, orphaned keys) are printed but do not fail.
"""

from __future__ import annotations

import ast
import json
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


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def section(title: str) -> None:
    print(f"\n{BOLD}[{title}]{RESET}")


def load_locale(lang: str) -> dict[str, str]:
    path = os.path.join(PROJECT_ROOT, "locales", f"{lang}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_placeholders(template: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", template))


def _source_files() -> list[str]:
    files: list[str] = []
    for d in ("analyzers", "lib"):
        full = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(full):
            continue
        for fname in sorted(os.listdir(full)):
            if fname.endswith(".py"):
                files.append(os.path.join(full, fname))
    for name in ("flight_report.py", "multi_report.py", "batch_report.py",
                 "fleet_report.py", "web_app.py"):
        p = os.path.join(PROJECT_ROOT, name)
        if os.path.isfile(p):
            files.append(p)
    return files


def _is_t_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name) and node.func.id in ("t", "_t"):
        return True
    return False


def collect_t_calls() -> list[tuple[str, int, str, set[str]]]:
    """Return list of (filepath, lineno, key, {kwarg_names})."""
    results = []
    for filepath in _source_files():
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_t_call(node):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                key = first_arg.value
                kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
                relpath = os.path.relpath(filepath, PROJECT_ROOT)
                results.append((relpath, node.lineno, key, kwargs))
    return results


INDIRECT_PREFIXES = [
    "action.",
    "ekf.fig_pos_innov", "ekf.fig_vel_innov", "ekf.innov_m",
    "failsafe.subsys_",
    "multi.metric_",
    "multi.pcat_",
    "trend.metric.",
    "trend.overlay_",
]


def collect_dynamic_prefixes() -> list[str]:
    """Find f-string t() patterns like t(f"status.{x}") and extract static prefixes."""
    prefixes = list(INDIRECT_PREFIXES)
    for filepath in _source_files():
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_t_call(node):
                continue
            if not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.JoinedStr):
                parts = []
                for v in first_arg.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    else:
                        break
                if parts:
                    prefixes.append("".join(parts))
    return prefixes


JS_INTERPOLATED_KEYS = {
    "history.date_group",
}


def check_placeholder_safety(zh: dict, en: dict, calls: list) -> tuple[list[str], list[str]]:
    section("Placeholder safety")
    errors = []
    warnings = []

    for filepath, lineno, key, kwargs in calls:
        if key in JS_INTERPOLATED_KEYS:
            continue
        for lang, locale in [("zh", zh), ("en", en)]:
            template = locale.get(key)
            if template is None:
                continue
            expected = extract_placeholders(template)
            if not expected and not kwargs:
                continue

            missing = expected - kwargs
            extra = kwargs - expected
            if missing:
                msg = f"{filepath}:{lineno}  t(\"{key}\") missing {missing} (template needs them)"
                errors.append(msg)
            if extra:
                msg = f"{filepath}:{lineno}  t(\"{key}\") extra kwargs {extra} (unused by {lang} template)"
                warnings.append(msg)

    seen = set()
    deduped_errors = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            deduped_errors.append(e)
    deduped_warnings = []
    seen.clear()
    for w in warnings:
        if w not in seen:
            seen.add(w)
            deduped_warnings.append(w)

    if deduped_errors:
        for e in deduped_errors:
            fail(e)
    else:
        ok(f"All {len(calls)} t() calls have matching placeholders")

    for w in deduped_warnings:
        warn(w)

    return deduped_errors, deduped_warnings


def check_missing_keys(zh: dict, en: dict, calls: list) -> list[str]:
    """Every literal t() key must exist in both locales.

    check_placeholder_safety skips keys absent from a locale (it can
    only validate placeholders on a template it found), so without this
    check a t("typo.key") present in NEITHER locale ships silently and
    lib.i18n.t() renders the raw key string in the report.
    """
    section("Missing locale keys")
    errors = []
    seen = set()
    for filepath, lineno, key, _ in calls:
        if key in seen:
            continue
        seen.add(key)
        in_zh = key in zh
        in_en = key in en
        if not in_zh and not in_en:
            errors.append(
                f"{filepath}:{lineno}  t(\"{key}\") — missing from BOTH "
                "locales (report will show the raw key)"
            )
        elif not in_zh:
            errors.append(
                f"{filepath}:{lineno}  t(\"{key}\") — missing from zh.json "
                "(zh is the fallback locale; every key must exist there)"
            )
        elif not in_en:
            errors.append(
                f"{filepath}:{lineno}  t(\"{key}\") — missing from en.json "
                "(English falls back to Chinese, breaking the EN report)"
            )
    if errors:
        for e in errors:
            fail(e)
    else:
        ok(f"All {len(seen)} distinct t() keys exist in both locales")
    return errors


def check_orphaned_keys(zh: dict, calls: list) -> list[str]:
    section("Orphaned locale keys")

    literal_keys = {key for _, _, key, _ in calls}
    dynamic_prefixes = collect_dynamic_prefixes()

    orphaned = []
    for key in sorted(zh.keys()):
        if key.startswith("_"):
            continue
        if key in literal_keys:
            continue
        if any(key.startswith(prefix) for prefix in dynamic_prefixes):
            continue
        orphaned.append(key)

    if orphaned:
        warn(f"{len(orphaned)} potentially orphaned keys (may be dynamically used):")
        for k in orphaned:
            warn(f"  {k}")
    else:
        ok("No orphaned locale keys found")

    return orphaned


CHECKED_KWARGS = {"title", "summary", "plain_summary", "yaxis_title",
                  "xaxis_title", "action_text", "evidence_source"}


def check_hardcoded_strings() -> list[str]:
    section("Hardcoded strings in analyzers")
    errors = []

    analyzer_dir = os.path.join(PROJECT_ROOT, "analyzers")
    for fname in sorted(os.listdir(analyzer_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        filepath = os.path.join(analyzer_dir, fname)
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            continue

        relpath = os.path.relpath(filepath, PROJECT_ROOT)

        def _flag(val_node, label):
            if (isinstance(val_node, ast.Constant)
                    and isinstance(val_node.value, str)):
                val = val_node.value
                # Short tokens and spaceless ALL-CAPS (message/field
                # names like "VIBE") are legitimate literals; an
                # all-caps SENTENCE is not.
                if len(val) < 4 or (val.isupper() and " " not in val):
                    return
                errors.append(
                    f"{relpath}:{val_node.lineno}  {label}=\"{val}\""
                    f" — should use t()"
                )

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg in CHECKED_KWARGS:
                    _flag(kw.value, kw.arg)
                # details=[...] literal list items
                if kw.arg == "details" and isinstance(kw.value, ast.List):
                    for elt in kw.value.elts:
                        _flag(elt, "details[]")
            # details.append("literal")
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "details"
                    and node.args):
                _flag(node.args[0], "details.append")

    if errors:
        for e in errors:
            fail(e)
    else:
        ok("No hardcoded strings found in analyzer fields")

    return errors


def check_trace_names() -> list[str]:
    section("Plotly trace name i18n")
    warnings = []

    analyzer_dir = os.path.join(PROJECT_ROOT, "analyzers")
    for fname in sorted(os.listdir(analyzer_dir)):
        if not fname.endswith(".py") or fname == "__init__.py":
            continue
        filepath = os.path.join(analyzer_dir, fname)
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            continue

        relpath = os.path.relpath(filepath, PROJECT_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "name":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    val = kw.value.value
                    if len(val) <= 3 or val.isupper():
                        continue
                    if val in ("P", "I", "D", "FF"):
                        continue
                    warnings.append(
                        f"{relpath}:{kw.value.lineno}  name=\"{val}\" — consider t()"
                    )

    if warnings:
        for w in warnings:
            warn(w)
    else:
        ok("No hardcoded Plotly trace names found in analyzers")

    return warnings


def main() -> int:
    zh = load_locale("zh")
    en = load_locale("en")
    calls = collect_t_calls()

    print(f"Scanned {len(_source_files())} files, found {len(calls)} t() calls, {len(zh)} locale keys")

    missing_errors = check_missing_keys(zh, en, calls)
    placeholder_errors, _ = check_placeholder_safety(zh, en, calls)
    check_orphaned_keys(zh, calls)
    hardcoded_errors = check_hardcoded_strings()
    check_trace_names()

    total_errors = (
        len(missing_errors) + len(placeholder_errors) + len(hardcoded_errors)
    )
    print()
    if total_errors:
        print(f"{RED}{BOLD}FAILED{RESET}: {total_errors} error(s) found")
        return 1
    else:
        print(f"{GREEN}{BOLD}ALL PASS{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
