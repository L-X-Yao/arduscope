"""
Board Power Analyzer: autopilot 5V rail health and brownout forensics.

Reads POWR (board Vcc / servo rail / MAV_POWER_STATUS flags) and the
optional MCU message (die temperature, MCU rail envelope). This is the
supply side of the power domain — the battery side lives in power.py.

Vcc instability is the classic brownout precursor: the wiki's log
diagnostics page calls anything beyond 0.10-0.15 V of variation a sign
of impending brown-out, and the powering page bounds normal operation
at 4.1-5.7 V. Flag transitions add hard events: losing BRICK_VALID
mid-flight means the primary supply dropped and the board survived on
a backup path; PERIPH_OVERCURRENT means the hardware itself reported
an overcurrent on a peripheral rail.

Required: POWR
Optional: MCU

Title: 板级供电
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer

# MAV_POWER_STATUS bits (mavlink common.xml; set in
# AP_HAL_ChibiOS/AnalogIn.cpp)
_FLAG_BRICK_VALID = 1
_FLAG_SERVO_VALID = 2
_FLAG_USB_CONNECTED = 4
_FLAG_PERIPH_OVERCURRENT = 8
_FLAG_PERIPH_HIPOWER_OVERCURRENT = 16


class BoardPowerAnalyzer(BaseAnalyzer):
    """
    Board supply rail analyzer.

    Generates:
      - Vcc time series with the 4.1/5.7 V operating-range lines
      - Vcc sag check (>0.15 V variation while armed)
      - MAV_POWER_STATUS flag events (brick loss, peripheral overcurrent)
      - MCU temperature / rail envelope (info)
    """

    registry_key = "board_power"
    sort_order = 66  # power domain: battery (60), power (65), board (66)
    required_messages = ["POWR"]
    optional_messages = ["MCU"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        powr = self.reader.get_messages_as_arrays(
            "POWR", ["Vcc", "VServo", "Flags", "AccFlags"])
        ts = powr["_timestamp"]
        vcc = powr["Vcc"]

        finite = np.isfinite(vcc) & (vcc > 0)
        if len(ts) == 0 or not np.any(finite):
            return self.make_result(
                title=t("analyzer.board_power"),
                status="info",
                summary=t("board_power.no_data"),
            )
        ts, vcc = ts[finite], vcc[finite]
        flags = np.nan_to_num(powr["Flags"][finite], nan=0.0).astype(np.int64)
        accflags = np.nan_to_num(
            powr["AccFlags"][finite], nan=0.0).astype(np.int64)

        # Median-of-3 smoothing: a single-sample ADC spike must not fake
        # a sag event, but a real dip (motor punch) spans many samples
        # at the ~10 Hz POWR rate and survives.
        if len(vcc) >= 3:
            sm_vcc = np.median(
                np.stack([vcc[:-2], vcc[1:-1], vcc[2:]]), axis=0)
            sm_ts = ts[1:-1]
            sm_flags = flags[1:-1]
        else:
            sm_vcc, sm_ts, sm_flags = vcc, ts, flags

        armed_intervals = self.reader.get_armed_intervals()
        armed_mask = self.get_armed_mask(sm_ts)
        have_armed = bool(armed_intervals) and int(np.sum(armed_mask)) >= 10

        # Ground-test logs get no supply verdict (info), but hardware
        # flag events (overcurrent) still escalate below.
        status = "good" if have_armed else "info"
        evidence_source = ""

        # -----------------------------------------------------------
        # 1. Vcc envelope (whole log, informational)
        # -----------------------------------------------------------
        details.append(t(
            "board_power.vcc_envelope",
            median=f"{float(np.median(sm_vcc)):.2f}",
            min=f"{float(np.min(sm_vcc)):.2f}",
            max=f"{float(np.max(sm_vcc)):.2f}",
        ))

        # -----------------------------------------------------------
        # 2. Sag / range checks — armed window only. On the bench the
        #    rail legitimately steps when USB or peripherals are
        #    plugged, so pre-arm variation must not escalate (same
        #    rule as the failsafe margins).
        # -----------------------------------------------------------
        sag = 0.0
        if have_armed:
            v_armed = sm_vcc[armed_mask]
            sag = float(np.max(v_armed) - np.min(v_armed))
            v_lo, v_hi = float(np.min(v_armed)), float(np.max(v_armed))

            if (v_lo < self.thresholds.board_vcc_low_crit
                    or v_hi > self.thresholds.board_vcc_high_crit):
                status = self.worst_status(status, "critical")
                details.append(t(
                    "board_power.vcc_range_crit",
                    min=f"{v_lo:.2f}", max=f"{v_hi:.2f}",
                    lo=f"{self.thresholds.board_vcc_low_crit:.1f}",
                    hi=f"{self.thresholds.board_vcc_high_crit:.1f}",
                ))
                evidence_source = t("board_power.evidence_range")

            if sag > self.thresholds.board_vcc_sag_warn:
                status = self.worst_status(status, "warning")
                details.append(t(
                    "board_power.vcc_sag_warn",
                    sag=f"{sag:.2f}",
                    threshold=f"{self.thresholds.board_vcc_sag_warn:.2f}",
                ))
                if not evidence_source:
                    evidence_source = t("board_power.evidence_sag")
            else:
                details.append(t("board_power.vcc_sag_ok",
                                 sag=f"{sag:.2f}"))
        else:
            details.append(t("board_power.unarmed_note"))

        # -----------------------------------------------------------
        # 3. MAV_POWER_STATUS flag events
        # -----------------------------------------------------------
        oc_bits = _FLAG_PERIPH_OVERCURRENT | _FLAG_PERIPH_HIPOWER_OVERCURRENT
        oc_seen = np.any((flags & oc_bits) != 0)
        # AccFlags accumulates every bit ever set — catches an
        # overcurrent blip between two POWR samples.
        oc_acc = np.any((accflags & oc_bits) != 0)
        if oc_seen or oc_acc:
            status = self.worst_status(status, "warning")
            oc_idx = np.flatnonzero((flags & oc_bits) != 0)
            first = float(ts[oc_idx[0]]) if len(oc_idx) else float(ts[0])
            details.append(t("board_power.overcurrent_warn",
                             time=f"{first:.1f}"))
            if not evidence_source:
                evidence_source = t("board_power.evidence_flags")

        # Brick loss: the primary supply was valid, then dropped while
        # armed. A log that never saw BRICK_VALID (bench/USB power) must
        # not fire.
        brick = (sm_flags & _FLAG_BRICK_VALID) != 0
        if have_armed and len(brick) > 1:
            lost = brick[:-1] & ~brick[1:] & armed_mask[1:]
            if np.any(lost):
                status = self.worst_status(status, "critical")
                t_lost = float(sm_ts[1:][lost][0])
                details.append(t("board_power.brick_lost_crit",
                                 time=f"{t_lost:.1f}"))
                evidence_source = t("board_power.evidence_flags")

        usb_armed = have_armed and np.any(
            ((sm_flags & _FLAG_USB_CONNECTED) != 0) & armed_mask)
        if usb_armed:
            details.append(t("board_power.usb_while_armed"))

        # -----------------------------------------------------------
        # 4. Servo rail (informational — only when actually powered)
        # -----------------------------------------------------------
        vservo = powr["VServo"][finite]
        vs_finite = vservo[np.isfinite(vservo)]
        servo_powered = len(vs_finite) > 0 and float(np.max(vs_finite)) > 1.0
        if servo_powered:
            details.append(t(
                "board_power.servo_rail",
                min=f"{float(np.min(vs_finite)):.2f}",
                max=f"{float(np.max(vs_finite)):.2f}",
            ))

        # -----------------------------------------------------------
        # 5. MCU temperature / rail envelope (informational)
        # -----------------------------------------------------------
        mcu_ts = np.array([])
        mtemp = np.array([])
        if self.reader.has_message("MCU"):
            mcu = self.reader.get_messages_as_arrays(
                "MCU", ["MTemp", "MVolt", "MVmin", "MVmax"])
            mcu_ts = mcu["_timestamp"]
            mtemp = mcu["MTemp"]
            mt_ok = np.isfinite(mtemp)
            if np.any(mt_ok):
                mt_lo = float(np.min(mtemp[mt_ok]))
                mt_hi = float(np.max(mtemp[mt_ok]))
                # Plausibility guard: an H7 Rev V with the AN5312 ÷2
                # ADC-divider issue logs hundreds of °C. Report broken
                # scaling as broken, not as a temperature.
                if (mt_lo < self.thresholds.board_mcu_temp_valid_min
                        or mt_hi > self.thresholds.board_mcu_temp_valid_max):
                    details.append(t("board_power.mcu_temp_implausible",
                                     min=f"{mt_lo:.0f}", max=f"{mt_hi:.0f}"))
                    mtemp = np.array([])  # suppress the figure too
                else:
                    details.append(t("board_power.mcu_temp",
                                     min=f"{mt_lo:.0f}", max=f"{mt_hi:.0f}"))
            mv = mcu["MVolt"]
            mv_ok = np.isfinite(mv) & (mv > 0)
            if np.any(mv_ok):
                mvmin = mcu["MVmin"][mv_ok]
                mvmax = mcu["MVmax"][mv_ok]
                details.append(t(
                    "board_power.mcu_volt",
                    volt=f"{float(np.median(mv[mv_ok])):.2f}",
                    min=f"{float(np.nanmin(mvmin)):.2f}",
                    max=f"{float(np.nanmax(mvmax)):.2f}",
                ))

        # -----------------------------------------------------------
        # 6. Figures
        # -----------------------------------------------------------
        fig_vcc = self.make_figure(
            title=t("board_power.fig_vcc"),
            xaxis_title=t("board_power.time_s"),
            yaxis_title=t("board_power.volt_v"),
            height=350,
        )
        fig_vcc.add_trace(go.Scattergl(
            x=ts, y=vcc, mode="lines",
            name=t("board_power.trace_vcc"),
            line=dict(color="#1f77b4", width=1),
        ))
        if servo_powered:
            fig_vcc.add_trace(go.Scattergl(
                x=ts, y=vservo, mode="lines",
                name=t("board_power.trace_vservo"),
                line=dict(color="#2ca02c", width=1),
            ))
        self.add_threshold_hline(
            fig_vcc, self.thresholds.board_vcc_low_crit,
            t("board_power.threshold_lo",
              val=f"{self.thresholds.board_vcc_low_crit:.1f}"),
            color="#d9534f")
        self.add_threshold_hline(
            fig_vcc, self.thresholds.board_vcc_high_crit,
            t("board_power.threshold_hi",
              val=f"{self.thresholds.board_vcc_high_crit:.1f}"),
            color="#d9534f")
        figures.append(fig_vcc)

        if len(mcu_ts) > 0 and np.any(np.isfinite(mtemp)):
            fig_mcu = self.make_figure(
                title=t("board_power.fig_mcu_temp"),
                xaxis_title=t("board_power.time_s"),
                yaxis_title=t("board_power.temp_c"),
                height=300,
            )
            fig_mcu.add_trace(go.Scattergl(
                x=mcu_ts, y=mtemp, mode="lines",
                name=t("board_power.trace_mtemp"),
                line=dict(color="#ff7f0e", width=1),
            ))
            figures.append(fig_mcu)

        # -----------------------------------------------------------
        # 7. Summary / action
        # -----------------------------------------------------------
        if status == "critical":
            summary = t("board_power.summary_critical")
            plain_summary = t("board_power.plain_critical")
            action_text = t("board_power.action_critical")
            action_level = "physical_check"
        elif status == "warning":
            if sag > self.thresholds.board_vcc_sag_warn:
                summary = t("board_power.summary_warning", sag=f"{sag:.2f}")
            else:
                # flag-event warning (overcurrent) without a Vcc sag
                summary = t("board_power.summary_warning_flags")
            plain_summary = t("board_power.plain_warning")
            action_text = t("board_power.action_warning")
            action_level = "physical_check"
        elif not have_armed:
            summary = t("board_power.summary_unarmed")
            plain_summary = ""
            action_text = ""
            action_level = "ok"
        else:
            summary = t("board_power.summary_good",
                        median=f"{float(np.median(sm_vcc)):.2f}",
                        sag=f"{sag:.2f}")
            plain_summary = ""
            action_text = ""
            action_level = "ok"

        return self.make_result(
            title=t("analyzer.board_power"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
        )
