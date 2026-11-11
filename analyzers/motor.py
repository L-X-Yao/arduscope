"""
Motor Analyzer: motor output analysis, saturation, and balance.

Evaluates PWM outputs for motor saturation at SPIN_MIN floor, motor balance
(mean PWM deviation), and hover thrust margin. Supports quad (4) and
octaquad (8) motor configurations.

Required: RCOU
Optional: MOTB

Title: 电机输出
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from lib.outputs import channels_with_function, read_outputs

from . import AnalysisResult, BaseAnalyzer


class MotorAnalyzer(BaseAnalyzer):
    """
    Motor output analyzer.

    Generates:
      - PWM time series (all motors)
      - Motor balance radar / polar chart
      - PWM distribution histogram
      - MOTB throttle / hover analysis (if available)
    """

    registry_key = "motor"
    sort_order = 30
    required_messages = ["RCOU"]
    optional_messages = ["MOTB", "RCO2", "RCO3"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Determine which outputs are motors
        # -----------------------------------------------------------
        channels = self.motor_channels()
        rcou = read_outputs(self.reader, channels)
        ts = rcou["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.motor"),
                status="info",
                summary=t("motor.empty"),
            )

        # Collect motor PWM arrays (skip channels that are all NaN)
        motor_data: dict[str, np.ndarray] = {}
        for ch in channels:
            arr = rcou[ch]
            if not np.all(np.isnan(arr)):
                motor_data[ch] = arr

        if not motor_data:
            return self.make_result(
                title=t("analyzer.motor"),
                status="info",
                summary=t("motor.no_valid_pwm"),
            )

        actual_motor_count = len(motor_data)
        details.append(t("motor.channel_count", n=actual_motor_count))

        # Filter to armed intervals only (exclude ground idle)
        armed = self.reader.get_armed_intervals()
        if armed:
            armed_mask = np.zeros(len(ts), dtype=bool)
            for start, end in armed:
                armed_mask |= (ts >= start) & (ts <= end)
            if np.sum(armed_mask) > 100:
                ts = ts[armed_mask]
                for ch in list(motor_data.keys()):
                    motor_data[ch] = motor_data[ch][armed_mask]

        # -----------------------------------------------------------
        # 2. PWM range and SPIN_MIN calculations
        # -----------------------------------------------------------
        if self.vehicle.is_quadplane:
            pwm_min = self.reader.get_param("Q_M_PWM_MIN",
                      self.reader.get_param("MOT_PWM_MIN", 1000))
            pwm_max = self.reader.get_param("Q_M_PWM_MAX",
                      self.reader.get_param("MOT_PWM_MAX", 2000))
        else:
            pwm_min = self.reader.get_param("MOT_PWM_MIN", 1000)
            pwm_max = self.reader.get_param("MOT_PWM_MAX", 2000)
        spin_min_name = "Q_M_SPIN_MIN" if self.vehicle.is_quadplane else "MOT_SPIN_MIN"
        spin_arm_name = "Q_M_SPIN_ARM" if self.vehicle.is_quadplane else "MOT_SPIN_ARM"
        spin_min = self.reader.get_param(
            spin_min_name, self.thresholds.mot_spin_min)
        spin_arm = self.reader.get_param(
            spin_arm_name, self.thresholds.mot_spin_arm)

        # MOT_PWM_MIN/MAX = 0 means "auto" (use the RC output range). Left
        # at 0 they collapse spin_min_pwm and the 95%-PWM saturation level
        # to 0 — disabling floor detection and making the forward-thrust
        # check treat every sample as saturated (false critical). Fall back
        # to the standard 1000-2000us range AP maps that auto-setting to.
        if pwm_min <= 0:
            pwm_min = 1000
        if pwm_max <= pwm_min:
            pwm_max = 2000

        spin_min_pwm = pwm_min + (pwm_max - pwm_min) * spin_min
        details.append(t("motor.pwm_range",
                         lo=f"{pwm_min:.0f}", hi=f"{pwm_max:.0f}"))
        details.append(t("motor.spin_min_detail",
                         name=spin_min_name, val=f"{spin_min:.3f}",
                         pwm=f"{spin_min_pwm:.0f}"))
        details.append(t("motor.spin_arm_detail",
                         name=spin_arm_name, val=f"{spin_arm:.3f}"))

        # METHODIC: SPIN_MIN must be at least SPIN_ARM + 0.03 so the
        # motors have measurable PID headroom above the arming idle.
        # Source: docs/convergence_criteria.md -> "Motor Output"
        # Tolerate float precision (e.g. 0.08 - 0.05 = 0.0299... in IEEE)
        spin_gap = spin_min - spin_arm
        gap_min = self.thresholds.mot_spin_gap_min
        if spin_gap < gap_min - 1e-4:
            status = "warning"
            details.append(
                t("motor.spin_gap_warn",
                  smin=spin_min_name, sarm=spin_arm_name,
                  gap=f"{spin_gap:.3f}")
            )
            details.append(
                t("motor.spin_gap_suggest",
                  smin=spin_min_name,
                  val=f"{spin_arm + gap_min:.2f}")
            )
            param_suggestions.append({
                "param": spin_min_name,
                "current": spin_min,
                "suggested": round(spin_arm + gap_min, 2),
                "reason": t("motor.spin_gap_reason"),
            })
        else:
            status = "good"

        # -----------------------------------------------------------
        # 3. Motor saturation analysis (at SPIN_MIN floor)
        # -----------------------------------------------------------
        floor_active_thr = spin_min_pwm + 50  # 50 PWM = "doing real work"
        if motor_data:
            motor_stack = np.column_stack(list(motor_data.values()))
            vtol_working_mask = np.any(
                motor_stack > floor_active_thr, axis=1
            )
        else:
            vtol_working_mask = np.zeros(0, dtype=bool)

        floor_warnings = []
        motor_means = {}
        motor_floor_pct = {}

        for ch, arr in motor_data.items():
            # Only samples where a motor is doing real work. With no working
            # window (pure ground idle / never flew under load) do NOT fall
            # back to raw idle samples — idle PWM sits near the SPIN_MIN
            # floor and would register as 100% floor saturation.
            arr = (arr[vtol_working_mask]
                   if np.any(vtol_working_mask) else arr[:0])
            active_mask = arr > pwm_min
            active_count = int(np.sum(active_mask))

            if active_count > 0:
                active_arr = arr[active_mask]
                motor_means[ch] = float(np.nanmean(active_arr))

                at_floor = int(np.sum(active_arr <= spin_min_pwm + 5))
                floor_frac = at_floor / active_count
                motor_floor_pct[ch] = floor_frac

                if floor_frac > self.thresholds.motor_saturation_crit:
                    floor_warnings.append(
                        t("motor.floor_crit",
                          ch=ch, pct=f"{floor_frac*100:.1f}")
                    )
                    status = self.worst_status(status, "critical")
                elif floor_frac > self.thresholds.motor_saturation_warn:
                    floor_warnings.append(
                        t("motor.floor_warn",
                          ch=ch, pct=f"{floor_frac*100:.1f}")
                    )
                    status = self.worst_status(status, "warning")
            else:
                motor_means[ch] = 0.0
                motor_floor_pct[ch] = 0.0

        if floor_warnings:
            for w in floor_warnings:
                details.append(t("motor.saturation_warn", detail=w))

        # -----------------------------------------------------------
        # 4. Motor balance analysis
        # -----------------------------------------------------------
        if motor_means and any(v > 0 for v in motor_means.values()):
            mean_vals = [v for v in motor_means.values() if v > 0]
            global_mean = float(np.mean(mean_vals))
            max_dev = 0.0

            for ch, m in motor_means.items():
                if m > 0 and global_mean > 0:
                    dev = abs(m - global_mean) / global_mean
                    max_dev = max(max_dev, dev)
                    details.append(
                        t("motor.balance_detail",
                          ch=ch, mean=f"{m:.0f}",
                          diff=f"{(m - global_mean):+.1f}",
                          pct=f"{dev*100:.1f}")
                    )

            if max_dev > self.thresholds.motor_imbalance_crit:
                status = self.worst_status(status, "critical")
                details.append(
                    t("motor.imbalance_crit",
                      pct=f"{max_dev*100:.1f}",
                      thresh=f"{self.thresholds.motor_imbalance_crit*100:.0f}")
                )
            elif max_dev > self.thresholds.motor_imbalance_warn:
                status = self.worst_status(status, "warning")
                details.append(
                    t("motor.imbalance_warn", pct=f"{max_dev*100:.1f}")
                )
            else:
                details.append(
                    t("motor.balance_good", pct=f"{max_dev*100:.1f}")
                )

        # -----------------------------------------------------------
        # 4b. Forward thrust motor check (QuadPlane only).
        # -----------------------------------------------------------
        if self.vehicle.is_quadplane:
            ft_status, ft_details = self._check_forward_thrust(pwm_max)
            status = self.worst_status(status, ft_status)
            details.extend(ft_details)

        # -----------------------------------------------------------
        # 5. Summary + action level.
        # -----------------------------------------------------------
        motor_check_steps = t("motor.physical_check_steps")

        if status == "critical":
            summary = t("motor.summary_critical")
            plain_summary = t("motor.critical_plain")
            action_text = motor_check_steps
            # spin_gap suggestion from the warning branch can survive when
            # motor balance / forward-thrust escalates status to critical.
            action_level = "apply_param" if param_suggestions else "physical_check"
        elif status == "warning":
            summary = t("motor.summary_warn")
            plain_summary = t("motor.warn_plain")
            action_text = motor_check_steps
            action_level = "apply_param" if param_suggestions else "physical_check"
        else:
            summary = t("motor.summary_good", count=actual_motor_count)
            plain_summary = t("motor.good_plain")
            action_text = ""
            action_level = "ok"

        # -----------------------------------------------------------
        # 6. Figure: PWM time series
        # -----------------------------------------------------------
        fig_pwm = self.make_figure(
            title=t("motor.fig_pwm_title"),
            xaxis_title=t("motor.time_s"),
            yaxis_title=t("motor.fig_pwm_yaxis"),
            height=450,
        )

        colors = [
            '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728',
            '#9467bd', '#8c564b', '#e377c2', '#7f7f7f',
        ]
        for i, (ch, arr) in enumerate(motor_data.items()):
            fig_pwm.add_trace(go.Scattergl(
                x=ts, y=arr,
                mode='lines', name=ch,
                line=dict(color=colors[i % len(colors)], width=1),
            ))

        self.add_threshold_hline(
            fig_pwm, spin_min_pwm,
            t("motor.spin_min_line", val=f"{spin_min_pwm:.0f}"),
            color="red", dash="dash",
        )
        figures.append(fig_pwm)

        # -----------------------------------------------------------
        # 7. Figure: Motor balance radar chart
        # -----------------------------------------------------------
        if motor_means and len(motor_means) >= 3:
            fig_radar = self._motor_balance_radar(motor_means)
            if fig_radar is not None:
                figures.append(fig_radar)

        # -----------------------------------------------------------
        # 8. Figure: PWM distribution histogram
        # -----------------------------------------------------------
        fig_hist = self.make_figure(
            title=t("motor.fig_hist_title"),
            xaxis_title=t("motor.fig_pwm_yaxis"),
            yaxis_title=t("motor.fig_hist_yaxis"),
        )
        for i, (ch, arr) in enumerate(motor_data.items()):
            # Only include active samples for histogram
            active = arr[arr > pwm_min]
            if len(active) > 0:
                fig_hist.add_trace(go.Histogram(
                    x=active,
                    name=ch,
                    opacity=0.5,
                    marker_color=colors[i % len(colors)],
                    nbinsx=60,
                ))
        fig_hist.update_layout(barmode='overlay')
        fig_hist.add_vline(
            x=spin_min_pwm, line_dash="dash", line_color="red",
            annotation_text=t("motor.spin_min_line", val=f"{spin_min_pwm:.0f}"),
            annotation_position="top left",
            annotation_font_color="red", annotation_font_size=10,
        )
        figures.append(fig_hist)

        # -----------------------------------------------------------
        # 9. Optional: MOTB (motor battery) analysis
        # -----------------------------------------------------------
        if self.reader.has_message("MOTB"):
            motb_fig = self._motb_analysis()
            if motb_fig is not None:
                figures.append(motb_fig)

        # Evidence source
        if status in ("warning", "critical"):
            evidence_source = t("motor.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.motor"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=param_suggestions,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_forward_thrust(
        self, pwm_max: float,
    ) -> tuple[str, list[str]]:
        """Flag sustained forward-thrust PWM saturation.

        Detects the k_throttle / throttleLeft / throttleRight output
        channel via SERVO_FUNCTION and checks how much time the PWM
        sits at or near PWM_MAX. Sustained saturation means ArduPilot
        was asking the motor for more thrust than it could deliver —
        the exact signature of a failed rear-pusher or a battery that
        can't supply the current.

        Restricted to armed intervals so pre-flight spin-up and
        post-flight rundown can't trigger a saturation warning.

        Returns ("good"/"warning"/"critical", details).
        """
        armed_intervals = self.reader.get_armed_intervals()
        details: list[str] = []

        # Forward-thrust SERVO_FUNCTION values: k_throttle(70),
        # throttleLeft(73), throttleRight(74)
        forward_fn_ids = {70: "throttle", 73: "throttleLeft",
                          74: "throttleRight"}
        ft_channels = [
            (ch, forward_fn_ids[fn])
            for ch, fn in channels_with_function(self.reader, forward_fn_ids)
        ]

        if not ft_channels:
            return "good", []

        # Read the forward-thrust channels (the main analyze() read only
        # fetched the lift motors).
        rcou = read_outputs(self.reader, [ch for ch, _ in ft_channels])
        rcou_t = rcou.get("_timestamp")

        # Armed-window mask: ground spin-ups / shutdown rundowns must
        # not be counted toward the saturation fraction.
        if rcou_t is not None and len(rcou_t) > 0 and armed_intervals:
            armed_mask = np.zeros(len(rcou_t), dtype=bool)
            for s, e in armed_intervals:
                armed_mask |= (rcou_t >= s) & (rcou_t <= e)
        else:
            armed_mask = None

        sat_level = pwm_max * self.thresholds.fwd_thrust_sat_level
        pct_warn = self.thresholds.fwd_thrust_sat_frac_warn
        pct_crit = self.thresholds.fwd_thrust_sat_frac_crit

        worst = "good"
        for ch, label in ft_channels:
            arr = rcou.get(ch)
            if arr is None or len(arr) == 0:
                continue
            if armed_mask is not None:
                arr = arr[armed_mask]
            valid = arr[~np.isnan(arr)]
            if len(valid) == 0 or np.max(valid) <= 900:
                continue  # channel never activated

            # Only count active flight samples (PWM > idle floor)
            active = valid[valid > 1000]
            if len(active) < 100:
                continue

            sat_frac = float(np.mean(active >= sat_level))
            peak = float(np.max(active))
            mean_active = float(np.mean(active))

            details.append(
                t("motor.fwd_thrust_detail",
                  ch=ch, label=label,
                  mean=f"{mean_active:.0f}", peak=f"{peak:.0f}",
                  sat=f"{sat_level:.0f}", pwm_max=f"{pwm_max:.0f}")
            )

            if sat_frac >= pct_crit:
                worst = self.worst_status(worst, "critical")
                details.append(
                    t("motor.fwd_thrust_crit",
                      ch=ch, pct=f"{sat_frac*100:.1f}")
                )
            elif sat_frac >= pct_warn:
                worst = self.worst_status(worst, "warning")
                details.append(
                    t("motor.fwd_thrust_warn",
                      ch=ch, pct=f"{sat_frac*100:.1f}")
                )

        return worst, details

    def _motor_balance_radar(
        self, motor_means: dict[str, float]
    ) -> go.Figure | None:
        """Create a polar/radar chart showing motor balance."""
        labels = list(motor_means.keys())
        values = [motor_means[k] for k in labels]

        if not values or all(v == 0 for v in values):
            return None

        # Close the polygon
        labels_closed = labels + [labels[0]]
        values_closed = values + [values[0]]

        fig = go.Figure()
        fig.add_trace(go.Scatterpolar(
            r=values_closed,
            theta=labels_closed,
            fill='toself',
            name=t("motor.radar_avg_pwm"),
            fillcolor='rgba(31, 119, 180, 0.2)',
            line=dict(color='#1f77b4', width=2),
        ))

        # Add reference circle at global mean
        global_mean = float(np.mean(values))
        fig.add_trace(go.Scatterpolar(
            r=[global_mean] * len(labels_closed),
            theta=labels_closed,
            name=t("motor.radar_mean", val=f"{global_mean:.0f}"),
            line=dict(color='red', width=1, dash='dash'),
        ))

        fig.update_layout(
            title=dict(text=t("motor.fig_radar_title"), font=dict(size=14)),
            polar=dict(
                radialaxis=dict(visible=True),
            ),
            height=400,
            margin=dict(l=60, r=60, t=50, b=50),
            template="plotly_white",
            font=dict(
                family="PingFang SC, Microsoft YaHei, Noto Sans SC, sans-serif",
                size=12,
            ),
            showlegend=True,
        )
        return fig

    def _motb_analysis(self) -> go.Figure | None:
        """Plot MOTB throttle and hover thrust data."""
        # BatRes was removed from MOTB upstream (battery resistance
        # lives in BAT.Res now) — reading it here was a dead all-NaN
        # column (caught by tools/check_msg_fields.py).
        motb = self.reader.get_messages_as_arrays("MOTB", [
            "LiftMax", "BatVolt", "ThLimit",
        ])
        ts = motb["_timestamp"]
        if len(ts) == 0:
            return None

        fig = self.make_figure(
            title=t("motor.fig_motb_title"),
            xaxis_title=t("motor.time_s"),
            yaxis_title=t("motor.fig_motb_yaxis"),
            height=350,
        )

        fig.add_trace(go.Scattergl(
            x=ts, y=motb["LiftMax"],
            mode='lines', name=t("motor.motb_liftmax"),
            line=dict(color='#1f77b4', width=1),
        ))
        fig.add_trace(go.Scattergl(
            x=ts, y=motb["ThLimit"],
            mode='lines', name=t("motor.motb_thlimit"),
            line=dict(color='#d62728', width=1),
        ))

        return fig
