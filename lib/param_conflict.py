"""
Detect conflicting parameter suggestions across analyzers.

When multiple analyzers suggest different values for the same parameter,
users need a heads-up so they can make an informed decision.
"""

from __future__ import annotations

from .results import AnalysisResult


def detect_param_conflicts(results: list[AnalysisResult]) -> list[dict]:
    """Collect param_suggestions from all results and flag conflicts.

    A conflict exists when 2+ analyzers suggest *different* values for
    the same parameter.  Same-value suggestions are harmless agreement.

    Returns:
        List of conflict dicts, each with:
          - param: parameter name
          - suggestions: [{source, value}, ...] from disagreeing analyzers
    """
    # param_name -> [{source, value}, ...]
    by_param: dict[str, list[dict]] = {}

    for r in results:
        _collect(by_param, r.title, r.param_suggestions)
        for sub in r.sub_results:
            _collect(by_param, f"{r.title}/{sub.title}", sub.param_suggestions)

    conflicts = []
    for param, entries in by_param.items():
        values = {e["value"] for e in entries}
        if len(values) < 2:
            continue
        conflicts.append({"param": param, "suggestions": entries})

    return conflicts


def merge_suggestions(
    results: list[AnalysisResult],
) -> tuple[list[dict], list[dict]]:
    """Suggestions safe to apply, and the conflicts a person must decide.

    Analyzers that agree collapse into one row. Picking a winner between
    analyzers that disagree would hide the disagreement in a file that
    goes straight onto an aircraft, so conflicting parameters are left out
    of the first list altogether.
    """
    conflicts = detect_param_conflicts(results)
    conflicted = {c["param"] for c in conflicts}
    merged: dict[str, dict] = {}
    for r in results:
        rows = [(r.title, s) for s in r.param_suggestions]
        for sub in r.sub_results:
            rows += [(f"{r.title}/{sub.title}", s)
                     for s in sub.param_suggestions]
        for source, s in rows:
            if s["param"] not in conflicted:
                merged.setdefault(s["param"], {**s, "source": source})
    return list(merged.values()), conflicts


def distinct_values(conflict: dict) -> list[dict]:
    """One entry per different value in a conflict, in source order."""
    by_value: dict = {}
    for entry in conflict["suggestions"]:
        by_value.setdefault(entry["value"], entry)
    return list(by_value.values())


def _collect(by_param: dict[str, list[dict]], source: str,
             suggestions: list[dict]) -> None:
    for s in suggestions:
        param = s.get("param", "")
        if not param:
            continue
        value = s.get("suggested", s.get("value", ""))
        by_param.setdefault(param, []).append({
            "source": source,
            "value": value,
        })
