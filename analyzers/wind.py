"""
Wind Estimation Analyzer: wind speed and direction from EKF.

Extracts wind estimates from the EKF state (XKF2/NKF2) and analyzes
wind conditions during flight.

Required: (none - uses custom can_run for XKF2 or NKF2)
Optional: XKF2, NKF2

Title: 风场估计
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


class WindAnalyzer(BaseAnalyzer):
    """
    Wind estimation analyzer.

    Generates:
      - Wind speed time series
      - Wind direction time series
      - Wind rose / polar plot
      - Wind statistics summary
    """

    registry_key = "wind"
    sort_order = 160
    required_messages: list[str] = []
    optional_messages = ["XKF2", "NKF2"]

    def can_run(self) -> bool:
        """Need either XKF2 or NKF2 for wind estimates."""
        return (self.reader.has_message("XKF2")
                or self.reader.has_message("NKF2"))

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read wind data from EKF
        # -----------------------------------------------------------
        kf2_type = "XKF2" if self.reader.has_message("XKF2") else "NKF2"

        kf2 = self.reader.get_messages_as_arrays(kf2_type, [
            "C", "VWN", "VWE",
        ])

        # Filter to EKF core 0
        core = kf2["C"]
        all_nan_core = np.all(np.isnan(core))
        if all_nan_core:
            core_mask = np.ones(len(kf2["_timestamp"]), dtype=bool)
        else:
            core_mask = np.nan_to_num(core, nan=-1).astype(int) == 0

        t_raw = kf2["_timestamp"][core_mask]

        if len(t_raw) == 0:
            return self.make_result(
                title=t("analyzer.wind"),
                status="info",
                summary=t("wind.no_ekf_data"),
            )

        vwn_raw = kf2["VWN"][core_mask]  # Wind North (m/s)
        vwe_raw = kf2["VWE"][core_mask]  # Wind East (m/s)

        # -----------------------------------------------------------
        # 2. Compute wind speed and direction (unfiltered for plots)
        # -----------------------------------------------------------
        wind_speed_raw = np.sqrt(vwn_raw**2 + vwe_raw**2)
        # Direction wind is coming FROM (meteorological convention)
        wind_dir_raw = (np.degrees(np.arctan2(-vwe_raw, -vwn_raw)) + 360) % 360

        # Keep unfiltered data for time-series plots
        ts = t_raw
        vwn = vwn_raw
        vwe = vwe_raw
        wind_speed = wind_speed_raw
        wind_dir = wind_dir_raw

        valid = ~np.isnan(wind_speed)
        if not np.any(valid):
            return self.make_result(
                title=t("analyzer.wind"),
                status="info",
                summary=t("wind.ekf_invalid"),
            )

        # -----------------------------------------------------------
        # 2b. Forward-flight filtering for statistics
        # -----------------------------------------------------------
        # Wind estimates are only valid during forward flight; during
        # hover the estimate collapses toward 0.  Filter to samples
        # where wind speed > 0.5 m/s.
        fly_mask = wind_speed_raw > 0.5
        fly_valid = fly_mask & valid
        if np.sum(fly_valid) > 10:
            stat_mask = fly_valid
        else:
            # Not enough forward-flight samples; fall back to all valid
            stat_mask = valid
        ws_valid = wind_speed[stat_mask]
        wd_valid = wind_dir[stat_mask]
        t_valid = ts[stat_mask]

        status = "good"

        # -----------------------------------------------------------
        # 3. Statistics
        # -----------------------------------------------------------
        mean_ws = float(np.nanmean(ws_valid))
        max_ws = float(np.nanmax(ws_valid))
        std_ws = float(np.nanstd(ws_valid))

        # Dominant direction (circular mean)
        sin_sum = float(np.nanmean(np.sin(np.radians(wd_valid))))
        cos_sum = float(np.nanmean(np.cos(np.radians(wd_valid))))
        mean_dir = (np.degrees(np.arctan2(sin_sum, cos_sum)) + 360) % 360

        dir_names = [
            t("wind.dir_n"), t("wind.dir_nne"), t("wind.dir_ne"), t("wind.dir_ene"),
            t("wind.dir_e"), t("wind.dir_ese"), t("wind.dir_se"), t("wind.dir_sse"),
            t("wind.dir_s"), t("wind.dir_ssw"), t("wind.dir_sw"), t("wind.dir_wsw"),
            t("wind.dir_w"), t("wind.dir_wnw"), t("wind.dir_nw"), t("wind.dir_nnw"),
        ]
        dir_idx = int((mean_dir + 11.25) / 22.5) % 16
        dir_name = dir_names[dir_idx]

        details.append(t("wind.avg_speed", ms=f"{mean_ws:.1f}", kmh=f"{mean_ws * 3.6:.1f}"))
        details.append(t("wind.max_speed", ms=f"{max_ws:.1f}", kmh=f"{max_ws * 3.6:.1f}"))
        details.append(t("wind.speed_std", val=f"{std_ws:.1f}"))
        details.append(t("wind.dominant_dir", name=dir_name, deg=f"{mean_dir:.0f}"))

        # Wind level assessment
        # Beaufort scale approximate
        if max_ws > self.thresholds.wind_speed_crit:
            status = self.worst_status(status, "critical")
            details.append(t("wind.beaufort_8plus"))
        elif max_ws > self.thresholds.wind_speed_warn:
            status = self.worst_status(status, "warning")
            details.append(t("wind.beaufort_5_6"))
        elif max_ws > 5:
            details.append(t("wind.beaufort_3_4"))
        else:
            details.append(t("wind.beaufort_1_2"))

        # Gustiness (variability)
        if (mean_ws > self.thresholds.wind_gust_min_mean
                and std_ws / mean_ws > self.thresholds.wind_gust_ratio_warn):
            status = self.worst_status(status, "warning")
            details.append(t("wind.gusty"))

        # -----------------------------------------------------------
        # 4. Figure: Wind speed time series
        # -----------------------------------------------------------
        fig_speed = self.make_figure(
            title=t("wind.fig_speed"),
            xaxis_title=t("wind.time_s"),
            yaxis_title=t("wind.speed_ms"),
            height=400,
        )
        fig_speed.add_trace(go.Scattergl(
            x=ts, y=wind_speed,
            mode='lines', name=t("wind.trace_speed"),
            line=dict(color='#1f77b4', width=1),
        ))

        # Wind component traces
        fig_speed.add_trace(go.Scattergl(
            x=ts, y=vwn,
            mode='lines', name=t("wind.north_component"),
            line=dict(color='#2ca02c', width=0.8, dash='dot'),
            visible='legendonly',
        ))
        fig_speed.add_trace(go.Scattergl(
            x=ts, y=vwe,
            mode='lines', name=t("wind.east_component"),
            line=dict(color='#d62728', width=0.8, dash='dot'),
            visible='legendonly',
        ))

        if max_ws > self.thresholds.wind_speed_warn:
            self.add_threshold_hline(
                fig_speed, self.thresholds.wind_speed_warn,
                t("wind.beaufort_5_line"), "#f0ad4e",
            )
        figures.append(fig_speed)

        # -----------------------------------------------------------
        # 5. Figure: Wind direction time series
        # -----------------------------------------------------------
        fig_dir = self.make_figure(
            title=t("wind.fig_direction"),
            xaxis_title=t("wind.time_s"),
            yaxis_title=t("wind.direction_deg"),
            height=350,
        )
        fig_dir.add_trace(go.Scattergl(
            x=t_valid, y=wd_valid,
            mode='markers', name=t("wind.trace_direction"),
            marker=dict(color='#ff7f0e', size=2, opacity=0.6),
        ))
        fig_dir.update_layout(yaxis=dict(range=[0, 360], dtick=45))
        figures.append(fig_dir)

        # -----------------------------------------------------------
        # 6. Figure: Wind rose (polar histogram)
        # -----------------------------------------------------------
        if len(wd_valid) > 50:
            # Bin into 16 directions
            bin_edges = np.linspace(0, 360, 17)
            bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
            counts, _ = np.histogram(wd_valid, bins=bin_edges)
            counts_pct = counts / len(wd_valid) * 100

            fig_rose = go.Figure()
            fig_rose.add_trace(go.Barpolar(
                r=counts_pct,
                theta=bin_centers,
                width=22.5,
                marker_color='#1f77b4',
                opacity=0.8,
                name=t("wind.frequency_pct"),
            ))
            fig_rose.update_layout(
                title=dict(text=t("wind.fig_rose"), font=dict(size=14)),
                height=450,
                polar=dict(
                    radialaxis=dict(showticklabels=True, ticksuffix='%'),
                    angularaxis=dict(
                        direction="clockwise",
                        rotation=90,
                        ticktext=[t("wind.dir_n"), t("wind.dir_ne"),
                                  t("wind.dir_e"), t("wind.dir_se"),
                                  t("wind.dir_s"), t("wind.dir_sw"),
                                  t("wind.dir_w"), t("wind.dir_nw")],
                        tickvals=[0, 45, 90, 135, 180, 225, 270, 315],
                    ),
                ),
                template="plotly_white",
                font=dict(
                    family="PingFang SC, Microsoft YaHei, Noto Sans SC, sans-serif",
                    size=12,
                ),
            )
            figures.append(fig_rose)

        # -----------------------------------------------------------
        # 7. Summary + actionable advice
        # -----------------------------------------------------------
        if status == "critical":
            summary = t("wind.summary_critical", max_ws=f"{max_ws:.1f}")
            action_level = "physical_check"
            plain_summary = t("wind.plain_critical")
            action_text = t(
                "wind.action_critical",
                max_ws=f"{max_ws:.1f}",
                max_kmh=f"{max_ws * 3.6:.0f}",
            )
        elif status == "warning":
            summary = t("wind.summary_warning", mean_ws=f"{mean_ws:.1f}", max_ws=f"{max_ws:.1f}")
            # Determine whether warning is from strong wind or gustiness
            is_gusty = (mean_ws > 2 and std_ws / mean_ws > 0.5)
            if is_gusty and max_ws <= 10:
                action_level = "physical_check"
                plain_summary = t("wind.plain_gusty")
                action_text = t(
                    "wind.action_gusty",
                    std=f"{std_ws:.1f}",
                    gust_pct=f"{std_ws / mean_ws * 100:.0f}",
                )
            else:
                action_level = "physical_check"
                plain_summary = t("wind.plain_strong")
                action_text = t("wind.action_strong", max_ws=f"{max_ws:.1f}")
        else:
            summary = t("wind.summary_good",
                        mean_ws=f"{mean_ws:.1f}",
                        max_ws=f"{max_ws:.1f}",
                        dir_name=dir_name)
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if status in ("warning", "critical"):
            evidence_source = t("wind.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.wind"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=[],
        )
