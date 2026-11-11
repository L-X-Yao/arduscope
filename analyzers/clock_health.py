"""
Clock Health Analyzer: detects timestamp issues in log data.

Checks ATT message timestamps for monotonicity violations, jitter
(std/median ratio), and large gaps (dt > 10x median).

Required: ATT

Title: 时钟健康
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t
from . import AnalysisResult, BaseAnalyzer


class ClockHealthAnalyzer(BaseAnalyzer):
    """
    Clock health analyzer.

    Generates:
      - Timestamp interval time series highlighting anomalies
    """

    registry_key = "clock_health"
    sort_order = 92
    required_messages = ["ATT"]

    def analyze(self) -> AnalysisResult:
        details: list[str] = []
        figures: list[go.Figure] = []

        # -----------------------------------------------------------
        # 1. Read ATT timestamps
        # -----------------------------------------------------------
        att = self.reader.get_messages_as_arrays("ATT", ["Roll"])
        ts = att["_timestamp"]

        if len(ts) < 3:
            return self.make_result(
                title=_t("analyzer.clock_health"),
                status="info",
                summary=_t("clock_health.insufficient"),
            )

        # -----------------------------------------------------------
        # 2. Compute dt
        # -----------------------------------------------------------
        dt = np.diff(ts)

        # -----------------------------------------------------------
        # 3. Check monotonicity (time going backward)
        # -----------------------------------------------------------
        non_mono = int(np.sum(dt < 0))
        details.append(
            _t("clock_health.non_mono_count", count=non_mono)
        )

        # -----------------------------------------------------------
        # 4. Compute jitter: std(dt) / median(dt)
        # -----------------------------------------------------------
        # Timing is judged inside one armed window. Logging pauses between
        # flights are normal, and one such pause alone pushes std/median
        # far past the limit on a healthy board. A stall in flight stays
        # in, so the check still catches it.
        armed = self.get_armed_mask(ts)
        starts = [start for start, _ in self.reader.get_armed_intervals()]
        in_flight = (armed[:-1] & armed[1:]
                     & (np.searchsorted(starts, ts[:-1], side="right")
                        == np.searchsorted(starts, ts[1:], side="right")))

        # Use only positive dt for median/std to avoid polluting stats
        # with backward timestamps
        positive_dt = dt[in_flight & (dt > 0)]
        if len(positive_dt) < 2:
            if non_mono > 0:
                return self.make_result(
                    title=_t("analyzer.clock_health"),
                    status="critical",
                    summary=_t("clock_health.summary_critical", count=non_mono),
                    details=details,
                    action_level="physical_check",
                    plain_summary=_t("clock_health.plain_critical"),
                    action_text=_t("clock_health.action_critical"),
                    evidence_source=_t("clock_health.evidence"),
                )
            return self.make_result(
                title=_t("analyzer.clock_health"),
                status="info",
                summary=_t("common.never_armed"
                           if self.reader.never_armed()
                           else "clock_health.insufficient"),
            )

        median_dt = float(np.median(positive_dt))
        if median_dt <= 0:
            return self.make_result(
                title=_t("analyzer.clock_health"),
                status="info",
                summary=_t("clock_health.insufficient"),
            )

        std_dt = float(np.std(positive_dt))
        jitter = std_dt / median_dt

        details.append(
            _t("clock_health.median_dt",
               val=f"{median_dt * 1000:.2f}")
        )
        details.append(
            _t("clock_health.jitter", val=f"{jitter:.2f}")
        )

        # -----------------------------------------------------------
        # 5. Check for gaps: dt > 10 * median(dt)
        # -----------------------------------------------------------
        gap_threshold = 10.0 * median_dt
        gap_count = int(np.sum(in_flight & (dt > gap_threshold)))
        details.append(
            _t("clock_health.gap_count",
               count=gap_count,
               threshold=f"{gap_threshold * 1000:.1f}")
        )

        # -----------------------------------------------------------
        # 6. Determine status
        # -----------------------------------------------------------
        jitter_warn = self.thresholds.clock_jitter_warn
        gap_warn = self.thresholds.clock_gap_count_warn

        if non_mono > 0:
            status = "critical"
        elif jitter > jitter_warn or gap_count > gap_warn:
            status = "warning"
        else:
            status = "good"

        # -----------------------------------------------------------
        # 7. Figure: timestamp intervals
        # -----------------------------------------------------------
        t_mid = (ts[:-1] + ts[1:]) / 2.0
        dt_ms = dt * 1000.0

        fig = self.make_figure(
            title=_t("clock_health.fig_title"),
            xaxis_title=_t("clock_health.time_s"),
            yaxis_title=_t("clock_health.interval_ms"),
            height=350,
        )

        # Color-code anomalies
        backward_mask = dt < 0
        gap_mask = in_flight & (dt > gap_threshold)
        normal_mask = in_flight & ~backward_mask & ~gap_mask

        fig.add_trace(go.Scattergl(
            x=t_mid[normal_mask],
            y=dt_ms[normal_mask],
            mode='markers',
            marker=dict(size=2, color='#1f77b4'),
            name=_t("clock_health.normal"),
        ))

        if np.any(gap_mask):
            fig.add_trace(go.Scattergl(
                x=t_mid[gap_mask],
                y=dt_ms[gap_mask],
                mode='markers',
                marker=dict(size=4, color='#f0ad4e'),
                name=_t("clock_health.gap"),
            ))

        if np.any(backward_mask):
            fig.add_trace(go.Scattergl(
                x=t_mid[backward_mask],
                y=dt_ms[backward_mask],
                mode='markers',
                marker=dict(size=4, color='red'),
                name=_t("clock_health.backward"),
            ))

        self.add_threshold_hline(
            fig, gap_threshold * 1000,
            _t("clock_health.gap_line",
               val=f"{gap_threshold * 1000:.1f}"),
            color="#f0ad4e",
        )

        figures.append(fig)

        # -----------------------------------------------------------
        # 8. Summary and action
        # -----------------------------------------------------------
        if status == "critical":
            summary = _t("clock_health.summary_critical",
                         count=non_mono)
            action_level = "physical_check"
            plain_summary = _t("clock_health.plain_critical")
            action_text = _t("clock_health.action_critical")
            evidence_source = _t("clock_health.evidence")
        elif status == "warning":
            summary = _t("clock_health.summary_warning",
                         jitter=f"{jitter:.1f}",
                         gaps=gap_count)
            action_level = "physical_check"
            plain_summary = _t("clock_health.plain_warning")
            action_text = _t("clock_health.action_warning")
            evidence_source = _t("clock_health.evidence")
        else:
            summary = _t("clock_health.summary_good")
            action_level = "ok"
            plain_summary = ""
            action_text = ""
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.clock_health"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
        )
