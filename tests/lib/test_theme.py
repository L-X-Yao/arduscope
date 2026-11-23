"""The shared theme tokens are the single home of the duplicated values."""

from __future__ import annotations

import ast

import pytest

from tests._repo import REPO_ROOT

from arduscope.lib.theme import COLORS, FONT_MONO, FONT_STACK, \
    FONT_STACK_CSS, FONT_STACK_CSS_COMPACT, FONT_STACK_PLOTLY

pytestmark = pytest.mark.fast

CONSUMERS = (
    "src/arduscope/lib/html_report.py",
    "src/arduscope/lib/comparison_report.py",
    "batch_report.py",
    "src/arduscope/lib/web_pages.py",
)


def test_font_stack_tokens_are_correct():
    assert FONT_STACK == ("PingFang SC", "Microsoft YaHei", "Noto Sans SC")
    assert FONT_MONO == "monospace"
    assert FONT_STACK_CSS == \
        '"PingFang SC", "Microsoft YaHei", "Noto Sans SC",'
    assert FONT_STACK_CSS_COMPACT == \
        '"PingFang SC","Microsoft YaHei","Noto Sans SC",'
    assert FONT_STACK_PLOTLY == \
        "PingFang SC, Microsoft YaHei, Noto Sans SC"


def test_colors_tokens_are_correct():
    assert COLORS["accent"] == "#409eff"


REQUIRED_TOKENS = {
    "src/arduscope/lib/html_report.py":
        ("FONT_STACK_CSS", "COLORS"),
    "src/arduscope/lib/comparison_report.py":
        ("FONT_MONO", "FONT_STACK_CSS", "FONT_STACK_PLOTLY"),
    "batch_report.py":
        ("COLORS", "FONT_MONO", "FONT_STACK_CSS_COMPACT"),
    "src/arduscope/lib/web_pages.py":
        ("COLORS", "FONT_STACK_CSS_COMPACT"),
}


def test_every_consumer_imports_and_uses_the_theme():
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "from arduscope.lib.theme import" in source \
            or "from .theme import" in source, rel
        # Import aliases are ast.alias nodes, not ast.Name, so only real
        # uses (including f-string expressions and subscripts) count.
        used = {node.id for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Name)}
        for token in REQUIRED_TOKENS[rel]:
            assert token in used, (rel, token)


def test_migrated_surfaces_no_longer_carry_the_font_literal():
    # Scope: only the four surfaces this phase migrated. The full CJK
    # stack still exists by design in surfaces out of Phase 8 scope --
    # Plotly figure fonts in analyzers/__init__.py, motor.py, wind.py,
    # pid_tuning.py and fleet_history.py, plus fleet_report.py's own
    # shorter stack -- the brief says those literals stay untouched.
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "PingFang" not in source, rel


def test_accent_literal_leaves_the_consumer_surfaces():
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "#409eff" not in source, rel


def test_accent_literal_leaves_the_lib_modules():
    # Scope note: this sweep covers src/arduscope/lib only;
    # batch_report.py is covered by the consumer assertions above, and the
    # remaining entry scripts and analyzers are outside this phase's scan.
    offenders = []
    for path in (REPO_ROOT / "src" / "arduscope" / "lib").rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        if rel != "src/arduscope/lib/theme.py" and "#409eff" in text:
            offenders.append(rel)
    assert offenders == []
