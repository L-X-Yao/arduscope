"""
EKF Analyzer: Extended Kalman Filter health and variance monitoring.

Evaluates EKF variance (velocity, position, height, compass) against
FS_EKF_THRESH failsafe threshold, and displays innovation data and
fault flags. Supports multiple EKF cores.

Required: XKF4 or NKF4
Optional: XKF3 or NKF3

Title: EKF状态
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.ekf_lanes import primary_lane_rows
from lib.i18n import t as _t
from lib.np_helpers import safe_nanmax
from . import AnalysisResult, BaseAnalyzer


class EKFAnalyzer(BaseAnalyzer):
    """
    EKF state analyzer.

    Generates:
      - SV/SP/SH/SM variance time series with FS_EKF_THRESH line
      - Innovation time series (from XKF3/NKF3)
      - Fault flag timeline
    """

    registry_key = "ekf"
    sort_order = 50
    # Override can_run to accept either XKF4 or NKF4
    required_messages: list[str] = []
    optional_messages = ["XKF4", "NKF4", "XKF3", "NKF3"]

    def can_run(self) -> bool:
        """Need at least one of XKF4 or NKF4."""
        return (
            self.reader.has_message("XKF4")
            or self.reader.has_message("NKF4")
        )

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Determine which EKF message type to use
        # -----------------------------------------------------------
        if self.reader.has_message("XKF4"):
            kf4_type = "XKF4"
            kf3_type = "XKF3"
        else:
            kf4_type = "NKF4"
            kf3_type = "NKF3"

        details.append(_t("ekf.msg_type", msg_type=kf4_type))

        # -----------------------------------------------------------
        # 2. Read KF4 (variance) data
        # -----------------------------------------------------------
        kf4 = self.reader.get_messages_as_arrays(kf4_type, [
            "C", "SV", "SP", "SH", "SM", "SS", "PI",
        ])
        t = kf4["_timestamp"]

        if len(t) == 0:
            return self.make_result(
                title=_t("analyzer.ekf"),
                status="info",
                summary=_t("ekf.empty", msg_type=kf4_type),
            )

        core_ids = kf4["C"]
        sv = kf4["SV"]  # Velocity variance
        sp = kf4["SP"]  # Position variance
        sh = kf4["SH"]  # Height variance
        sm = kf4["SM"]  # Compass variance
        ss = kf4["SS"]  # Solution status / fault flags

        # -----------------------------------------------------------
        # 3. Separate by EKF core
        # -----------------------------------------------------------
        all_nan_cores = np.all(np.isnan(core_ids))
        unique_cores = sorted(set(int(c) for c in core_ids if not np.isnan(c)))
        if not unique_cores:
            unique_cores = [0]

        details.append(_t("ekf.core_count", count=len(unique_cores)))

        # -----------------------------------------------------------
        # 4. Compute max variances and status
        # -----------------------------------------------------------
        ekf_thresh = self.thresholds.ekf_variance_crit
        ekf_warn = self.thresholds.ekf_variance_warn

        # Status is evaluated on the primary lane only. XKF4/NKF4 log one
        # row per core (C column); AP judges the EKF failsafe on the
        # primary core, so a backup-lane variance spike must not drive the
        # status. Combine the primary-core mask with the armed-window mask
        # (boot convergence spikes would otherwise inflate the max). The
        # full per-core arrays are kept for the figures below.
        if all_nan_cores:
            stat_mask = np.ones(len(t), dtype=bool)
        else:
            stat_mask = primary_lane_rows(core_ids, kf4["PI"])

        armed = self.reader.get_armed_intervals()
        if armed:
            armed_mask = np.zeros(len(t), dtype=bool)
            for start, end in armed:
                armed_mask |= (t >= start) & (t <= end)
            if np.sum(armed_mask & stat_mask) > 10:
                stat_mask = stat_mask & armed_mask

        sv_stat = sv[stat_mask]
        sp_stat = sp[stat_mask]
        sh_stat = sh[stat_mask]
        sm_stat = sm[stat_mask]

        max_sv = safe_nanmax(sv_stat, default=0.0)
        max_sp = safe_nanmax(sp_stat, default=0.0)
        max_sh = safe_nanmax(sh_stat, default=0.0)
        max_sm = safe_nanmax(sm_stat, default=0.0)
        max_variance = max(max_sv, max_sp, max_sh, max_sm)

        details.append(_t("ekf.fs_thresh_detail", val=f"{ekf_thresh:.2f}"))
        details.append(_t("ekf.sv_max", val=f"{max_sv:.3f}"))
        details.append(_t("ekf.sp_max", val=f"{max_sp:.3f}"))
        details.append(_t("ekf.sh_max", val=f"{max_sh:.3f}"))
        details.append(_t("ekf.sm_max", val=f"{max_sm:.3f}"))

        # Note: SS field is a solution status bitmask indicating which
        # solutions are available (attitude, horiz_vel, vert_vel, etc.)
        # and operational flags (takeoff, touchdown, using_gps, etc.).
        # These are NOT fault flags -- all non-zero values are normal.
        # True EKF faults are detected via the ERR message subsystem.

        ss_val = ss[-1] if len(ss) > 0 else None
        ss_display = int(ss_val) if ss_val is not None and np.isfinite(ss_val) else "N/A"
        details.append(_t("ekf.ss_status", val=ss_display))

        # Actionable instructions:
        # critical → safety_stop (navigation unreliable, real risk)
        # warning  → physical_check (GPS placement / compass calibration)
        if max_variance >= ekf_thresh:
            status = "critical"
            summary = _t("ekf.crit_summary",
                         val=f"{max_variance:.3f}",
                         thresh=f"{ekf_thresh:.2f}")
            plain_summary = _t("ekf.crit_plain")
            action_text = _t("ekf.crit_action")
            action_level = "safety_stop"

            # A stricter FS_EKF_THRESH lowers this limit, which the reader
            # should know. Loosening a failsafe fixes nothing, so it is not
            # a suggestion.
            fs_ekf_current = self.reader.get_param("FS_EKF_THRESH", 0.8)
            if fs_ekf_current < 0.8:
                details.append(_t("ekf.fs_thresh_strict",
                                  thresh=f"{fs_ekf_current:.2f}"))
        elif max_variance >= ekf_warn:
            status = "warning"
            summary = _t("ekf.warn_summary",
                         val=f"{max_variance:.3f}",
                         thresh=f"{ekf_warn:.2f}")
            plain_summary = _t("ekf.warn_plain")
            action_text = _t("ekf.warn_action")
            action_level = "physical_check"
        else:
            status = "good"
            summary = _t("ekf.good_summary", val=f"{max_variance:.3f}")
            plain_summary = _t("ekf.good_plain")
            action_text = ""
            action_level = "ok"

        # -----------------------------------------------------------
        # 5. Figure: Variance time series (per core)
        # -----------------------------------------------------------
        core_colors = ['#1f77b4', '#ff7f0e', '#2ca02c']

        for var_name, var_data, ylabel in [
            (_t("ekf.sv_label"), sv, "SV"),
            (_t("ekf.sp_label"), sp, "SP"),
            (_t("ekf.sh_label"), sh, "SH"),
            (_t("ekf.sm_label"), sm, "SM"),
        ]:
            fig = self.make_figure(
                title=_t("ekf.fig_var_title", var_name=var_name),
                xaxis_title=_t("ekf.time_s"),
                yaxis_title=ylabel,
                height=300,
            )

            for ci, core_id in enumerate(unique_cores):
                if all_nan_cores:
                    mask = np.ones(len(t), dtype=bool)
                else:
                    mask = core_ids == core_id
                if not np.any(mask):
                    continue
                color = core_colors[ci % len(core_colors)]
                label = (_t("ekf.core_label", id=core_id) + " " + ylabel
                         if len(unique_cores) > 1 else ylabel)

                fig.add_trace(go.Scattergl(
                    x=t[mask], y=var_data[mask],
                    mode='lines', name=label,
                    line=dict(color=color, width=1),
                ))

            self.add_threshold_hline(
                fig, ekf_thresh,
                _t("ekf.fs_thresh_line", val=f"{ekf_thresh:.2f}"),
                color="#d9534f",
            )
            self.add_threshold_hline(
                fig, ekf_warn,
                _t("ekf.thresh_warn", val=f"{ekf_warn:.2f}"),
                color="#f0ad4e",
            )

            figures.append(fig)

        # -----------------------------------------------------------
        # 6. Figure: Solution status (SS)
        # -----------------------------------------------------------
        fig_ss = self.make_figure(
            title=_t("ekf.fig_ss"),
            xaxis_title=_t("ekf.time_s"),
            yaxis_title=_t("ekf.status_flags"),
            height=250,
        )
        for ci, core_id in enumerate(unique_cores):
            if all_nan_cores:
                mask = np.ones(len(t), dtype=bool)
            else:
                mask = core_ids == core_id
            if not np.any(mask):
                continue
            color = core_colors[ci % len(core_colors)]
            label = (_t("ekf.core_label", id=core_id) + " SS"
                     if len(unique_cores) > 1 else "SS")
            fig_ss.add_trace(go.Scattergl(
                x=t[mask], y=ss[mask],
                mode='lines', name=label,
                line=dict(color=color, width=1),
            ))
        figures.append(fig_ss)

        # -----------------------------------------------------------
        # 7. Optional: Innovation data from XKF3/NKF3
        # -----------------------------------------------------------
        if self.reader.has_message(kf3_type):
            innov_figs = self._innovation_plots(kf3_type, unique_cores)
            figures.extend(innov_figs)

        # Evidence source
        if status == "critical":
            evidence_source = _t("ekf.evidence_crit",
                                 thresh=f"{ekf_thresh:.2f}")
        elif status == "warning":
            evidence_source = _t("ekf.evidence_warn",
                                 thresh=f"{ekf_warn:.2f}")
        else:
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.ekf"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _innovation_plots(
        self, kf3_type: str, core_ids_expected: list[int],
    ) -> list[go.Figure]:
        """Plot innovation (velocity, position, height) from KF3 messages."""
        kf3 = self.reader.get_messages_as_arrays(kf3_type, [
            "C", "IVN", "IVE", "IVD", "IPN", "IPE", "IPD",
        ])
        t = kf3["_timestamp"]
        if len(t) == 0:
            return []

        core_ids = kf3["C"]
        all_nan = np.all(np.isnan(core_ids))
        core_colors = ['#1f77b4', '#ff7f0e', '#2ca02c']
        multi = len(core_ids_expected) > 1
        figures = []

        for fig_title_key, yaxis_key, field_set in [
            ("ekf.fig_vel_innov", "ekf.innov_ms",
             [("IVN", {}), ("IVE", {"dash": "dash"}), ("IVD", {"dash": "dot"})]),
            ("ekf.fig_pos_innov", "ekf.innov_m",
             [("IPN", {}), ("IPE", {"dash": "dash"}), ("IPD", {"dash": "dot"})]),
        ]:
            fig = self.make_figure(
                title=_t(fig_title_key, msg_type=kf3_type),
                xaxis_title=_t("ekf.time_s"),
                yaxis_title=_t(yaxis_key),
                height=350,
            )
            for ci, core_id in enumerate(core_ids_expected):
                if all_nan:
                    mask = np.ones(len(t), dtype=bool)
                else:
                    mask = core_ids == core_id
                if not np.any(mask):
                    continue
                prefix = (_t("ekf.core_label", id=core_id) + " ") if multi else ""
                c = core_colors[ci % len(core_colors)]
                for field, extra_style in field_set:
                    fig.add_trace(go.Scattergl(
                        x=t[mask], y=kf3[field][mask],
                        mode='lines',
                        name=f"{prefix}{field}",
                        line=dict(color=c, width=1, **extra_style),
                    ))
            figures.append(fig)

        return figures
