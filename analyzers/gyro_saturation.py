"""
Gyro Saturation Analyzer: detects gyroscope sensor saturation events.

When IMU gyro values hit the range the driver set for the sensor, the flight
controller loses accurate rotation measurement, which can degrade attitude
control and trigger EKF anomalies.

Required: IMU

Title: 陀螺仪饱和
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer

# Device types the driver runs at +/-4000 deg/s: ICM45686 and IIM42653
# (AP_InertialSensor_Invensensev3.cpp). The rest of the fleet is +/-2000.
_GYRO_4000DPS_DEVTYPES = {0x3B, 0x3D}


class GyroSaturationAnalyzer(BaseAnalyzer):
    """
    Gyroscope saturation analysis.

    Detects samples where gyro readings approach the sensor's range. A
    50 deg/s margin is used to account for noise near that boundary.
    """

    registry_key = "gyro_saturation"
    sort_order = 42
    required_messages = ["IMU"]

    GYRO_SAT_MARGIN_DPS = 50.0

    def _gyro_range_dps(self) -> float:
        """Range of the primary IMU's gyro, from the device type in bits
        16-23 of its id. Older logs call the parameter INS_GYR_ID; with
        neither name, assume the common range."""
        dev_id = self.reader.get_param(
            "INS_GYR1_ID", self.reader.get_param("INS_GYR_ID", 0))
        devtype = (int(dev_id) >> 16) & 0xFF
        return 4000.0 if devtype in _GYRO_4000DPS_DEVTYPES else 2000.0

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read IMU gyro data
        # -----------------------------------------------------------
        # Read the primary IMU only. get_messages_as_arrays("IMU", …)
        # interleaves every instance at shared timestamps, which dilutes
        # the saturation fraction denominator by the IMU count and
        # understates the reported percentage on multi-IMU logs.
        imu = self.read_primary_imu(["GyrX", "GyrY", "GyrZ"])
        ts = imu["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.gyro_saturation"),
                status="info",
                summary=t("gyro_sat.empty"),
            )

        # ArduPilot logs IMU.GyrX/Y/Z in rad/s (pymavlink applies no
        # unit conversion). The saturation threshold and all display
        # labels are in deg/s (sensor datasheet units, e.g. ICM-42688-P
        # ±2000 deg/s), so convert here.
        _RAD2DEG = 180.0 / np.pi
        gyr_x = imu["GyrX"] * _RAD2DEG
        gyr_y = imu["GyrY"] * _RAD2DEG
        gyr_z = imu["GyrZ"] * _RAD2DEG

        # -----------------------------------------------------------
        # 2. Filter to armed intervals only
        # -----------------------------------------------------------
        armed = self.reader.get_armed_intervals()
        if armed:
            armed_mask = np.zeros(len(ts), dtype=bool)
            for start, end in armed:
                armed_mask |= (ts >= start) & (ts <= end)
            if np.sum(armed_mask) > 100:
                ts = ts[armed_mask]
                gyr_x = gyr_x[armed_mask]
                gyr_y = gyr_y[armed_mask]
                gyr_z = gyr_z[armed_mask]

        total_samples = len(ts)
        if total_samples == 0:
            return self.make_result(
                title=t("analyzer.gyro_saturation"),
                status="info",
                summary=t("gyro_sat.empty"),
            )

        # -----------------------------------------------------------
        # 3. Compute saturation statistics
        # -----------------------------------------------------------
        abs_x = np.abs(gyr_x)
        abs_y = np.abs(gyr_y)
        abs_z = np.abs(gyr_z)

        gyro_range = self._gyro_range_dps()
        sat_threshold = gyro_range - self.GYRO_SAT_MARGIN_DPS
        sat_mask = (abs_x >= sat_threshold) | \
                   (abs_y >= sat_threshold) | \
                   (abs_z >= sat_threshold)
        saturated_samples = int(np.sum(sat_mask))
        sat_fraction = saturated_samples / total_samples

        max_gyr_x = float(np.nanmax(abs_x)) if not np.all(np.isnan(gyr_x)) else 0.0
        max_gyr_y = float(np.nanmax(abs_y)) if not np.all(np.isnan(gyr_y)) else 0.0
        max_gyr_z = float(np.nanmax(abs_z)) if not np.all(np.isnan(gyr_z)) else 0.0
        max_gyr = max(max_gyr_x, max_gyr_y, max_gyr_z)

        details.append(
            t("gyro_sat.axis_detail", axis="X", mx=f"{max_gyr_x:.1f}")
        )
        details.append(
            t("gyro_sat.axis_detail", axis="Y", mx=f"{max_gyr_y:.1f}")
        )
        details.append(
            t("gyro_sat.axis_detail", axis="Z", mx=f"{max_gyr_z:.1f}")
        )
        details.append(
            t("gyro_sat.sat_count",
              count=saturated_samples, total=total_samples,
              pct=f"{sat_fraction * 100:.3f}")
        )

        # -----------------------------------------------------------
        # 4. Status determination
        # -----------------------------------------------------------
        warn_thresh = self.thresholds.gyro_sat_warn
        crit_thresh = self.thresholds.gyro_sat_crit

        if sat_fraction > crit_thresh:
            status = "critical"
            summary = t("gyro_sat.critical_summary",
                        pct=f"{sat_fraction * 100:.2f}",
                        thresh=f"{crit_thresh * 100:.1f}")
            plain_summary = t("gyro_sat.critical_plain")
            action_text = t("gyro_sat.action_steps")
            evidence_source = t("gyro_sat.evidence", limit=int(gyro_range),
                                thresh=int(sat_threshold))
        elif sat_fraction > warn_thresh:
            status = "warning"
            summary = t("gyro_sat.warn_summary",
                        pct=f"{sat_fraction * 100:.2f}",
                        thresh=f"{warn_thresh * 100:.1f}")
            plain_summary = t("gyro_sat.warn_plain")
            action_text = t("gyro_sat.action_steps")
            evidence_source = t("gyro_sat.evidence", limit=int(gyro_range),
                                thresh=int(sat_threshold))
        else:
            status = "good"
            summary = t("gyro_sat.good_summary",
                        mx=f"{max_gyr:.1f}")
            plain_summary = t("gyro_sat.good_plain")
            action_text = ""
            evidence_source = ""

        # No filter suggestion: saturation happens at the sensor, before
        # INS_GYRO_FILTER, so filtering cannot prevent it.
        action_level = "ok" if status == "good" else "physical_check"

        # -----------------------------------------------------------
        # 6. Figure: max gyro magnitude time series
        # -----------------------------------------------------------
        max_gyro_ts = np.maximum(abs_x, np.maximum(abs_y, abs_z))

        fig = self.make_figure(
            title=t("gyro_sat.fig_title"),
            xaxis_title=t("gyro_sat.time_s"),
            yaxis_title=t("gyro_sat.fig_yaxis"),
            height=400,
        )

        fig.add_trace(go.Scattergl(
            x=ts, y=max_gyro_ts,
            mode='lines', name=t("gyro_sat.trace_max_gyro"),
            line=dict(color='#1f77b4', width=1),
        ))

        self.add_threshold_hline(
            fig, sat_threshold,
            t("gyro_sat.thresh_line", val=int(sat_threshold)),
            color="#d9534f",
        )

        figures.append(fig)

        # -----------------------------------------------------------
        # 7. Return result
        # -----------------------------------------------------------
        return self.make_result(
            title=t("analyzer.gyro_saturation"),
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
