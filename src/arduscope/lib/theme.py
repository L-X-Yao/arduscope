"""Shared report theme tokens.

Only values that at least two report surfaces already write identically
live here. Consumers interpolate the tokens into their own CSS with their
own spacing and separators, so every generated style block stays
byte-identical to its pre-theme output.

Palette roles not listed here (background, surface, border, text, muted,
good, warning, critical) differ between surfaces -- the single-log reports
use the Bootstrap-derived palette while the web surfaces use Element-UI
colors and light/dark CSS variables -- so they must stay per-surface.
"""

FONT_STACK = ("PingFang SC", "Microsoft YaHei", "Noto Sans SC")

# Ready-to-interpolate renderings of the stack; the trailing comma is part
# of every CSS use site. The Plotly form is unquoted.
_QUOTED = tuple(f'"{name}"' for name in FONT_STACK)
FONT_STACK_CSS = ", ".join(_QUOTED) + ","
FONT_STACK_CSS_COMPACT = ",".join(_QUOTED) + ","
FONT_STACK_PLOTLY = ", ".join(FONT_STACK)

# The shared monospace value; html_report's 'Consolas', 'Monaco', monospace
# stack is a different, single-surface stack and stays literal.
FONT_MONO = "monospace"

COLORS = {
    # Interactive accent: links, buttons, focus rings, the map accent and
    # info toasts. Every use in every surface is this exact literal.
    "accent": "#409eff",
}
