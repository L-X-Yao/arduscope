"""The shared theme tokens are the single home of the duplicated values."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests._repo import REPO_ROOT

from arduscope.lib.theme import COLORS, FONT_STACK, FONT_STACK_CSS, \
    FONT_STACK_CSS_COMPACT, FONT_STACK_PLOTLY

pytestmark = pytest.mark.fast

CONSUMERS = (
    "src/arduscope/lib/html_report.py",
    "src/arduscope/lib/comparison_report.py",
    "batch_report.py",
    "src/arduscope/lib/web_pages.py",
)


def test_font_stack_tokens_are_correct():
    assert FONT_STACK == ("PingFang SC", "Microsoft YaHei", "Noto Sans SC")
    assert FONT_STACK_CSS == \
        '"PingFang SC", "Microsoft YaHei", "Noto Sans SC",'
    assert FONT_STACK_CSS_COMPACT == \
        '"PingFang SC","Microsoft YaHei","Noto Sans SC",'
    assert FONT_STACK_PLOTLY == \
        "PingFang SC, Microsoft YaHei, Noto Sans SC"


def test_colors_tokens_are_correct():
    assert COLORS["accent"] == "#409eff"


def test_every_consumer_imports_the_theme():
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "from arduscope.lib.theme import" in source \
            or "from .theme import" in source, rel


def test_consumer_surfaces_no_longer_carry_the_font_literal():
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "PingFang" not in source, rel


def test_font_literal_lives_only_in_theme_py():
    # fleet_history.py renders its own Plotly figure fonts; it is not one
    # of the four report surfaces this phase migrates, and the brief says
    # other font literals stay untouched.
    allowed = {"src/arduscope/lib/theme.py",
               "src/arduscope/lib/fleet_history.py"}
    offenders = []
    for path in (REPO_ROOT / "src" / "arduscope" / "lib").rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in allowed:
            continue
        if "PingFang" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if "PingFang" in source:
            offenders.append(rel)
    assert offenders == []


def test_accent_literal_leaves_the_consumer_surfaces():
    for rel in CONSUMERS:
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "#409eff" not in source, rel


def test_the_accent_literal_is_only_defined_in_the_theme():
    offenders = []
    for path in (REPO_ROOT / "src" / "arduscope" / "lib").rglob("*.py"):
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        if rel != "src/arduscope/lib/theme.py" and "#409eff" in text:
            offenders.append(rel)
    for rel in CONSUMERS:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        if "#409eff" in text:
            offenders.append(rel)
    assert offenders == []


def test_theme_module_has_no_behaviour():
    assert not hasattr(sys.modules["arduscope.lib.theme"], "analyze")
