"""
RC Input Analyzer: remote control channel monitoring and failsafe detection.

Monitors RC channel values, detects stick saturation and failsafe events
(all channels dropping to same value simultaneously).

Required: RCIN
Optional: (none)

Title: 遥控输入
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t
from . import AnalysisResult, BaseAnalyzer


class RCInputAnalyzer(BaseAnalyzer):
    """
    RC input analyzer.

    Generates:
      - RC channels time series (C1-C8)
      - Stick range summary (min/max/mean bar chart)
      - Failsafe detection (all channels going to same value)
    """

    registry_key = "rc_input"
    sort_order = 110
    required_messages = ["RCIN"]
    optional_messages: list[str] = []

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read RCIN data (up to 8 channels)
        # -----------------------------------------------------------
        channels = [f"C{i}" for i in range(1, 9)]
        rcin = self.reader.get_messages_as_arrays("RCIN", channels)
        t = rcin["_timestamp"]

        if len(t) == 0:
            return self.make_result(
                title=_t("analyzer.rc_input"),
                status="info",
                summary=_t("rc_input.empty"),
            )

        # Filter to channels that have real data
        active_channels: dict[str, np.ndarray] = {}
        for ch in channels:
            arr = rcin[ch]
            if not np.all(np.isnan(arr)) and np.nanmax(arr) > 0:
                active_channels[ch] = arr

        if not active_channels:
            return self.make_result(
                title=_t("analyzer.rc_input"),
                status="info",
                summary=_t("rc_input.no_channels"),
            )

        details.append(_t("rc_input.active_channels", count=len(active_channels)))

        overall_status = "good"

        # Armed window for everything judged as flight: pre-arm stick checks
        # push sticks to endpoints, and a transmitter switched off on the
        # ground drops the channels just as RC loss in the air does.
        armed_intervals = self.reader.get_armed_intervals()
        armed_mask = np.zeros(len(t), dtype=bool)
        if armed_intervals:
            for start, end in armed_intervals:
                armed_mask |= (t >= start) & (t <= end)

        # -----------------------------------------------------------
        # 2. Channel statistics (whole log)
        # -----------------------------------------------------------
        ch_stats = {}
        for ch, arr in active_channels.items():
            valid = arr[~np.isnan(arr)]
            if len(valid) == 0:
                continue
            ch_stats[ch] = {
                'min': float(np.min(valid)),
                'max': float(np.max(valid)),
                'mean': float(np.mean(valid)),
                'std': float(np.std(valid)),
            }
            details.append(
                _t("rc_input.ch_stats",
                   ch=ch,
                   min=f"{ch_stats[ch]['min']:.0f}",
                   max=f"{ch_stats[ch]['max']:.0f}",
                   mean=f"{ch_stats[ch]['mean']:.0f}",
                   std=f"{ch_stats[ch]['std']:.1f}")
            )

        # -----------------------------------------------------------
        # 3. Per-channel PWM stats during armed flight — informational
        #
        # The previous "端点饱和" warning (>5% time at PWM ≤1010 or
        # ≥1990) was a false-positive machine: baseline on 18 real
        # logs fired it on 15 (~83%). Root cause: aux channels (C5-C8)
        # are commonly mode switches parked at a single PWM value, so
        # they legitimately sit at an endpoint 100% of the time. PWM
        # being at an endpoint is not by itself an ArduPilot-defined
        # problem — it's either a stick fully pushed (intentional) or
        # a non-stick channel in its switch position (intentional).
        #
        # Per POSITIONING.md (no E-class speculative thresholds), the
        # warning was removed. Only authoritative signals remain:
        #   - RC failsafe (below) detects actual signal loss via
        #     ArduPilot's FS_THR_VALUE mechanism.
        #   - Channel min/max stats are still shown in details so the
        #     engineer can spot anomalies manually.
        if np.any(armed_mask):
            for ch, arr in active_channels.items():
                armed_arr = arr[armed_mask]
                valid = armed_arr[~np.isnan(armed_arr)]
                if len(valid) == 0:
                    continue
                at_min_pct = (
                    np.sum(valid <= self.thresholds.rc_pwm_min_sat) / len(valid) * 100
                )
                at_max_pct = (
                    np.sum(valid >= self.thresholds.rc_pwm_max_sat) / len(valid) * 100
                )
                # Only note channels that spent notable time at endpoints
                # for engineer awareness — no status change.
                limit_pct = self.thresholds.rc_sat_frac_warn * 100
                if at_min_pct > limit_pct or at_max_pct > limit_pct:
                    details.append(
                        _t("rc_input.endpoint_dist",
                           ch=ch,
                           low=f"{at_min_pct:.0f}",
                           high=f"{at_max_pct:.0f}")
                    )

        # -----------------------------------------------------------
        # 4. Detect RC failsafe
        # -----------------------------------------------------------
        fs_pwm = self.thresholds.rc_failsafe_pwm
        fs_detected = self._detect_failsafe(
            t, active_channels, fs_pwm, armed_mask)
        if fs_detected:
            overall_status = self.worst_status(overall_status, "critical")
            details.append(
                _t("rc_input.failsafe_detected",
                   count=len(fs_detected), pwm=fs_pwm)
            )
            for fs_time in fs_detected[:5]:
                details.append(
                    f"  - {_t('rc_input.failsafe_event', time=f'{fs_time:.1f}')}"
                )

        # Outside armed flight the same signal is a note, not a failsafe:
        # the receiver is often still silent at boot, and with no known
        # armed window nothing here can be called a flight event.
        fs_disarmed = self._detect_failsafe(
            t, active_channels, fs_pwm, ~armed_mask)
        if fs_disarmed:
            details.append(
                _t("rc_input.failsafe_disarmed", count=len(fs_disarmed)))

        # -----------------------------------------------------------
        # 4b. Throttle deadzone forensics (copter, info only)
        #
        # In ALT_HOLD-family modes get_pilot_desired_climb_rate()
        # applies THR_DZ around mid-stick: inside the band the climb
        # command is exactly zero. A pilot slowly raising the stick
        # that never leaves the band looks like "the vehicle ignores
        # my throttle" — flag those windows so the investigation
        # checks the band BEFORE blaming motors or firmware. Note the
        # deadzone is THR_DZ, not RC3_DZ.
        # -----------------------------------------------------------
        if self.reader.is_copter and "C3" in active_channels:
            dz_events = self._detect_deadzone_stuck(
                t, active_channels["C3"], armed_mask)
            if dz_events:
                band_lo, band_hi = self._thr_dz_band_pwm()
                details.append(
                    _t("rc_input.dz_note",
                       count=len(dz_events),
                       lo=f"{band_lo:.0f}", hi=f"{band_hi:.0f}")
                )
                for start, dur in dz_events[:3]:
                    details.append(
                        f"  - {_t('rc_input.dz_event', time=f'{start:.1f}', dur=f'{dur:.1f}')}"
                    )
                details.append(_t("rc_input.dz_explain"))

        # -----------------------------------------------------------
        # 5. Figure: RC channels time series
        # -----------------------------------------------------------
        fig_rc = self.make_figure(
            title=_t("rc_input.fig_channels"),
            xaxis_title=_t("rc_input.time_s"),
            yaxis_title=_t("rc_input.pwm_us"),
            height=450,
        )

        colors = [
            '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728',
            '#9467bd', '#8c564b', '#e377c2', '#7f7f7f',
        ]
        for i, (ch, arr) in enumerate(active_channels.items()):
            fig_rc.add_trace(go.Scattergl(
                x=t, y=arr,
                mode='lines', name=ch,
                line=dict(color=colors[i % len(colors)], width=1),
            ))
        figures.append(fig_rc)

        # -----------------------------------------------------------
        # 6. Figure: Stick range summary (bar chart)
        # -----------------------------------------------------------
        if ch_stats:
            fig_range = self.make_figure(
                title=_t("rc_input.fig_range"),
                xaxis_title=_t("rc_input.channel"),
                yaxis_title=_t("rc_input.pwm_us"),
                height=350,
            )

            ch_names = list(ch_stats.keys())
            mins = [ch_stats[c]['min'] for c in ch_names]
            maxs = [ch_stats[c]['max'] for c in ch_names]
            means = [ch_stats[c]['mean'] for c in ch_names]

            fig_range.add_trace(go.Bar(
                x=ch_names, y=mins,
                name=_t("rc_input.min_val"),
                marker_color='#5bc0de',
            ))
            fig_range.add_trace(go.Bar(
                x=ch_names, y=means,
                name=_t("rc_input.mean_val"),
                marker_color='#5cb85c',
            ))
            fig_range.add_trace(go.Bar(
                x=ch_names, y=maxs,
                name=_t("rc_input.max_val"),
                marker_color='#d9534f',
            ))
            fig_range.update_layout(barmode='group')
            figures.append(fig_range)

        # -----------------------------------------------------------
        # 7. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = _t("rc_input.crit_summary")
        else:
            summary = _t("rc_input.good_summary", count=len(active_channels))

        if overall_status == "critical":
            action_level = "physical_check"
            plain_summary = _t("rc_input.crit_plain")
            action_text = _t("rc_input.crit_action")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source — only RC failsafe remains as a warning trigger,
        # and it's backed by ArduPilot's FS_THR_VALUE mechanism directly.
        if overall_status == "critical":
            evidence_source = _t("rc_input.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.rc_input"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
            param_suggestions=[],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ALT_HOLD-family modes where the pilot's stick drives climb rate
    # through the THR_DZ deadband (ArduCopter mode numbers):
    # ALT_HOLD, LOITER, POSHOLD, BRAKE, GUIDED_NOGPS.
    _DZ_MODES = {2, 5, 16, 17, 20}

    def _thr_dz_band_pwm(self) -> tuple[float, float]:
        """THR_DZ band converted to PWM.

        get_pilot_desired_climb_rate() zeroes the command when
        |control_in - 500| <= THR_DZ on the 0-1000 control_in scale;
        control_in maps linearly from RC3_MIN..RC3_MAX.
        """
        thr_dz = self.reader.get_param("THR_DZ", 100)
        rc3_min = self.reader.get_param("RC3_MIN", 1100)
        rc3_max = self.reader.get_param("RC3_MAX", 1900)
        span = max(rc3_max - rc3_min, 1.0)
        lo = rc3_min + (500.0 - thr_dz) / 1000.0 * span
        hi = rc3_min + (500.0 + thr_dz) / 1000.0 * span
        return lo, hi

    def _detect_deadzone_stuck(
        self,
        t: np.ndarray,
        c3: np.ndarray,
        armed_mask: np.ndarray,
    ) -> list[tuple[float, float]]:
        """
        Windows where the pilot pushed the throttle stick up but never
        crossed the top of the THR_DZ band, so a positive climb command
        was never issued — the classic "the vehicle ignores my
        throttle" misdiagnosis.

        Returns [(start_time, duration), ...]. Requires mode history to
        gate on ALT_HOLD-family modes — without it, returns [] (a
        mid-stick in STABILIZE means hover throttle, not a deadzone).
        """
        mode_list = getattr(self.reader, "mode_list", None)
        if not mode_list:
            return []
        mode_mask = np.zeros(len(t), dtype=bool)
        for i, mc in enumerate(mode_list):
            start = mc.timestamp
            end = (mode_list[i + 1].timestamp
                   if i + 1 < len(mode_list) else float(t[-1]) + 1.0)
            if mc.mode_number in self._DZ_MODES:
                mode_mask |= (t >= start) & (t < end)

        _lo, hi = self._thr_dz_band_pwm()
        c3f = np.nan_to_num(c3, nan=0.0)
        # The forensic invariant is "no positive climb command was ever
        # issued": that covers the whole region at or below the band
        # top (inside the band = zero command, below it = descend).
        no_climb_cmd = armed_mask & mode_mask & (c3f <= hi)

        idx = np.where(no_climb_cmd)[0]
        if len(idx) == 0:
            return []

        events = []
        run_start = idx[0]
        prev = idx[0]
        min_dur = self.thresholds.rc_dz_stuck_min_s
        move_min = self.thresholds.rc_dz_stick_move_pwm

        def _close_run(a: int, b: int):
            dur = float(t[b] - t[a])
            if dur < min_dur:
                return
            seg = c3f[a:b + 1]
            if len(seg) < 4:
                return
            # The pilot must be pushing UP: net rise between the run's
            # first and last quarter. A parked stick (hover) or a
            # lowered stick (intentional descent) stays silent.
            q = max(1, len(seg) // 4)
            net_rise = float(np.median(seg[-q:]) - np.median(seg[:q]))
            if net_rise >= move_min:
                events.append((float(t[a]), dur))

        for i in idx[1:]:
            if t[i] - t[prev] > 1.0:
                _close_run(run_start, prev)
                run_start = i
            prev = i
        _close_run(run_start, prev)
        return events

    def _detect_failsafe(
        self,
        t: np.ndarray,
        channels: dict[str, np.ndarray],
        fs_pwm: int,
        window: np.ndarray,
    ) -> list[float]:
        """
        Detect failsafe events where multiple channels drop to the same
        value simultaneously, among the samples selected by `window`.

        Returns list of timestamps where failsafe was detected.
        """
        n_ch = len(channels)
        if n_ch < 3:
            return []

        # Stack all channel data
        ch_arrays = list(channels.values())
        stacked = np.column_stack(ch_arrays)

        below_fs = np.nan_to_num(stacked, nan=9999) <= fs_pwm
        fs_count = np.sum(below_fs, axis=1)

        # Require at least ~60% of active channels to be below fs_pwm
        # simultaneously. Hardcoded "3" did not scale to 4/8-channel logs.
        threshold = max(3, int(np.ceil(n_ch * 0.6)))
        fs_mask = (fs_count >= threshold) & window

        if not np.any(fs_mask):
            return []

        # Group consecutive failsafe samples into events — use time gap
        # (≥1s) so grouping is independent of sample rate.
        fs_indices = np.where(fs_mask)[0]
        events = []
        if len(fs_indices) > 0:
            event_start = fs_indices[0]
            for i in range(1, len(fs_indices)):
                if t[fs_indices[i]] - t[fs_indices[i-1]] > 1.0:
                    events.append(float(t[event_start]))
                    event_start = fs_indices[i]
            events.append(float(t[event_start]))

        return events
