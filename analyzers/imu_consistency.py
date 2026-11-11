"""
IMU Consistency Analyzer: multi-IMU comparison.

Compares accelerometer and gyroscope readings across IMU instances to detect
sensor disagreement. Requires at least 2 IMU instances (I field).

Required: IMU
Optional: (none)

Title: IMU一致性
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer


def _safe_interp_diff(
    t0: np.ndarray, d0: np.ndarray,
    t1: np.ndarray, d1: np.ndarray,
) -> np.ndarray | None:
    """
    Interpolate d1 onto t0 while dropping NaN from the source arrays.

    np.interp silently treats NaN in y-values as their raw float value,
    producing garbage. Filter non-finite samples first, then interp.
    Returns None if there's not enough valid data on either side.
    """
    good1 = np.isfinite(t1) & np.isfinite(d1)
    if np.sum(good1) < 2:
        return None
    d1_interp = np.interp(t0, t1[good1], d1[good1])
    return d0 - d1_interp


class IMUConsistencyAnalyzer(BaseAnalyzer):
    """
    IMU consistency analyzer.

    Generates:
      - AccX/Y/Z overlay per instance
      - GyrX/Y/Z overlay per instance
      - Inter-IMU difference (IMU0 - IMU1) for AccX/Y/Z
      - Temperature per instance (if available)
    """

    registry_key = "imu_consistency"
    sort_order = 100
    required_messages = ["IMU"]
    optional_messages: list[str] = []

    def can_run(self) -> bool:
        """Need IMU messages present with at least 2 instances."""
        if not self.reader.has_message("IMU"):
            return False
        # Check if we have multi-instance (I field) or old-style IMU2
        return True

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read IMU data with instance field
        # -----------------------------------------------------------
        imu = self.reader.get_messages_as_arrays("IMU", [
            "I", "AccX", "AccY", "AccZ",
            "GyrX", "GyrY", "GyrZ", "T",
        ])
        ts = imu["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.imu_consistency"),
                status="info",
                summary=t("imu_consistency.empty"),
            )

        inst = imu["I"]

        # -----------------------------------------------------------
        # 2. Separate by IMU instance
        # -----------------------------------------------------------
        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )
        all_nan_inst = len(unique_instances) == 0

        # For old-style logs without I field, try IMU2/IMU3
        if all_nan_inst:
            return self._analyze_old_style()

        if len(unique_instances) < 2:
            return self.make_result(
                title=t("analyzer.imu_consistency"),
                status="info",
                summary=t("imu_consistency.single_imu",
                           n=len(unique_instances)),
            )

        details.append(t("imu_consistency.instance_count",
                          n=len(unique_instances)))

        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
        overall_status = "good"

        # Store per-instance data for difference calculation
        instance_data: dict[int, dict[str, tuple[np.ndarray, np.ndarray]]] = {}

        # -----------------------------------------------------------
        # 3. Accelerometer overlay
        # -----------------------------------------------------------
        for axis in ["AccX", "AccY", "AccZ"]:
            fig = self.make_figure(
                title=t("imu_consistency.fig_accel_title", axis=axis),
                xaxis_title=t("imu_consistency.time_s"),
                yaxis_title=t("imu_consistency.fig_accel_yaxis", axis=axis),
                height=350,
            )

            for inst_id in unique_instances:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
                if not np.any(mask):
                    continue

                color = instance_colors[inst_id % len(instance_colors)]
                # Subsample for plot performance
                t_inst = ts[mask]
                data_inst = imu[axis][mask]

                step = max(1, len(t_inst) // 5000)

                fig.add_trace(go.Scattergl(
                    x=t_inst[::step], y=data_inst[::step],
                    mode='lines', name=f"IMU{inst_id}",
                    line=dict(color=color, width=1),
                ))

                # Store for difference calc
                if inst_id not in instance_data:
                    instance_data[inst_id] = {}
                instance_data[inst_id][axis] = (t_inst, data_inst)

            figures.append(fig)

        # -----------------------------------------------------------
        # 4. Gyroscope overlay
        # -----------------------------------------------------------
        for axis in ["GyrX", "GyrY", "GyrZ"]:
            fig = self.make_figure(
                title=t("imu_consistency.fig_gyro_title", axis=axis),
                xaxis_title=t("imu_consistency.time_s"),
                yaxis_title=t("imu_consistency.fig_gyro_yaxis", axis=axis),
                height=350,
            )

            for inst_id in unique_instances:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
                if not np.any(mask):
                    continue

                color = instance_colors[inst_id % len(instance_colors)]
                t_inst = ts[mask]
                data_inst = imu[axis][mask]
                step = max(1, len(t_inst) // 5000)

                fig.add_trace(go.Scattergl(
                    x=t_inst[::step], y=data_inst[::step],
                    mode='lines', name=f"IMU{inst_id}",
                    line=dict(color=color, width=1),
                ))

                if inst_id not in instance_data:
                    instance_data[inst_id] = {}
                instance_data[inst_id][axis] = (t_inst, data_inst)

            figures.append(fig)

        # -----------------------------------------------------------
        # 5. Inter-IMU difference -- compare all instances against IMU[0]
        #    Use armed intervals only for RMS status to avoid ground-
        #    handling thermal drift inflating the difference.
        # -----------------------------------------------------------
        id0 = unique_instances[0]

        accel_diff_rms_values = []
        gyro_diff_rms_values = []

        for id_other in unique_instances[1:]:
            for axis in ("AccX", "AccY", "AccZ"):
                if axis in instance_data.get(id0, {}) and \
                   axis in instance_data.get(id_other, {}):
                    t0, d0 = instance_data[id0][axis]
                    t1, d1 = instance_data[id_other][axis]
                    _, rms = self._armed_rms(t0, d0, t1, d1)
                    if not np.isnan(rms):
                        accel_diff_rms_values.append(rms)
                    details.append(
                        t("imu_consistency.diff_rms_accel",
                          id0=id0, id1=id_other, axis=axis,
                          rms=f"{rms:.4f}")
                    )
            for axis in ("GyrX", "GyrY", "GyrZ"):
                if axis in instance_data.get(id0, {}) and \
                   axis in instance_data.get(id_other, {}):
                    t0, d0 = instance_data[id0][axis]
                    t1, d1 = instance_data[id_other][axis]
                    _, rms = self._armed_rms(t0, d0, t1, d1)
                    if not np.isnan(rms):
                        gyro_diff_rms_values.append(rms)
                    details.append(
                        t("imu_consistency.diff_rms_gyro",
                          id0=id0, id1=id_other, axis=axis,
                          rms=f"{rms:.4f}")
                    )

        # Overall accel diff RMS -- NaN must not silently pass
        if accel_diff_rms_values:
            max_diff_rms = max(accel_diff_rms_values)
            if max_diff_rms > self.thresholds.imu_accel_diff_crit:
                overall_status = self.worst_status(overall_status, "critical")
                details.append(
                    t("imu_consistency.accel_diff_crit",
                      rms=f"{max_diff_rms:.4f}",
                      thresh=self.thresholds.imu_accel_diff_crit)
                )
            elif max_diff_rms > self.thresholds.imu_accel_diff_warn:
                overall_status = self.worst_status(overall_status, "warning")
                details.append(
                    t("imu_consistency.accel_diff_warn",
                      rms=f"{max_diff_rms:.4f}",
                      thresh=self.thresholds.imu_accel_diff_warn)
                )
        else:
            details.append(t("imu_consistency.all_nan"))
            overall_status = self.worst_status(overall_status, "info")

        # Same check for gyro diff
        overall_status = self.worst_status(
            overall_status,
            self._gyro_diff_status(gyro_diff_rms_values, details))

        # Plot accel differences (IMU0 vs IMU1 only for brevity)
        id1 = unique_instances[1]
        if "AccX" in instance_data.get(id0, {}) and \
           "AccX" in instance_data.get(id1, {}):
            fig_diff = self.make_figure(
                title=t("imu_consistency.fig_diff_title",
                        id0=id0, id1=id1),
                xaxis_title=t("imu_consistency.time_s"),
                yaxis_title=t("imu_consistency.fig_diff_yaxis"),
                height=350,
            )

            diff_colors = ['#d62728', '#ff7f0e', '#9467bd']
            for i, axis in enumerate(["AccX", "AccY", "AccZ"]):
                if axis in instance_data[id0] and axis in instance_data[id1]:
                    t0, d0 = instance_data[id0][axis]
                    t1, d1 = instance_data[id1][axis]
                    diff = _safe_interp_diff(t0, d0, t1, d1)
                    if diff is None:
                        continue
                    step = max(1, len(t0) // 5000)

                    fig_diff.add_trace(go.Scattergl(
                        x=t0[::step], y=diff[::step],
                        mode='lines', name=axis,
                        line=dict(color=diff_colors[i], width=1),
                    ))

            self.add_threshold_hline(
                fig_diff, self.thresholds.imu_accel_diff_warn,
                t("imu_consistency.thresh_warn",
                  val=self.thresholds.imu_accel_diff_warn),
                color="#f0ad4e",
            )
            self.add_threshold_hline(
                fig_diff, -self.thresholds.imu_accel_diff_warn,
                t("imu_consistency.neg_thresh_warn"), color="#f0ad4e",
            )
            self.add_threshold_hline(
                fig_diff, self.thresholds.imu_accel_diff_crit,
                t("imu_consistency.thresh_crit",
                  val=self.thresholds.imu_accel_diff_crit),
                color="#d9534f",
            )
            self.add_threshold_hline(
                fig_diff, -self.thresholds.imu_accel_diff_crit,
                t("imu_consistency.neg_thresh_crit"), color="#d9534f",
            )

            figures.append(fig_diff)

        # -----------------------------------------------------------
        # 6. Temperature per instance
        # -----------------------------------------------------------
        fig_temp = self.make_figure(
            title=t("imu_consistency.fig_temp_title"),
            xaxis_title=t("imu_consistency.time_s"),
            yaxis_title=t("imu_consistency.fig_temp_yaxis"),
            height=300,
        )
        has_temp = False

        for inst_id in unique_instances:
            mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            t_inst = ts[mask]
            temp = imu["T"][mask]

            # Check if temperature data is valid
            valid_temp = temp[~np.isnan(temp)]
            if len(valid_temp) > 0 and np.any(valid_temp != 0):
                has_temp = True
                color = instance_colors[inst_id % len(instance_colors)]
                step = max(1, len(t_inst) // 3000)

                fig_temp.add_trace(go.Scattergl(
                    x=t_inst[::step], y=temp[::step],
                    mode='lines', name=f"IMU{inst_id}",
                    line=dict(color=color, width=1),
                ))

                details.append(
                    t("imu_consistency.temp_range",
                      imu=inst_id,
                      lo=f"{float(np.nanmin(temp)):.1f}",
                      hi=f"{float(np.nanmax(temp)):.1f}")
                )

        if has_temp:
            figures.append(fig_temp)

        # -----------------------------------------------------------
        # 7. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = t("imu_consistency.summary_critical")
        elif overall_status == "warning":
            summary = t("imu_consistency.summary_warn")
        else:
            summary = t("imu_consistency.summary_good",
                        n=len(unique_instances))

        # IMU inconsistency is a physical issue (sensor damage, bad mounting).
        if overall_status == "critical":
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = t("imu_consistency.critical_plain")
            action_text = t("imu_consistency.critical_action")
        elif overall_status == "warning":
            action_level = "physical_check"
            plain_summary = t("imu_consistency.warn_plain")
            action_text = t("imu_consistency.warn_action")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = t("imu_consistency.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.imu_consistency"),
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

    def _armed_rms(self, t0, d0, t1, d1):
        """(diff, RMS of diff over armed samples), for the status only:
        ground-handling thermal drift would inflate the difference."""
        diff = _safe_interp_diff(t0, d0, t1, d1)
        if diff is None:
            return diff, float('nan')
        armed_m = self.get_armed_mask(t0)
        armed_diff = diff[armed_m] if np.sum(armed_m) > 10 else diff
        rms_raw = np.sqrt(np.nanmean(armed_diff**2)) \
            if len(armed_diff) > 0 and not np.all(np.isnan(armed_diff)) \
            else float('nan')
        return diff, float(rms_raw)

    def _gyro_diff_status(self, rms_values: list[float],
                          details: list[str]) -> str:
        """Status of the worst gyro difference, noted in details."""
        if not rms_values:
            return "good"
        max_gyro_rms = max(rms_values)
        if max_gyro_rms > self.thresholds.imu_gyro_diff_crit:
            details.append(
                t("imu_consistency.gyro_diff_crit",
                  rms=f"{max_gyro_rms:.4f}",
                  thresh=self.thresholds.imu_gyro_diff_crit)
            )
            return "critical"
        if max_gyro_rms > self.thresholds.imu_gyro_diff_warn:
            details.append(
                t("imu_consistency.gyro_diff_warn",
                  rms=f"{max_gyro_rms:.4f}",
                  thresh=self.thresholds.imu_gyro_diff_warn)
            )
            return "warning"
        return "good"

    # ------------------------------------------------------------------
    # Old-style log support (IMU / IMU2 / IMU3 separate message types)
    # ------------------------------------------------------------------

    def _analyze_old_style(self) -> AnalysisResult:
        """
        Handle old-style logs where each IMU is a separate message type
        (IMU, IMU2, IMU3) rather than using an I (instance) field.
        """
        fields = ["AccX", "AccY", "AccZ", "GyrX", "GyrY", "GyrZ", "T"]
        imu_types = []
        imu_data = {}

        for msg_type in ["IMU", "IMU2", "IMU3"]:
            if self.reader.has_message(msg_type):
                data = self.reader.get_messages_as_arrays(msg_type, fields)
                if len(data["_timestamp"]) > 0:
                    imu_types.append(msg_type)
                    imu_data[msg_type] = data

        if len(imu_types) < 2:
            return self.make_result(
                title=t("analyzer.imu_consistency"),
                status="info",
                summary=t("imu_consistency.single_imu",
                           n=len(imu_types)),
            )

        details = [t("imu_consistency.instance_count_old",
                      n=len(imu_types),
                      types=", ".join(imu_types))]
        figures = []
        param_suggestions: list[dict] = []
        overall_status = "good"
        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c']

        # Accel overlay
        for axis in ["AccX", "AccY", "AccZ"]:
            fig = self.make_figure(
                title=t("imu_consistency.fig_accel_title", axis=axis),
                xaxis_title=t("imu_consistency.time_s"),
                yaxis_title=t("imu_consistency.fig_accel_yaxis", axis=axis),
                height=350,
            )
            for i, mt in enumerate(imu_types):
                d = imu_data[mt]
                step = max(1, len(d["_timestamp"]) // 5000)
                fig.add_trace(go.Scattergl(
                    x=d["_timestamp"][::step], y=d[axis][::step],
                    mode='lines', name=mt,
                    line=dict(color=instance_colors[i % len(instance_colors)],
                              width=1),
                ))
            figures.append(fig)

        # Gyro overlay
        for axis in ["GyrX", "GyrY", "GyrZ"]:
            fig = self.make_figure(
                title=t("imu_consistency.fig_gyro_title", axis=axis),
                xaxis_title=t("imu_consistency.time_s"),
                yaxis_title=t("imu_consistency.fig_gyro_yaxis", axis=axis),
                height=350,
            )
            for i, mt in enumerate(imu_types):
                d = imu_data[mt]
                step = max(1, len(d["_timestamp"]) // 5000)
                fig.add_trace(go.Scattergl(
                    x=d["_timestamp"][::step], y=d[axis][::step],
                    mode='lines', name=mt,
                    line=dict(color=instance_colors[i % len(instance_colors)],
                              width=1),
                ))
            figures.append(fig)

        # Inter-IMU difference
        mt0 = imu_types[0]
        mt1 = imu_types[1]
        d0 = imu_data[mt0]
        d1 = imu_data[mt1]
        accel_diff_rms_values = []
        gyro_diff_rms_values = []

        fig_diff = self.make_figure(
            title=t("imu_consistency.fig_diff_old_title",
                    mt0=mt0, mt1=mt1),
            xaxis_title=t("imu_consistency.time_s"),
            yaxis_title=t("imu_consistency.fig_diff_yaxis"),
            height=350,
        )
        diff_colors = ['#d62728', '#ff7f0e', '#9467bd']

        for i, axis in enumerate(["AccX", "AccY", "AccZ"]):
            diff, rms = self._armed_rms(
                d0["_timestamp"], d0[axis],
                d1["_timestamp"], d1[axis],
            )
            if diff is None:
                continue
            if not np.isnan(rms):
                accel_diff_rms_values.append(rms)
            details.append(
                t("imu_consistency.diff_rms_old",
                  mt0=mt0, mt1=mt1, axis=axis,
                  rms=f"{rms:.4f}")
            )

            step = max(1, len(d0["_timestamp"]) // 5000)
            fig_diff.add_trace(go.Scattergl(
                x=d0["_timestamp"][::step], y=diff[::step],
                mode='lines', name=axis,
                line=dict(color=diff_colors[i], width=1),
            ))

        for axis in ("GyrX", "GyrY", "GyrZ"):
            _, rms = self._armed_rms(
                d0["_timestamp"], d0[axis],
                d1["_timestamp"], d1[axis],
            )
            if not np.isnan(rms):
                gyro_diff_rms_values.append(rms)
            details.append(
                t("imu_consistency.diff_rms_old",
                  mt0=mt0, mt1=mt1, axis=axis,
                  rms=f"{rms:.4f}")
            )

        self.add_threshold_hline(
            fig_diff, self.thresholds.imu_accel_diff_warn,
            t("imu_consistency.thresh_warn",
              val=self.thresholds.imu_accel_diff_warn),
            color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig_diff, -self.thresholds.imu_accel_diff_warn,
            t("imu_consistency.neg_thresh_warn"), color="#f0ad4e",
        )
        figures.append(fig_diff)

        # Check thresholds
        if accel_diff_rms_values:
            max_diff_rms = max(accel_diff_rms_values)
            if max_diff_rms > self.thresholds.imu_accel_diff_crit:
                overall_status = "critical"
            elif max_diff_rms > self.thresholds.imu_accel_diff_warn:
                overall_status = "warning"
        overall_status = self.worst_status(
            overall_status,
            self._gyro_diff_status(gyro_diff_rms_values, details))

        if overall_status == "critical":
            summary = t("imu_consistency.summary_critical")
        elif overall_status == "warning":
            summary = t("imu_consistency.summary_warn")
        else:
            summary = t("imu_consistency.summary_good",
                        n=len(imu_types))

        if overall_status == "critical":
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = t("imu_consistency.critical_plain")
            action_text = t("imu_consistency.critical_action")
        elif overall_status == "warning":
            action_level = "physical_check"
            plain_summary = t("imu_consistency.warn_plain")
            action_text = t("imu_consistency.warn_action")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = t("imu_consistency.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.imu_consistency"),
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
