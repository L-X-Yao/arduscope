"""
PID Tuning Quality Analyzer: evaluates PID controller performance.

Analyzes PIDR/PIDP/PIDY messages to assess:
  - Tracking quality (Tar vs Act correlation and error)
  - P/I/D/FF component balance
  - D-term noise level (indicator of noisy sensors or aggressive D gains)
  - I-term accumulation (potential windup)
  - Output saturation
  - Oscillation detection (zero-crossing rate of tracking error)

Also generates concrete, actionable tuning suggestions with current
parameter values and specific recommended adjustments.

Required: (custom can_run -- needs at least one of PIDR, PIDP, PIDY)
Optional: RATE

Title: PID 调参质量
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from lib.plane_phase import (
    MIN_FIXED_WING_S, covered_seconds, stat_at, vtol_windows_mask)

from . import AnalysisResult, BaseAnalyzer


# Axis -> PID parameter name mapping (Copter: ATC_RAT_*, QuadPlane: Q_A_RAT_*)
_AXIS_PARAMS_COPTER: dict[str, dict[str, str]] = {
    "Roll": dict(
        P="ATC_RAT_RLL_P",
        I="ATC_RAT_RLL_I",
        D="ATC_RAT_RLL_D",
        FF="ATC_RAT_RLL_FF",
        FLTD="ATC_RAT_RLL_FLTD",
        FLTT="ATC_RAT_RLL_FLTT",
    ),
    "Pitch": dict(
        P="ATC_RAT_PIT_P",
        I="ATC_RAT_PIT_I",
        D="ATC_RAT_PIT_D",
        FF="ATC_RAT_PIT_FF",
        FLTD="ATC_RAT_PIT_FLTD",
        FLTT="ATC_RAT_PIT_FLTT",
    ),
    "Yaw": dict(
        P="ATC_RAT_YAW_P",
        I="ATC_RAT_YAW_I",
        D="ATC_RAT_YAW_D",
        FF="ATC_RAT_YAW_FF",
        FLTD="ATC_RAT_YAW_FLTD",
        FLTT="ATC_RAT_YAW_FLTT",
    ),
}

_AXIS_PARAMS_QUADPLANE: dict[str, dict[str, str]] = {
    "Roll": dict(
        P="Q_A_RAT_RLL_P",
        I="Q_A_RAT_RLL_I",
        D="Q_A_RAT_RLL_D",
        FF="Q_A_RAT_RLL_FF",
        FLTD="Q_A_RAT_RLL_FLTD",
        FLTT="Q_A_RAT_RLL_FLTT",
    ),
    "Pitch": dict(
        P="Q_A_RAT_PIT_P",
        I="Q_A_RAT_PIT_I",
        D="Q_A_RAT_PIT_D",
        FF="Q_A_RAT_PIT_FF",
        FLTD="Q_A_RAT_PIT_FLTD",
        FLTT="Q_A_RAT_PIT_FLTT",
    ),
    "Yaw": dict(
        P="Q_A_RAT_YAW_P",
        I="Q_A_RAT_YAW_I",
        D="Q_A_RAT_YAW_D",
        FF="Q_A_RAT_YAW_FF",
        FLTD="Q_A_RAT_YAW_FLTD",
        FLTT="Q_A_RAT_YAW_FLTT",
    ),
}

# Fixed-wing rate controllers (APM_Control AP_Roll/Pitch/YawController,
# AC_PID subgroup "_RATE_"). On planes AND on the plane phase of a
# QuadPlane these are what PIDR/PIDP/PIDY log — the VTOL phase logs
# PIQR/PIQP/PIQY instead (governed by Q_A_RAT_*).
_AXIS_PARAMS_PLANE: dict[str, dict[str, str]] = {
    "Roll": dict(
        P="RLL_RATE_P",
        I="RLL_RATE_I",
        D="RLL_RATE_D",
        FF="RLL_RATE_FF",
        FLTD="RLL_RATE_FLTD",
        FLTT="RLL_RATE_FLTT",
    ),
    "Pitch": dict(
        P="PTCH_RATE_P",
        I="PTCH_RATE_I",
        D="PTCH_RATE_D",
        FF="PTCH_RATE_FF",
        FLTD="PTCH_RATE_FLTD",
        FLTT="PTCH_RATE_FLTT",
    ),
    "Yaw": dict(
        P="YAW_RATE_P",
        I="YAW_RATE_I",
        D="YAW_RATE_D",
        FF="YAW_RATE_FF",
        FLTD="YAW_RATE_FLTD",
        FLTT="YAW_RATE_FLTT",
    ),
}


class PIDTuningAnalyzer(BaseAnalyzer):
    """
    PID controller tuning quality analyzer.

    Evaluates each axis (Roll/Pitch/Yaw) independently, then produces
    a composite status. Helps identify:
      - Under-tuned controllers (poor tracking)
      - Over-tuned controllers (excessive D-term noise)
      - I-term windup (large I accumulation)
      - Saturation (output clamped at limits)
      - Oscillation patterns (zero-crossing rate of tracking error)

    Generates concrete parameter suggestions with current values and
    recommended adjustments.
    """

    registry_key = "pid_tuning"
    sort_order = 25   # right after attitude (20)
    required_messages = []  # custom can_run
    # The three tables below, and what the axes are checked against. Left
    # undeclared, each of the PIQ and PSC types cost a pass over the file.
    optional_messages = ["PIDR", "PIDP", "PIDY", "PIQR", "PIQP", "PIQY",
                         "PSCN", "PSCE", "PSCD", "RATE", "POS"]

    _PID_MSGS = [
        ("PIDR", "Roll"),
        ("PIDP", "Pitch"),
        ("PIDY", "Yaw"),
    ]

    # QuadPlane VTOL-phase attitude rate PIDs. PIQA (Z-accel PID) is
    # deliberately not analyzed: the attitude-rate gates and their
    # sources do not transfer to an acceleration loop.
    _PIQ_MSGS = [
        ("PIQR", "Roll"),
        ("PIQP", "Pitch"),
        ("PIQY", "Yaw"),
    ]

    def can_run(self) -> bool:
        """Need at least one PID message type."""
        return any(self.reader.has_message(m)
                   for m, _ in self._PID_MSGS + self._PIQ_MSGS)

    def analyze(self) -> AnalysisResult:
        sub_results = []
        all_statuses = []

        # Phase plan. On a QuadPlane PIDR/PIDP/PIDY carry the
        # fixed-wing controllers and PIQ* the VTOL controllers — two
        # different parameter namespaces and flight phases. The VTOL
        # phase leads: it is where hover tuning problems live.
        if self.vehicle.is_quadplane:
            plan = ([(m, a, "vtol") for m, a in self._PIQ_MSGS]
                    + [(m, a, "plane") for m, a in self._PID_MSGS])
        elif "Plane" in self.vehicle.vehicle_type:
            plan = [(m, a, "plane") for m, a in self._PID_MSGS]
        else:
            plan = [(m, a, "copter") for m, a in self._PID_MSGS]

        for msg_type, axis_name, phase in plan:
            if not self.reader.has_message(msg_type):
                continue
            if not self._rate_loop_active(msg_type, phase):
                continue
            result = self._analyze_axis(msg_type, axis_name, phase)
            sub_results.append(result)
            all_statuses.append(result.status)

        # Position-controller velocity tracking (info-only, PSC*).
        # Not part of all_statuses: info must not dilute a good verdict.
        psc_result = self._analyze_psc()
        if psc_result is not None:
            sub_results.append(psc_result)

        if not sub_results:
            return self.make_result(
                title=t("analyzer.pid_tuning"),
                status="info",
                summary=t("pid_tuning.no_data"),
                plain_summary=t("pid_tuning.no_data_plain"),
            )

        # Aggregate assessed axes only: an axis that had no data to
        # judge (info — e.g. the fixed-wing phase of a hover-only
        # QuadPlane flight) must not outrank three good axes.
        assessed = [s for s in all_statuses if s != "info"]
        overall_status = (self.worst_status(*assessed)
                          if assessed else "info")

        # Build summary from sub-results
        axis_summaries = []
        for r in sub_results:
            axis_name = r.title.split()[0]
            icon = {'good': '✓', 'warning': '⚠', 'critical': '✗', 'info': '-'}
            axis_summaries.append(f"{axis_name}={icon.get(r.status, '?')}")

        summary = t("pid_tuning.axis_status",
                     axes=", ".join(axis_summaries))

        # Aggregate param_suggestions from all sub-results
        all_param_suggestions: list[dict] = []
        for r in sub_results:
            all_param_suggestions.extend(r.param_suggestions)

        # Drop no-op suggestions (suggested == current — e.g. a cur*0.7
        # scaling of a zero gain yields 0.0 -> 0.0). They carry no action
        # yet would force action_level=apply_param and clutter the .param
        # export with meaningless rows.
        all_param_suggestions = [
            s for s in all_param_suggestions
            if round(float(s.get("suggested", 0)), 6)
            != round(float(s.get("current", 0)), 6)
        ]

        if overall_status in ("critical", "warning"):
            plain_summary = t("pid_tuning.critical_plain"
                              if overall_status == "critical"
                              else "pid_tuning.warn_plain")
            # Use apply_param when we have concrete gain adjustments. With
            # none, AutoTune answers poor tracking and nothing else.
            if all_param_suggestions:
                action_level = "apply_param"
            elif any(r.action_level == "run_autotune" for r in sub_results):
                action_level = "run_autotune"
            else:
                action_level = "physical_check"
            action_text = t("pid_tuning.check_airframe"
                            if action_level == "physical_check"
                            else "pid_tuning.autotune_steps")
        else:
            plain_summary = t("pid_tuning.good_plain")
            action_text = ""
            # good + non-empty suggestions: an axis reported good status but
            # still produced parameter suggestions (independent signals).
            # apply_param keeps the invariant intact.
            action_level = "apply_param" if all_param_suggestions else "ok"

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = t("pid_tuning.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.pid_tuning"),
            status=overall_status,
            summary=summary,
            sub_results=sub_results,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=all_param_suggestions,
        )

    def _rate_loop_active(self, msg_type: str, phase: str) -> bool:
        """False when the stream is not a rate loop at all.

        With YAW_RATE_ENABLE off (the default) ArduPlane's PIDY logs the
        yaw damper: Tar is 0 and D dominates by construction, so rate-loop
        gates would condemn a healthy damper and tune inactive parameters.
        """
        return not (phase == "plane" and msg_type == "PIDY"
                    and self.reader.get_param("YAW_RATE_ENABLE", 0) == 0)

    def _get_param(self, name: str) -> float | None:
        """Return a parameter value from the log, or None if not present."""
        val = self.reader.params.get(name)
        return float(val) if val is not None else None

    def _analyze_axis(self, msg_type: str, axis_name: str,
                      phase: str = "copter") -> AnalysisResult:
        """Analyze PID tuning for a single axis.

        phase: "copter" | "plane" | "vtol" — selects the parameter
        namespace, the axis title, and whether RATE.*Out (a VTOL/copter
        attitude-controller product) is a valid authoritative metric.
        """
        if phase == "vtol":
            axis_title = t("pid_tuning.axis_title_vtol", axis=axis_name)
        elif phase == "plane" and self.vehicle.is_quadplane:
            axis_title = t("pid_tuning.axis_title_plane", axis=axis_name)
        else:
            axis_title = t("pid_tuning.axis_title", axis=axis_name)

        pid = self.reader.get_messages_as_arrays(msg_type, [
            "P", "I", "D", "FF", "Tar", "Act",
        ])
        ts = pid["_timestamp"]
        if len(ts) < 10:
            return self.make_result(
                title=axis_title,
                status="info",
                summary=t("pid_tuning.insufficient"),
            )

        p_arr = pid["P"]
        i_arr = pid["I"]
        d_arr = pid["D"]
        ff_arr = pid["FF"]
        tar = pid["Tar"]
        act = pid["Act"]

        # Use armed intervals for analysis
        armed_intervals = self.reader.get_armed_intervals()
        if armed_intervals:
            mask = np.zeros(len(ts), dtype=bool)
            for start, end in armed_intervals:
                mask |= (ts >= start) & (ts <= end)
        else:
            mask = np.ones(len(ts), dtype=bool)

        if np.sum(mask) < 10:
            return self.make_result(
                title=axis_title,
                status="info",
                summary=t("pid_tuning.armed_insufficient"),
            )

        # Fixed-wing phase hygiene. PIDR/PIDP/PIDY keep logging through
        # VTOL flight (where the airframe rotates under the Q
        # controller) and on the ground (where a stabilized mode
        # demands rates a parked airframe can't follow) — both drive
        # tracking ratios past 100% without saying anything about
        # fixed-wing tuning.
        if phase == "plane":
            # Also drops MANUAL/TRAINING, where the rate controllers are
            # bypassed and a pure Plane logs stale rows just the same.
            in_vtol = vtol_windows_mask(self.reader, ts)
            if in_vtol is not None:
                mask &= ~in_vtol
            # STAT.isFlying is the firmware's own flight judgment
            # (ArduPlane Log.cpp: "True if aircraft is probably
            # flying") — the authoritative airborne gate.
            flying = stat_at(self.reader, ts, "isFlying")
            if flying is not None:
                mask &= np.nan_to_num(flying, nan=0.0) > 0.5
            if (np.sum(mask) < 10
                    or covered_seconds(ts[mask]) < MIN_FIXED_WING_S):
                return self.make_result(
                    title=axis_title,
                    status="info",
                    summary=t("pid_tuning.plane_phase_empty"),
                )

        details = []
        figures = []
        status = "good"
        suggestions: list[str] = []
        param_suggestions: list[dict] = []

        # ------------------------------------------------------------------
        # PRIMARY PID quality metric: RATE.*out RMS (METHODIC tuning guide)
        # ------------------------------------------------------------------
        rate_out_rms: float | None = None
        rate_out_peak: float | None = None
        rate_axis_fields = {
            "Roll":  ("ROut", "RDes"),
            "Pitch": ("POut", "PDes"),
            "Yaw":   ("YOut", "YDes"),
        }.get(axis_name)
        # RATE is written by the copter/QuadPlane attitude controller,
        # so it is authoritative for the copter and VTOL phases only —
        # gating a fixed-wing axis on it would judge the plane phase by
        # the hover controller's output.
        if (phase != "plane" and rate_axis_fields
                and self.reader.has_message("RATE")):
            out_field, des_field = rate_axis_fields
            rate = self.reader.get_messages_as_arrays(
                "RATE", [out_field, des_field],
            )
            rate_t = rate["_timestamp"]
            out_vals = rate[out_field]
            des_vals = rate[des_field]
            rate_mask = np.ones(len(rate_t), dtype=bool)
            if len(rate_t) > 0 and armed_intervals:
                rate_mask = np.zeros(len(rate_t), dtype=bool)
                for s, e in armed_intervals:
                    rate_mask |= (rate_t >= s) & (rate_t <= e)
            if phase == "vtol" and len(ts) > 1 and len(rate_t) > 0:
                # Keep only RATE samples inside the VTOL windows. The
                # PIQ* stream itself defines those windows (the Q
                # controllers log only while running); 0.5 s of grace
                # covers logging jitter between the two streams.
                idx = np.searchsorted(ts, rate_t)
                prev_gap = np.abs(
                    rate_t - ts[np.clip(idx - 1, 0, len(ts) - 1)])
                next_gap = np.abs(
                    ts[np.clip(idx, 0, len(ts) - 1)] - rate_t)
                rate_mask &= np.minimum(prev_gap, next_gap) <= 0.5
            out_vals = out_vals[rate_mask]
            des_vals = des_vals[rate_mask]
            valid_mask = ~np.isnan(out_vals) & ~np.isnan(des_vals)
            out_vals = out_vals[valid_mask]
            des_vals = des_vals[valid_mask]

            if len(out_vals) > 10:
                des_max = float(np.max(np.abs(des_vals)))
                rate_out_rms = float(np.sqrt(np.mean(out_vals ** 2)))
                rate_out_peak = float(np.max(np.abs(out_vals)))
                details.append(
                    t("pid_tuning.rate_out_rms",
                      field=out_field, rms=f"{rate_out_rms:.3f}",
                      warn=f"{self.thresholds.pid_rate_out_warn:.2f}",
                      des_max=f"{des_max:.1f}")
                )
                if rate_out_rms >= self.thresholds.pid_rate_out_crit:
                    status = self.worst_status(status, "critical")
                    details.append(
                        t("pid_tuning.rate_out_crit",
                          axis=axis_name, field=out_field,
                          rms=f"{rate_out_rms:.3f}",
                          thresh=f"{self.thresholds.pid_rate_out_crit:.2f}")
                    )
                elif rate_out_rms >= self.thresholds.pid_rate_out_warn:
                    status = self.worst_status(status, "warning")
                    details.append(
                        t("pid_tuning.rate_out_warn",
                          axis=axis_name, field=out_field,
                          rms=f"{rate_out_rms:.3f}",
                          thresh=f"{self.thresholds.pid_rate_out_warn:.2f}")
                    )

        # GROUND TEST DETECTION
        flew = False
        if self.reader.has_message("POS"):
            pos = self.reader.get_messages_as_arrays("POS", ["Alt"])
            pos_t = pos["_timestamp"]
            alt_arr = pos["Alt"]
            if len(pos_t) > 0:
                if armed_intervals:
                    pos_mask = np.zeros(len(pos_t), dtype=bool)
                    for s, e in armed_intervals:
                        pos_mask |= (pos_t >= s) & (pos_t <= e)
                else:
                    # No arming events (e.g. ARMING_REQUIRE=0 planes): fall
                    # back to all samples, matching the PID-mask fallback.
                    # Otherwise a clearly-airborne flight is forced
                    # flew=False and mislabeled a ground-vibration test.
                    pos_mask = np.ones(len(pos_t), dtype=bool)
                alt_armed = alt_arr[pos_mask]
                alt_armed = alt_armed[~np.isnan(alt_armed)]
                if len(alt_armed) > 10:
                    alt_range = float(np.max(alt_armed) - np.min(alt_armed))
                    if alt_range > 2.0:
                        flew = True

        # Ground-vibration fallback
        ground_vib_mode = False
        if not flew:
            rms_trigger = (
                rate_out_rms is not None
                and rate_out_rms >= self.thresholds.pid_rate_out_warn
            )
            peak_trigger = (
                rate_out_peak is not None
                and rate_out_peak >= self.thresholds.pid_ground_vib_peak
            )
            if not (rms_trigger or peak_trigger):
                return self.make_result(
                    title=axis_title,
                    status="info",
                    summary=t("pid_tuning.no_takeoff"),
                    details=[t("pid_tuning.no_takeoff_detail")],
                )
            ground_vib_mode = True
            trigger_desc = []
            if rms_trigger:
                trigger_desc.append(
                    t("pid_tuning.rms_val", val=f"{rate_out_rms:.3f}"))
            if peak_trigger:
                trigger_desc.append(
                    t("pid_tuning.peak_val",
                      val=f"{rate_out_peak:.2f}"))
            details.append(
                t("pid_tuning.ground_vib_mode",
                  triggers=", ".join(trigger_desc))
            )

        # Filter to armed data
        p_m = p_arr[mask]
        i_m = i_arr[mask]
        d_m = d_arr[mask]
        ff_m = ff_arr[mask]
        tar_m = tar[mask]
        act_m = act[mask]
        t_m = ts[mask]

        # Replace NaN with 0 for calculations
        p_m = np.nan_to_num(p_m)
        i_m = np.nan_to_num(i_m)
        d_m = np.nan_to_num(d_m)
        ff_m = np.nan_to_num(ff_m)
        tar_m = np.nan_to_num(tar_m)
        act_m = np.nan_to_num(act_m)

        # Read current axis parameter values from log. The namespace
        # follows the PHASE, not just the vehicle: on a QuadPlane the
        # PIDR/PIDP/PIDY stream is the fixed-wing controller
        # (RLL_RATE_*/PTCH_RATE_*/YAW_RATE_*), while PIQ* is the VTOL
        # controller (Q_A_RAT_*).

        if phase == "vtol":
            param_names = _AXIS_PARAMS_QUADPLANE.get(axis_name, {})
            if self._get_param(param_names.get("P", "")) is None:
                param_names_alt = _AXIS_PARAMS_COPTER.get(axis_name, {})
                if self._get_param(param_names_alt.get("P", "")) is not None:
                    param_names = param_names_alt
        elif phase == "plane":
            param_names = _AXIS_PARAMS_PLANE.get(axis_name, {})
        else:
            param_names = _AXIS_PARAMS_COPTER.get(axis_name, {})
        cur_p  = self._get_param(param_names.get("P", ""))
        cur_i  = self._get_param(param_names.get("I", ""))
        cur_d  = self._get_param(param_names.get("D", ""))
        cur_ff = self._get_param(param_names.get("FF", ""))
        cur_fltd = self._get_param(param_names.get("FLTD", ""))
        cur_gyro_flt = self._get_param("INS_GYRO_FILTER")

        # ------------------------------------------------------------------
        # Autotune result quality (METHODIC)
        # ------------------------------------------------------------------
        ang_p_param = {
            "Roll":  "ANG_RLL_P",
            "Pitch": "ANG_PIT_P",
            "Yaw":   "ANG_YAW_P",
        }.get(axis_name)
        # The angle-P autotune check belongs to the multicopter attitude
        # controller — meaningless for a fixed-wing axis.
        if ang_p_param and phase == "plane":
            ang_p_param = None
        if ang_p_param:
            ang_p_name = (f"Q_A_{ang_p_param}" if phase == "vtol"
                          else f"ATC_{ang_p_param}")
            ang_p = self._get_param(ang_p_name)
            if ang_p is None and phase == "vtol":
                ang_p = self._get_param(f"ATC_{ang_p_param}")
                if ang_p is not None:
                    ang_p_name = f"ATC_{ang_p_param}"
            if ang_p is not None:
                p_min = self.thresholds.autotune_atc_p_min_acceptable
                p_max = self.thresholds.autotune_atc_p_excellent
                if ang_p < p_min:
                    details.append(
                        t("pid_tuning.ang_p_low",
                          param=ang_p_name, val=f"{ang_p:.2f}",
                          pmin=p_min)
                    )
                    suggestions.append(
                        t("pid_tuning.ang_p_low_suggest",
                          param=ang_p_name, val=f"{ang_p:.2f}",
                          pmax=f"{p_max:.0f}")
                    )
                else:
                    details.append(
                        t("pid_tuning.ang_p_ok",
                          param=ang_p_name, val=f"{ang_p:.2f}",
                          pmin=p_min, pmax=f"{p_max:.0f}")
                    )

        # ------------------------------------------------------------------
        # 1. Tracking quality: RMS error / RMS target
        # ------------------------------------------------------------------
        tracking_err = tar_m - act_m
        rms_err = float(np.sqrt(np.mean(tracking_err ** 2)))
        rms_tar = float(np.sqrt(np.mean(tar_m ** 2)))

        if rms_tar > 0.01:
            tracking_ratio = rms_err / rms_tar
        else:
            tracking_ratio = 0.0

        details.append(
            t("pid_tuning.tracking_err_rms",
              rms=f"{rms_err:.3f}", tar=f"{rms_tar:.3f}",
              ratio=f"{tracking_ratio:.2%}")
        )

        # Dual gate. The absolute floors are in deg/s, so compare in deg/s.
        # rms_err is in the controller's native units: rad/s on the copter
        # and VTOL rate loops, but deg/s on the fixed-wing ones, which log
        # the desired rate and degrees(rate) (AP_FW_Controller.cpp).
        rms_err_degs = (rms_err if phase == "plane"
                        else rms_err * (180.0 / np.pi))
        ratio_crit = self.thresholds.pid_tracking_crit
        ratio_warn = self.thresholds.pid_tracking_warn
        abs_crit = self.thresholds.pid_tracking_abs_crit
        abs_warn = self.thresholds.pid_tracking_abs_warn
        if tracking_ratio >= ratio_crit and rms_err_degs >= abs_crit:
            status = self.worst_status(status, "critical")
            details.append(
                t("pid_tuning.tracking_poor",
                  ratio=f"{tracking_ratio:.1%}",
                  rms=f"{rms_err_degs:.1f}")
            )
        elif tracking_ratio >= ratio_warn and rms_err_degs >= abs_warn:
            status = self.worst_status(status, "warning")
            details.append(
                t("pid_tuning.tracking_weak",
                  ratio=f"{tracking_ratio:.1%}",
                  rms=f"{rms_err_degs:.1f}")
            )

        # ------------------------------------------------------------------
        # 2. Component balance
        # ------------------------------------------------------------------
        mean_abs_p = float(np.mean(np.abs(p_m)))
        mean_abs_i = float(np.mean(np.abs(i_m)))
        mean_abs_d = float(np.mean(np.abs(d_m)))
        mean_abs_ff = float(np.mean(np.abs(ff_m)))
        total_output = mean_abs_p + mean_abs_i + mean_abs_d + mean_abs_ff

        if total_output > 0.001:
            p_pct = mean_abs_p / total_output
            i_pct = mean_abs_i / total_output
            d_pct = mean_abs_d / total_output
            ff_pct = mean_abs_ff / total_output
        else:
            p_pct = i_pct = d_pct = ff_pct = 0.0

        details.append(
            t("pid_tuning.component_ratio",
              p=f"{p_pct:.0%}", i=f"{i_pct:.0%}",
              d=f"{d_pct:.0%}", ff=f"{ff_pct:.0%}")
        )

        # ------------------------------------------------------------------
        # 3. D-term noise
        # ------------------------------------------------------------------
        total_out = p_m + i_m + d_m + ff_m
        d_std = float(np.std(d_m))
        total_std = float(np.std(total_out))

        if total_std > 0.001:
            d_noise_ratio = d_std / total_std
        else:
            d_noise_ratio = 0.0

        details.append(t("pid_tuning.d_noise_ratio",
                          val=f"{d_noise_ratio:.0%}"))

        if d_noise_ratio >= self.thresholds.pid_dterm_noise_crit:
            status = self.worst_status(status, "critical")
            details.append(
                t("pid_tuning.d_noise_crit",
                  val=f"{d_noise_ratio:.0%}",
                  thresh=f"{self.thresholds.pid_dterm_noise_crit:.0%}")
            )
        elif d_noise_ratio >= self.thresholds.pid_dterm_noise_warn:
            status = self.worst_status(status, "warning")
            details.append(
                t("pid_tuning.d_noise_warn",
                  val=f"{d_noise_ratio:.0%}")
            )

        # ------------------------------------------------------------------
        # 4. I-term accumulation / windup
        # ------------------------------------------------------------------
        if total_output > 0.001:
            i_ratio = mean_abs_i / total_output
            if i_ratio >= self.thresholds.pid_iterm_ratio_warn:
                status = self.worst_status(status, "warning")
                details.append(
                    t("pid_tuning.i_ratio_high",
                      ratio=f"{i_ratio:.0%}")
                )

        if len(i_m) > 100:
            i_diff = np.diff(i_m)
            # Each direction against its own share: an I-term that holds
            # still is not rising, which does not make it falling.
            positive_frac = float(np.mean(i_diff > 0))
            negative_frac = float(np.mean(i_diff < 0))
            mono_thr = self.thresholds.pid_iterm_monotonic_warn
            if positive_frac > mono_thr or negative_frac > mono_thr:
                status = self.worst_status(status, "warning")
                direction = (t("pid_tuning.i_growing")
                             if positive_frac > mono_thr
                             else t("pid_tuning.i_falling"))
                details.append(
                    t("pid_tuning.i_monotonic",
                      direction=direction,
                      pct=f"{max(positive_frac, negative_frac):.0%}")
                )

        # ------------------------------------------------------------------
        # 5. Oscillation detection
        # ------------------------------------------------------------------
        osc_crossings_per_sec = 0.0
        if len(tracking_err) > 20 and len(t_m) > 1:
            sign_changes = np.sum(np.diff(np.sign(tracking_err)) != 0)
            # Time the samples cover, not first to last: VTOL rows of a
            # transition flight sit either side of minutes of fixed-wing
            # cruise, and counting that dilutes the rate tenfold.
            steps = np.diff(t_m)
            duration = float(np.sum(steps[steps < 1.0]))
            if duration > 0:
                osc_crossings_per_sec = sign_changes / duration

        # ------------------------------------------------------------------
        # 6. Tuning suggestions
        # ------------------------------------------------------------------
        p_name  = param_names.get("P", "")
        i_name  = param_names.get("I", "")
        d_name  = param_names.get("D", "")
        ff_name = param_names.get("FF", "")
        fltd_name = param_names.get("FLTD", "")

        # 6a. Poor tracking. RMS error over RMS target says how far the axis
        # is from its target, not which way it misses: an over-driven axis,
        # one that barely moves, and a well-tuned hover where target and
        # response are both noise all land near 1. With no direction in the
        # metric there is no gain change to recommend, only a pointer.
        tracking_poor = (tracking_ratio >= ratio_warn
                         and rms_err_degs >= abs_warn)
        if tracking_poor:
            suggestions.append(
                t("pid_tuning.suggest_tracking_poor", param=p_name))
            if osc_crossings_per_sec > self.thresholds.pid_osc_crossings_warn:
                suggestions.append(
                    t("pid_tuning.suggest_tracking_osc",
                      zc=f"{osc_crossings_per_sec:.1f}"))
            if (phase == "plane" and cur_ff is not None
                    and cur_ff < 0.05 and ff_name):
                suggestions.append(
                    t("pid_tuning.suggest_ff_low",
                      param=ff_name, cur=f"{cur_ff:.4f}")
                )

        # 6a-ext. FF dominance warning
        if (tracking_ratio < self.thresholds.pid_tracking_warn
                and total_output > 0.001 and ff_pct >= 0.6):
            suggestions.append(
                t("pid_tuning.suggest_ff_dominant",
                  param=ff_name, pct=f"{ff_pct:.0%}")
            )

        # 6b. D-term noise. The ratio is a share of the output, so it stays
        # high at any gain: applied over and over in SITL this took D down 73%
        # before stopping, and 278 of the 296 axes it flagged in the corpora
        # were under METHODIC's rate output limit. The wiki lowers D to clear
        # an oscillation, so moving a gain takes one, by the same test that
        # grades the axis: on the ground its peak counts too, as a copter that
        # shakes and never lifts off spends most of the log still.
        oscillating = rate_out_rms is not None and (
            rate_out_rms >= self.thresholds.pid_rate_out_warn
            or (ground_vib_mode and rate_out_peak
                >= self.thresholds.pid_ground_vib_peak))
        if d_noise_ratio >= self.thresholds.pid_dterm_noise_warn and oscillating:
            if d_noise_ratio >= self.thresholds.pid_dterm_noise_crit:
                reduce_pct = 0.80
                label = t("pid_tuning.noise_excessive")
            else:
                reduce_pct = 0.90
                label = t("pid_tuning.noise_elevated")

            if cur_d is not None and d_name:
                new_d = round(cur_d * reduce_pct, 6)
                suggestions.append(
                    t("pid_tuning.suggest_d_reduce",
                      param=d_name, cur=f"{cur_d:.4f}",
                      label=label, new=f"{new_d:.4f}",
                      pct=f"{round((1 - reduce_pct) * 100)}")
                )
                param_suggestions.append({
                    "param": d_name,
                    "current": cur_d,
                    "suggested": new_d,
                    "reason": t("pid_tuning.reason_d_noise"),
                })
            elif d_name:
                suggestions.append(
                    t("pid_tuning.suggest_d_reduce_generic",
                      label=label, param=d_name)
                )

            if cur_fltd is not None and fltd_name:
                if cur_fltd > 20:
                    suggestions.append(
                        t("pid_tuning.suggest_fltd_reduce",
                          param=fltd_name, cur=f"{cur_fltd:.1f}")
                    )
                elif cur_fltd == 0:
                    suggestions.append(
                        t("pid_tuning.suggest_fltd_enable",
                          param=fltd_name)
                    )
            elif fltd_name and cur_fltd is None:
                suggestions.append(
                    t("pid_tuning.suggest_fltd_missing",
                      param=fltd_name)
                )

            if cur_gyro_flt is not None and cur_gyro_flt < 20:
                suggestions.append(
                    t("pid_tuning.suggest_gyro_flt_low",
                      cur=f"{cur_gyro_flt:.0f}")
                )
            elif cur_gyro_flt is not None and cur_gyro_flt > 80:
                suggestions.append(
                    t("pid_tuning.suggest_gyro_flt_high",
                      cur=f"{cur_gyro_flt:.0f}")
                )

        # 6c. I-term issues
        i_high_ratio = (total_output > 0.001
                        and mean_abs_i / total_output
                        >= self.thresholds.pid_iterm_ratio_warn)
        i_windup = False
        if len(i_m) > 100:
            i_diff = np.diff(i_m)
            # Windup is the I-term monotonically GROWING. A constant/zero
            # I-term gives all-zero diffs -> positive_frac 0, which the old
            # `< (1-mono_thr)` branch misread as windup and emitted a no-op
            # cur*0.7 suggestion (e.g. ATC_RAT_*_I 0.0 -> 0.0). Require real
            # growth; a falling I-term is not windup.
            if np.any(np.abs(i_diff) > 1e-9):
                positive_frac = float(np.mean(i_diff > 0))
                mono_thr = self.thresholds.pid_iterm_monotonic_warn
                i_windup = positive_frac > mono_thr

        if i_windup and i_name:
            # A growing I term is chasing an offset that keeps building, and
            # a lower gain only chases it worse: in SITL, ATC_RAT_YAW_I x 0.7
            # alone raised yaw error 22%. So this is a pointer too.
            suggestions.append(t("pid_tuning.suggest_i_windup", param=i_name))
        elif i_high_ratio and i_name:
            # A large share means P and D have little left to do while the
            # I term holds a steady offset, which is what it is for. Lowering
            # it takes that away: in SITL with one weak motor, roll attitude
            # error rose 31%. So this is a pointer, not a gain change.
            suggestions.append(t("pid_tuning.suggest_i_high", param=i_name))

        # 6e. Good tracking but gyro filter may need tuning
        if (status == "good" and cur_gyro_flt is not None
                and cur_gyro_flt < 20):
            suggestions.append(
                t("pid_tuning.suggest_gyro_conservative",
                  cur=f"{cur_gyro_flt:.0f}")
            )

        # 6f. No issues -- show current values
        if not suggestions and any(v is not None for v in [cur_p, cur_i, cur_d]):
            param_info = []
            if cur_p is not None:
                param_info.append(f"{p_name}={cur_p:.4f}")
            if cur_i is not None:
                param_info.append(f"{i_name}={cur_i:.4f}")
            if cur_d is not None:
                param_info.append(f"{d_name}={cur_d:.4f}")
            if cur_ff is not None and ff_name:
                param_info.append(f"{ff_name}={cur_ff:.4f}")
            suggestions.append(
                t("pid_tuning.current_params",
                  params=", ".join(param_info)))

        # Append suggestions block to details
        if suggestions:
            details.append(t("pid_tuning.suggest_header"))
            for s in suggestions:
                details.append(f"  • {s}")

        # ------------------------------------------------------------------
        # 7. Figures
        # ------------------------------------------------------------------

        # 7a. Tracking: Tar vs Act
        fig_track = self.make_figure(
            title=t("pid_tuning.fig_track_title", axis=axis_name),
            xaxis_title=t("pid_tuning.time_s"),
            yaxis_title=t("pid_tuning.fig_track_yaxis"),
            height=350,
        )
        fig_track.add_trace(go.Scattergl(
            x=ts, y=tar, mode='lines',
            name=t("pid_tuning.trace_target"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_track.add_trace(go.Scattergl(
            x=ts, y=act, mode='lines',
            name=t("pid_tuning.trace_actual"),
            line=dict(color='#d62728', width=1),
        ))
        figures.append(fig_track)

        # 7b. Component balance bar chart
        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(
            x=['P', 'I', 'D', 'FF'],
            y=[mean_abs_p, mean_abs_i, mean_abs_d, mean_abs_ff],
            marker_color=['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728'],
        ))
        fig_bar.update_layout(
            title=dict(
                text=t("pid_tuning.fig_bar_title", axis=axis_name),
                font=dict(size=14),
            ),
            yaxis_title=t("pid_tuning.fig_bar_yaxis"),
            height=280,
            margin=dict(l=60, r=30, t=50, b=50),
            template="plotly_white",
            font=dict(
                family="PingFang SC, Microsoft YaHei, Noto Sans SC, sans-serif",
                size=12,
            ),
        )
        figures.append(fig_bar)

        # 7c. Tracking error distribution
        fig_hist = self.make_figure(
            title=t("pid_tuning.fig_hist_title", axis=axis_name),
            xaxis_title=t("pid_tuning.fig_hist_xaxis"),
            yaxis_title=t("pid_tuning.fig_hist_yaxis"),
            height=280,
        )
        fig_hist.add_trace(go.Histogram(
            x=tracking_err,
            nbinsx=60,
            marker_color='#1f77b4',
            opacity=0.7,
        ))
        figures.append(fig_hist)

        # AUTHORITATIVE FINAL DECISION
        if rate_out_rms is not None:
            peak_severe = (
                ground_vib_mode
                and rate_out_peak is not None
                and rate_out_peak >= self.thresholds.pid_ground_vib_peak_crit
            )
            peak_warn = (
                ground_vib_mode
                and rate_out_peak is not None
                and rate_out_peak >= self.thresholds.pid_ground_vib_peak
            )
            if rate_out_rms >= self.thresholds.pid_rate_out_crit or peak_severe:
                status = "critical"
            elif rate_out_rms >= self.thresholds.pid_rate_out_warn or peak_warn:
                status = "warning"
            else:
                status = "good"

        # Build summary for this axis
        if rate_out_rms is not None:
            out_field_name = rate_axis_fields[0] if rate_axis_fields else "out"
            authoritative = f"RATE.{out_field_name} RMS={rate_out_rms:.3f}"
            prefix = t("pid_tuning.ground_osc_prefix") if ground_vib_mode else ""
            if status == "good":
                summary = t("pid_tuning.axis_good",
                            prefix=prefix, auth=authoritative)
            elif status == "warning":
                summary = t("pid_tuning.axis_warn",
                            prefix=prefix, auth=authoritative)
            else:
                summary = t("pid_tuning.axis_crit",
                            prefix=prefix, auth=authoritative)
        else:
            if status == "good":
                summary = t("pid_tuning.axis_good_legacy",
                            ratio=f"{tracking_ratio:.1%}")
            elif status == "warning":
                summary = t("pid_tuning.axis_warn_legacy",
                            ratio=f"{tracking_ratio:.1%}")
            else:
                summary = t("pid_tuning.axis_crit_legacy",
                            ratio=f"{tracking_ratio:.1%}")

        # Enforce invariant: apply_param ↔ non-empty param_suggestions.
        # status and param_suggestions are driven by independent signals
        # (rate_out_rms vs tracking_ratio), so good+suggestions is reachable
        # — e.g. high tracking error with small rate command. param_suggestions
        # takes precedence so the invariant always holds.
        if param_suggestions:
            sub_action_level = "apply_param"
        elif tracking_poor:
            sub_action_level = "run_autotune"
        elif status == "good":
            sub_action_level = "ok"
        else:
            sub_action_level = "physical_check"

        return self.make_result(
            title=axis_title,
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=sub_action_level,
            evidence_source=(t("pid_tuning.evidence")
                             if status in ("warning", "critical") else ""),
            param_suggestions=param_suggestions,
        )

    # ------------------------------------------------------------------
    # Position controller velocity tracking (PSC*, info-only)
    # ------------------------------------------------------------------

    _PSC_AXES = [("PSCN", "N"), ("PSCE", "E"), ("PSCD", "D")]

    def _analyze_psc(self) -> AnalysisResult | None:
        """Report PSC target-vs-actual velocity tracking as metrics.

        No authoritative threshold exists for position-loop velocity
        tracking, so per POSITIONING this carries no verdict — it gives
        the tuning engineer the numbers (RMS / peak error per axis).
        """
        parts = []
        details = []
        for msg, axis in self._PSC_AXES:
            if not self.reader.has_message(msg):
                continue
            data = self.reader.get_messages_as_arrays(
                msg, [f"TV{axis}", f"V{axis}"])
            ts = data["_timestamp"]
            if len(ts) < 10:
                continue
            tv = data[f"TV{axis}"]
            v = data[f"V{axis}"]
            sel = (self.get_armed_mask(ts)
                   & np.isfinite(tv) & np.isfinite(v))
            if int(np.sum(sel)) < 10:
                continue
            err = tv[sel] - v[sel]
            rms = float(np.sqrt(np.mean(err ** 2)))
            peak = float(np.max(np.abs(err)))
            parts.append(f"{axis}={rms:.2f}")
            details.append(t("pid_tuning.psc_axis", axis=axis,
                             rms=f"{rms:.2f}", peak=f"{peak:.2f}"))
        if not details:
            return None
        return self.make_result(
            title=t("pid_tuning.psc_title"),
            status="info",
            summary=t("pid_tuning.psc_summary", vals=", ".join(parts)),
            details=details,
        )
