"""Optional online analysis agent.

The offline analyzers remain the authority. This package adds a model that
reads their results through bounded, read-only tools, checks them against
the log, and writes an explanation whose claims cite the tool calls behind
them. It never changes a verdict, the health score, or a parameter
suggestion.

Public entry point: :func:`analyze_with_agent`.
"""

from __future__ import annotations

from .card import analyze_with_agent

__all__ = ["analyze_with_agent"]
