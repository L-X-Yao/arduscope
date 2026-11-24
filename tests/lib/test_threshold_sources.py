"""Threshold defaults, source metadata and the published table stay in sync."""

from __future__ import annotations

import sys
from dataclasses import fields

import pytest

from tests._repo import REPO_ROOT, TOOLS_DIR

sys.path.insert(0, str(TOOLS_DIR))

import check_thresholds as ct  # noqa: E402
from arduscope.lib.threshold_sources import THRESHOLD_SOURCES  # noqa: E402
from arduscope.lib.thresholds import Thresholds  # noqa: E402

pytestmark = pytest.mark.fast


def test_source_map_has_exactly_one_entry_per_threshold():
    names = {field.name for field in fields(Thresholds)}
    assert len(names) == 139
    assert len(THRESHOLD_SOURCES) == 139
    assert set(THRESHOLD_SOURCES) == names
    assert ct.validate_sources() == []


def test_source_levels_are_valid():
    assert {source.level for source in THRESHOLD_SOURCES.values()} <= {
        "A", "A(soft)", "D", "E"}


def test_every_citation_is_non_empty():
    assert all(source.citation.strip()
               for source in THRESHOLD_SOURCES.values())


def test_document_table_matches_generated_table():
    assert ct.check_document() == []


def test_document_values_match_default_reprs():
    rows = {}
    for line in ct.DOC_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[cells[0].strip("`")] = cells
    defaults = Thresholds()
    assert len(rows) == 139
    for field in fields(Thresholds):
        assert rows[field.name][1] == repr(getattr(defaults, field.name))


def test_package_does_not_introspect_threshold_source_code():
    offenders = [str(path.relative_to(REPO_ROOT))
                 for path in (REPO_ROOT / "src" / "arduscope").rglob("*.py")
                 if "getsource(" in path.read_text(encoding="utf-8")]
    assert offenders == []


def test_render_refuses_an_orphan_source(monkeypatch):
    monkeypatch.setitem(ct.THRESHOLD_SOURCES, "orphan_threshold",
                        ct.THRESHOLD_SOURCES["vibe_warn"])
    with pytest.raises(ValueError, match="orphan source: orphan_threshold"):
        ct.render_table()


def test_check_document_reports_an_orphan_without_raising(monkeypatch):
    monkeypatch.setitem(ct.THRESHOLD_SOURCES, "orphan_threshold",
                        ct.THRESHOLD_SOURCES["vibe_warn"])
    errors = ct.check_document()
    assert "orphan source: orphan_threshold" in errors
