"""
Rangefinder Analyzer: distance measurement quality and comparison with baro.

Evaluates rangefinder health by monitoring distance, quality (signal strength),
and comparing with barometric altitude if CTUN is available.

Required: RFND
Optional: CTUN

Title: 测距传感器
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t
from . import AnalysisResult, BaseAnalyzer


class RangefinderAnalyzer(BaseAnalyzer):
    """
    Rangefinder analyzer.

    Generates:
      - Distance vs time with quality overlay
      - Quality vs time
      - Comparison with baro altitude (if CTUN available)
    """

    registry_key = "rangefinder"
    sort_order = 120
    required_messages = ["RFND"]
    optional_messages = ["CTUN"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read RFND data
        # -----------------------------------------------------------
        rfnd = self.reader.get_messages_as_arrays("RFND", [
            "I", "Dist", "Stat", "Orient",
        ])
        t = rfnd["_timestamp"]

        if len(t) == 0:
            return self.make_result(
                title=_t("analyzer.rangefinder"),
                status="info",
                summary=_t("rangefinder.empty"),
            )

        inst = rfnd["I"]
        dist = rfnd["Dist"]
        stat = rfnd["Stat"]

        # -----------------------------------------------------------
        # 2. Separate by instance (typically just 1)
        # -----------------------------------------------------------
        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )
        all_nan_inst = len(unique_instances) == 0
        if not unique_instances:
            unique_instances = [0]

        details.append(_t("rangefinder.instance_count", count=len(unique_instances)))

        overall_status = "good"
        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c']

        # Max range moved to SI units upstream: RNGFND1_MAX (m) replaced
        # RNGFND1_MAX_CM (cm). Logs arrive from both generations.
        max_range_m = self.reader.get_param("RNGFND1_MAX", 0.0)
        max_range_param = "RNGFND1_MAX"
        if max_range_m <= 0:
            max_range_m = self.reader.get_param("RNGFND1_MAX_CM", 0) / 100.0
            max_range_param = "RNGFND1_MAX_CM"
        if max_range_m > 0:
            details.append(f"{max_range_param}: {max_range_m:.1f} m")

        for inst_id in unique_instances:
            if all_nan_inst:
                mask = np.ones(len(t), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            dist_inst = dist[mask]
            stat_inst = stat[mask]
            color = instance_colors[inst_id % len(instance_colors)]
            label = (
                f"RFND{inst_id}" if len(unique_instances) > 1
                else "RFND"
            )

            # Statistics
            valid_dist = dist_inst[~np.isnan(dist_inst)]
            if len(valid_dist) > 0:
                details.append(
                    _t("rangefinder.dist_range",
                       label=label,
                       min=f"{float(np.nanmin(valid_dist)):.2f}",
                       max=f"{float(np.nanmax(valid_dist)):.2f}",
                       mean=f"{float(np.nanmean(valid_dist)):.2f}")
                )

            # Quality check (Stat: 0=NotConnected, 1=NoData,
            #   2=OutOfRangeLow, 3=OutOfRangeHigh, 4=Good)
            if np.all(np.isnan(stat_inst)):
                # Stat field not available in this firmware version
                details.append(_t("rangefinder.stat_unavailable", label=label))
            else:
                total_readings = len(stat_inst)
                # OutOfRangeHigh only says the target is beyond the sensor's
                # range (AP_RangeFinder.h Status); the sensor itself works.
                good_readings = int(np.nansum(stat_inst >= 3))
                if total_readings > 0:
                    quality_pct = good_readings / total_readings * 100
                    details.append(
                        _t("rangefinder.quality_pct",
                           label=label,
                           pct=f"{quality_pct:.1f}",
                           good=good_readings,
                           total=total_readings)
                    )

                    if quality_pct < self.thresholds.rangefinder_quality_pct_warn:
                        overall_status = self.worst_status(
                            overall_status, "warning"
                        )
                        details.append(
                            _t("rangefinder.low_quality", label=label)
                        )

        # -----------------------------------------------------------
        # 3. Figure: Distance time series with status overlay
        # -----------------------------------------------------------
        fig_dist = self.make_figure(
            title=_t("rangefinder.fig_distance"),
            xaxis_title=_t("rangefinder.time_s"),
            yaxis_title=_t("rangefinder.distance_m"),
            height=400,
        )

        for inst_id in unique_instances:
            if all_nan_inst:
                mask = np.ones(len(t), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            color = instance_colors[inst_id % len(instance_colors)]
            label = (
                f"RFND{inst_id}" if len(unique_instances) > 1
                else _t("rangefinder.distance")
            )

            fig_dist.add_trace(go.Scattergl(
                x=t[mask], y=dist[mask],
                mode='lines', name=label,
                line=dict(color=color, width=1.5),
            ))

        if max_range_m > 0:
            self.add_threshold_hline(
                fig_dist, max_range_m,
                f"MAX_RANGE ({max_range_m:.1f}m)",
                color="#d9534f",
            )

        figures.append(fig_dist)

        # -----------------------------------------------------------
        # 4. Figure: Quality/status time series
        # -----------------------------------------------------------
        fig_stat = self.make_figure(
            title=_t("rangefinder.fig_status"),
            xaxis_title=_t("rangefinder.time_s"),
            yaxis_title=_t("rangefinder.status"),
            height=250,
        )

        for inst_id in unique_instances:
            if all_nan_inst:
                mask = np.ones(len(t), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            color = instance_colors[inst_id % len(instance_colors)]
            label = (
                f"RFND{inst_id}" if len(unique_instances) > 1
                else _t("rangefinder.status")
            )

            fig_stat.add_trace(go.Scattergl(
                x=t[mask], y=stat[mask],
                mode='lines', name=label,
                line=dict(color=color, width=1, shape='hv'),
            ))

        fig_stat.update_layout(
            yaxis=dict(
                tickvals=[0, 1, 2, 3, 4],
                ticktext=[
                    _t("rangefinder.not_connected"),
                    _t("rangefinder.no_data"),
                    _t("rangefinder.below_range"),
                    _t("rangefinder.above_range"),
                    _t("rangefinder.normal"),
                ],
            )
        )
        figures.append(fig_stat)

        # -----------------------------------------------------------
        # 5. Optional: Compare with baro altitude from CTUN
        # -----------------------------------------------------------
        if self.reader.has_message("CTUN"):
            ctun_fig = self._baro_comparison(
                t, inst, dist, unique_instances, all_nan_inst
            )
            if ctun_fig is not None:
                figures.append(ctun_fig)

        # -----------------------------------------------------------
        # 6. Summary + actionable advice
        # -----------------------------------------------------------
        if overall_status == "warning":
            summary = _t("rangefinder.warn_summary")
            action_level = "physical_check"
            plain_summary = _t("rangefinder.warn_plain")
            action_text = _t("rangefinder.warn_action", max_m=f"{max_range_m:.1f}")
        else:
            summary = _t("rangefinder.good_summary", count=len(unique_instances))
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = _t("rangefinder.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.rangefinder"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=[],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _baro_comparison(
        self,
        t_rfnd: np.ndarray,
        inst: np.ndarray,
        dist: np.ndarray,
        unique_instances: list[int],
        all_nan_inst: bool = False,
    ) -> go.Figure | None:
        """Compare rangefinder distance with CTUN baro altitude."""
        ctun = self.reader.get_messages_as_arrays("CTUN", [
            "BAlt", "DAlt", "Alt",
        ])
        t_ctun = ctun["_timestamp"]

        if len(t_ctun) == 0:
            return None

        fig = self.make_figure(
            title=_t("rangefinder.fig_baro_compare"),
            xaxis_title=_t("rangefinder.time_s"),
            yaxis_title=_t("rangefinder.alt_dist_m"),
            height=400,
        )

        # CTUN altitude
        fig.add_trace(go.Scattergl(
            x=t_ctun, y=ctun["Alt"],
            mode='lines', name=_t("rangefinder.trace_ctun_alt"),
            line=dict(color='#2ca02c', width=1),
        ))

        fig.add_trace(go.Scattergl(
            x=t_ctun, y=ctun["BAlt"],
            mode='lines', name=_t("rangefinder.trace_baro_alt"),
            line=dict(color='#9467bd', width=1, dash='dash'),
        ))

        # Rangefinder distance (first instance)
        inst_id = unique_instances[0]
        if all_nan_inst:
            mask = np.ones(len(t_rfnd), dtype=bool)
        else:
            mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
        if np.any(mask):
            fig.add_trace(go.Scattergl(
                x=t_rfnd[mask], y=dist[mask],
                mode='lines', name=_t("rangefinder.trace_rfnd_dist"),
                line=dict(color='#1f77b4', width=1.5),
            ))

        return fig
