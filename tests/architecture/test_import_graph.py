"""Entry modules must not import one another.

The project linter checks `lib` and `analyzers`; this test covers the entry
layer. The current violations are expected to disappear with the application
boundary, so the test is marked xfail until Phase 4 removes the marker.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._repo import REPO_ROOT

ENTRY_MODULES = (
    "web_app",
    "flight_report",
    "multi_report",
    "batch_report",
    "fleet_report",
)


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


@pytest.mark.fast
@pytest.mark.xfail(
    strict=True,
    reason="Phase 4 removes the entry-to-entry imports",
)
def test_entry_modules_do_not_import_each_other():
    offenders = []
    for name in ENTRY_MODULES:
        path = REPO_ROOT / f"{name}.py"
        for module, lineno in _imported_modules(path):
            top = module.split(".", 1)[0]
            if top in ENTRY_MODULES and top != name:
                offenders.append(f"{name}.py:{lineno} imports {module}")

    assert not offenders, "\n".join(offenders)
