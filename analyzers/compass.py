"""
Compass Analyzer: magnetic field quality and interference detection.

Evaluates compass health by analyzing field strength consistency, offset
magnitude, and optionally motor interference correlation with throttle.
Supports multiple compass instances (I field).

Required: MAG
Optional: RCOU (for motor interference correlation)

Title: 罗盘分析
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from lib.outputs import read_outputs
from . import AnalysisResult, BaseAnalyzer


class CompassAnalyzer(BaseAnalyzer):
    """
    Compass analysis.

    Generates:
      - Field strength magnitude time series per instance
      - XY scatter plot (MagX vs MagY, should form circle)
      - Offset magnitude time series (OfsX, OfsY, OfsZ)
      - Motor interference correlation (MAG vs throttle, if RCOU available)
    """

    registry_key = "compass"
    sort_order = 80
    required_messages = ["MAG"]
    optional_messages = ["RCOU"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read MAG data
        # -----------------------------------------------------------
        mag = self.reader.get_messages_as_arrays("MAG", [
            "I", "MagX", "MagY", "MagZ", "OfsX", "OfsY", "OfsZ",
        ])
        ts = mag["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.compass"),
                status="info",
                summary=t("compass.mag_empty"),
            )

        inst = mag["I"]
        mag_x = mag["MagX"]
        mag_y = mag["MagY"]
        mag_z = mag["MagZ"]
        # Offsets can be absent / all-NaN on some logs. nan_to_num so
        # field_mag (raw - offset) does not collapse to all-NaN and leak
        # 'nan' into the field/offset detail strings or force
        # field_variation to 0% (disabling the interference-variation
        # warning). Matches compass_consistency; a missing offset == 0.
        ofs_x = np.nan_to_num(mag["OfsX"], nan=0.0)
        ofs_y = np.nan_to_num(mag["OfsY"], nan=0.0)
        ofs_z = np.nan_to_num(mag["OfsZ"], nan=0.0)

        # -----------------------------------------------------------
        # 2. Separate by compass instance
        # -----------------------------------------------------------
        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )
        all_nan_inst = len(unique_instances) == 0
        if not unique_instances:
            unique_instances = [0]

        details.append(t("compass.instance_count", count=len(unique_instances)))

        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
        overall_status = "good"

        # Use the firmware's own offset limit (COMPASS_OFFS_MAX) as the
        # critical threshold rather than a fixed default. ArduPilot's
        # pre-arm check rejects offsets above this value, so anything
        # below it is by definition flight-acceptable. The hardcoded
        # default 1000 was too tight for industrial vehicles where
        # high-current cables near the compass produce legitimate
        # offsets in the 1000-1500 range.
        firmware_ofs_max = self.reader.get_param("COMPASS_OFFS_MAX", 0)
        if firmware_ofs_max and firmware_ofs_max > 0:
            ofs_crit = float(firmware_ofs_max)
            ofs_warn = ofs_crit * 0.7
        else:
            ofs_warn = self.thresholds.compass_ofs_warn
            ofs_crit = self.thresholds.compass_ofs_crit

        # -----------------------------------------------------------
        # 3. Figures: Field strength per instance
        # -----------------------------------------------------------
        fig_strength = self.make_figure(
            title=t("compass.fig_field_strength"),
            xaxis_title=t("compass.time_s"),
            yaxis_title=t("compass.field_mgauss"),
            height=400,
        )

        fig_ofs = self.make_figure(
            title=t("compass.fig_offset"),
            xaxis_title=t("compass.time_s"),
            yaxis_title=t("compass.offset_mgauss"),
            height=350,
        )

        for inst_id in unique_instances:
            if all_nan_inst:
                mask = np.ones(len(ts), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            t_inst = ts[mask]
            mx = mag_x[mask]
            my = mag_y[mask]
            mz = mag_z[mask]
            ox = ofs_x[mask]
            oy = ofs_y[mask]
            oz = ofs_z[mask]
            color = instance_colors[inst_id % len(instance_colors)]
            label = f"MAG{inst_id}" if len(unique_instances) > 1 else "MAG"

            # Field strength — use OFFSET-CORRECTED magnitude.
            # Raw mx/my/mz include hard-iron offsets: as the craft
            # rotates, projections of the offset vector onto each axis
            # change, making raw magnitude APPEAR to vary even when the
            # actual Earth field is constant. ArduPilot subtracts OfsX/
            # Y/Z before using the compass in EKF; the diagnostic check
            # must do the same.
            #
            # Baseline 2026-04-16 showed the bug caused both false
            # positives (log_87: raw 40.9% warn, corrected 28.1% ok)
            # and false negatives (log_81: raw 28.6% ok, corrected
            # 35.8% warn). Fix aligns with ArduPilot compass_learn.cpp
            # which uses corrected field variance.
            field_mag = np.sqrt(
                (mx - ox)**2 + (my - oy)**2 + (mz - oz)**2
            )
            # Graded on the armed window: the limit is about flight, and
            # carrying the aircraft past steel moves the field for no
            # reason that matters. The plot still shows the whole log.
            in_flight = self.get_armed_mask(t_inst)
            graded = field_mag[in_flight] if np.any(in_flight) else field_mag
            mean_field = float(np.nanmean(graded))
            min_field = float(np.nanmin(graded))
            max_field = float(np.nanmax(graded))
            field_variation = (
                (max_field - min_field) / mean_field * 100
                if mean_field > 0 else 0
            )

            details.append(
                t("compass.field_stats",
                  label=label,
                  mean=f"{mean_field:.0f}",
                  min=f"{min_field:.0f}",
                  max=f"{max_field:.0f}",
                  var=f"{field_variation:.1f}")
            )

            # Check field variation threshold.
            # ArduPilot compass_learn.cpp warns at 35% variance; we use
            # the same value for alignment with authoritative source.
            var_warn = self.thresholds.compass_field_var_warn
            if field_variation > var_warn:
                overall_status = self.worst_status(overall_status, "warning")
                details.append(
                    t("compass.field_var_warn",
                      label=label,
                      var=f"{field_variation:.1f}",
                      threshold=f"{var_warn:.0f}")
                )

            fig_strength.add_trace(go.Scattergl(
                x=t_inst, y=field_mag,
                mode='lines', name=label,
                line=dict(color=color, width=1),
            ))

            # Offset magnitude
            ofs_mag = np.sqrt(ox**2 + oy**2 + oz**2)
            max_ofs = float(np.nanmax(ofs_mag))
            mean_ofs = float(np.nanmean(ofs_mag))

            details.append(
                t("compass.offset_stats",
                  label=label,
                  mean=f"{mean_ofs:.0f}",
                  max=f"{max_ofs:.0f}")
            )

            if max_ofs > ofs_crit:
                overall_status = self.worst_status(overall_status, "critical")
                details.append(
                    t("compass.offset_above_crit",
                      label=label, val=f"{max_ofs:.0f}", threshold=f"{ofs_crit:.0f}")
                )
            elif max_ofs > ofs_warn:
                overall_status = self.worst_status(overall_status, "warning")
                details.append(
                    t("compass.offset_above_warn",
                      label=label, val=f"{max_ofs:.0f}", threshold=f"{ofs_warn:.0f}")
                )

            fig_ofs.add_trace(go.Scattergl(
                x=t_inst, y=ofs_mag,
                mode='lines', name=label,
                line=dict(color=color, width=1),
            ))

            # -----------------------------------------------------------
            # 4. XY scatter plot (only first instance to save space)
            # -----------------------------------------------------------
            if inst_id == unique_instances[0]:
                fig_xy = self.make_figure(
                    title=t("compass.fig_xy_scatter", label=label),
                    xaxis_title=t("compass.fig_xy_xaxis"),
                    yaxis_title=t("compass.fig_xy_yaxis"),
                    height=450,
                )
                # Subsample for performance
                step = max(1, len(mx) // 5000)
                fig_xy.add_trace(go.Scattergl(
                    x=mx[::step], y=my[::step],
                    mode='markers',
                    marker=dict(size=2, color=color, opacity=0.5),
                    name=label,
                ))
                fig_xy.update_layout(
                    yaxis=dict(scaleanchor="x", scaleratio=1),
                )
                figures.append(fig_xy)

        # Threshold lines for offset
        self.add_threshold_hline(
            fig_ofs, ofs_warn,
            t("compass.threshold_warn", val=f"{ofs_warn:.0f}"), color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig_ofs, ofs_crit,
            t("compass.threshold_crit", val=f"{ofs_crit:.0f}"), color="#d9534f",
        )

        figures.insert(0, fig_strength)
        figures.append(fig_ofs)

        # -----------------------------------------------------------
        # 5. Optional: Motor interference correlation
        # -----------------------------------------------------------
        interf_result = None
        if self.reader.has_message("RCOU"):
            interf_result = self._motor_interference(
                ts, inst, mag_x, mag_y, mag_z,
                ofs_x, ofs_y, ofs_z,
                unique_instances, all_nan_inst,
            )
            if interf_result is not None:
                interf_fig, interf_pct = interf_result
                figures.append(interf_fig)
                if interf_pct is not None:
                    details.append(
                        t("compass.motor_interference",
                          pct=f"{interf_pct:.1f}")
                    )
                    if interf_pct > self.thresholds.compass_interference_crit:
                        overall_status = self.worst_status(
                            overall_status, "critical")
                        details.append(
                            t("compass.interference_crit",
                              pct=f"{interf_pct:.1f}",
                              threshold=self.thresholds.compass_interference_crit)
                        )
                    elif interf_pct > self.thresholds.compass_interference_warn:
                        overall_status = self.worst_status(
                            overall_status, "warning")
                        details.append(
                            t("compass.interference_warn",
                              pct=f"{interf_pct:.1f}",
                              threshold=self.thresholds.compass_interference_warn)
                        )

        # -----------------------------------------------------------
        # 5b. Motor interference compensation
        # -----------------------------------------------------------
        if self.reader.has_message("RCOU"):
            # COMPASS_MOTCT is the compensation *type* (0=off, 1=throttle,
            # 2=current). COMPASS_MOT_X/Y/Z hold the compensation vector;
            # there is no COMPASS_MOT_COMP, so the old name read as 0
            # forever and shipped a no-op line in the user's .param file.
            current_mot_comp = self.reader.get_param("COMPASS_MOTCT", 0)
            if (interf_result is not None
                    and interf_result[1] is not None
                    and interf_result[1] > self.thresholds.compass_interference_warn
                    and current_mot_comp == 0):
                # The type only selects how the vector is applied, and with
                # a zero vector it changes nothing (AP_Compass_Backend.cpp).
                calibrated = any(
                    self.reader.get_param(f"COMPASS_MOT_{axis}", 0) != 0
                    for axis in "XYZ")
                # AP_Compass.cpp documents MOTCT as not to be changed by
                # hand: CompassMot sets it, so a report only points there.
                details.append(t("compass.mot_comp_off" if calibrated
                                 else "compass.mot_cal_needed"))

        # -----------------------------------------------------------
        # 6. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = t("compass.summary_critical")
        elif overall_status == "warning":
            summary = t("compass.summary_warning")
        else:
            summary = t("compass.summary_good", count=len(unique_instances))

        # Compass issues are physical placement problems — move
        # compass away from motors/power lines or adds shielding.
        # Read current offsets for specific advice.
        ofs_x = self.reader.get_param("COMPASS_OFS_X", 0)
        ofs_y = self.reader.get_param("COMPASS_OFS_Y", 0)
        ofs_z = self.reader.get_param("COMPASS_OFS_Z", 0)
        ofs_mag = (ofs_x**2 + ofs_y**2 + ofs_z**2) ** 0.5
        ofs_info = ""
        if ofs_mag > 1:
            ofs_info = t("compass.ofs_info",
                         x=f"{ofs_x:.0f}", y=f"{ofs_y:.0f}",
                         z=f"{ofs_z:.0f}", mag=f"{ofs_mag:.0f}")

        if overall_status == "critical":
            action_level = "physical_check"
            plain_summary = t("compass.plain_critical")
            action_text = t("compass.action_critical", ofs_info=ofs_info)
        elif overall_status == "warning":
            action_level = "physical_check"
            plain_summary = t("compass.plain_warning")
            action_text = t("compass.action_warning", ofs_info=ofs_info)
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = t("compass.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.compass"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
            evidence=([{"field": "motor_interference_pct",
                        "value": interf_result[1],
                        "threshold": self.thresholds.compass_interference_warn}]
                      if interf_result is not None
                      and interf_result[1] is not None else []),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _motor_interference(
        self,
        t_mag: np.ndarray,
        inst: np.ndarray,
        mag_x: np.ndarray,
        mag_y: np.ndarray,
        mag_z: np.ndarray,
        ofs_x: np.ndarray,
        ofs_y: np.ndarray,
        ofs_z: np.ndarray,
        unique_instances: list[int],
        all_nan_inst: bool = False,
    ) -> tuple[go.Figure, float | None] | None:
        """
        Plot compass field strength vs throttle output to check interference.

        Returns (figure, interference_pct) where interference_pct is the
        relative change in mean field strength between high-throttle (top
        20% PWM) and low-throttle (bottom 20% PWM) samples. None if there
        isn't enough data to compute the contrast.

        Uses mean RCOU PWM as proxy for throttle.
        """
        # Read RCOU for throttle proxy
        channels = self.motor_channels()
        rcou = read_outputs(self.reader, channels)
        t_rcou = rcou["_timestamp"]

        if len(t_rcou) < 10:
            return None

        # Compute mean motor output as throttle proxy
        motor_arrs = []
        for ch in channels:
            arr = rcou[ch]
            if not np.all(np.isnan(arr)):
                motor_arrs.append(arr)
        if not motor_arrs:
            return None

        mean_pwm = np.nanmean(np.column_stack(motor_arrs), axis=1)

        # Each compass has its own position relative to the motors, so
        # report the most exposed one rather than whichever comes first.
        worst = None
        for inst_id in ([None] if all_nan_inst else unique_instances):
            mask = (np.ones(len(t_mag), dtype=bool) if inst_id is None else
                    np.nan_to_num(inst, nan=-1).astype(int) == inst_id)
            if not np.any(mask):
                continue
            field = np.sqrt(
                (mag_x[mask] - ofs_x[mask])**2
                + (mag_y[mask] - ofs_y[mask])**2
                + (mag_z[mask] - ofs_z[mask])**2
            )
            # Interpolate throttle to compass timestamps
            throttle = np.interp(t_mag[mask], t_rcou, mean_pwm)
            pct = self._throttle_field_contrast(throttle, field)
            if worst is None or (pct or 0.0) > (worst[2] or 0.0):
                worst = (throttle, field, pct)
        if worst is None:
            return None
        throttle_interp, field, interf_pct = worst

        # Subsample for scatter plot
        step = max(1, len(field) // 3000)

        fig = self.make_figure(
            title=t("compass.fig_interference"),
            xaxis_title=t("compass.avg_motor_pwm"),
            yaxis_title=t("compass.field_mgauss"),
            height=400,
        )
        fig.add_trace(go.Scattergl(
            x=throttle_interp[::step],
            y=field[::step],
            mode='markers',
            marker=dict(size=3, color='#1f77b4', opacity=0.4),
            name=t("compass.fig_interference_trace"),
        ))

        return fig, interf_pct

    @staticmethod
    def _throttle_field_contrast(throttle: np.ndarray,
                                 field: np.ndarray) -> float | None:
        """Mean field strength contrast between top 20% and bottom 20%
        throttle samples, in percent. Larger means the motors are more
        likely coupling a magnetic field into the compass."""
        valid = ~np.isnan(throttle) & ~np.isnan(field)
        thr_v = throttle[valid]
        field_v = field[valid]
        if len(thr_v) < 50:
            return None
        low_cut = np.percentile(thr_v, 20)
        high_cut = np.percentile(thr_v, 80)
        low_field = field_v[thr_v <= low_cut]
        high_field = field_v[thr_v >= high_cut]
        if len(low_field) < 5 or len(high_field) < 5:
            return None
        low_mean = float(np.mean(low_field))
        if low_mean <= 0:
            return None
        high_mean = float(np.mean(high_field))
        return abs(high_mean - low_mean) / low_mean * 100.0
