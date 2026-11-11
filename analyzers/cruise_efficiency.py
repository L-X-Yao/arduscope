"""
Cruise Efficiency Analyzer: power consumption and efficiency visualization.

Computes instantaneous power, total energy, and power-vs-airspeed scatter
for fixed-wing aircraft. Informational analyzer (always good/info status).
Only runs on ArduPlane or QuadPlane logs.

Required: BAT
Optional: ARSP, GPS

Title: 巡航效率
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.battery_packs import read_packs, sum_over_packs
from lib.i18n import t as _t
from . import AnalysisResult, BaseAnalyzer


class CruiseEfficiencyAnalyzer(BaseAnalyzer):
    """
    Cruise efficiency analyzer (ArduPlane / QuadPlane only).

    Generates:
      - Power over time
      - Power vs airspeed scatter with efficiency curve
      - Total energy consumption (Wh)
      - Optimal cruise speed identification
    """

    registry_key = "cruise_efficiency"
    sort_order = 146
    required_messages = ["BAT"]
    optional_messages = ["ARSP", "GPS"]

    def can_run(self) -> bool:
        if not self.reader.has_message("BAT"):
            return False
        # Substring, not exact-equality: vendor-renamed banners carry the
        # family as "<banner> (ArduPlane)" (canonical_vehicle_type).
        return (
            "Plane" in self.vehicle.vehicle_type
            or self.vehicle.is_quadplane
        )

    def analyze(self) -> AnalysisResult:
        # Packs on one vehicle feed the same load, so total power is the
        # sum over the packs that measure current. The first pack sets the
        # time base; interleaving the rows instead would mix packs in one
        # stream and under-report power.
        packs = read_packs(self.reader)
        if not packs:
            rows = self.reader.get_messages_as_arrays("BAT", ["Volt"])
            return self.make_result(
                title=_t("analyzer.cruise_efficiency"),
                status="info",
                summary=_t("cruise_efficiency.no_current"
                           if len(rows["_timestamp"]) >= 2
                           else "cruise_efficiency.empty"),
            )

        ts = packs[0].ts
        volt = packs[0].volt
        curr = packs[0].curr
        power = sum_over_packs(packs, [p.volt * p.curr for p in packs])

        if len(ts) < 2:
            return self.make_result(
                title=_t("analyzer.cruise_efficiency"),
                status="info",
                summary=_t("cruise_efficiency.empty"),
            )

        valid_mask = ~(np.isnan(volt) | np.isnan(curr))
        if not np.any(valid_mask):
            return self.make_result(
                title=_t("analyzer.cruise_efficiency"),
                status="info",
                summary=_t("cruise_efficiency.no_current"),
            )

        power[~valid_mask] = np.nan

        details = []
        figures = []

        # Total energy (Wh) via trapezoidal integration
        valid_pw = ~np.isnan(power)
        if np.sum(valid_pw) >= 2:
            dt = np.diff(ts[valid_pw])
            avg_p = (power[valid_pw][:-1] + power[valid_pw][1:]) / 2.0
            energy_ws = float(np.sum(avg_p * dt))
            energy_wh = energy_ws / 3600.0
        else:
            energy_wh = 0.0

        mean_power = float(np.nanmean(power))
        max_power = float(np.nanmax(power))
        details.append(_t("cruise_efficiency.energy_wh", wh=f"{energy_wh:.1f}"))
        details.append(_t("cruise_efficiency.mean_power", watts=f"{mean_power:.1f}"))
        details.append(_t("cruise_efficiency.max_power", watts=f"{max_power:.1f}"))
        if len(packs) > 1:
            details.append(_t(
                "battery.packs_summed", count=len(packs),
                ids=", ".join(str(p.instance) for p in packs)))

        # Figure 1: Power over time
        fig_power = self.make_figure(
            title=_t("cruise_efficiency.fig_power"),
            xaxis_title=_t("cruise_efficiency.time_s"),
            yaxis_title=_t("cruise_efficiency.power_w"),
            height=400,
        )
        fig_power.add_trace(go.Scattergl(
            x=ts, y=power,
            mode='lines', name=_t("cruise_efficiency.power_label"),
            line=dict(color='#ff7f0e', width=1.5),
        ))
        figures.append(fig_power)

        # Try to get airspeed data for scatter plot
        airspeed = None
        airspeed_ts = None
        if self.reader.has_message("ARSP"):
            arsp = self.reader.get_messages_as_arrays("ARSP", ["Airspeed"])
            if len(arsp["_timestamp"]) > 0:
                airspeed = arsp["Airspeed"]
                airspeed_ts = arsp["_timestamp"]
        if airspeed is None and self.reader.has_message("GPS"):
            gps = self.reader.get_messages_as_arrays("GPS", ["Spd"])
            if len(gps["_timestamp"]) > 0:
                airspeed = gps["Spd"]
                airspeed_ts = gps["_timestamp"]
                details.append(_t("cruise_efficiency.using_gps_speed"))

        optimal_speed = None
        if airspeed is not None and airspeed_ts is not None:
            good_as = np.isfinite(airspeed_ts) & np.isfinite(airspeed)
            spd_interp = np.interp(ts, airspeed_ts[good_as], airspeed[good_as]) if np.sum(good_as) >= 2 else np.full_like(ts, np.nan)

            # Filter to valid data where speed > 3 m/s
            scatter_mask = (
                ~np.isnan(power) & ~np.isnan(spd_interp) & (spd_interp > 3.0)
            )

            if np.sum(scatter_mask) > 10:
                scatter_spd = spd_interp[scatter_mask]
                scatter_pw = power[scatter_mask]

                # Figure 2: Power vs airspeed scatter
                fig_scatter = self.make_figure(
                    title=_t("cruise_efficiency.fig_scatter"),
                    xaxis_title=_t("cruise_efficiency.airspeed_ms"),
                    yaxis_title=_t("cruise_efficiency.power_w"),
                    height=400,
                )
                fig_scatter.add_trace(go.Scattergl(
                    x=scatter_spd, y=scatter_pw,
                    mode='markers', name=_t("cruise_efficiency.data_points"),
                    marker=dict(color='#1f77b4', size=3, opacity=0.4),
                ))

                # Compute efficiency curve (power/speed ratio binned)
                spd_min = float(np.min(scatter_spd))
                spd_max = float(np.max(scatter_spd))
                n_bins = min(20, max(5, int((spd_max - spd_min) / 1.0)))
                bin_edges = np.linspace(spd_min, spd_max, n_bins + 1)
                bin_centers = []
                bin_means = []
                bin_eff = []
                for i in range(n_bins):
                    in_bin = (
                        (scatter_spd >= bin_edges[i])
                        & (scatter_spd < bin_edges[i + 1])
                    )
                    if np.sum(in_bin) >= 3:
                        bc = float((bin_edges[i] + bin_edges[i + 1]) / 2.0)
                        bm = float(np.mean(scatter_pw[in_bin]))
                        bin_centers.append(bc)
                        bin_means.append(bm)
                        if bc > 0:
                            bin_eff.append(bm / bc)

                if len(bin_centers) >= 3:
                    fig_scatter.add_trace(go.Scatter(
                        x=bin_centers, y=bin_means,
                        mode='lines+markers',
                        name=_t("cruise_efficiency.mean_curve"),
                        line=dict(color='#d62728', width=2),
                        marker=dict(size=6),
                    ))

                    # Optimal cruise speed: minimum power/speed ratio
                    if bin_eff:
                        min_idx = int(np.argmin(bin_eff))
                        optimal_speed = bin_centers[min_idx]
                        details.append(
                            _t("cruise_efficiency.optimal_speed",
                               speed=f"{optimal_speed:.1f}"))
                        fig_scatter.add_vline(
                            x=optimal_speed,
                            line_dash="dash", line_color="green",
                            annotation_text=_t(
                                "cruise_efficiency.optimal_label",
                                speed=f"{optimal_speed:.1f}"),
                            annotation_position="top right",
                        )

                figures.append(fig_scatter)

        # Summary
        if optimal_speed is not None:
            summary = _t("cruise_efficiency.summary_with_speed",
                          wh=f"{energy_wh:.1f}",
                          speed=f"{optimal_speed:.1f}")
        else:
            summary = _t("cruise_efficiency.summary_basic",
                          wh=f"{energy_wh:.1f}")

        return self.make_result(
            title=_t("analyzer.cruise_efficiency"),
            status="info",
            summary=summary,
            details=details,
            figures=figures,
            action_level="ok",
        )
