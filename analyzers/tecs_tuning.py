"""
TECS Tuning Quality Analyzer: height and speed tracking evaluation.

Analyzes TECS (Total Energy Control System) performance by computing
RMS tracking errors for height and speed against desired values, over
the rows where the aircraft cruises on its wing. Only runs on ArduPlane
or QuadPlane logs.

AUTO logs TECS through every stage of a flight, but cruise is the only
one its tuning parameters speak for. AP_TECS::_update_pitch sets its own
speed weight for takeoff, landing and VTOL, where "the usual relationship
of speed and height is broken by the VTOL motors". A hovering QuadPlane
reads 0 m/s against a cruise demand, and a plane rolling out after
touchdown does the same.

Required: TECS
Optional: STAT, QTUN, MODE

Title: TECS 调参
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t
from lib.plane_phase import (
    FLIGHT_STAGE_NORMAL, MIN_FIXED_WING_S, covered_seconds, stat_at,
    vtol_windows_mask)
from . import AnalysisResult, BaseAnalyzer


class TECSTuningAnalyzer(BaseAnalyzer):
    """
    TECS tuning quality analyzer (ArduPlane / QuadPlane only).

    Generates:
      - Height tracking error RMS (hdem vs hgt)
      - Speed tracking error RMS (spdem vs spd)
      - Desired vs actual height over time
      - Desired vs actual speed over time
    """

    registry_key = "tecs_tuning"
    sort_order = 145
    required_messages = ["TECS"]
    optional_messages = ["STAT", "QTUN", "MODE"]

    def can_run(self) -> bool:
        if not self.reader.has_message("TECS"):
            return False
        return self.vehicle.is_plane or self.vehicle.is_quadplane

    def _uses_airspeed(self) -> bool:
        """AP_TECS::use_airspeed: without it the speed weight is forced to 0,
        so TECS_SPDWEIGHT changes nothing. ARSP.U is the sensor being used."""
        if self.reader.get_param("TECS_SYNAIRSPEED", 0) > 0:
            return True
        if not self.reader.has_message("ARSP"):
            return False
        arsp = self.reader.get_messages_as_arrays("ARSP", ["U"])
        return bool(np.any(np.nan_to_num(arsp["U"]) > 0))

    def _cruise_mask(self, ts: np.ndarray) -> np.ndarray:
        """True on rows flown in cruise, on the wing.

        STAT.Stage leaves out takeoff, landing and VTOL, and STAT.isFlying
        the ground. Outside AUTO the stage reads NORMAL whatever the
        airframe does (Plane::update_flight_stage), so QRTL and assisted
        flight need the VTOL windows too. Without STAT no row can be
        placed, and none is graded.
        """
        stage = stat_at(self.reader, ts, "Stage")
        flying = stat_at(self.reader, ts, "isFlying")
        if stage is None or flying is None:
            return np.zeros(len(ts), dtype=bool)
        mask = (stage == FLIGHT_STAGE_NORMAL) & (flying > 0.5)
        in_vtol = vtol_windows_mask(self.reader, ts)
        return mask if in_vtol is None else mask & ~in_vtol

    def analyze(self) -> AnalysisResult:
        tecs = self.reader.get_messages_as_arrays("TECS", [
            "hdem", "hgt", "spdem", "spd",
        ])
        ts = tecs["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=_t("analyzer.tecs_tuning"),
                status="info",
                summary=_t("tecs_tuning.empty"),
            )

        cruise = self._cruise_mask(ts)
        cruise_s = covered_seconds(ts[cruise])
        if cruise_s < MIN_FIXED_WING_S:
            return self.make_result(
                title=_t("analyzer.tecs_tuning"),
                status="info",
                summary=_t("tecs_tuning.no_cruise",
                           min_s=f"{MIN_FIXED_WING_S:.0f}"),
            )

        # NaN keeps every other row out of the RMS and the figures alike.
        hdem = np.where(cruise, tecs["hdem"], np.nan)
        hgt = np.where(cruise, tecs["hgt"], np.nan)
        spdem = np.where(cruise, tecs["spdem"], np.nan)
        spd = np.where(cruise, tecs["spd"], np.nan)

        details = [_t("tecs_tuning.cruise_time",
                      cruise_s=f"{cruise_s:.0f}",
                      total_s=f"{covered_seconds(ts):.0f}")]
        figures = []
        suggestions = []

        # Height tracking error
        h_valid = ~(np.isnan(hdem) | np.isnan(hgt))
        if np.any(h_valid):
            h_err = (hdem[h_valid] - hgt[h_valid])
            h_rms = float(np.sqrt(np.mean(h_err ** 2)))
        else:
            h_rms = 0.0

        # Speed tracking error
        s_valid = ~(np.isnan(spdem) | np.isnan(spd))
        if np.any(s_valid):
            s_err = (spdem[s_valid] - spd[s_valid])
            s_rms = float(np.sqrt(np.mean(s_err ** 2)))
        else:
            s_rms = 0.0

        details.append(_t("tecs_tuning.height_rms", rms=f"{h_rms:.2f}"))
        details.append(_t("tecs_tuning.speed_rms", rms=f"{s_rms:.2f}"))

        # Determine status
        h_status = "good"
        if h_rms >= self.thresholds.tecs_height_err_crit:
            h_status = "critical"
        elif h_rms >= self.thresholds.tecs_height_err_warn:
            h_status = "warning"

        s_status = "good"
        if s_rms >= self.thresholds.tecs_speed_err_crit:
            s_status = "critical"
        elif s_rms >= self.thresholds.tecs_speed_err_warn:
            s_status = "warning"

        overall_status = self.worst_status(h_status, s_status)

        # Height error details
        if h_status == "critical":
            details.append(_t("tecs_tuning.height_crit",
                              rms=f"{h_rms:.2f}",
                              thresh=f"{self.thresholds.tecs_height_err_crit:.1f}"))
        elif h_status == "warning":
            details.append(_t("tecs_tuning.height_warn",
                              rms=f"{h_rms:.2f}",
                              thresh=f"{self.thresholds.tecs_height_err_warn:.1f}"))

        # Speed error details
        if s_status == "critical":
            details.append(_t("tecs_tuning.speed_crit",
                              rms=f"{s_rms:.2f}",
                              thresh=f"{self.thresholds.tecs_speed_err_crit:.1f}"))
        elif s_status == "warning":
            details.append(_t("tecs_tuning.speed_warn",
                              rms=f"{s_rms:.2f}",
                              thresh=f"{self.thresholds.tecs_speed_err_warn:.1f}"))

        # Height error names no parameter. TECS holds hdem back when pitch
        # or throttle clips (AP_TECS::_update_height_demand), so this error
        # cannot show a pitch limit, and raising TECS_PITCH_MAX made it worse
        # in SITL. TECS_LAND_ARSPD has no bearing on cruise.
        if s_status in ("warning", "critical"):
            tecs_spdweight = self.reader.get_param("TECS_SPDWEIGHT", -1)
            if tecs_spdweight >= 0 and not self._uses_airspeed():
                details.append(_t("tecs_tuning.no_airspeed"))
            elif tecs_spdweight >= 0:
                suggested_wt = round(min(max(tecs_spdweight + 0.5, 0.0), 2.0), 1)
                suggestions.append({
                    "param": "TECS_SPDWEIGHT",
                    "current": float(tecs_spdweight),
                    "suggested": suggested_wt,
                    "reason": _t("tecs_tuning.reason_spdweight"),
                })

        # Figure 1: Desired vs actual height
        fig_height = self.make_figure(
            title=_t("tecs_tuning.fig_height"),
            xaxis_title=_t("tecs_tuning.time_s"),
            yaxis_title=_t("tecs_tuning.height_m"),
            height=400,
        )
        fig_height.add_trace(go.Scattergl(
            x=ts, y=hdem,
            mode='lines', name=_t("tecs_tuning.desired_height"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_height.add_trace(go.Scattergl(
            x=ts, y=hgt,
            mode='lines', name=_t("tecs_tuning.actual_height"),
            line=dict(color='#d62728', width=1.5),
        ))
        figures.append(fig_height)

        # Figure 2: Desired vs actual speed
        fig_speed = self.make_figure(
            title=_t("tecs_tuning.fig_speed"),
            xaxis_title=_t("tecs_tuning.time_s"),
            yaxis_title=_t("tecs_tuning.speed_ms"),
            height=400,
        )
        fig_speed.add_trace(go.Scattergl(
            x=ts, y=spdem,
            mode='lines', name=_t("tecs_tuning.desired_speed"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_speed.add_trace(go.Scattergl(
            x=ts, y=spd,
            mode='lines', name=_t("tecs_tuning.actual_speed"),
            line=dict(color='#2ca02c', width=1.5),
        ))
        figures.append(fig_speed)

        # Summary
        if overall_status == "critical":
            summary = _t("tecs_tuning.crit_summary",
                          h_rms=f"{h_rms:.2f}", s_rms=f"{s_rms:.2f}")
        elif overall_status == "warning":
            summary = _t("tecs_tuning.warn_summary",
                          h_rms=f"{h_rms:.2f}", s_rms=f"{s_rms:.2f}")
        else:
            summary = _t("tecs_tuning.good_summary",
                          h_rms=f"{h_rms:.2f}", s_rms=f"{s_rms:.2f}")

        # Action level and text
        evidence_source = ""
        if overall_status in ("warning", "critical"):
            action_level = "apply_param" if suggestions else "physical_check"
            plain_summary = _t("tecs_tuning.warn_plain")
            action_text = _t("tecs_tuning.action_text")
            evidence_source = _t("tecs_tuning.evidence")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        return self.make_result(
            title=_t("analyzer.tecs_tuning"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            param_suggestions=suggestions,
            evidence_source=evidence_source,
            evidence=[
                {"field": "height_rms", "value": h_rms,
                 "threshold": self.thresholds.tecs_height_err_warn},
                {"field": "speed_rms", "value": s_rms,
                 "threshold": self.thresholds.tecs_speed_err_warn},
            ],
        )
