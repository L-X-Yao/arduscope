"""
Temperature Analyzer: IMU and board temperature monitoring.

Monitors temperature drift and high-temperature conditions from
IMU temperature data.

Required: IMU
Optional: BAT

Title: 温度监控
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


class TemperatureAnalyzer(BaseAnalyzer):
    """
    Temperature monitoring analyzer.

    Generates:
      - IMU temperature time series per instance
      - Temperature rate of change
      - High temperature warnings
    """

    registry_key = "temperature"
    sort_order = 105
    required_messages = ["IMU"]
    optional_messages = ["BAT"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Collect temperature from IMU instances
        # -----------------------------------------------------------
        temps: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c']

        imu = self.reader.get_messages_as_arrays("IMU", ["I", "T"])
        t_all = imu["_timestamp"]
        inst = imu["I"]
        temp_all = imu["T"]

        # Split by instance
        instances: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        all_nan_inst = np.all(np.isnan(inst))
        if all_nan_inst:
            instances[0] = (t_all, temp_all)
        else:
            inst_int = np.nan_to_num(inst, nan=-1).astype(int)
            for iid in sorted(set(inst_int)):
                if iid < 0:
                    continue
                mask = inst_int == iid
                instances[iid] = (t_all[mask], temp_all[mask])

        for iid, (ts, temp) in instances.items():
            label = f"IMU{iid}"
            if len(ts) > 0 and not np.all(np.isnan(temp)):
                temps[label] = (ts, temp)

        if not temps:
            return self.make_result(
                title=t("analyzer.temperature"),
                status="info",
                summary=t("temperature.no_data"),
            )

        status = "good"

        # -----------------------------------------------------------
        # 2. Analyze each IMU temperature
        # -----------------------------------------------------------
        fig_temp = self.make_figure(
            title=t("temperature.fig_imu_temp"),
            xaxis_title=t("temperature.time_s"),
            yaxis_title=t("temperature.temp_c"),
            height=400,
        )

        for i, (label, (ts, temp)) in enumerate(temps.items()):
            valid = temp[~np.isnan(temp)]
            if len(valid) == 0:
                continue

            min_t = float(np.nanmin(valid))
            max_t = float(np.nanmax(valid))
            mean_t = float(np.nanmean(valid))
            drift = max_t - min_t

            details.append(
                t("temperature.imu_range",
                  label=label,
                  min=f"{min_t:.1f}", max=f"{max_t:.1f}",
                  mean=f"{mean_t:.1f}", drift=f"{drift:.1f}")
            )

            # High temperature warning
            if max_t > self.thresholds.temp_crit:
                status = self.worst_status(status, "critical")
                details.append(
                    t("temperature.too_high",
                      label=label,
                      val=f"{max_t:.1f}",
                      thresh=f"{self.thresholds.temp_crit:.0f}")
                )
            elif max_t > self.thresholds.temp_warn:
                status = self.worst_status(status, "warning")
                details.append(
                    t("temperature.high",
                      label=label,
                      val=f"{max_t:.1f}",
                      thresh=f"{self.thresholds.temp_warn:.0f}")
                )

            # Large drift warning
            if drift > self.thresholds.temp_drift_warn:
                status = self.worst_status(status, "warning")
                details.append(
                    t("temperature.large_drift",
                      label=label, drift=f"{drift:.1f}")
                )

            color = colors[i % len(colors)]
            fig_temp.add_trace(go.Scattergl(
                x=ts, y=temp,
                mode='lines', name=label,
                line=dict(color=color, width=1),
            ))

        # Threshold lines
        tw = self.thresholds.temp_warn
        tc = self.thresholds.temp_crit
        self.add_threshold_hline(
            fig_temp, tw,
            t("temperature.thresh_warn", val=f"{tw:.0f}"), "#f0ad4e",
        )
        self.add_threshold_hline(
            fig_temp, tc,
            t("temperature.thresh_crit", val=f"{tc:.0f}"), "#d9534f",
        )

        figures.append(fig_temp)

        # -----------------------------------------------------------
        # 3. Temperature rate of change
        # -----------------------------------------------------------
        # Use first IMU instance
        first_label = list(temps.keys())[0]
        t_first, temp_first = temps[first_label]
        valid_mask = ~np.isnan(temp_first)
        if np.sum(valid_mask) > 20:
            t_v = t_first[valid_mask]
            temp_v = temp_first[valid_mask]

            # Compute rate (°C/min) with smoothing
            dt = np.diff(t_v)
            dt[dt < 1e-3] = 1e-3
            d_temp = np.diff(temp_v)
            rate = d_temp / dt * 60  # °C/min

            # Smooth with moving average
            kernel_size = min(20, len(rate) // 4)
            if kernel_size > 1:
                kernel = np.ones(kernel_size) / kernel_size
                rate_smooth = np.convolve(rate, kernel, mode='valid')
                t_rate = t_v[kernel_size // 2:kernel_size // 2 + len(rate_smooth)]
            else:
                rate_smooth = rate
                t_rate = t_v[1:]

            fig_rate = self.make_figure(
                title=t("temperature.fig_rate"),
                xaxis_title=t("temperature.time_s"),
                yaxis_title=t("temperature.rate_unit"),
                height=300,
            )
            fig_rate.add_trace(go.Scattergl(
                x=t_rate, y=rate_smooth,
                mode='lines', name=t("temperature.trace_rate"),
                line=dict(color='#d62728', width=1),
            ))
            fig_rate.add_hline(
                y=0, line_dash="dot", line_color="#999",
            )
            figures.append(fig_rate)

        # -----------------------------------------------------------
        # 4. Inter-IMU temperature consistency
        # -----------------------------------------------------------
        labels = list(temps.keys())
        if len(labels) >= 2:
            t1, temp1 = temps[labels[0]]
            t2, temp2 = temps[labels[1]]

            good2 = np.isfinite(t2) & np.isfinite(temp2)
            if np.sum(good2) >= 2:
                temp2_interp = np.interp(t1, t2[good2], temp2[good2])
                diff = temp1 - temp2_interp
                valid_diff = diff[np.isfinite(temp1)]

                if len(valid_diff) > 0:
                    mean_diff = float(np.nanmean(np.abs(valid_diff)))
                    max_diff = float(np.nanmax(np.abs(valid_diff)))
                    details.append(
                        t("temperature.inter_imu_diff",
                          mean=f"{mean_diff:.1f}",
                          max=f"{max_diff:.1f}")
                    )
                    if max_diff > self.thresholds.temp_inter_imu_warn:
                        status = self.worst_status(status, "warning")
                        details.append(t("temperature.large_inter_diff", val=f"{max_diff:.1f}"))

        # -----------------------------------------------------------
        # 5. Summary
        # -----------------------------------------------------------
        valid_maxes = [
            float(np.nanmax(temp[~np.isnan(temp)]))
            for _, (_, temp) in temps.items()
            if np.any(~np.isnan(temp))
        ]
        all_max = max(valid_maxes) if valid_maxes else 0.0

        if status == "critical":
            summary = t("temperature.crit_summary", max=f"{all_max:.1f}")
        elif status == "warning":
            summary = t("temperature.warn_summary", max=f"{all_max:.1f}")
        else:
            summary = t("temperature.good_summary",
                        count=len(temps), max=f"{all_max:.1f}")

        # High temperature is a physical/cooling issue.
        if status == "critical":
            action_level = "physical_check"
            plain_summary = t("temperature.crit_plain", max=f"{all_max:.0f}")
            action_text = t("temperature.crit_action")
        elif status == "warning":
            action_level = "physical_check"
            plain_summary = t("temperature.warn_plain", max=f"{all_max:.0f}")
            action_text = t("temperature.warn_action")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if status in ("warning", "critical"):
            evidence_source = t("temperature.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.temperature"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
            param_suggestions=[],
        )
