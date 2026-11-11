"""
GPS Analyzer: GPS quality, satellite count, dilution of precision.

Evaluates GPS health by monitoring NSats, HDop, fix status, and optionally
HAcc/VAcc from GPA messages. Supports dual GPS instances (I field).

Required: GPS
Optional: GPA

Title: GPS质量
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


class GPSAnalyzer(BaseAnalyzer):
    """
    GPS quality analyzer.

    Generates:
      - NSats time series per instance (with threshold line at 8)
      - HDop time series per instance (with threshold at 2.0)
      - GPS fix status step plot
      - Ground track (Lat vs Lng colored by altitude)
      - HAcc/VAcc time series (if GPA available)
    """

    registry_key = "gps"
    sort_order = 70
    required_messages = ["GPS"]
    optional_messages = ["GPA"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read GPS data
        # -----------------------------------------------------------
        gps = self.reader.get_messages_as_arrays("GPS", [
            "I", "Status", "GMS", "GWk", "NSats", "HDop",
            "Lat", "Lng", "Alt", "Spd",
        ])
        ts = gps["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.gps"),
                status="info",
                summary=t("gps.gps_empty"),
            )

        inst = gps["I"]
        status_arr = gps["Status"]
        nsats = gps["NSats"]
        hdop = gps["HDop"]
        lat = gps["Lat"]
        lng = gps["Lng"]
        alt = gps["Alt"]

        # -----------------------------------------------------------
        # 2. Separate by GPS instance
        # -----------------------------------------------------------
        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )
        all_nan_inst = len(unique_instances) == 0
        if not unique_instances:
            unique_instances = [0]

        details.append(t("gps.instance_count", count=len(unique_instances)))

        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
        overall_status = "good"

        # -----------------------------------------------------------
        # 3. Analyze each instance
        # -----------------------------------------------------------
        nsats_warn = self.thresholds.gps_nsats_warn
        nsats_crit = self.thresholds.gps_nsats_crit
        hdop_warn = self.thresholds.gps_hdop_warn
        hdop_crit = self.thresholds.gps_hdop_crit

        # Figures: NSats per instance
        fig_nsats = self.make_figure(
            title=t("gps.fig_nsats"),
            xaxis_title=t("gps.time_s"),
            yaxis_title=t("gps.nsats"),
            height=350,
        )

        # Figures: HDop per instance
        fig_hdop = self.make_figure(
            title=t("gps.fig_hdop"),
            xaxis_title=t("gps.time_s"),
            yaxis_title=t("gps.fig_hdop_yaxis"),
            height=350,
        )

        # Figures: Fix status per instance
        fig_fix = self.make_figure(
            title=t("gps.fig_fix_status"),
            xaxis_title=t("gps.time_s"),
            yaxis_title=t("gps.fig_fix_yaxis"),
            height=300,
        )

        armed_intervals = self.reader.get_armed_intervals()

        for inst_id in unique_instances:
            if all_nan_inst:
                mask = np.ones(len(ts), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            t_inst = ts[mask]
            nsats_inst = nsats[mask]
            hdop_inst = hdop[mask]
            status_inst = status_arr[mask]
            color = instance_colors[inst_id % len(instance_colors)]
            label_prefix = f"GPS{inst_id}" if len(unique_instances) > 1 else "GPS"

            if np.all(np.isnan(nsats_inst)):
                details.append(
                    t("gps.nsats_all_nan", prefix=label_prefix)
                )
                continue
            min_nsats = int(np.nanmin(nsats_inst))
            max_nsats = int(np.nanmax(nsats_inst))
            avg_nsats = float(np.nanmean(nsats_inst))
            max_hdop = float(np.nanmax(hdop_inst)) \
                if not np.all(np.isnan(hdop_inst)) else float('nan')
            avg_hdop = float(np.nanmean(hdop_inst)) \
                if not np.all(np.isnan(hdop_inst)) else float('nan')

            details.append(
                t("gps.stats",
                  prefix=label_prefix,
                  min_nsats=min_nsats,
                  max_nsats=max_nsats,
                  avg_nsats=f"{avg_nsats:.1f}",
                  avg_hdop=f"{avg_hdop:.2f}",
                  max_hdop=f"{max_hdop:.2f}")
            )

            if armed_intervals:
                armed_mask_inst = np.zeros(len(t_inst), dtype=bool)
                for start, end in armed_intervals:
                    armed_mask_inst |= (t_inst >= start) & (t_inst <= end)
            else:
                armed_mask_inst = np.ones(len(t_inst), dtype=bool)

            if np.any(armed_mask_inst):
                armed_status = status_inst[armed_mask_inst]
                n_armed = len(armed_status)
                if n_armed > 0:
                    no_fix_frac = float(np.sum(armed_status < 3)) / n_armed
                else:
                    no_fix_frac = 0.0
                if no_fix_frac > self.thresholds.gps_no_fix_frac_crit:
                    overall_status = self.worst_status(overall_status, "critical")
                    details.append(
                        t("gps.no_fix_pct",
                          prefix=label_prefix,
                          pct=f"{no_fix_frac*100:.1f}")
                    )

                # Check HDop during flight
                armed_hdop = hdop_inst[armed_mask_inst]
                n_hdop = len(armed_hdop)
                high_hdop_frac = float(np.sum(armed_hdop > hdop_crit)) / n_hdop if n_hdop > 0 else 0.0
                if high_hdop_frac > self.thresholds.gps_high_hdop_frac_warn:
                    overall_status = self.worst_status(overall_status, "warning")
                    details.append(
                        t("gps.high_hdop_pct",
                          prefix=label_prefix,
                          pct=f"{high_hdop_frac*100:.1f}",
                          threshold=hdop_crit)
                    )

                # Check NSats during flight
                armed_nsats = nsats_inst[armed_mask_inst]
                n_nsats = len(armed_nsats)
                low_nsats_frac = float(
                    np.sum(armed_nsats < nsats_crit)
                ) / n_nsats if n_nsats > 0 else 0.0
                if low_nsats_frac > self.thresholds.gps_low_nsats_frac_warn:
                    overall_status = self.worst_status(overall_status, "warning")
                    details.append(
                        t("gps.low_nsats_pct",
                          prefix=label_prefix,
                          pct=f"{low_nsats_frac*100:.1f}",
                          threshold=nsats_crit)
                    )

            # NSats trace
            fig_nsats.add_trace(go.Scattergl(
                x=t_inst, y=nsats_inst,
                mode='lines', name=label_prefix,
                line=dict(color=color, width=1),
            ))

            # HDop trace
            fig_hdop.add_trace(go.Scattergl(
                x=t_inst, y=hdop_inst,
                mode='lines', name=label_prefix,
                line=dict(color=color, width=1),
            ))

            # Fix status trace (step plot)
            fig_fix.add_trace(go.Scattergl(
                x=t_inst, y=status_inst,
                mode='lines', name=label_prefix,
                line=dict(color=color, width=2, shape='hv'),
            ))

        # Threshold lines
        self.add_threshold_hline(
            fig_nsats, nsats_crit,
            t("gps.threshold_crit", val=nsats_crit), color="#d9534f",
        )
        self.add_threshold_hline(
            fig_nsats, nsats_warn,
            t("gps.threshold_warn", val=nsats_warn), color="#f0ad4e",
        )

        self.add_threshold_hline(
            fig_hdop, hdop_warn,
            t("gps.threshold_warn", val=hdop_warn), color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig_hdop, hdop_crit,
            t("gps.threshold_crit", val=hdop_crit), color="#d9534f",
        )

        # Fix status labels
        fig_fix.update_layout(
            yaxis=dict(
                tickvals=[0, 1, 2, 3, 4, 5, 6],
                ticktext=[
                    t("gps.fix_nogps"), t("gps.fix_nofix"),
                    t("gps.fix_2d"), t("gps.fix_3d"),
                    t("gps.fix_dgps"), t("gps.fix_rtk_float"),
                    t("gps.fix_rtk_fixed"),
                ],
            )
        )

        figures.append(fig_nsats)
        figures.append(fig_hdop)
        figures.append(fig_fix)

        # -----------------------------------------------------------
        # 3b. Dual GPS auto-switch suggestion
        # -----------------------------------------------------------
        if (len(unique_instances) >= 2
                and overall_status in ("warning", "critical")):
            gps_auto_switch = self.reader.get_param("GPS_AUTO_SWITCH", 0)
            if gps_auto_switch == 0:
                param_suggestions.append({
                    "param": "GPS_AUTO_SWITCH",
                    "current": 0,
                    "suggested": 1,
                    "reason": t("gps.auto_switch_reason"),
                })

        # -----------------------------------------------------------
        # 4. Ground track (Lat vs Lng colored by altitude)
        # -----------------------------------------------------------
        # Use first instance with valid coordinates
        for inst_id in unique_instances:
            if all_nan_inst:
                mask = np.ones(len(ts), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            lat_inst = lat[mask]
            lng_inst = lng[mask]
            alt_inst = alt[mask]

            valid = (
                ~np.isnan(lat_inst) & ~np.isnan(lng_inst)
                & (lat_inst != 0) & (lng_inst != 0)
            )
            if np.sum(valid) > 10:
                fig_track = self.make_figure(
                    title=t("gps.fig_ground_track"),
                    xaxis_title=t("gps.longitude"),
                    yaxis_title=t("gps.latitude"),
                    height=500,
                )
                fig_track.add_trace(go.Scattergl(
                    x=lng_inst[valid],
                    y=lat_inst[valid],
                    mode='markers',
                    marker=dict(
                        size=3,
                        color=alt_inst[valid],
                        colorscale='Viridis',
                        colorbar=dict(title=t("gps.altitude_m")),
                        showscale=True,
                    ),
                    name=t("gps.track"),
                    hovertemplate=(
                        f"{t('gps.hover_lat')}: %{{y:.6f}}<br>"
                        f"{t('gps.hover_lng')}: %{{x:.6f}}<br>"
                        f"{t('gps.hover_alt')}: %{{marker.color:.1f}}m"
                        "<extra></extra>"
                    ),
                ))
                fig_track.update_layout(
                    yaxis=dict(scaleanchor="x", scaleratio=1),
                )
                figures.append(fig_track)
                break  # Only first instance with valid data

        # -----------------------------------------------------------
        # 5. Optional: HAcc/VAcc from GPA
        # -----------------------------------------------------------
        if self.reader.has_message("GPA"):
            gpa_figs = self._gpa_accuracy()
            figures.extend(gpa_figs)

        # -----------------------------------------------------------
        # 6. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = t("gps.summary_critical")
        elif overall_status == "warning":
            summary = t("gps.summary_warning")
        else:
            summary = t("gps.summary_good", count=len(unique_instances))

        # GPS issues are usually physical (antenna placement, sky obstruction).
        if overall_status == "critical":
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = t("gps.plain_critical")
            action_text = t("gps.action_critical")
        elif overall_status == "warning":
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = t("gps.plain_warning")
            action_text = t("gps.action_warning")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = t("gps.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.gps"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
            param_suggestions=param_suggestions,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _gpa_accuracy(self) -> list[go.Figure]:
        """Plot HAcc/VAcc from GPA messages."""
        gpa = self.reader.get_messages_as_arrays("GPA", [
            "I", "HAcc", "VAcc", "SAcc",
        ])
        ts = gpa["_timestamp"]
        if len(ts) == 0:
            return []

        figures = []
        inst = gpa["I"]
        hacc = gpa["HAcc"]
        vacc = gpa["VAcc"]

        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )
        all_nan_gpa = len(unique_instances) == 0
        if not unique_instances:
            unique_instances = [0]

        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']

        fig_acc = self.make_figure(
            title=t("gps.fig_accuracy"),
            xaxis_title=t("gps.time_s"),
            yaxis_title=t("gps.accuracy_m"),
            height=350,
        )

        for inst_id in unique_instances:
            if all_nan_gpa:
                mask = np.ones(len(ts), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            color = instance_colors[inst_id % len(instance_colors)]
            prefix = f"GPS{inst_id}" if len(unique_instances) > 1 else ""

            fig_acc.add_trace(go.Scattergl(
                x=ts[mask], y=hacc[mask],
                mode='lines',
                name=f"{prefix} HAcc" if prefix else "HAcc",
                line=dict(color=color, width=1),
            ))
            fig_acc.add_trace(go.Scattergl(
                x=ts[mask], y=vacc[mask],
                mode='lines',
                name=f"{prefix} VAcc" if prefix else "VAcc",
                line=dict(color=color, width=1, dash='dash'),
            ))

        self.add_threshold_hline(
            fig_acc, self.thresholds.gps_hacc_warn,
            t("gps.hacc_warn", val=self.thresholds.gps_hacc_warn),
            color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig_acc, self.thresholds.gps_hacc_crit,
            t("gps.hacc_crit", val=self.thresholds.gps_hacc_crit),
            color="#d9534f",
        )

        figures.append(fig_acc)
        return figures
