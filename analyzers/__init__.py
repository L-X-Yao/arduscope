"""
Analyzer framework: BaseAnalyzer base class and auto-discovery.

AnalysisResult and action/confidence constants live in lib/results.py
to avoid circular dependencies between lib/ and analyzers/.
Everything is re-exported here for backward compatibility.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
from typing import Optional

import numpy as np
import plotly.graph_objects as go

from lib.log_reader import LogReader
from lib.outputs import motor_channels
from lib.results import (  # noqa: F401 — re-export
    ACTION_APPLY_PARAM,
    ACTION_OK,
    ACTION_PHYSICAL_CHECK,
    ACTION_RUN_AUTOTUNE,
    ACTION_SAFETY_STOP,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    VALID_ACTION_LEVELS,
    VALID_CONFIDENCE_LEVELS,
    AnalysisResult,
    derive_default_action_level,
    worst_action_level,
)
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo


class BaseAnalyzer:
    """
    Base class for all flight log analyzers.

    Subclasses must:
      1. Set `registry_key` (unique string identifier for --skip/--only)
      2. Set `required_messages` (list of message type strings)
      3. Implement `analyze()` returning an AnalysisResult
      4. Optionally set `optional_messages`
      5. Optionally set `sort_order` (lower = earlier in report, default 100)

    Usage:
        analyzer = MyAnalyzer(reader, thresholds, vehicle_info)
        if analyzer.can_run():
            result = analyzer.analyze()
    """

    # Unique key for this analyzer (used for --skip/--only filtering)
    registry_key: str = ""

    # Message types that MUST be present for this analyzer to run
    required_messages: list[str] = []

    # Message types that are used if available but not strictly required
    optional_messages: list[str] = []

    # Sort order in report output (lower = earlier). Default 100.
    sort_order: int = 100

    def __init__(self, reader: LogReader, thresholds: Thresholds,
                 vehicle_info: VehicleInfo):
        self.reader = reader
        self.thresholds = thresholds
        self.vehicle = vehicle_info

    def can_run(self) -> bool:
        """Check if all required message types exist in the log."""
        for msg_type in self.required_messages:
            if not self.reader.has_message(msg_type):
                return False
        return True

    def analyze(self) -> AnalysisResult:
        """
        Execute the analysis. Must be overridden by subclasses.

        Returns:
            AnalysisResult with status, summary, details, and figures.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement analyze()"
        )

    # ------------------------------------------------------------------
    # Helper methods for subclasses
    # ------------------------------------------------------------------

    def make_result(self, title: str, status: str = "info",
                    summary: str = "", details: Optional[list[str]] = None,
                    figures: Optional[list[go.Figure]] = None,
                    sub_results: Optional[list[AnalysisResult]] = None,
                    *,
                    confidence: str = CONFIDENCE_HIGH,
                    action_level: Optional[str] = None,
                    action_text: str = "",
                    plain_summary: str = "",
                    evidence: Optional[list[dict]] = None,
                    evidence_source: str = "",
                    param_suggestions: Optional[list[dict]] = None,
                    ) -> AnalysisResult:
        """
        Convenience factory for creating an AnalysisResult.

        New keyword-only fields (action_level, plain_summary, etc.) are
        optional. When omitted, action_level falls back to a conservative
        mapping from `status` (see derive_default_action_level).

        POSITIONING.md mandates every warning/critical cite an
        authoritative source via ``evidence_source``.
        """
        return AnalysisResult(
            title=title,
            status=status,
            summary=summary,
            details=details or [],
            figures=figures or [],
            sub_results=sub_results or [],
            confidence=confidence,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence=evidence or [],
            evidence_source=evidence_source,
            param_suggestions=param_suggestions or [],
        )

    def make_figure(self, title: str = "",
                    xaxis_title: str = "",
                    yaxis_title: str = "",
                    height: int = 400) -> go.Figure:
        """
        Create a plotly Figure with standard layout.

        Returns a Figure ready to have traces added.
        """
        fig = go.Figure()
        fig.update_layout(
            title=dict(text=title, font=dict(size=14)),
            xaxis_title=xaxis_title,
            yaxis_title=yaxis_title,
            height=height,
            margin=dict(l=60, r=30, t=50, b=50),
            template="plotly_white",
            font=dict(
                family="PingFang SC, Microsoft YaHei, Noto Sans SC, sans-serif",
                size=12,
            ),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="right",
                x=1,
            ),
        )
        return fig

    def add_threshold_hline(self, fig: go.Figure, y: float,
                            label: str, color: str = "red",
                            dash: str = "dash"):
        """Add a horizontal threshold line to a figure."""
        fig.add_hline(
            y=y,
            line_dash=dash,
            line_color=color,
            annotation_text=label,
            annotation_position="top left",
            annotation_font_color=color,
            annotation_font_size=10,
        )

    def worst_status(self, *statuses: str) -> str:
        """Return the worst (most severe) status from the given statuses."""
        priority = {'critical': 3, 'warning': 2, 'info': 1, 'good': 0}
        worst = 'good'
        for s in statuses:
            if priority.get(s, 0) > priority.get(worst, 0):
                worst = s
        return worst

    # ------------------------------------------------------------------
    # Data helpers — eliminate repeated boilerplate across analyzers
    # ------------------------------------------------------------------

    def get_armed_mask(self, ts) -> np.ndarray:
        """Return a boolean mask selecting samples within armed intervals.

        A log that shows it never armed has none. Falls back to all-True
        when the log cannot say: one with no arming events and no
        POWR.Safety may hold a whole flight.
        """
        armed = self.reader.get_armed_intervals()
        if not armed and not self.reader.never_armed():
            return np.ones(len(ts), dtype=bool)
        mask = np.zeros(len(ts), dtype=bool)
        for s, e in armed:
            mask |= (ts >= s) & (ts <= e)
        return mask

    @staticmethod
    def safe_interp(x, xp, fp):
        """np.interp that filters NaN/Inf from source arrays first."""
        good = np.isfinite(xp) & np.isfinite(fp)
        if np.sum(good) < 2:
            return np.full_like(x, np.nan, dtype=float)
        return np.interp(x, xp[good], fp[good])

    def read_primary_instance(self, msg_type: str, fields: list[str],
                              inst_col: str) -> dict:
        """Read `fields` of `msg_type` for the primary (lowest) instance.

        Many ArduPilot messages interleave instances at shared
        timestamps under different column names — BAT.Inst, VIBE.IMU,
        XKF*/NKF*.C (core), GPS.I, RFND.Instance. Reducing across the
        interleaved rows mixes independent sensors/cores: it dilutes
        fractions by the instance count, lets a non-primary spike drive
        the max, and inflates FFT sample rates. This selects the lowest
        instance index so the series is a single physical sensor/lane.

        Falls back to the raw (unfiltered) data when the instance column
        is absent or all-NaN (old single-instance logs).
        """
        data = self.reader.get_messages_as_arrays(
            msg_type, [inst_col] + list(fields))
        ts = data["_timestamp"]
        inst = data.get(inst_col)
        if inst is None or len(ts) == 0:
            return data
        valid = inst[~np.isnan(inst)]
        if len(valid) == 0:
            return data  # old-style log without an instance field
        primary = int(np.min(valid))
        mask = np.nan_to_num(inst, nan=-1).astype(int) == primary
        out = {"_timestamp": ts[mask]}
        for f in fields:
            out[f] = data[f][mask]
        return out

    def read_primary_imu(self, fields: list[str]) -> dict:
        """Read IMU `fields` for the primary instance (lowest I) only.

        get_messages_as_arrays("IMU", …) returns every IMU instance
        interleaved (IMU0/1/2 share each cycle's TimeUS). Feeding that
        straight into an FFT is wrong: the samples are not a uniform
        single-sensor time series, so the sample-rate estimate is
        inflated by the instance count and the spectrum picks up ghost
        peaks. This selects one instance so dt and the FFT are physical.

        Falls back to the raw (unfiltered) data for old-style logs with
        no `I` field.
        """
        return self.read_primary_instance("IMU", fields, "I")

    def motor_channels(self) -> list[str]:
        """Output channels that drive lift motors, in motor order.

        Empty on a plane with no VTOL motors. Falls back to C1..Cn only
        when the log carries no SERVOn_FUNCTION motor assignment at all.
        """
        channels = motor_channels(self.reader)
        if channels:
            return channels
        if self.vehicle.is_plane and not self.vehicle.is_quadplane:
            return []
        return [f"C{i}" for i in range(1, (self.vehicle.motor_count or 4) + 1)]

    def motor_param(self, name: str) -> str:
        """Return the correct MOT_* or Q_M_* parameter name."""
        if self.vehicle.is_quadplane:
            return f"Q_M_{name}"
        return f"MOT_{name}"

    def rate_param(self, name: str) -> str:
        """Return the correct ATC_RAT_* or Q_A_RAT_* parameter name."""
        if self.vehicle.is_quadplane:
            return f"Q_A_RAT_{name}"
        return f"ATC_RAT_{name}"


def discover_analyzers() -> list[tuple[str, type[BaseAnalyzer]]]:
    """
    Auto-discover all analyzer classes in the analyzers/ package.

    Scans all modules in this directory, imports them, and collects
    any BaseAnalyzer subclass that has a non-empty `registry_key`.
    pkgutil sees .py files and the same-directory .pyc files of the
    Windows package alike.

    Returns:
        Sorted list of (registry_key, analyzer_class) tuples,
        ordered by sort_order then registry_key.
    """
    found: list[tuple[str, type[BaseAnalyzer]]] = []
    package_dir = os.path.dirname(__file__)
    module_names: set[str] = set()

    for _importer, modname, _ispkg in pkgutil.iter_modules([package_dir]):
        if not modname.startswith('_'):
            module_names.add(modname)

    for modname in sorted(module_names):
        try:
            module = importlib.import_module(f'.{modname}', package=__name__)
        except Exception as e:
            import warnings
            warnings.warn(f"Failed to load analyzer '{modname}': {e}")
            continue

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, BaseAnalyzer)
                    and obj is not BaseAnalyzer
                    and obj.registry_key):
                found.append((obj.registry_key, obj))

    # Sort by sort_order, then alphabetically by key
    found.sort(key=lambda x: (x[1].sort_order, x[0]))
    return found
