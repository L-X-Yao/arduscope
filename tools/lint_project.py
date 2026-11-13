#!/usr/bin/env python3
"""
ArduScope project linter -- checks cross-file consistency issues.

Checks:
  1. Locale key completeness   (t() calls vs locale JSON files)
  2. Locale placeholder parity ({param} sets must match across locales)
  3. Analyzer count consistency (code vs hardcoded numbers in docs)
  4. Import shadowing           (t used as both i18n import and local var)
  5. Locale file symmetry       (zh.json and en.json must have same keys)
  6. param_suggestions / action_level consistency
  7. Untranslated locale keys   (value equals key name)
  8. Module layering            (analyzers -> lib, never upward)

Usage:
    python3 tools/lint_project.py

Exit 0 if all checks pass, 1 if any fail.
"""

from __future__ import annotations

import json
import os
import re
import sys

# ---------------------------------------------------------------------------
# Resolve project root (parent of tools/)
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def collect_py_files(*paths: str) -> list[str]:
    """Collect .py files: each path may be a directory (searched
    recursively) or a single file (relative to root)."""
    files: list[str] = []
    for p in paths:
        full = os.path.join(PROJECT_ROOT, p)
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = sorted(d for d in dirnames
                                     if d != "__pycache__")
                for fname in sorted(filenames):
                    if fname.endswith(".py"):
                        files.append(os.path.join(dirpath, fname))
        elif os.path.isfile(full) and p.endswith(".py"):
            files.append(full)
    return files


# CLI/web entry modules also call t() — they were invisible to every
# check that scanned only analyzers/ + lib/.
ENTRY_FILES = ("flight_report.py", "multi_report.py",
               "batch_report.py", "fleet_report.py", "web_app.py")


def load_locale(lang: str) -> dict[str, str]:
    path = os.path.join(PROJECT_ROOT, "locales", f"{lang}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_placeholders(template: str) -> set[str]:
    """Extract {name} placeholders from a format string."""
    return set(re.findall(r"\{(\w+)\}", template))


# ---------------------------------------------------------------------------
# Check 1: Locale key completeness
# ---------------------------------------------------------------------------
def check_locale_key_completeness() -> list[str]:
    section("1. Locale key completeness")
    errors: list[str] = []

    # Collect all t("key") and _t("key") calls from analyzers/, lib/
    # and the entry modules
    py_files = collect_py_files("analyzers", "lib", *ENTRY_FILES)
    # Pattern: t("key") or _t("key") with single or double quotes
    t_call_pat = re.compile(r"""(?:^|(?<=\W))_?t\(\s*["']([^"']+)["']""")

    used_keys: set[str] = set()
    for fpath in py_files:
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        for m in t_call_pat.finditer(content):
            key = m.group(1)
            # Only keys containing a dot (like "vibration.good")
            if "." in key:
                used_keys.add(key)

    zh = load_locale("zh")
    en = load_locale("en")

    missing_zh: list[str] = []
    missing_en: list[str] = []
    for key in sorted(used_keys):
        if key not in zh:
            missing_zh.append(key)
        if key not in en:
            missing_en.append(key)

    if missing_zh:
        for k in missing_zh:
            fail(f"Key missing from zh.json: {k}")
        errors.extend(missing_zh)
    if missing_en:
        for k in missing_en:
            fail(f"Key missing from en.json: {k}")
        errors.extend(missing_en)

    if not errors:
        ok(f"All {len(used_keys)} used locale keys exist in both zh.json and en.json")

    return errors


# ---------------------------------------------------------------------------
# Check 2: Locale placeholder consistency
# ---------------------------------------------------------------------------
def check_locale_placeholder_consistency() -> list[str]:
    section("2. Locale placeholder consistency")
    errors: list[str] = []

    zh = load_locale("zh")
    en = load_locale("en")

    common_keys = set(zh.keys()) & set(en.keys())
    for key in sorted(common_keys):
        zh_ph = extract_placeholders(zh[key])
        en_ph = extract_placeholders(en[key])
        if zh_ph != en_ph:
            only_zh = zh_ph - en_ph
            only_en = en_ph - zh_ph
            parts = []
            if only_zh:
                parts.append(f"only in zh: {only_zh}")
            if only_en:
                parts.append(f"only in en: {only_en}")
            msg = f"Placeholder mismatch for '{key}': {'; '.join(parts)}"
            fail(msg)
            errors.append(msg)

    if not errors:
        ok(f"All {len(common_keys)} shared keys have matching placeholders")

    return errors


# ---------------------------------------------------------------------------
# Check 3: Analyzer count consistency
# ---------------------------------------------------------------------------
def check_analyzer_count() -> list[str]:
    section("3. Analyzer count consistency")
    errors: list[str] = []

    # Get actual count via discover_analyzers()
    sys.path.insert(0, PROJECT_ROOT)
    from analyzers import discover_analyzers
    actual_count = len(discover_analyzers())

    # Patterns to find hardcoded counts in docs
    count_patterns = [
        re.compile(r"(\d+)\s*个"),               # "21 个"
        re.compile(r"(\d+)\s+diagnostic"),        # "21 diagnostic"
        re.compile(r"(\d+)\s+analyzers"),         # "21 analyzers"
        re.compile(r"(\d+)\s+auto-discovered"),   # "21 auto-discovered"
    ]

    doc_files = ["README.md", "README_zh.md", "POSITIONING.md",
                 "POSITIONING_zh.md", "CLAUDE.md"]
    for doc in doc_files:
        fpath = os.path.join(PROJECT_ROOT, doc)
        if not os.path.exists(fpath):
            continue
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        for pat in count_patterns:
            for m in pat.finditer(content):
                claimed = int(m.group(1))
                if claimed != actual_count:
                    msg = (
                        f"{doc}: claims {claimed} but actual is "
                        f"{actual_count} (matched: '{m.group(0)}')"
                    )
                    fail(msg)
                    errors.append(msg)

    if not errors:
        ok(f"All docs agree: {actual_count} analyzers")

    return errors


# ---------------------------------------------------------------------------
# Check 4: Import shadowing check
# ---------------------------------------------------------------------------
def check_import_shadowing() -> list[str]:
    section("4. Import shadowing check")
    errors: list[str] = []

    py_files = collect_py_files("analyzers", "lib", *ENTRY_FILES)

    # Check files that import bare `t` in ANY position of the import
    # list (`import t`, `import set_locale, t`, ...) — the old
    # exact-line pattern missed multi-name imports entirely. `t as _t`
    # aliases are intentionally exempt.
    import_pat = re.compile(
        r"^from\s+lib\.i18n\s+import\s+([^\n#]+)", re.MULTILINE)

    def imports_bare_t(text: str) -> bool:
        for m in import_pat.finditer(text):
            names = [n.strip() for n in m.group(1).split(",")]
            if "t" in names:
                return True
        return False
    # Assignment to bare `t`: `t = ...` or `for t in` or `(t, ...` unpacking
    # Exclude keyword args like `dict(t=50)`, `margin=dict(l=60, r=60, t=50)`
    # and f-string usages like `t={val}`
    assign_pat = re.compile(
        r"""
        (?:^[ \t]*t\s*=)           # t = something (start of line / indented)
        |(?:^[ \t]*for\s+t\s+in)   # for t in ...
        |(?:,\s*t\s*(?:,|\)))      # (..., t, ...) unpacking
        """,
        re.MULTILINE | re.VERBOSE,
    )

    for fpath in py_files:
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        if not imports_bare_t(content):
            continue
        for m in assign_pat.finditer(content):
            # Get the matched text to report
            lineno = content[:m.start()].count("\n") + 1
            basename = os.path.relpath(fpath, PROJECT_ROOT)
            msg = f"{basename}:{lineno}: 't' shadows i18n import"
            fail(msg)
            errors.append(msg)

    if not errors:
        ok("No import shadowing of t found")

    return errors


# ---------------------------------------------------------------------------
# Check 5: Locale file symmetry
# ---------------------------------------------------------------------------
def check_locale_symmetry() -> list[str]:
    section("5. Locale file symmetry")
    errors: list[str] = []

    zh = load_locale("zh")
    en = load_locale("en")

    only_zh = set(zh.keys()) - set(en.keys())
    only_en = set(en.keys()) - set(zh.keys())

    if only_zh:
        for k in sorted(only_zh):
            fail(f"Key in zh.json but not en.json: {k}")
        errors.extend(sorted(only_zh))

    if only_en:
        for k in sorted(only_en):
            fail(f"Key in en.json but not zh.json: {k}")
        errors.extend(sorted(only_en))

    if not errors:
        ok(f"zh.json and en.json have identical key sets ({len(zh)} keys)")

    return errors


# ---------------------------------------------------------------------------
# Check 6: param_suggestions / action_level consistency
# ---------------------------------------------------------------------------
def check_param_action_consistency() -> list[str]:
    section("6. param_suggestions / action_level consistency")
    errors: list[str] = []

    analyzer_files = collect_py_files("analyzers")

    apply_param_pat = re.compile(
        r"""['"]apply_param['"]"""
    )
    param_suggestions_pat = re.compile(
        r"param_suggestions"
    )

    for fpath in analyzer_files:
        basename = os.path.relpath(fpath, PROJECT_ROOT)
        if basename.endswith("__init__.py"):
            continue
        with open(fpath, encoding="utf-8") as f:
            content = f.read()

        has_apply = bool(apply_param_pat.search(content))
        has_suggestions = bool(param_suggestions_pat.search(content))

        if has_apply and not has_suggestions:
            msg = (
                f"{basename}: sets action_level='apply_param' "
                f"but never references param_suggestions"
            )
            fail(msg)
            errors.append(msg)

    if not errors:
        ok("All analyzers with apply_param also use param_suggestions")

    return errors


# ---------------------------------------------------------------------------
# Check 7: Untranslated locale keys (value == key name)
# ---------------------------------------------------------------------------
def check_untranslated_keys() -> list[str]:
    section("7. Untranslated locale keys")
    errors: list[str] = []

    for lang in ("zh", "en"):
        data = load_locale(lang)
        for key, val in sorted(data.items()):
            if val == key:
                msg = f"{lang}: untranslated key '{key}' (value equals key name)"
                fail(msg)
                errors.append(msg)

    if not errors:
        ok("No untranslated locale keys found")

    return errors


# ---------------------------------------------------------------------------
# Check 8: Module layering
# ---------------------------------------------------------------------------
# analyzers -> lib is the direction. Two lib modules call discover_analyzers()
# from inside a function to break the import cycle; those are the only
# allowed references, and they must stay function-local.
LAZY_ANALYZER_FILES = {"lib/pipeline.py", "lib/fleet_history.py"}
ENTRY_MODULES = ("web_app", "flight_report", "multi_report", "batch_report",
                 "fleet_report")
_ANALYZER_IMPORT = re.compile(
    r"^\s*(?:from\s+analyzers(?:\.|\s|$)|import\s+analyzers(?:\.|\s|$))")
_ENTRY_IMPORT = re.compile(
    r"^\s*(?:from\s+(?:%s)(?:\.|\s|$)|import\s+(?:%s)(?:\.|\s|$))"
    % ("|".join(ENTRY_MODULES), "|".join(ENTRY_MODULES)))
_AGENT_IMPORT = re.compile(
    r"^\s*(?:from\s+lib\.agent(?:\.|\s|$)|import\s+lib\.agent(?:\.|\s|$)"
    r"|from\s+lib\s+import\s+agent(?:\.|\s|$))")


def check_dependency_direction() -> list[str]:
    section("8. Module layering")
    errors: list[str] = []

    def report(relpath: str, lineno: int, rule: str) -> None:
        msg = f"{relpath}:{lineno}: {rule}"
        fail(msg)
        errors.append(msg)

    for fpath in collect_py_files("lib"):
        relpath = os.path.relpath(fpath, PROJECT_ROOT)
        with open(fpath, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if _ANALYZER_IMPORT.match(line):
                    if relpath not in LAZY_ANALYZER_FILES:
                        report(relpath, lineno,
                               "lib must not import analyzers")
                    elif not line[:1].isspace():
                        report(relpath, lineno,
                               "the analyzers import must stay inside a function")
                if _ENTRY_IMPORT.match(line):
                    report(relpath, lineno,
                           "lib must not import an entry module")

    for fpath in collect_py_files("analyzers"):
        relpath = os.path.relpath(fpath, PROJECT_ROOT)
        with open(fpath, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if _AGENT_IMPORT.match(line):
                    report(relpath, lineno,
                           "analyzers must not import the online agent")
                if _ENTRY_IMPORT.match(line):
                    report(relpath, lineno,
                           "analyzers must not import an entry module")

    if not errors:
        ok("lib and analyzers only import downward")

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print(f"{BOLD}ArduScope project linter{RESET}")
    print(f"Project root: {PROJECT_ROOT}")

    all_errors: list[str] = []
    all_errors.extend(check_locale_key_completeness())
    all_errors.extend(check_locale_placeholder_consistency())
    all_errors.extend(check_analyzer_count())
    all_errors.extend(check_import_shadowing())
    all_errors.extend(check_locale_symmetry())
    all_errors.extend(check_param_action_consistency())
    all_errors.extend(check_untranslated_keys())
    all_errors.extend(check_dependency_direction())

    print()
    if all_errors:
        print(f"{RED}{BOLD}FAILED{RESET}: {len(all_errors)} issue(s) found")
        return 1
    else:
        print(f"{GREEN}{BOLD}ALL CHECKS PASSED{RESET}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
