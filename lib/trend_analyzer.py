"""
Trend analyzer — turns N time-ordered flight logs into a trend report.

Iterative tuning convergence workflow:

  1. Engineer flies → uploads log
  2. Tool diagnoses → outputs X, Y, Z parameter suggestions
  3. Engineer applies fixes, flies again → uploads new log
  4. Tool compares → "X improved, Y still out of range, Z now in METHODIC range"
  5. Repeat until all tracked metrics sit within METHODIC recommended ranges.

The TrendAnalyzer doesn't run the per-log analyzers itself — it takes
their outputs (or a flat metrics dict per log) and computes:
  - per-metric trend direction (improving / regressing / stable)
  - relative change vs the previous flight
  - convergence judgment against the converge_* thresholds
  - human-readable interpretation text (so the user doesn't have
    to read line charts themselves)

Sources for the convergence thresholds: see docs/convergence_criteria.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from lib.i18n import t as _t
from lib.thresholds import Thresholds


# Direction of change for a single metric across the time series.
TREND_IMPROVING = "improving"      # ↗ getting better
TREND_REGRESSING = "regressing"    # ↘ getting worse
TREND_STABLE = "stable"            # → no significant change
TREND_CONVERGED = "converged"      # ✅ at/below the perfect threshold
TREND_INSUFFICIENT = "insufficient"  # not enough data points


# For each tracked metric, encode whether "lower is better" or "higher
# is better", plus the convergence threshold field name on Thresholds.
# Adding a metric here makes the trend analyzer support it without code
# changes elsewhere.
@dataclass
class _MetricSpec:
    key: str               # dict key in the per-log metrics dict
    label: str             # short Chinese label for the report
    unit: str              # display unit
    lower_is_better: bool  # True if smaller values = better
    convergence_attr: Optional[str] = None  # name on Thresholds, or None


_METRIC_SPECS: list[_MetricSpec] = [
    # === Convergence-tracked (all must fall in METHODIC recommended range) ===
    # Source: docs/convergence_criteria.md → "Convergence" section
    _MetricSpec("max_vibe", "trend.metric.max_vibe", "m/s²", True, "converge_vibe_max"),
    _MetricSpec("max_clip", "trend.metric.max_clip", "", True, "converge_clip_max"),
    _MetricSpec("max_rate_out", "trend.metric.max_rate_out", "", True, "converge_rate_out_max"),
    _MetricSpec("hover_thr_err", "trend.metric.hover_thr_err", "", True, "converge_hover_thr_err"),
    _MetricSpec("max_ekf_var", "trend.metric.max_ekf_var", "", True, "converge_ekf_var_max"),
    # === Informational (no convergence threshold) ===
    _MetricSpec("att_rms_error", "trend.metric.att_rms_error", "°", True, None),
    _MetricSpec("min_batt_volt", "trend.metric.min_batt_volt", "V", False, None),
    # mean_hover_thrust is intentionally NOT in the list:
    # neither "lower" nor "higher" is unambiguously better — the right
    # value is "close to the ideal range" (30-50% for typical
    # multirotors). Single-axis trend analysis can't express that
    # without per-vehicle ideal-value config. Leave it informational
    # in the comparison table instead of trending it.
    _MetricSpec("error_count", "trend.metric.error_count", "", True, None),
]


def get_metric_specs() -> list[_MetricSpec]:
    """Return the list of metric specs (for tests / external introspection)."""
    return list(_METRIC_SPECS)


@dataclass
class MetricTrend:
    """Trend summary for a single metric across N logs."""
    key: str
    label: str
    unit: str
    values: list[Optional[float]]   # one per flight, in time order
    direction: str                   # TREND_*
    relative_change: float           # most recent / previous, as a ratio
    converged: bool                  # True if latest value <= threshold
    interpretation: str              # plain Chinese text
    has_threshold: bool              # whether convergence is configurable


@dataclass
class ConvergenceState:
    """Overall convergence judgment across all metrics."""
    fully_converged: bool                       # all tracked metrics passed
    converged_metric_keys: list[str] = field(default_factory=list)
    pending_metric_keys: list[str] = field(default_factory=list)
    insufficient_metric_keys: list[str] = field(default_factory=list)
    # Subset of insufficient_metric_keys that have a convergence threshold.
    # Used for headline count — excludes informational metrics (no threshold)
    # that are always in insufficient_metric_keys regardless of data volume.
    threshold_insufficient_keys: list[str] = field(default_factory=list)


@dataclass
class TrendReport:
    """The result of analyzing N logs in time order."""
    n_logs: int
    log_names: list[str]
    metrics: list[MetricTrend]
    convergence: ConvergenceState
    headline: str             # one-line plain summary
    next_step_text: str       # what to do next
    notices: list[str] = field(default_factory=list)  # limits on the above


# ====================================================================
# Internal helpers
# ====================================================================

def _number(value: Optional[float]) -> Optional[float]:
    """value, or None if it is NaN or infinite and so says nothing.

    Left in the series, NaN fails every comparison and falls through to
    "regressing", and prints as "nan" in the text.
    """
    return value if value is not None and math.isfinite(value) else None


def _direction(values: list[Optional[float]],
               lower_is_better: bool,
               threshold: Optional[float],
               *,
               significance: float = 0.05) -> tuple[str, float]:
    """
    Determine the trend direction.

    significance: minimum relative change to call it "improving" or
    "regressing" rather than "stable". Default 5%.
    """
    valid = [v for v in values if v is not None]
    # A latest flight with no data has no verdict: the value before it
    # says how an older flight went, not this one.
    if len(valid) < 2 or values[-1] is None:
        return TREND_INSUFFICIENT, 0.0

    latest = valid[-1]
    previous = valid[-2]

    # Convergence trumps direction — if we're at the perfect threshold,
    # the headline is "you're done", regardless of direction.
    if threshold is not None:
        if lower_is_better and latest <= threshold:
            return TREND_CONVERGED, 0.0
        if not lower_is_better and latest >= threshold:
            return TREND_CONVERGED, 0.0

    if previous == 0:
        # Avoid div-by-zero; use absolute delta sign instead
        delta = latest - previous
        if abs(delta) < 1e-9:
            return TREND_STABLE, 0.0
        improving = (delta < 0) if lower_is_better else (delta > 0)
        return (TREND_IMPROVING if improving else TREND_REGRESSING), 0.0

    rel_change = (latest - previous) / abs(previous)
    if abs(rel_change) < significance:
        return TREND_STABLE, rel_change

    improving = (rel_change < 0) if lower_is_better else (rel_change > 0)
    return (TREND_IMPROVING if improving else TREND_REGRESSING), rel_change


def _value_text(spec: _MetricSpec, value: float) -> str:
    return f"{value:.2f} {spec.unit}" if spec.unit else f"{value:.2f}"


def _value_only(spec: _MetricSpec, values: list[Optional[float]]) -> str:
    """Latest value with no judgement, for flights that cannot be compared."""
    valid = [v for v in values if v is not None]
    label = _t(spec.label)
    if not valid:
        return _t("trend.no_data", label=label)
    return _t("trend.not_comparable", label=label,
              latest=_value_text(spec, valid[-1]))


def _interpret(spec: _MetricSpec, values: list[Optional[float]],
               direction: str, rel_change: float,
               threshold: Optional[float]) -> str:
    """
    Build a plain-Chinese explanation of what the metric did and what
    it means. The user should NOT have to read the line chart —
    this text replaces that work.
    """
    valid = [v for v in values if v is not None]
    label = _t(spec.label)
    if not valid:
        return _t("trend.no_data", label=label)
    if values[-1] is None:
        return _t("trend.latest_missing", label=label)

    latest = valid[-1]
    n = len(valid)
    latest_str = _value_text(spec, latest)

    if direction == TREND_INSUFFICIENT:
        return _t("trend.insufficient", label=label, latest=latest_str, n=n)

    if direction == TREND_CONVERGED:
        thr_str = f"{threshold:.2f}" if threshold is not None else "?"
        if spec.unit:
            thr_str = f"{thr_str} {spec.unit}"
        return _t("trend.converged", label=label, latest=latest_str, thr=thr_str)

    if direction == TREND_STABLE:
        return _t("trend.stable", label=label, latest=latest_str)

    pct = abs(rel_change) * 100
    if direction == TREND_IMPROVING:
        text = _t("trend.improving", label=label, latest=latest_str, pct=f"{pct:.0f}")
        if threshold is not None:
            gap = abs(latest - threshold)
            if (spec.lower_is_better and latest > threshold) or \
               (not spec.lower_is_better and latest < threshold):
                text += _t("trend.gap", gap=f"{gap:.2f}")
        return text

    return _t("trend.regressing", label=label, latest=latest_str, pct=f"{abs(rel_change)*100:.0f}")


def _build_headline(convergence: ConvergenceState,
                    n_logs: int) -> str:
    """One-line top-level summary."""
    if n_logs < 2:
        return _t("trend.headline_single", n=n_logs)

    n_pending = len(convergence.pending_metric_keys)
    if convergence.fully_converged:
        return _t("trend.headline_converged", n=n_logs)
    if n_pending == 0 and convergence.threshold_insufficient_keys:
        return _t("trend.headline_insufficient",
                   n=n_logs, n_insuf=len(convergence.threshold_insufficient_keys))
    return _t("trend.headline_pending",
              n=n_logs,
              n_conv=len(convergence.converged_metric_keys),
              n_pend=n_pending)


def _build_next_step(metrics: list[MetricTrend],
                     convergence: ConvergenceState) -> str:
    """Tell the user what to do next, prioritized."""
    if convergence.fully_converged:
        return _t("trend.next_converged")

    regressing = [m for m in metrics if m.direction == TREND_REGRESSING]
    if regressing:
        names = ", ".join(_t(m.label) for m in regressing)
        return _t("trend.next_regressing", names=names)

    pending = [
        m for m in metrics
        if m.direction in (TREND_IMPROVING, TREND_STABLE) and not m.converged
        and m.has_threshold
    ]
    if pending:
        names = ", ".join(_t(m.label) for m in pending[:3])
        return _t("trend.next_pending", names=names)

    return _t("trend.next_collect")


def _distinct(logs: list[dict], key: str) -> set:
    return {m[key] for m in logs if m.get(key)}


def _notices(per_log_metrics: list[dict], flights: list[dict]) -> list[str]:
    """What the reader has to know before trusting the verdicts."""
    n_logs = len(per_log_metrics)
    if n_logs < 2:
        return []
    notices = []
    undated = sum(1 for m in per_log_metrics if m.get("flight_time") is None)
    if undated:
        notices.append(_t("trend.notice_undated", n=undated, total=n_logs))
    if len(flights) < n_logs:
        notices.append(_t("trend.notice_ground",
                          n=n_logs - len(flights), total=n_logs))
    firmware = sorted(_distinct(flights, "firmware"))
    if len(firmware) > 1:
        notices.append(_t("trend.notice_firmware",
                          versions=", ".join(firmware)))
    unconfirmed = sum(1 for m in flights if not m.get("serial"))
    if unconfirmed:
        notices.append(_t("trend.notice_unverified",
                          n=unconfirmed, total=len(flights)))
    return notices


# ====================================================================
# Public API
# ====================================================================

def analyze_trends(per_log_metrics: list[dict[str, Optional[float]]],
                   log_names: list[str],
                   thresholds: Thresholds) -> TrendReport:
    """
    Analyze a time-ordered list of per-log metric dicts.

    Args:
        per_log_metrics: list[i] = metrics dict for log i (oldest first)
        log_names: short label for each log (basename works)
        thresholds: a Thresholds instance — convergence_* fields are read
                    to decide "perfect"

    Returns:
        TrendReport with per-metric trends + overall convergence state.
    """
    if len(per_log_metrics) != len(log_names):
        raise ValueError("per_log_metrics and log_names must have same length")

    n_logs = len(per_log_metrics)
    # A ground run has no flight in it to judge. flew is None when the log
    # cannot say, which is not the same as False.
    flights = [m for m in per_log_metrics if m.get("flew") is not False]
    # What changes between two aircraft says nothing about a tuning change.
    kinds = max(len(_distinct(flights, "serial")),
                len(_distinct(flights, "vehicle")))
    comparable = kinds <= 1
    metric_trends: list[MetricTrend] = []

    converged_keys: list[str] = []
    pending_keys: list[str] = []
    insufficient_keys: list[str] = []

    for spec in _METRIC_SPECS:
        values: list[Optional[float]] = [
            _number(m.get(spec.key)) for m in flights
        ]
        threshold: Optional[float] = None
        if spec.convergence_attr:
            threshold = getattr(thresholds, spec.convergence_attr, None)

        if comparable:
            direction, rel_change = _direction(
                values, spec.lower_is_better, threshold,
            )
            interp = _interpret(
                spec, values, direction, rel_change, threshold)
        else:
            direction, rel_change = TREND_INSUFFICIENT, 0.0
            interp = _value_only(spec, values)
        converged = direction == TREND_CONVERGED

        trend = MetricTrend(
            key=spec.key,
            label=spec.label,
            unit=spec.unit,
            values=values,
            direction=direction,
            relative_change=rel_change,
            converged=converged,
            interpretation=interp,
            has_threshold=threshold is not None,
        )
        metric_trends.append(trend)

        # Bucket for the overall convergence judgment.
        # Only metrics with a configured threshold count toward
        # "fully converged". Others stay informational.
        if not trend.has_threshold:
            insufficient_keys.append(spec.key)
            continue
        if direction == TREND_INSUFFICIENT:
            insufficient_keys.append(spec.key)
        elif converged:
            converged_keys.append(spec.key)
        else:
            pending_keys.append(spec.key)

    # Only declare fully converged when ALL threshold-tracked metrics
    # have been evaluated (no INSUFFICIENT with a convergence_attr).
    insufficient_with_threshold = [
        k for k in insufficient_keys
        if any(s.key == k and s.convergence_attr for s in _METRIC_SPECS)
    ]
    convergence = ConvergenceState(
        fully_converged=(
            len(pending_keys) == 0
            and len(converged_keys) > 0
            and len(insufficient_with_threshold) == 0
        ),
        converged_metric_keys=converged_keys,
        pending_metric_keys=pending_keys,
        insufficient_metric_keys=insufficient_keys,
        threshold_insufficient_keys=insufficient_with_threshold,
    )

    if comparable:
        headline = _build_headline(convergence, n_logs)
        next_step = _build_next_step(metric_trends, convergence)
    else:
        headline = _t("trend.headline_not_comparable", n=n_logs, kinds=kinds)
        next_step = _t("trend.next_not_comparable")

    return TrendReport(
        n_logs=n_logs,
        log_names=log_names,
        metrics=metric_trends,
        convergence=convergence,
        headline=headline,
        next_step_text=next_step,
        notices=_notices(per_log_metrics, flights),
    )
