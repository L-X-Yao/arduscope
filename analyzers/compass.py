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

from dataclasses import dataclass

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from lib.outputs import read_outputs
from . import AnalysisResult, BaseAnalyzer


# AP_Arming.cpp: AP_ARMING_COMPASS_MAGFIELD_EXPECTED, the denominator used
# by CompassMot's interference_pct (ArduCopter/compassmot.cpp:206-216).
_COMPASS_EXPECTED_FIELD_MGAUSS = 530.0


@dataclass
class _InterferenceResult:
    figure: go.Figure
    scalar_pct: float | None
    vector_pct: float | None
    vector_label: str | None
    vector_driver: str | None

    @property
    def worst_pct(self) -> float | None:
        values = [v for v in (self.scalar_pct, self.vector_pct)
                  if v is not None]
        return max(values) if values else None


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
                figures.append(interf_result.figure)
                if interf_result.scalar_pct is not None:
                    details.append(
                        t("compass.motor_interference",
                          pct=f"{interf_result.scalar_pct:.1f}")
                    )
                if interf_result.vector_pct is not None:
                    details.append(
                        t("compass.motor_vector_interference",
                          pct=f"{interf_result.vector_pct:.1f}",
                          label=interf_result.vector_label,
                          driver=interf_result.vector_driver)
                    )
                interf_pct = interf_result.worst_pct
                if interf_pct is not None:
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
                    and interf_result.worst_pct is not None
                    and interf_result.worst_pct > self.thresholds.compass_interference_warn
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
                        "value": interf_result.worst_pct,
                        "threshold": self.thresholds.compass_interference_warn}]
                      if interf_result is not None
                      and interf_result.worst_pct is not None else []),
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
    ) -> _InterferenceResult | None:
        """Measure scalar contrast and CompassMot's full-throttle vector.

        CompassMot subtracts a zero-throttle vector, divides the three-axis
        impact by throttle or current, and reports its norm at full throttle
        (ArduCopter/compassmot.cpp:172-216). Here a regression on the nine
        body-to-earth DCM entries removes attitude rotation without assuming
        the compass's installation orientation. The driver coefficient is
        therefore the vector impact after orthogonalising attitude. Low- and
        high-throttle support are still required; an unidentifiable driver
        returns no vector metric rather than guessing.
        """
        channels = self.motor_channels()
        rcou = read_outputs(self.reader, channels)
        t_rcou = rcou["_timestamp"]
        if len(t_rcou) < 10:
            return None

        motor_arrs = [rcou[ch] for ch in channels
                      if not np.all(np.isnan(rcou[ch]))]
        if not motor_arrs:
            return None
        mean_pwm = np.nanmean(np.column_stack(motor_arrs), axis=1)

        pwm_min = self.reader.get_param(self.motor_param("PWM_MIN"), 1000)
        pwm_max = self.reader.get_param(self.motor_param("PWM_MAX"), 2000)
        if pwm_min <= 0:
            pwm_min = 1000
        if pwm_max <= pwm_min:
            pwm_max = 2000
        throttle_rcou = np.clip(
            (mean_pwm - pwm_min) / (pwm_max - pwm_min), 0.0, 1.0)
        throttle = np.interp(t_mag, t_rcou, throttle_rcou)
        # Keep the legacy scalar metric on raw PWM. Affine normalization
        # would not matter until clipping, which can change its percentile bins.
        mean_pwm_mag = np.interp(t_mag, t_rcou, mean_pwm)

        attitude = self._attitude_design(t_mag)
        driver = throttle
        driver_name = "RCOU"
        current_based = False
        if attitude is not None:
            primary_current = self._primary_current()
            if primary_current is not None:
                t_cur, cur = primary_current
                current = self.safe_interp(t_mag, t_cur, cur)
                if np.count_nonzero(np.isfinite(current)) >= 50:
                    # CompassMot prefers current when the primary monitor
                    # provides it (compassmot.cpp:80-85,194-216).
                    driver = current
                    driver_name = "BAT.Curr"
                    current_based = True

        scalar_worst = None
        vector_worst = None
        for inst_id in ([None] if all_nan_inst else unique_instances):
            mask = (np.ones(len(t_mag), dtype=bool) if inst_id is None else
                    np.nan_to_num(inst, nan=-1).astype(int) == inst_id)
            if not np.any(mask):
                continue
            field_xyz = np.column_stack([
                mag_x[mask] - ofs_x[mask],
                mag_y[mask] - ofs_y[mask],
                mag_z[mask] - ofs_z[mask],
            ])
            field = np.linalg.norm(field_xyz, axis=1)
            throttle_inst = throttle[mask]
            scalar_pct = self._throttle_field_contrast(
                mean_pwm_mag[mask], field)
            if (scalar_worst is None
                    or (scalar_pct or 0.0) > (scalar_worst[2] or 0.0)):
                scalar_worst = (mean_pwm_mag[mask], field, scalar_pct)

            vector_pct = None
            if attitude is not None:
                vector_pct = self._vector_motor_impact(
                    driver[mask], throttle_inst, field_xyz, attitude[mask],
                    current_based)
            label = "MAG" if inst_id is None else f"MAG{inst_id}"
            if vector_pct is not None and (
                    vector_worst is None or vector_pct > vector_worst[0]):
                vector_worst = (vector_pct, label, driver_name)

        if scalar_worst is None:
            return None
        throttle_interp, field, scalar_pct = scalar_worst

        # The existing figure remains the scalar field/PWM view. The vector
        # regression is a separate detail and severity input, not a new plot.
        step = max(1, len(field) // 3000)
        fig = self.make_figure(
            title=t("compass.fig_interference"),
            xaxis_title=t("compass.avg_motor_pwm"),
            yaxis_title=t("compass.field_mgauss"),
            height=400,
        )
        fig.add_trace(go.Scattergl(
            x=throttle_interp[::step], y=field[::step],
            mode='markers', marker=dict(size=3, color='#1f77b4', opacity=0.4),
            name=t("compass.fig_interference_trace"),
        ))
        return _InterferenceResult(
            figure=fig, scalar_pct=scalar_pct,
            vector_pct=vector_worst[0] if vector_worst else None,
            vector_label=vector_worst[1] if vector_worst else None,
            vector_driver=vector_worst[2] if vector_worst else None,
        )

    def _primary_current(self) -> tuple[np.ndarray, np.ndarray] | None:
        """Primary BAT current when CompassMot would choose current mode.

        AP_BattMonitor::current_amps defaults to instance zero and requires
        has_current (AP_BattMonitor.cpp:839-845). Do not substitute another
        pack or a sum monitor. If the instance column is absent, pack identity
        is unknown, so return None rather than merging potentially mixed rows.
        CompassMot skips current updates below 3 A, so a lower maximum falls
        back to throttle as well.
        """
        if not self.reader.has_message("BAT"):
            return None
        bat = self.reader.get_messages_as_arrays("BAT", ["Inst", "Curr"])
        inst = bat["Inst"]
        known = np.isfinite(inst)
        if not np.any(known):
            return None
        mask = inst == 0
        ts = bat["_timestamp"][mask]
        curr = bat["Curr"][mask]
        valid = np.isfinite(ts) & np.isfinite(curr)
        if np.count_nonzero(valid) < 50 or np.max(curr[valid]) < 3.0:
            return None
        return ts[valid], curr[valid]

    def _attitude_design(self, t_mag: np.ndarray) -> np.ndarray | None:
        """Intercept plus nine ATT DCM entries, interpolated to MAG rows."""
        if not self.reader.has_message("ATT"):
            return None
        att = self.reader.get_messages_as_arrays(
            "ATT", ["Roll", "Pitch", "Yaw"])
        t_att = att["_timestamp"]
        angles = []
        for name in ("Roll", "Pitch", "Yaw"):
            values = np.radians(att[name])
            good = np.isfinite(t_att) & np.isfinite(values)
            if np.count_nonzero(good) < 2:
                return None
            # Unwrap before interpolation so a yaw crossing +/-180 degrees
            # does not manufacture a full rotation between adjacent rows.
            unwrapped = np.unwrap(values[good])
            angles.append(np.interp(t_mag, t_att[good], unwrapped))
        roll, pitch, yaw = angles
        cr, sr = np.cos(roll), np.sin(roll)
        cp, sp = np.cos(pitch), np.sin(pitch)
        cy, sy = np.cos(yaw), np.sin(yaw)
        # Matrix3::from_euler, AP_Math/matrix3.cpp:26-44 (321 ordering).
        rot = np.empty((len(t_mag), 3, 3))
        rot[:, 0, 0] = cp * cy
        rot[:, 0, 1] = sr * sp * cy - cr * sy
        rot[:, 0, 2] = cr * sp * cy + sr * sy
        rot[:, 1, 0] = cp * sy
        rot[:, 1, 1] = sr * sp * sy + cr * cy
        rot[:, 1, 2] = cr * sp * sy - sr * cy
        rot[:, 2, 0] = -sp
        rot[:, 2, 1] = sr * cp
        rot[:, 2, 2] = cr * cp
        return np.column_stack([
            np.ones(len(t_mag)), rot.reshape(len(t_mag), 9)])

    @staticmethod
    def _vector_motor_impact(
        driver: np.ndarray,
        throttle: np.ndarray,
        field_xyz: np.ndarray,
        attitude: np.ndarray,
        current_based: bool,
    ) -> float | None:
        """Full-throttle vector impact in percent of the expected 530 mG."""
        valid = (np.isfinite(driver) & np.isfinite(throttle)
                 & np.all(np.isfinite(field_xyz), axis=1)
                 & np.all(np.isfinite(attitude), axis=1))
        if np.count_nonzero(valid) < 50:
            return None
        thr = throttle[valid]
        # CompassMot's baseline is zero throttle. Offline, accept only rows
        # near the lowest observed output (and within the bottom quintile),
        # so a handful of idle rows cannot be diluted by a high 20th percentile.
        low_cut = min(float(np.percentile(thr, 20)),
                      float(np.min(thr)) + 0.05)
        high_cut = float(np.percentile(thr, 80))
        if np.count_nonzero(thr <= low_cut) < 5 \
                or np.count_nonzero(thr >= high_cut) < 5:
            return None

        drv = driver[valid]
        design = attitude[valid]
        # Without driver variation orthogonal to attitude, a motor effect
        # and an attitude effect are not separately identifiable.
        driver_span = float(np.std(drv))
        residual = drv - design @ np.linalg.lstsq(design, drv, rcond=None)[0]
        if driver_span <= 1e-9 or np.std(residual) < 0.05 * driver_span:
            return None

        regressors = np.column_stack([design, drv])
        coefficients = np.linalg.lstsq(
            regressors, field_xyz[valid], rcond=None)[0]
        impact = coefficients[-1]
        if not np.all(np.isfinite(impact)):
            return None
        full_scale = 1.0
        if current_based:
            # compassmot.cpp:214-215: impact/amp at the observed current,
            # expressed at full throttle via current_max/throttle_max.
            throttle_max = float(np.max(thr))
            if throttle_max <= 0:
                return None
            full_scale = float(np.max(drv)) / throttle_max
        return float(np.linalg.norm(impact) * full_scale
                     / _COMPASS_EXPECTED_FIELD_MGAUSS * 100.0)

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
