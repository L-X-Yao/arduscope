"""
Attitude Analyzer: attitude tracking quality and PID analysis.

Evaluates roll/pitch/yaw tracking error (desired vs actual),
rate tracking, and PID component contributions.

Required: ATT
Optional: RATE, PIDR, PIDP, PIDY

Title: 姿态控制
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer


class AttitudeAnalyzer(BaseAnalyzer):
    """
    Attitude control quality analyzer.

    Generates:
      - Roll/Pitch/Yaw desired vs actual tracking figures
      - Attitude error histogram
      - Rate tracking (if RATE messages available)
      - PID component breakdown (if PIDx messages available)
    """

    registry_key = "attitude"
    sort_order = 20
    required_messages = ["ATT"]
    optional_messages = ["RATE", "PIDR", "PIDP", "PIDY"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read ATT data
        # -----------------------------------------------------------
        att = self.reader.get_messages_as_arrays("ATT", [
            "DesRoll", "Roll", "DesPitch", "Pitch", "DesYaw", "Yaw",
        ])
        t_att = att["_timestamp"]

        if len(t_att) == 0:
            return self.make_result(
                title=t("analyzer.attitude"),
                status="info",
                summary=t("attitude.empty"),
            )

        des_roll = att["DesRoll"]
        roll = att["Roll"]
        des_pitch = att["DesPitch"]
        pitch = att["Pitch"]
        des_yaw = att["DesYaw"]
        yaw = att["Yaw"]

        # -----------------------------------------------------------
        # 2. Compute tracking errors (only during armed intervals)
        # -----------------------------------------------------------
        # An empty interval list can also mean a bench log; only then is
        # the vehicle known to have stood still, so do not grade it.
        if self.reader.never_armed():
            return self.make_result(
                title=t("analyzer.attitude"),
                status="info",
                summary=t("attitude.never_armed"),
            )
        armed_mask = self.get_armed_mask(t_att)

        # Require at least 10 armed samples
        if np.sum(armed_mask) < 10:
            return self.make_result(
                title=t("analyzer.attitude"),
                status="info",
                summary=t("attitude.insufficient_data"),
            )

        # All three angles wrap at 180 degrees: a continuous roll or loop
        # passes through +-180, where a plain difference reads ~360.
        roll_err = (des_roll - roll + 180) % 360 - 180
        pitch_err = (des_pitch - pitch + 180) % 360 - 180
        yaw_err = (des_yaw - yaw + 180) % 360 - 180

        roll_rms = float(np.sqrt(np.nanmean(roll_err[armed_mask] ** 2)))
        pitch_rms = float(np.sqrt(np.nanmean(pitch_err[armed_mask] ** 2)))
        # DesYaw can be absent, which leaves nothing finite to average.
        yaw_err_armed = yaw_err[armed_mask]
        yaw_err_armed = yaw_err_armed[np.isfinite(yaw_err_armed)]
        yaw_rms = (float(np.sqrt(np.mean(yaw_err_armed ** 2)))
                   if len(yaw_err_armed) else float("nan"))

        # Guard against all-NaN slices
        if np.isnan(roll_rms) or np.isnan(pitch_rms):
            return self.make_result(
                title=t("analyzer.attitude"),
                status="info",
                summary=t("attitude.all_nan"),
            )

        max_rp_rms = max(roll_rms, pitch_rms)

        # Multi-dimensional attitude quality
        roll_err_armed = roll_err[armed_mask]
        pitch_err_armed = pitch_err[armed_mask]
        des_roll_armed = des_roll[armed_mask]
        des_pitch_armed = des_pitch[armed_mask]

        roll_max_abs = float(np.nanmax(np.abs(roll_err_armed)))
        pitch_max_abs = float(np.nanmax(np.abs(pitch_err_armed)))
        max_peak_err = max(roll_max_abs, pitch_max_abs)

        # Commanded attitude range
        des_roll_range = float(np.nanmax(des_roll_armed)
                                - np.nanmin(des_roll_armed))
        des_pitch_range = float(np.nanmax(des_pitch_armed)
                                 - np.nanmin(des_pitch_armed))
        des_range = max(des_roll_range, des_pitch_range)

        # P95 error
        roll_p95 = float(np.nanpercentile(np.abs(roll_err_armed), 95))
        pitch_p95 = float(np.nanpercentile(np.abs(pitch_err_armed), 95))

        details.append(t("attitude.roll_rms", val=f"{roll_rms:.2f}"))
        details.append(t("attitude.pitch_rms", val=f"{pitch_rms:.2f}"))
        yaw_rms_str = f"{yaw_rms:.2f}" if not np.isnan(yaw_rms) else "N/A"
        details.append(t("attitude.yaw_rms", val=yaw_rms_str))
        details.append(
            t("attitude.roll_peak",
              peak=f"{roll_max_abs:.1f}", p95=f"{roll_p95:.1f}")
        )
        details.append(
            t("attitude.pitch_peak",
              peak=f"{pitch_max_abs:.1f}", p95=f"{pitch_p95:.1f}")
        )
        details.append(
            t("attitude.des_range",
              roll=f"{des_roll_range/2:.1f}",
              pitch=f"{des_pitch_range/2:.1f}")
        )

        # -----------------------------------------------------------
        # 3. Status determination (multi-criterion)
        # -----------------------------------------------------------
        warn_thresh = self.thresholds.att_rms_warn
        crit_thresh = self.thresholds.att_rms_crit

        rms_crit = max_rp_rms >= crit_thresh
        rms_warn = max_rp_rms >= warn_thresh

        peak_beyond_command = (
            des_range < 20.0
            and max_peak_err > crit_thresh
        )
        peak_severe = max_peak_err > 15.0

        p99_roll = float(np.nanpercentile(np.abs(roll_err_armed), 99))
        p99_pitch = float(np.nanpercentile(np.abs(pitch_err_armed), 99))
        max_p99 = max(p99_roll, p99_pitch)
        p99_rms_ratio = max_p99 / max_rp_rms if max_rp_rms > 0.3 else 0.0
        oscillation_burst = (
            p99_rms_ratio > 4.0
            and max_p99 > warn_thresh * 1.5
        )

        if rms_crit or peak_severe:
            status = "critical"
            if rms_crit:
                summary = t("attitude.crit_rms_summary",
                            roll=f"{roll_rms:.2f}",
                            pitch=f"{pitch_rms:.2f}",
                            thresh=crit_thresh)
            else:
                summary = t("attitude.crit_peak_summary",
                            peak=f"{max_peak_err:.1f}")
        elif rms_warn or peak_beyond_command or oscillation_burst:
            status = "warning"
            reasons = []
            if rms_warn:
                reasons.append(
                    t("attitude.warn_rms_reason",
                      rms=f"{max_rp_rms:.2f}"))
            if peak_beyond_command:
                reasons.append(
                    t("attitude.warn_peak_reason",
                      peak=f"{max_peak_err:.1f}",
                      half=f"{des_range/2:.1f}"))
            if oscillation_burst:
                reasons.append(
                    t("attitude.warn_osc_reason",
                      ratio=f"{p99_rms_ratio:.1f}",
                      p99=f"{max_p99:.1f}"))
            summary = t("attitude.warn_summary_prefix") + \
                       t("attitude.semicolon_join").join(reasons)
        else:
            status = "good"
            summary = t("attitude.good_summary",
                        roll=f"{roll_rms:.2f}",
                        pitch=f"{pitch_rms:.2f}",
                        peak=f"{max_peak_err:.1f}")

        # -----------------------------------------------------------
        # 4. Figure: Roll tracking
        # -----------------------------------------------------------
        fig_roll = self.make_figure(
            title=t("attitude.fig_roll_title"),
            xaxis_title=t("attitude.time_s"),
            yaxis_title=t("attitude.angle_deg"),
        )
        fig_roll.add_trace(go.Scattergl(
            x=t_att, y=des_roll,
            mode='lines', name=t("attitude.trace_des_roll"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_roll.add_trace(go.Scattergl(
            x=t_att, y=roll,
            mode='lines', name=t("attitude.trace_roll"),
            line=dict(color='#d62728', width=1),
        ))
        figures.append(fig_roll)

        # -----------------------------------------------------------
        # 5. Figure: Pitch tracking
        # -----------------------------------------------------------
        fig_pitch = self.make_figure(
            title=t("attitude.fig_pitch_title"),
            xaxis_title=t("attitude.time_s"),
            yaxis_title=t("attitude.angle_deg"),
        )
        fig_pitch.add_trace(go.Scattergl(
            x=t_att, y=des_pitch,
            mode='lines', name=t("attitude.trace_des_pitch"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_pitch.add_trace(go.Scattergl(
            x=t_att, y=pitch,
            mode='lines', name=t("attitude.trace_pitch"),
            line=dict(color='#2ca02c', width=1),
        ))
        figures.append(fig_pitch)

        # -----------------------------------------------------------
        # 6. Figure: Yaw tracking
        # -----------------------------------------------------------
        fig_yaw = self.make_figure(
            title=t("attitude.fig_yaw_title"),
            xaxis_title=t("attitude.time_s"),
            yaxis_title=t("attitude.angle_deg"),
        )
        fig_yaw.add_trace(go.Scattergl(
            x=t_att, y=des_yaw,
            mode='lines', name=t("attitude.trace_des_yaw"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_yaw.add_trace(go.Scattergl(
            x=t_att, y=yaw,
            mode='lines', name=t("attitude.trace_yaw"),
            line=dict(color='#9467bd', width=1),
        ))
        figures.append(fig_yaw)

        # -----------------------------------------------------------
        # 7. Figure: Error histogram
        # -----------------------------------------------------------
        fig_hist = self.make_figure(
            title=t("attitude.fig_hist_title"),
            xaxis_title=t("attitude.fig_hist_xaxis"),
            yaxis_title=t("attitude.fig_hist_yaxis"),
        )
        err_data_roll = roll_err[armed_mask]
        err_data_pitch = pitch_err[armed_mask]

        fig_hist.add_trace(go.Histogram(
            x=err_data_roll,
            name=t("attitude.hist_roll_err"),
            opacity=0.6,
            marker_color='#1f77b4',
            nbinsx=80,
        ))
        fig_hist.add_trace(go.Histogram(
            x=err_data_pitch,
            name=t("attitude.hist_pitch_err"),
            opacity=0.6,
            marker_color='#2ca02c',
            nbinsx=80,
        ))
        fig_hist.update_layout(barmode='overlay')
        figures.append(fig_hist)

        # -----------------------------------------------------------
        # 8. Optional: Rate tracking (RATE message)
        # -----------------------------------------------------------
        if self.reader.has_message("RATE"):
            rate_fig = self._rate_tracking()
            if rate_fig is not None:
                figures.append(rate_fig)

        # -----------------------------------------------------------
        # 9. Optional: PID components
        # -----------------------------------------------------------
        for pid_msg, axis_name in [
            ("PIDR", "Roll"), ("PIDP", "Pitch"), ("PIDY", "Yaw"),
        ]:
            if self.reader.has_message(pid_msg):
                pid_fig = self._pid_components(pid_msg, axis_name)
                if pid_fig is not None:
                    figures.append(pid_fig)

        is_copter = 'Copter' in (self.vehicle.vehicle_type or '')
        is_qp = self.vehicle.is_quadplane
        has_rate_pid = is_copter or is_qp

        pid_info = ""
        if has_rate_pid:
            p_prefix = "ATC_RAT" if is_copter else "Q_A_RAT"
            rll_p = self.reader.get_param(f"{p_prefix}_RLL_P", 0)
            pit_p = self.reader.get_param(f"{p_prefix}_PIT_P", 0)
            if rll_p > 0 or pit_p > 0:
                pid_info = t("attitude.pid_info",
                             prefix=p_prefix,
                             rll=f"{rll_p:.4f}", pit=f"{pit_p:.4f}")

        suggestions = []
        autotune_param = "Q_AUTOTUNE_AXES" if is_qp else "AUTOTUNE_AXES"
        autotune_axes = self.reader.get_param(autotune_param, 0)
        if status == "critical":
            plain_summary = t("attitude.critical_plain")
            action_text = t("attitude.critical_action", pid_info=pid_info)
            if autotune_axes not in (7, 0):
                suggestions.append({
                    "param": autotune_param,
                    "current": int(autotune_axes),
                    "suggested": 7,
                    "reason": t("attitude.autotune_axes_reason"),
                })
            action_level = "apply_param" if suggestions else "run_autotune"
        elif status == "warning":
            plain_summary = t("attitude.warn_plain")
            action_text = t("attitude.warn_action", pid_info=pid_info)
            if autotune_axes not in (7, 0):
                suggestions.append({
                    "param": autotune_param,
                    "current": int(autotune_axes),
                    "suggested": 7,
                    "reason": t("attitude.autotune_axes_reason"),
                })
            action_level = "apply_param" if suggestions else "run_autotune"
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if status in ("warning", "critical"):
            if peak_severe:
                evidence_source = t("attitude.evidence_peak_severe",
                                    peak=f"{max_peak_err:.1f}")
            elif peak_beyond_command:
                evidence_source = t("attitude.evidence_peak_beyond",
                                    crit=crit_thresh,
                                    peak=f"{max_peak_err:.1f}",
                                    half=f"{des_range/2:.1f}")
            elif oscillation_burst:
                evidence_source = t("attitude.evidence_osc",
                                    ratio=f"{p99_rms_ratio:.1f}",
                                    p99=f"{max_p99:.1f}",
                                    warn=warn_thresh)
            else:
                evidence_source = t("attitude.evidence_rms",
                                    warn=warn_thresh, crit=crit_thresh)
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.attitude"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            param_suggestions=suggestions,
            evidence_source=evidence_source,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rate_tracking(self) -> go.Figure | None:
        """Plot desired vs actual angular rates."""
        rate = self.reader.get_messages_as_arrays("RATE", [
            "RDes", "R", "PDes", "P", "YDes", "Y",
        ])
        ts = rate["_timestamp"]
        if len(ts) == 0:
            return None

        fig = self.make_figure(
            title=t("attitude.fig_rate_title"),
            xaxis_title=t("attitude.time_s"),
            yaxis_title=t("attitude.fig_rate_yaxis"),
            height=500,
        )

        colors = {
            'Roll': ('#1f77b4', '#aec7e8'),
            'Pitch': ('#2ca02c', '#98df8a'),
            'Yaw': ('#9467bd', '#c5b0d5'),
        }
        for axis, des_field, act_field in [
            ('Roll', 'RDes', 'R'),
            ('Pitch', 'PDes', 'P'),
            ('Yaw', 'YDes', 'Y'),
        ]:
            c_act, c_des = colors[axis]
            fig.add_trace(go.Scattergl(
                x=ts, y=rate[des_field],
                mode='lines', name=t("attitude.trace_des", axis=axis),
                line=dict(color=c_des, width=1, dash='dot'),
            ))
            fig.add_trace(go.Scattergl(
                x=ts, y=rate[act_field],
                mode='lines', name=t("attitude.trace_act", axis=axis),
                line=dict(color=c_act, width=1),
            ))

        return fig

    def _pid_components(self, msg_type: str,
                        axis_name: str) -> go.Figure | None:
        """Plot P, I, D, FF components from a PID message."""
        pid = self.reader.get_messages_as_arrays(msg_type, [
            "P", "I", "D", "FF", "Tar", "Act",
        ])
        ts = pid["_timestamp"]
        if len(ts) == 0:
            return None

        fig = self.make_figure(
            title=t("attitude.fig_pid_title",
                    axis=axis_name, msg=msg_type),
            xaxis_title=t("attitude.time_s"),
            yaxis_title=t("attitude.fig_pid_yaxis"),
            height=400,
        )

        for comp, color in [
            ("P", "#1f77b4"),
            ("I", "#ff7f0e"),
            ("D", "#2ca02c"),
            ("FF", "#d62728"),
        ]:
            fig.add_trace(go.Scattergl(
                x=ts, y=pid[comp],
                mode='lines', name=comp,
                line=dict(color=color, width=1),
            ))

        return fig
