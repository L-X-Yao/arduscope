"""
Throttle Collapse Analyzer: collective-output floor collapse and the
resulting attitude limit cycle (copter only).

Signature: in an altitude-controlled mode, CTUN.ThO drops to the mixer
floor and stays there while the vehicle is airborne. ThO is a thrust from
0 to 1, and thrust 0 is what puts the motors at MOT_SPIN_MIN
(thrust_to_actuator), so the floor is ThO 0 whatever that parameter says.
The altitude loop only demands that when it believes the vehicle is far
above target — the classic trigger is a barometer ground-effect false
climb (prop wash near ground). With all motors pinned at the floor the
attitude mixer has no collective headroom and rate control degrades
into a low-frequency bang-bang limit cycle that can end in loss of
control.

Required: CTUN
Optional: ATT, RCOU

Title: 油门塌缩
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from lib.outputs import read_outputs
from . import AnalysisResult, BaseAnalyzer

# Copter altitude-controlled flight modes (ArduCopter mode numbers):
# ALT_HOLD, AUTO, GUIDED, LOITER, RTL, LAND, POSHOLD, BRAKE,
# GUIDED_NOGPS, SMART_RTL, AUTO_RTL. Manual-throttle modes are excluded
# because a pilot chopping throttle there is intentional.
_ALT_CTRL_MODES = {2, 3, 4, 5, 6, 9, 16, 17, 20, 21, 27}

# LogEvent ids of the land detector's verdict (AP_Logger.h).
_EV_LAND_COMPLETE = 18
_EV_NOT_LANDED = 28

# LAND_DETECTOR_VEL_Z_MAX (ArduCopter/config.h): under this, AP itself says
# the vehicle may be landed. Near zero thrust a flying one falls past it.
_STILL_MS = 1.0


class ThrottleCollapseAnalyzer(BaseAnalyzer):
    """
    Detects sustained collective-throttle collapse to the mixer floor
    while airborne, plus the attitude limit cycle that follows.

    Generates:
      - ThO vs time with the floor threshold line and collapse windows
      - Roll/Pitch during the post-collapse window when oscillation found
    """

    registry_key = "throttle_collapse"
    sort_order = 118
    required_messages = ["CTUN"]
    optional_messages = ["ATT", "RCOU"]

    def analyze(self) -> AnalysisResult:
        if not self.reader.is_copter:
            return self.make_result(
                title=t("analyzer.throttle_collapse"),
                status="info",
                summary=t("throttle_collapse.copter_only"),
            )

        ctun = self.reader.get_messages_as_arrays("CTUN", ["ThO", "Alt"])
        ts = ctun["_timestamp"]
        tho = ctun["ThO"]
        alt = ctun["Alt"]

        if len(ts) < 20 or np.all(np.isnan(tho)):
            return self.make_result(
                title=t("analyzer.throttle_collapse"),
                status="info",
                summary=t("throttle_collapse.no_data"),
            )

        details = []
        figures = []
        overall_status = "good"

        floor_thr = self.thresholds.collapse_tho_margin

        # -----------------------------------------------------------
        # 1. Eligibility mask: armed + altitude-controlled mode + airborne
        # -----------------------------------------------------------
        mask = np.zeros(len(ts), dtype=bool)
        for start, end in self.reader.get_armed_intervals():
            mask |= (ts >= start) & (ts <= end)

        mode_mask = self._alt_ctrl_mode_mask(ts)
        if mode_mask is not None:
            mask &= mode_mask

        mask &= (self._height_in_flight(ts, alt)
                 > self.thresholds.log_abrupt_airborne_alt_m)

        # -----------------------------------------------------------
        # 2. Collapse windows: ThO pinned at the mixer floor
        # -----------------------------------------------------------
        collapsed = mask & (np.nan_to_num(tho, nan=1.0) <= floor_thr)
        events = [
            (t0, t1) for t0, t1 in self._group_events(
                ts, collapsed, self.thresholds.collapse_min_dur_s)
            if self._moved_vertically(ts, alt, t0, t1)
        ]

        if not events:
            return self.make_result(
                title=t("analyzer.throttle_collapse"),
                status="good",
                summary=t("throttle_collapse.none",
                          floor=f"{floor_thr:.2f}"),
            )

        # -----------------------------------------------------------
        # 3. Per-event forensics
        # -----------------------------------------------------------
        overall_status = "warning"
        details.append(t("throttle_collapse.found",
                         count=len(events), floor=f"{floor_thr:.2f}"))

        osc_found = False
        for t0, t1 in events[:5]:
            dur = t1 - t0
            details.append(t("throttle_collapse.event",
                             start=f"{t0:.1f}", dur=f"{dur:.1f}"))

            # 3a. Baro false-climb trigger: climb-rate spike just before
            pre = (ts >= t0 - 2.0) & (ts <= t0 + 0.5)
            if np.sum(pre) >= 3:
                pre_ts = ts[pre]
                pre_alt = np.nan_to_num(alt[pre], nan=0.0)
                rate = np.gradient(pre_alt, pre_ts)
                peak_rate = float(np.max(rate))
                if peak_rate > self.thresholds.collapse_climb_spike_ms:
                    details.append(t("throttle_collapse.climb_spike",
                                     rate=f"{peak_rate:.1f}"))

            # 3b. Collective floor pin on motor outputs
            floor_frac = self._rcou_floor_fraction(t0, t1)
            if floor_frac is not None and floor_frac > 0.5:
                details.append(t("throttle_collapse.rcou_floor",
                                 pct=f"{floor_frac*100:.0f}"))

            # 3c. Post-collapse attitude limit cycle
            osc = self._attitude_oscillation(t0, t1 + 6.0)
            if osc is not None:
                amp, freq, fs = osc
                osc_found = True
                if freq is not None and fs >= 4.0 * freq:
                    details.append(t("throttle_collapse.osc",
                                     amp=f"{amp:.0f}", freq=f"{freq:.1f}"))
                else:
                    # Below 4x oversampling a frequency estimate aliases;
                    # report the oscillation without naming a number.
                    details.append(t("throttle_collapse.osc_aliased",
                                     amp=f"{amp:.0f}"))

        if osc_found:
            overall_status = "critical"

        # -----------------------------------------------------------
        # 4. Figure: ThO with collapse windows
        # -----------------------------------------------------------
        fig = self.make_figure(
            title=t("throttle_collapse.fig_tho"),
            xaxis_title=t("throttle_collapse.time_s"),
            yaxis_title=t("throttle_collapse.tho_axis"),
            height=350,
        )
        fig.add_trace(go.Scattergl(
            x=ts, y=tho, mode='lines',
            name="ThO", line=dict(color='#1f77b4', width=1),
        ))
        self.add_threshold_hline(
            fig, floor_thr,
            t("throttle_collapse.floor_line", val=f"{floor_thr:.2f}"),
            color="#d9534f",
        )
        for t0, t1 in events[:5]:
            fig.add_vrect(x0=t0, x1=t1, fillcolor="#d9534f",
                          opacity=0.15, line_width=0)
        figures.append(fig)

        # -----------------------------------------------------------
        # 5. Result
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = t("throttle_collapse.summary_crit")
        else:
            summary = t("throttle_collapse.summary_warn")

        return self.make_result(
            title=t("analyzer.throttle_collapse"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level="physical_check",
            plain_summary=t("throttle_collapse.plain"),
            action_text=t("throttle_collapse.action"),
            evidence_source=t("throttle_collapse.evidence"),
            param_suggestions=[],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _height_in_flight(self, ts: np.ndarray,
                          alt: np.ndarray) -> np.ndarray:
        """Height above the takeoff point while the land detector says the
        vehicle is flying, -inf otherwise.

        CTUN.Alt alone cannot say: it counts from the EKF origin, and real
        logs show parked frames at 3 to 7 m on it.
        """
        flights = []
        takeoff = None
        for ev in self.reader.events:
            if ev.event_id == _EV_NOT_LANDED and takeoff is None:
                takeoff = ev.timestamp
            elif ev.event_id == _EV_LAND_COMPLETE and takeoff is not None:
                flights.append((takeoff, ev.timestamp))
                takeoff = None
        if takeoff is not None:
            flights.append((takeoff, float(ts[-1])))

        height = np.full(len(ts), -np.inf)
        for start, end in flights:
            flying = (ts >= start) & (ts <= end)
            if np.any(flying):
                height[flying] = alt[flying] - alt[np.argmax(flying)]
        return height

    @staticmethod
    def _moved_vertically(ts: np.ndarray, alt: np.ndarray,
                          t0: float, t1: float) -> bool:
        """False for a frame that sat still through [t0, t1] at the floor.

        That is a landing the detector has not declared yet, a few metres
        off the takeoff height: a landing spot is not always level with
        it, and the baro drifts in flight.
        """
        win = (ts >= t0) & (ts <= t1)
        if np.sum(win) < 2:
            return True
        rate = np.gradient(np.nan_to_num(alt[win], nan=0.0), ts[win])
        return float(np.max(np.abs(rate))) >= _STILL_MS

    def _alt_ctrl_mode_mask(self, ts: np.ndarray) -> np.ndarray | None:
        """Boolean mask of samples inside altitude-controlled modes.

        Returns None when the log carries no mode history (mask cannot
        be built; caller then treats all armed time as eligible)."""
        mode_list = getattr(self.reader, "mode_list", None)
        if not mode_list:
            return None
        # With no MODE messages, mode_list is the flightmode_list fallback:
        # real mode_names but mode_number=0 for every entry (see LogReader),
        # which matches no altitude-controlled mode and silently disables
        # the analyzer. Mode 0 is also a real mode (copter STABILIZE /
        # plane MANUAL), so key off MODE-message presence, not the numbers.
        # Without MODE messages, treat modes as unknown -> all armed time
        # eligible.
        if not self.reader.has_message("MODE"):
            return None
        mask = np.zeros(len(ts), dtype=bool)
        for i, mc in enumerate(mode_list):
            start = mc.timestamp
            end = (mode_list[i + 1].timestamp
                   if i + 1 < len(mode_list) else float(ts[-1]) + 1.0)
            if mc.mode_number in _ALT_CTRL_MODES:
                mask |= (ts >= start) & (ts < end)
        return mask

    @staticmethod
    def _group_events(ts: np.ndarray, flag: np.ndarray,
                      min_dur_s: float) -> list[tuple[float, float]]:
        """Group consecutive flagged samples into (start, end) windows
        and keep those lasting at least min_dur_s."""
        idx = np.where(flag)[0]
        if len(idx) == 0:
            return []
        events = []
        run_start = idx[0]
        prev = idx[0]
        for i in idx[1:]:
            if ts[i] - ts[prev] > 1.0:
                events.append((float(ts[run_start]), float(ts[prev])))
                run_start = i
            prev = i
        events.append((float(ts[run_start]), float(ts[prev])))
        return [(a, b) for a, b in events if (b - a) >= min_dur_s]

    def _rcou_floor_fraction(self, t0: float, t1: float) -> float | None:
        """Fraction of samples in [t0, t1] where all motor outputs sit at
        the PWM floor (collective pin): MOT_SPIN_MIN up the PWM range, as
        output_to_pwm has it, not the bottom of that range."""
        if not self.reader.has_message("RCOU"):
            return None
        channels = self.motor_channels()
        rcou = read_outputs(self.reader, channels)
        rts = rcou["_timestamp"]
        win = (rts >= t0) & (rts <= t1)
        if np.sum(win) < 3:
            return None
        pwm_min = self.reader.get_param("MOT_PWM_MIN", 1000)
        pwm_max = self.reader.get_param("MOT_PWM_MAX", 2000)
        if pwm_min <= 0 or pwm_max <= pwm_min:
            pwm_min, pwm_max = 1000, 2000
        spin_min = self.reader.get_param("MOT_SPIN_MIN",
                                         self.thresholds.mot_spin_min)
        floor = pwm_min + (pwm_max - pwm_min) * spin_min + 25
        stacked = np.column_stack([
            np.nan_to_num(rcou[c][win], nan=9999)
            for c in channels
        ])
        all_floor = np.all(stacked <= floor, axis=1)
        return float(np.mean(all_floor))

    def _attitude_oscillation(
        self, t0: float, t1: float,
    ) -> tuple[float, float | None, float] | None:
        """Detect a sustained large-amplitude roll/pitch oscillation in
        [t0, t1]. Returns (amplitude_deg, freq_hz_or_None, sample_hz)
        or None when absent.

        Frequency comes from zero crossings of the demeaned dominant
        axis; the caller must reject the estimate when the sample rate
        is under 4x the frequency (aliasing — a 10 Hz log stream once
        turned a 1.7 Hz attitude cycle into a phantom "5 Hz" reading).
        """
        if not self.reader.has_message("ATT"):
            return None
        att = self.reader.get_messages_as_arrays("ATT", ["Roll", "Pitch"])
        ats = att["_timestamp"]
        win = (ats >= t0) & (ats <= t1)
        if np.sum(win) < 8:
            return None
        wts = ats[win]
        roll = np.nan_to_num(att["Roll"][win], nan=0.0)
        pitch = np.nan_to_num(att["Pitch"][win], nan=0.0)

        # Amplitude and crossings both from the swing around the mean: a
        # steady 22 degree bank is not an oscillation, however far from
        # level it holds, and sensor noise crosses its mean all the time.
        roll = roll - np.mean(roll)
        pitch = pitch - np.mean(pitch)
        amp = float(max(np.max(np.abs(roll)), np.max(np.abs(pitch))))
        if amp < self.thresholds.collapse_att_amp_deg:
            return None

        sig = roll if np.max(np.abs(roll)) >= np.max(np.abs(pitch)) else pitch
        cross_idx = np.where(np.diff(np.signbit(sig)) != 0)[0]
        crossings = len(cross_idx)
        if crossings < self.thresholds.collapse_osc_min_crossings:
            return None

        span = float(wts[-1] - wts[0])
        if span <= 0:
            return None
        fs = (len(wts) - 1) / span
        # Frequency over the span actually containing the crossings —
        # averaging over quiet lead-in/out would dilute the estimate.
        osc_span = float(wts[cross_idx[-1]] - wts[cross_idx[0]])
        freq = ((crossings - 1) / (2.0 * osc_span)
                if osc_span > 0 else None)
        return amp, freq, fs
