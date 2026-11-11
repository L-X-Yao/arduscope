"""
Airspeed Analyzer: airspeed sensor health and consistency.

Evaluates airspeed sensors by comparing against GPS ground speed,
checking multi-sensor consistency, and detecting potential pitot blockage.

Required: ARSP
Optional: GPS

Title: 空速分析
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer


class AirspeedAnalyzer(BaseAnalyzer):
    """
    Airspeed sensor analyzer.

    Generates:
      - Airspeed time series per instance
      - Airspeed vs GPS ground speed comparison
      - Multi-sensor consistency (if dual airspeed)
      - Pitot blockage detection (stuck readings)
    """

    registry_key = "airspeed"
    sort_order = 150
    required_messages = ["ARSP"]
    optional_messages = ["GPS"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read ARSP data
        # -----------------------------------------------------------
        arsp = self.reader.get_messages_as_arrays("ARSP", [
            "I", "Airspeed", "DiffPress", "Temp", "RawPress",
            "Offset", "U", "Health", "Primary",
        ])

        # Filter to primary instance (instance 0)
        inst = arsp["I"]
        all_nan = np.all(np.isnan(inst))
        if all_nan:
            mask = np.ones(len(arsp["_timestamp"]), dtype=bool)
        else:
            mask = np.nan_to_num(inst, nan=-1).astype(int) == 0

        ts = arsp["_timestamp"][mask]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.airspeed"),
                status="info",
                summary=t("airspeed.empty"),
            )

        airspeed = arsp["Airspeed"][mask]
        diff_press = arsp["DiffPress"][mask]
        healthy = arsp["Health"][mask]

        status = "good"

        # -----------------------------------------------------------
        # 2. Airspeed statistics
        # -----------------------------------------------------------
        valid = airspeed[~np.isnan(airspeed)]
        if len(valid) > 0:
            max_as = float(np.nanmax(valid))
            mean_as = float(np.nanmean(valid[valid > 1.0])) if np.any(valid > 1.0) else 0.0
            details.append(t("airspeed.max_speed", val=f"{max_as:.1f}"))
            details.append(t("airspeed.mean_speed", val=f"{mean_as:.1f}"))
        else:
            max_as = 0.0
            mean_as = 0.0

        # -----------------------------------------------------------
        # 2b. Noise detection (sample-to-sample jitter)
        # -----------------------------------------------------------
        if len(valid) > 100:
            diffs = np.abs(np.diff(valid))
            noise_std = float(np.std(diffs))
            noise_frac_5 = float(np.mean(diffs > 5.0))
            if noise_frac_5 > self.thresholds.airspeed_noise_frac_crit:
                status = self.worst_status(status, "critical")
                details.append(t("airspeed.noise_crit",
                                 pct=f"{noise_frac_5*100:.0f}",
                                 std=f"{noise_std:.1f}"))
            elif noise_frac_5 > self.thresholds.airspeed_noise_frac_warn:
                status = self.worst_status(status, "warning")
                details.append(t("airspeed.noise_warn",
                                 pct=f"{noise_frac_5*100:.0f}",
                                 std=f"{noise_std:.1f}"))

        # -----------------------------------------------------------
        # 3. Health check
        # -----------------------------------------------------------
        unhealthy_pct = 0.0
        if len(healthy) > 0:
            h_arr = np.nan_to_num(healthy, nan=1.0)
            unhealthy_pct = float(np.mean(h_arr == 0) * 100)
            if unhealthy_pct > self.thresholds.airspeed_unhealthy_pct_warn:
                status = self.worst_status(status, "warning")
                details.append(t("airspeed.unhealthy_pct",
                                 pct=f"{unhealthy_pct:.1f}"))
            if unhealthy_pct > self.thresholds.airspeed_unhealthy_pct_crit:
                status = self.worst_status(status, "critical")

        # -----------------------------------------------------------
        # 4. Pitot blockage detection (stuck readings)
        # -----------------------------------------------------------
        if len(valid) > 100:
            # Check for segments where airspeed doesn't change
            window = min(50, len(valid) // 4)
            # Vectorized rolling std using cumulative sums (O(n) vs O(n*window))
            v = valid.astype(np.float64)
            cs = np.cumsum(v)
            cs2 = np.cumsum(v ** 2)
            # Prefix sums for window [i-window, i]
            lo = np.maximum(np.arange(len(v)) - window, 0)
            n_w = np.arange(len(v)) - lo + 1
            sum_x = np.where(lo > 0, cs - cs[lo - 1], cs)
            sum_x2 = np.where(lo > 0, cs2 - cs2[lo - 1], cs2)
            var = sum_x2 / n_w - (sum_x / n_w) ** 2
            rolling_std = np.sqrt(np.maximum(var, 0))
            # Stuck if std < 0.1 m/s for sustained period while speed > 5 m/s
            stuck_mask = (rolling_std < 0.1) & (valid > 5.0)
            stuck_pct = float(np.mean(stuck_mask) * 100)
            if stuck_pct > self.thresholds.airspeed_stuck_pct_warn:
                status = self.worst_status(status, "warning")
                details.append(
                    t("airspeed.pitot_blocked", pct=f"{stuck_pct:.1f}")
                )

        # -----------------------------------------------------------
        # 5. Figure: Airspeed time series
        # -----------------------------------------------------------
        fig_as = self.make_figure(
            title=t("airspeed.fig_airspeed_title"),
            xaxis_title=t("airspeed.time_s"),
            yaxis_title=t("airspeed.fig_airspeed_yaxis"),
            height=400,
        )
        fig_as.add_trace(go.Scattergl(
            x=ts, y=airspeed,
            mode='lines', name=t("airspeed.trace_airspeed"),
            line=dict(color='#1f77b4', width=1),
        ))
        figures.append(fig_as)

        # -----------------------------------------------------------
        # 6. Figure: Differential pressure
        # -----------------------------------------------------------
        valid_dp = diff_press[~np.isnan(diff_press)]
        if len(valid_dp) > 0:
            fig_dp = self.make_figure(
                title=t("airspeed.fig_diffpress_title"),
                xaxis_title=t("airspeed.time_s"),
                yaxis_title=t("airspeed.fig_diffpress_yaxis"),
                height=300,
            )
            fig_dp.add_trace(go.Scattergl(
                x=ts, y=diff_press,
                mode='lines', name=t("airspeed.trace_diffpress"),
                line=dict(color='#ff7f0e', width=1),
            ))
            figures.append(fig_dp)

        # -----------------------------------------------------------
        # 7. Airspeed vs GPS ground speed comparison
        # Ground truth: in normal wind conditions the two disagree by
        # at most ~5 m/s. Sustained larger disagreement is either
        # very strong wind (should show in WIND msg) or a pitot fault
        # (blockage = over-read, leak = under-read).
        # -----------------------------------------------------------
        if self.reader.has_message("GPS"):
            gps = self.reader.get_messages_as_arrays("GPS", ["Spd"])
            t_gps = gps["_timestamp"]
            gspeed = gps["Spd"]

            if len(t_gps) > 10 and len(ts) > 10:
                good_gps = np.isfinite(t_gps) & np.isfinite(gspeed)
                gspeed_interp = np.interp(ts, t_gps[good_gps], gspeed[good_gps])

                # Use GPS-based flight mask (not airspeed) so a failing
                # airspeed sensor that reads near 0 doesn't filter
                # itself out of the check.
                fly_mask = (gspeed_interp > 10.0) & ~np.isnan(airspeed)
                n_fly = int(np.sum(fly_mask))
                if n_fly > 50:
                    signed_diff = (
                        airspeed[fly_mask] - gspeed_interp[fly_mask])
                    diff = np.abs(signed_diff)
                    mean_signed = float(np.nanmean(signed_diff))
                    mean_diff = float(np.nanmean(diff))
                    max_diff = float(np.nanmax(diff))
                    frac_over_5 = float(np.mean(diff > 5))
                    frac_over_10 = float(np.mean(diff > 10))

                    bias_dir = ""
                    if abs(mean_signed) > 2:
                        bias_dir = (
                            t("airspeed.bias_under")
                            if mean_signed < 0
                            else t("airspeed.bias_over")
                        )

                    details.append(
                        t("airspeed.gps_diff_detail",
                          mean=f"{mean_diff:.1f}",
                          mx=f"{max_diff:.1f}",
                          pct5=f"{frac_over_5*100:.0f}",
                          bias=bias_dir)
                    )

                    # Crit: sustained large disagreement = sensor fault.
                    # Warn: persistent moderate disagreement.
                    # Gates are OR so a sustained bias alone (or a burst
                    # of large-gap samples alone) is enough — an AND
                    # gate missed e.g. -7 m/s mean + 35% samples.
                    th = self.thresholds
                    if (abs(mean_signed) > th.airspeed_gps_diff_crit
                            or frac_over_10 > th.airspeed_gps_frac_crit):
                        status = self.worst_status(status, "critical")
                        details.append(
                            t("airspeed.gps_diff_crit",
                              mean=f"{mean_signed:+.1f}",
                              pct10=f"{frac_over_10*100:.0f}",
                              crit=f"{th.airspeed_gps_diff_crit:.0f}",
                              warn=f"{th.airspeed_gps_diff_warn:.0f}")
                        )
                        # Suggest disabling airspeed use as a safety
                        # measure when persistent disagreement is critical.
                        arspd_use = self.reader.get_param("ARSPD_USE", 1)
                        if arspd_use == 1:
                            param_suggestions.append({
                                "param": "ARSPD_USE",
                                "current": 1,
                                "suggested": 0,
                                "reason": t("airspeed.arspd_use_reason"),
                            })
                    elif (abs(mean_signed) > th.airspeed_gps_diff_warn
                            or frac_over_5 > th.airspeed_gps_frac_warn):
                        status = self.worst_status(status, "warning")
                        details.append(
                            t("airspeed.gps_diff_warn",
                              mean=f"{mean_signed:+.1f}",
                              pct5=f"{frac_over_5*100:.0f}",
                              warn=f"{th.airspeed_gps_diff_warn:.0f}")
                        )
                    elif mean_diff > th.airspeed_gps_diff_crit:
                        details.append(
                            t("airspeed.gps_diff_large")
                        )

                fig_cmp = self.make_figure(
                    title=t("airspeed.fig_vs_gps_title"),
                    xaxis_title=t("airspeed.time_s"),
                    yaxis_title=t("airspeed.fig_vs_gps_yaxis"),
                    height=400,
                )
                fig_cmp.add_trace(go.Scattergl(
                    x=ts, y=airspeed,
                    mode='lines', name=t("airspeed.trace_airspeed"),
                    line=dict(color='#1f77b4', width=1),
                ))
                fig_cmp.add_trace(go.Scattergl(
                    x=t_gps, y=gspeed,
                    mode='lines', name=t("airspeed.trace_gps_gspeed"),
                    line=dict(color='#2ca02c', width=1),
                ))
                figures.append(fig_cmp)

        # -----------------------------------------------------------
        # 8. Multi-sensor consistency (dual airspeed)
        # -----------------------------------------------------------
        if not all_nan:
            unique_inst = np.unique(np.nan_to_num(inst, nan=-1).astype(int))
            unique_inst = unique_inst[unique_inst >= 0]
            n_sensors = len(unique_inst)
            if n_sensors >= 2:
                inst_int = np.nan_to_num(inst, nan=-1).astype(int)
                ts_all = arsp["_timestamp"]
                as_all = arsp["Airspeed"]

                m0 = inst_int == 0
                m1 = inst_int == 1
                ts0, as0 = ts_all[m0], as_all[m0]
                ts1, as1 = ts_all[m1], as_all[m1]

                good1 = np.isfinite(ts1) & np.isfinite(as1)
                # Sensor 1 needs two numeric samples to be put on sensor
                # 0's time base; truncating it instead misaligns the
                # arrays whenever the sample counts differ.
                if len(ts0) > 50 and len(ts1) > 50 and np.sum(good1) >= 2:
                    as1_interp = np.interp(ts0, ts1[good1], as1[good1])
                    fly = (as0 > 3.0) | (as1_interp > 3.0)
                    if np.sum(fly) > 50:
                        diff_dual = as0[fly] - as1_interp[fly]
                        rms_dual = float(np.sqrt(np.mean(diff_dual ** 2)))
                        mean_dual = float(np.mean(diff_dual))
                        max_dual = float(np.max(np.abs(diff_dual)))

                        details.append(
                            t("airspeed.dual_stats",
                              n=n_sensors,
                              rms=f"{rms_dual:.1f}",
                              mean=f"{mean_dual:+.1f}",
                              mx=f"{max_dual:.1f}"))

                        if rms_dual > self.thresholds.airspeed_dual_rms_crit:
                            status = self.worst_status(status, "critical")
                            details.append(t("airspeed.dual_crit",
                                             rms=f"{rms_dual:.1f}",
                                             thresh=f"{self.thresholds.airspeed_dual_rms_crit:.0f}"))
                        elif rms_dual > self.thresholds.airspeed_dual_rms_warn:
                            status = self.worst_status(status, "warning")
                            details.append(t("airspeed.dual_warn",
                                             rms=f"{rms_dual:.1f}",
                                             thresh=f"{self.thresholds.airspeed_dual_rms_warn:.0f}"))

                        fig_dual = self.make_figure(
                            title=t("airspeed.fig_dual_title"),
                            xaxis_title=t("airspeed.time_s"),
                            yaxis_title=t("airspeed.fig_airspeed_yaxis"),
                            height=350,
                        )
                        fig_dual.add_trace(go.Scattergl(
                            x=ts0, y=as0,
                            mode='lines', name=t("airspeed.trace_as0"),
                            line=dict(color='#1f77b4', width=1),
                        ))
                        fig_dual.add_trace(go.Scattergl(
                            x=ts1, y=as1,
                            mode='lines', name=t("airspeed.trace_as1"),
                            line=dict(color='#ff7f0e', width=1),
                        ))
                        figures.append(fig_dual)

        # -----------------------------------------------------------
        # 9. Summary
        # -----------------------------------------------------------
        if status == "critical":
            summary = t("airspeed.summary_critical")
        elif status == "warning":
            summary = t("airspeed.summary_warn", val=f"{max_as:.1f}")
        else:
            summary = t("airspeed.summary_good", val=f"{max_as:.1f}")

        # Airspeed issues are physical (pitot tube blockage, sensor wiring).
        if status == "critical":
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = t("airspeed.critical_plain")
            action_text = t("airspeed.critical_action")
        elif status == "warning":
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = t("airspeed.warn_plain")
            action_text = t("airspeed.warn_action")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if status in ("warning", "critical"):
            evidence_source = t("airspeed.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.airspeed"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
            param_suggestions=param_suggestions,
        )
