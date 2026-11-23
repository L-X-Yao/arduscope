#!/usr/bin/env python3
"""Keep threshold defaults, source citations and their documentation in sync."""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "convergence_criteria.md"
sys.path.insert(0, str(REPO_ROOT / "src"))

from arduscope.lib.threshold_sources import THRESHOLD_SOURCES  # noqa: E402
from arduscope.lib.thresholds import Thresholds  # noqa: E402

ALLOWED_LEVELS = {"A", "A(soft)", "D", "E"}
TABLE_HEADER = "| Field | Value | Source | Citation |"
TABLE_SEPARATOR = "|-------|-------|--------|----------|"


def validate_sources() -> list[str]:
    """Return structural errors in the source map and threshold defaults."""
    errors = []
    defaults = Thresholds()
    names = [field.name for field in fields(Thresholds)]
    known = set(names)
    mapped = set(THRESHOLD_SOURCES)
    for name in sorted(known - mapped):
        errors.append(f"missing source: {name}")
    for name in sorted(mapped - known):
        errors.append(f"orphan source: {name}")
    for name in names:
        value = getattr(defaults, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"non-numeric default: {name}={value!r}")
    for name, source in THRESHOLD_SOURCES.items():
        if source.level not in ALLOWED_LEVELS:
            errors.append(f"invalid source level: {name}={source.level!r}")
        if not source.citation.strip():
            errors.append(f"empty citation: {name}")
    return errors


def render_table() -> str:
    """Render the complete Markdown table in source-map order."""
    defaults = Thresholds()
    lines = [TABLE_HEADER, TABLE_SEPARATOR]
    for name, source in THRESHOLD_SOURCES.items():
        if not hasattr(defaults, name):
            raise ValueError(f"orphan source: {name}")
        value = getattr(defaults, name)
        lines.append(
            f"| `{name}` | {value!r} | {source.level} | {source.citation} |")
    return "\n".join(lines) + "\n"


def _table_span(text: str) -> tuple[list[str], int, int]:
    lines = text.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if line.rstrip("\r\n") == TABLE_HEADER:
            end = start + 1
            while end < len(lines) and lines[end].startswith("|"):
                end += 1
            return lines, start, end
    raise ValueError(f"threshold table not found in {DOC_PATH}")


def replace_table(text: str) -> str:
    """Replace only the table, preserving every line around it."""
    lines, start, end = _table_span(text)
    return "".join(lines[:start]) + render_table() + "".join(lines[end:])


def check_document() -> list[str]:
    errors = validate_sources()
    try:
        lines, start, end = _table_span(DOC_PATH.read_text(encoding="utf-8"))
    except ValueError as exc:
        return errors + [str(exc)]
    if "".join(lines[start:end]) != render_table():
        errors.append(
            "docs/convergence_criteria.md table differs; run "
            "tools/check_thresholds.py --write")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="regenerate only the documentation table")
    args = parser.parse_args(argv)
    if args.write:
        errors = validate_sources()
        if not errors:
            try:
                updated = replace_table(DOC_PATH.read_text(encoding="utf-8"))
            except ValueError as exc:
                errors = [str(exc)]
            else:
                DOC_PATH.write_text(updated, encoding="utf-8")
    else:
        errors = check_document()
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    action = "generated" if args.write else "matches source map"
    print(f"PASS: {len(THRESHOLD_SOURCES)} threshold sources; table {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
