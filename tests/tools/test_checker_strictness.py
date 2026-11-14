"""The checkers must reject what they exist to reject."""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import ast
import sys

import pytest

_TOOLS = str(TOOLS_DIR)
sys.path.insert(0, _TOOLS)

import check_analyzers  # noqa: E402
import check_msg_fields  # noqa: E402

pytestmark = pytest.mark.fast


class TestEvidenceSource:
    @pytest.mark.parametrize("code", [
        "# evidence_source is set elsewhere\nx = 1",
        '"""evidence_source documented only here."""',
        "text = 'evidence_source'",
    ])
    def test_the_word_alone_does_not_count(self, code):
        assert not check_analyzers._sets_evidence_source(ast.parse(code))

    @pytest.mark.parametrize("code", [
        "evidence_source = t('x')",
        "result.evidence_source = t('x')",
        "self.make_result(title='x', evidence_source=t('x'))",
    ])
    def test_setting_it_counts(self, code):
        assert check_analyzers._sets_evidence_source(ast.parse(code))


def test_annotated_empty_required_messages_is_caught(tmp_path, monkeypatch):
    pkg = tmp_path / "analyzers"
    pkg.mkdir()
    (pkg / "fake.py").write_text(
        "class FakeAnalyzer(BaseAnalyzer):\n"
        "    registry_key = 'fake'\n"
        "    sort_order = 1\n"
        "    required_messages: list[str] = []\n"
        "    def analyze(self):\n"
        "        return None\n", encoding="utf-8")
    monkeypatch.setattr(check_analyzers, "PROJECT_ROOT", str(tmp_path))
    assert any("required_messages is empty" in e
               for e in check_analyzers.check_contracts())


def test_the_entry_points_are_scanned_for_field_names():
    assert "multi_report.py" in check_msg_fields.ENTRY_POINTS
