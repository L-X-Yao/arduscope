"""
Failsafe Analyzer: safety margin assessment.

Evaluates how close each monitored metric came to its failsafe threshold,
using shaded margin areas (green -> yellow -> red gradient). Detects
triggered failsafes from ERR messages.

Required: (uses pre-scan errors/events from reader)
Optional: BAT, GPS, XKF4, NKF4

Title: 安全裕度
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.ekf_lanes import primary_lane_rows
from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


# ArduPilot LogErrorSubsystem values whose name carries the FAILSAFE_
# prefix — i.e. an actual failsafe fired. Values per AP_Logger/AP_Logger.h.
# EKFCHECK(16) and BARO(18) were previously listed but are *detection*
# subsystems, not failsafes: a baro glitch or EKF variance flag is not
# itself a failsafe (the EKF failsafe is subsys 17). ADSB(20),
# TERRAIN(23), SENSORS(26), LEAK(27), VIBE(29) and DEADRECKON(31) are
# real failsafes that were missing entirely.
_FS_SUBSYS_KEYS = {
    5: "failsafe.subsys_radio",
    6: "failsafe.subsys_battery",
    7: "failsafe.subsys_gps",
    8: "failsafe.subsys_gcs",
    9: "failsafe.subsys_fence",
    17: "failsafe.subsys_ekf",
    20: "failsafe.subsys_adsb",
    23: "failsafe.subsys_terrain",
    26: "failsafe.subsys_sensors",
    27: "failsafe.subsys_leak",
    29: "failsafe.subsys_vibe",
    31: "failsafe.subsys_deadreckon",
}


class FailsafeAnalyzer(BaseAnalyzer):
    """
    Safety margin analyzer.

    Generates:
      - Combined margin plot with shaded green/yellow/red areas
      - Battery voltage margin (if BAT available)
      - GPS NSats margin (if GPS available)
      - EKF variance margin (if XKF4/NKF4 available)
    """

    registry_key = "failsafe"
    sort_order = 90
    # Always can run -- uses pre-scan data at minimum
    required_messages: list[str] = []  # custom can_run
    optional_messages = ["BAT", "GPS", "XKF4", "NKF4"]

    def can_run(self) -> bool:
        """Checks whatever failsafe evidence the log holds, so any flight log will do."""
        return True

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        margin_statuses = []

        # -----------------------------------------------------------
        # 1. Check triggered failsafes from ERR messages
        # -----------------------------------------------------------
        triggered = []
        for err in self.reader.errors:
            # ECode 0 == ERROR_RESOLVED (the failsafe cleared). Counting
            # it as a trigger double-counts every transient failsafe and
            # turns a log that only holds recovery records into a false
            # critical. Only the occurrence (ECode != 0) is a trigger.
            if err.ecode != 0 and err.subsys in _FS_SUBSYS_KEYS:
                name = t(_FS_SUBSYS_KEYS[err.subsys])
                triggered.append(
                    t("failsafe.triggered_event",
                      name=name, time=f"{err.timestamp:.1f}",
                      code=err.ecode)
                )

        if triggered:
            details.append(t("failsafe.triggered_count", count=len(triggered)))
            for t_str in triggered:
                details.append(f"  - {t_str}")
            margin_statuses.append("critical")
        else:
            details.append(t("failsafe.none_triggered"))

        # -----------------------------------------------------------
        # 2. Battery voltage margin
        # -----------------------------------------------------------
        if self.reader.has_message("BAT"):
            bat_fig, bat_status = self._battery_margin(details)
            if bat_fig is not None:
                figures.append(bat_fig)
            margin_statuses.append(bat_status)

        # -----------------------------------------------------------
        # 3. GPS NSats margin
        # -----------------------------------------------------------
        if self.reader.has_message("GPS"):
            gps_fig, gps_status = self._gps_margin(details)
            if gps_fig is not None:
                figures.append(gps_fig)
            margin_statuses.append(gps_status)

        # -----------------------------------------------------------
        # 4. EKF variance margin
        # -----------------------------------------------------------
        kf4_type = None
        if self.reader.has_message("XKF4"):
            kf4_type = "XKF4"
        elif self.reader.has_message("NKF4"):
            kf4_type = "NKF4"

        if kf4_type:
            ekf_fig, ekf_status = self._ekf_margin(kf4_type, details)
            if ekf_fig is not None:
                figures.append(ekf_fig)
            margin_statuses.append(ekf_status)

        # -----------------------------------------------------------
        # 5. Overall status
        # -----------------------------------------------------------
        if not margin_statuses:
            margin_statuses = ["info"]

        overall = "good"
        for s in margin_statuses:
            overall = self.worst_status(overall, s)

        # Evidence source per severity:
        # - critical: authoritative — actual failsafe fired OR a metric
        #   crossed the exact ArduPilot threshold during armed flight.
        # - warning: NSats dropped below pre-arm minimum (AHRS_GPS_MINSATS)
        #   during armed flight — pre-arm would have refused but we're
        #   already flying, so it's a navigation-degraded state.
        # All "margin < 20%" style speculative warnings have been removed
        # per POSITIONING.md — they were E-class judgment, not authority.
        # Engineer can still see the exact min_margin in details and
        # decide for themselves.
        if overall == "critical":
            evidence_source = t("failsafe.evidence_critical")
        elif overall == "warning":
            evidence_source = t("failsafe.evidence_warning")
        else:
            evidence_source = ""

        fs_ekf = self.reader.get_param("FS_EKF_THRESH", 0.8)

        suggestions: list[dict] = []
        if overall == "critical":
            summary = t("failsafe.summary_critical")
            plain_summary = t("failsafe.plain_critical")
            # No parameter suggestion here. The old code recommended
            # BATT_LOW_VOLT = 0.92 x observed peak voltage, which is both
            # unsourced and dangerous: on a full 4S pack that resolves to
            # 3.86 V/cell — well above the ~3.5 V/cell guidance — and would
            # itself provoke a false low-battery failsafe. It also fired on
            # *any* critical (EKF, GPS, ...), asserting a battery problem
            # unrelated to the actual trigger. A fired failsafe means "find
            # out why", not "raise the threshold"; battery-threshold advice
            # belongs to the battery analyzer, which has cell-count context.
            action_text = t(
                "failsafe.action_critical",
                fs_ekf=f"{fs_ekf:.2g}",
            )
            action_level = "physical_check"
        elif overall == "warning":
            summary = t("failsafe.summary_warning")
            plain_summary = t("failsafe.plain_warning")
            action_text = t("failsafe.action_warning")
            action_level = "physical_check"
        else:
            summary = t("failsafe.summary_good")
            plain_summary = t("failsafe.plain_good")
            action_text = ""
            action_level = "ok"

        return self.make_result(
            title=t("analyzer.failsafe"),
            status=overall,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=suggestions,
        )

    # ------------------------------------------------------------------
    # Internal: margin computation helpers
    # ------------------------------------------------------------------

    def _battery_margin(
        self, details: list[str]
    ) -> tuple[go.Figure | None, str]:
        """Compute battery voltage margin to failsafe threshold."""
        bat = self.reader.get_messages_as_arrays("BAT", ["Inst", "Volt"])
        ts = bat["_timestamp"]
        if len(ts) == 0:
            return None, "good"

        inst = bat["Inst"]
        volt = bat["Volt"]

        # Use instance 0
        mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
        if not np.any(mask):
            mask = np.ones(len(ts), dtype=bool)

        t_v = ts[mask]
        v = volt[mask]

        # Filter to armed time only — pre-arm voltage (often 0V before
        # the BEC powers on) made min_margin go negative even on
        # perfectly fine flights. Same lesson as battery analyzer.
        armed = self.reader.get_armed_intervals()
        v_for_margin = v
        have_armed = False
        if armed:
            armed_mask = np.zeros(len(t_v), dtype=bool)
            for s, e in armed:
                armed_mask |= (t_v >= s) & (t_v <= e)
            if np.sum(armed_mask) > 10:
                v_for_margin = v[armed_mask]
                have_armed = True

        low_volt = self.thresholds.batt_low_volt
        if low_volt <= 0:
            low_volt = self.reader.get_param("BATT_LOW_VOLT", 0)
        if low_volt <= 0:
            details.append(t("failsafe.batt_low_volt_not_set"))
            return None, "good"

        margin = v_for_margin - low_volt
        min_margin = float(np.nanmin(margin))

        details.append(
            t("failsafe.batt_margin",
              min_margin=f"{min_margin:.2f}",
              low_volt=f"{low_volt:.1f}")
        )

        # Diagnostic semantics per POSITIONING.md:
        #   - critical: voltage crossed BATT_LOW_VOLT during armed flight
        #     (authoritative: ArduPilot triggers低电告警 at this exact threshold)
        #   - good: voltage stayed above the threshold
        # The "margin < 20% of threshold" check was removed — it was pure
        # engineering judgment (E-class), not authoritative. If the voltage
        # dropped close to threshold but didn't cross, the engineer can
        # see the exact min_margin in details and judge themselves.
        # Only a breach inside a known armed window is authoritative. With
        # no armed window, pre-arm 0V / boot / power-down samples push the
        # raw minimum below the threshold — we cannot claim a breach
        # "during armed flight", so do not escalate (min_margin is in the
        # details for the engineer).
        status = "good"
        if min_margin < 0 and have_armed:
            status = "critical"
        elif min_margin < 0:
            details.append(t("failsafe.margin_unarmed"))

        # Figure with shaded margin
        fig = self.make_figure(
            title=t("failsafe.fig_batt_margin"),
            xaxis_title=t("failsafe.time_s"),
            yaxis_title=t("failsafe.voltage_v"),
            height=350,
        )

        fig.add_trace(go.Scattergl(
            x=t_v, y=v,
            mode='lines', name=t("failsafe.voltage"),
            line=dict(color='#1f77b4', width=1.5),
        ))

        # Shaded area: voltage to threshold
        fig.add_trace(go.Scatter(
            x=np.concatenate([t_v, t_v[::-1]]),
            y=np.concatenate([v, np.full(len(t_v), low_volt)[::-1]]),
            fill='toself',
            fillcolor='rgba(92, 184, 92, 0.2)',
            line=dict(width=0),
            name=t("failsafe.margin_zone"),
            showlegend=True,
            hoverinfo='skip',
        ))

        self.add_threshold_hline(
            fig, low_volt,
            t("failsafe.batt_low_line", val=f"{low_volt:.1f}"),
            color="#d9534f",
        )

        return fig, status

    def _gps_margin(
        self, details: list[str]
    ) -> tuple[go.Figure | None, str]:
        """Compute GPS NSats margin to warning threshold."""
        gps = self.reader.get_messages_as_arrays("GPS", ["I", "NSats"])
        ts = gps["_timestamp"]
        if len(ts) == 0:
            return None, "good"

        inst = gps["I"]
        nsats = gps["NSats"]

        # Use instance 0
        mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
        if not np.any(mask):
            mask = np.ones(len(ts), dtype=bool)

        t_g = ts[mask]
        ns = nsats[mask]

        warn_nsats = self.thresholds.gps_nsats_warn
        # Filter to armed time only (pre-arm GPS acquiring doesn't count)
        armed = self.reader.get_armed_intervals()
        ns_for_margin = ns
        have_armed = False
        if armed:
            armed_mask = np.zeros(len(t_g), dtype=bool)
            for s, e in armed:
                armed_mask |= (t_g >= s) & (t_g <= e)
            if np.sum(armed_mask) > 10:
                ns_for_margin = ns[armed_mask]
                have_armed = True

        margin = ns_for_margin - warn_nsats
        min_margin = float(np.nanmin(margin)) if len(margin) > 0 else 0.0

        details.append(
            t("failsafe.gps_margin",
              min_margin=f"{min_margin:.0f}",
              warn_nsats=warn_nsats)
        )

        # Diagnostic semantics:
        #   - warning: NSats dropped below AHRS_GPS_MINSATS during armed
        #     flight (pre-arm would have refused; navigation degraded)
        #   - good: stayed above threshold
        # Without a known armed window, a cold-start NSats=0 would falsely
        # trip this, so only escalate inside an armed window.
        status = "good"
        if min_margin < 0 and have_armed:
            status = "warning"
        elif min_margin < 0:
            details.append(t("failsafe.margin_unarmed"))

        fig = self.make_figure(
            title=t("failsafe.fig_gps_margin"),
            xaxis_title=t("failsafe.time_s"),
            yaxis_title=t("failsafe.nsats"),
            height=300,
        )

        fig.add_trace(go.Scattergl(
            x=t_g, y=ns,
            mode='lines', name='NSats',
            line=dict(color='#2ca02c', width=1.5),
        ))

        self.add_threshold_hline(
            fig, warn_nsats,
            t("failsafe.warn_threshold", val=warn_nsats),
            color="#f0ad4e",
        )

        return fig, status

    def _ekf_margin(
        self, kf4_type: str, details: list[str]
    ) -> tuple[go.Figure | None, str]:
        """Compute EKF variance margin to FS_EKF_THRESH."""
        kf4 = self.reader.get_messages_as_arrays(kf4_type, [
            "C", "SV", "SP", "SH", "SM", "PI",
        ])
        ts = kf4["_timestamp"]
        if len(ts) == 0:
            return None, "good"

        # AP evaluates the EKF failsafe on the primary lane only. XKF4/
        # NKF4 log one row per core (C column); a variance spike on a
        # non-primary lane must not read as a breach of FS_EKF_THRESH.
        core = kf4.get("C")
        if core is not None and not np.all(np.isnan(core)):
            core_mask = primary_lane_rows(core, kf4["PI"])
            ts = ts[core_mask]
            for _k in ("SV", "SP", "SH", "SM"):
                kf4[_k] = kf4[_k][core_mask]

        ekf_thresh = self.thresholds.ekf_variance_crit

        # Combined max variance at each timestamp
        sv = kf4["SV"]
        sp = kf4["SP"]
        sh = kf4["SH"]
        sm = kf4["SM"]
        max_var = np.maximum(np.maximum(sv, sp), np.maximum(sh, sm))

        # Filter to armed flight only (pre-arm EKF initialization noise
        # would otherwise make every log look bad).
        armed = self.reader.get_armed_intervals()
        max_var_for_margin = max_var
        have_armed = False
        if armed:
            armed_mask = np.zeros(len(ts), dtype=bool)
            for s, e in armed:
                armed_mask |= (ts >= s) & (ts <= e)
            if np.sum(armed_mask) > 10:
                max_var_for_margin = max_var[armed_mask]
                have_armed = True

        margin = ekf_thresh - max_var_for_margin
        if np.all(np.isnan(margin)):
            details.append(t("failsafe.ekf_all_nan"))
            return None, "info"

        min_margin = float(np.nanmin(margin))

        details.append(
            t("failsafe.ekf_margin",
              min_margin=f"{min_margin:.3f}",
              ekf_thresh=f"{ekf_thresh:.2f}")
        )

        # Diagnostic semantics:
        #   - critical: variance crossed FS_EKF_THRESH during armed flight
        #     (authoritative: ArduPilot triggers EKF failsafe at this
        #     exact threshold)
        #   - good: stayed below threshold
        # Without a known armed window, pre-arm EKF convergence spikes
        # would falsely trip this, so only escalate inside an armed window.
        status = "good"
        if min_margin < 0 and have_armed:
            status = "critical"
        elif min_margin < 0:
            details.append(t("failsafe.margin_unarmed"))

        fig = self.make_figure(
            title=t("failsafe.fig_ekf_margin"),
            xaxis_title=t("failsafe.time_s"),
            yaxis_title=t("failsafe.variance"),
            height=350,
        )

        fig.add_trace(go.Scattergl(
            x=ts, y=max_var,
            mode='lines', name=t("failsafe.max_variance"),
            line=dict(color='#9467bd', width=1.5),
        ))

        # Shaded area: threshold to max_var
        fig.add_trace(go.Scatter(
            x=np.concatenate([ts, ts[::-1]]),
            y=np.concatenate([
                np.full(len(ts), ekf_thresh),
                max_var[::-1],
            ]),
            fill='toself',
            fillcolor='rgba(92, 184, 92, 0.2)',
            line=dict(width=0),
            name=t("failsafe.margin_zone"),
            showlegend=True,
            hoverinfo='skip',
        ))

        self.add_threshold_hline(
            fig, ekf_thresh,
            t("failsafe.ekf_thresh_line", val=f"{ekf_thresh:.2f}"),
            color="#d9534f",
        )

        return fig, status
