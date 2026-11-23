"""Typed parameter suggestions shared by analyzers and export boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParamSuggestion:
    param: str
    current: float | int | str
    suggested: float | int | str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.param, str) or not self.param:
            raise ValueError("param must be a non-empty string")

    def to_dict(self) -> dict[str, float | int | str]:
        """Return the stable wire representation used by exports."""
        return {
            "param": self.param,
            "current": self.current,
            "suggested": self.suggested,
            "reason": self.reason,
        }
