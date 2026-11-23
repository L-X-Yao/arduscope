"""Check the model's claims against the tool calls it cites.

The model submits observations with the sequence numbers of the tool
calls behind them. A claim whose numbers are not in those results, or
whose citation does not exist, is marked unverified and counted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_OBSERVATIONS = 20

_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


@dataclass
class ToolRecord:
    seq: int
    name: str
    arguments: dict
    content: str
    is_error: bool = False


@dataclass
class Observation:
    text: str
    cites: list[int] = field(default_factory=list)
    kind: str = "observation"
    unverified: bool = False
    reason: str = ""


@dataclass
class Submission:
    summary: str
    observations: list[Observation]
    unverified_count: int
    summary_unverified: bool = False


def _numbers(text: str) -> list[float]:
    values = []
    for token in _NUMBER_RE.findall(text):
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def _content_numbers(records: list[ToolRecord]) -> list[float]:
    values = []
    for record in records:
        values.extend(_numbers(record.content))
    return values


def _close(value: float, other: float) -> bool:
    return abs(value - other) <= max(0.01, abs(value) * 0.01)


def _unique_numbers(known: list[float]) -> list[float]:
    seen = set()
    unique = []
    for value in known:
        key = round(value, 6)
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _matches(value: float, known: list[float]) -> bool:
    """The number, or a simple arithmetic result from two cited numbers.

    A model that writes a difference, a percentage or a ratio is still
    grounded when both operands are in the cited result; refusing those
    would mark honest arithmetic as invented. With a long message series
    the pair check would be quadratic, so it is skipped there: a derived
    figure is then unverified unless it is also a raw value.
    """
    known = _unique_numbers(known)
    if any(_close(value, other) for other in known):
        return True
    if len(known) > 200:
        return False
    for i, left in enumerate(known):
        for right in known[i + 1:]:
            candidates = [left + right, abs(left - right), left * right]
            if right:
                candidates.append(abs(left / right))
                candidates.append(abs(left / right) * 100.0)
                candidates.append(abs(left - right) / abs(right) * 100.0)
            if left:
                candidates.append(abs(right / left))
                candidates.append(abs(right / left) * 100.0)
                candidates.append(abs(left - right) / abs(left) * 100.0)
            if any(_close(value, candidate) for candidate in candidates):
                return True
    return False


def validate(raw: dict, records: list[ToolRecord]) -> Submission:
    """Turn the model's submission into observations, marking the doubts."""
    if not isinstance(raw, dict):
        raw = {}
    summary = str(raw.get("summary") or "").strip()
    rows = raw.get("observations")
    if not isinstance(rows, list):
        rows = []

    by_seq = {record.seq: record for record in records}
    observations: list[Observation] = []
    unverified = 0

    for row in rows[:MAX_OBSERVATIONS]:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        kind = str(row.get("kind") or "observation")
        if kind not in ("observation", "correlation", "data_gap"):
            kind = "observation"
        is_gap = kind == "data_gap"
        cites: list[int] = []
        reason = ""
        # A model may send one number instead of a list, or 3.0 for 3.
        # Neither is a wrong citation, and a bare number must not crash.
        raw_cites = row.get("cites")
        if type(raw_cites) in (int, float):  # bool is an int subclass
            raw_cites = [raw_cites]
        if not isinstance(raw_cites, list):
            raw_cites = []
        for cite in raw_cites:
            if isinstance(cite, float) and cite.is_integer():
                cite = int(cite)
            if isinstance(cite, bool) or not isinstance(cite, int):
                if not is_gap:
                    reason = "bad_citation"
                continue
            if cite not in by_seq:
                if not is_gap:
                    reason = "unknown_citation"
                continue
            if by_seq[cite].is_error and not is_gap:
                reason = "error_citation"
                continue
            if cite not in cites:
                cites.append(cite)

        if is_gap:
            # A data gap names what is missing. It needs no citation, and
            # its evidence is often an error, but a figure it repeats must
            # still exist somewhere in the tool results.
            known = _content_numbers([record for record in records
                                      if not record.is_error])
            for value in _numbers(text):
                if not _matches(value, known):
                    reason = "number_not_in_citation"
                    break
        elif not cites and not reason:
            reason = "no_citation"
        elif cites:
            known = _content_numbers([by_seq[c] for c in cites])
            for value in _numbers(text):
                if not _matches(value, known):
                    reason = "number_not_in_citation"
                    break

        observation = Observation(
            text=text, cites=cites, kind=kind,
            unverified=bool(reason), reason=reason,
        )
        if observation.unverified:
            unverified += 1
        observations.append(observation)

    known = _content_numbers([record for record in records
                              if not record.is_error])
    summary_unverified = any(not _matches(value, known)
                             for value in _numbers(summary))
    return Submission(summary=summary, observations=observations,
                      unverified_count=unverified,
                      summary_unverified=summary_unverified)


def records_as_text(records: list[ToolRecord]) -> str:
    """A compact transcript for the card's citation list."""
    return json.dumps([{"call": r.seq, "tool": r.name,
                        "error": r.is_error} for r in records],
                      ensure_ascii=False)
