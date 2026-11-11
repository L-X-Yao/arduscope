"""
Servo Dither Analyzer: high-frequency oscillation detection in servo/motor outputs.

Detects dither (rapid oscillation) in RCOU channels by measuring the
zero-crossing rate of the first-difference of PWM values. High zero-crossing
rate indicates the controller is constantly reversing direction -- a symptom
of oscillation or excessive PID gain.

Required: RCOU
Optional: (none)

Title: Servo Dither
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t

from . import AnalysisResult, BaseAnalyzer


class ServoDitherAnalyzer(BaseAnalyzer):
    """
    Servo dither analyzer.

    For each active RCOU channel (C1-C8), computes the zero-crossing rate
    of the first-difference signal. High zero-crossing rate = dither.

    Generates:
      - Windowed zero-crossing rate per channel over time
    """

    registry_key = "servo_dither"
    sort_order = 35
    required_messages = ["RCOU"]
    optional_messages: list[str] = []

    def analyze(self) -> AnalysisResult:
        details: list[str] = []
        figures: list[go.Figure] = []

        # -----------------------------------------------------------
        # 1. Read RCOU channels C1-C8
        # -----------------------------------------------------------
        channels = [f"C{i}" for i in range(1, 9)]
        rcou = self.reader.get_messages_as_arrays("RCOU", channels)
        ts = rcou["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=_t("analyzer.servo_dither"),
                status="info",
                summary=_t("servo_dither.empty"),
            )

        # -----------------------------------------------------------
        # 2. Identify active channels
        # -----------------------------------------------------------
        active_channels: dict[str, np.ndarray] = {}
        for ch in channels:
            arr = rcou[ch]
            if np.all(np.isnan(arr)):
                continue
            valid = arr[~np.isnan(arr)]
            if len(valid) < 10:
                continue
            # Skip constant channels (no variation)
            if np.std(valid) < 1.0:
                continue
            active_channels[ch] = arr

        if not active_channels:
            return self.make_result(
                title=_t("analyzer.servo_dither"),
                status="info",
                summary=_t("servo_dither.no_active"),
            )

        details.append(_t("servo_dither.active_channels",
                           n=len(active_channels)))

        # -----------------------------------------------------------
        # 3. Compute zero-crossing rate per channel
        # -----------------------------------------------------------
        overall_status = "good"
        armed_intervals = self.reader.get_armed_intervals()
        if armed_intervals:
            duration = sum(e - s for s, e in armed_intervals)
        else:
            duration = float(ts[-1] - ts[0]) if len(ts) > 1 else 1.0
        if duration <= 0:
            duration = 1.0

        channel_zc_rates: dict[str, float] = {}
        worst_channels: list[str] = []

        for ch, arr in active_channels.items():
            diff = np.diff(arr)
            # Filter to armed intervals only so crossings/duration are
            # computed over the same time window (ts[:-1] = interval starts).
            ts_diff = ts[:-1]
            armed_mask = self.get_armed_mask(ts_diff)
            valid_diff = diff[np.isfinite(diff) & armed_mask]
            if len(valid_diff) < 2:
                continue

            # Count zero-crossings: where sign changes (ignoring holds)
            signs = np.sign(valid_diff)
            nonzero = signs[signs != 0]
            if len(nonzero) < 2:
                channel_zc_rates[ch] = 0.0
                continue

            crossings = int(np.sum(nonzero[:-1] != nonzero[1:]))
            zc_rate = crossings / duration

            channel_zc_rates[ch] = zc_rate
            details.append(
                _t("servo_dither.channel_zc_rate",
                    ch=ch, rate=f"{zc_rate:.1f}")
            )

            if zc_rate > self.thresholds.servo_dither_crit:
                overall_status = self.worst_status(
                    overall_status, "critical")
                details.append(
                    _t("servo_dither.zc_crit",
                        ch=ch, rate=f"{zc_rate:.1f}",
                        thresh=self.thresholds.servo_dither_crit)
                )
                worst_channels.append(ch)
            elif zc_rate > self.thresholds.servo_dither_warn:
                overall_status = self.worst_status(
                    overall_status, "warning")
                details.append(
                    _t("servo_dither.zc_warn",
                        ch=ch, rate=f"{zc_rate:.1f}",
                        thresh=self.thresholds.servo_dither_warn)
                )
                worst_channels.append(ch)

        # -----------------------------------------------------------
        # 4. Figure: windowed zero-crossing rate per channel
        # -----------------------------------------------------------
        fig = self.make_figure(
            title=_t("servo_dither.fig_zc_rate"),
            xaxis_title=_t("servo_dither.time_s"),
            yaxis_title=_t("servo_dither.zc_rate_hz"),
            height=400,
        )

        colors = [
            '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728',
            '#9467bd', '#8c564b', '#e377c2', '#7f7f7f',
        ]
        window_samples = 200  # sliding window size

        for idx, (ch, arr) in enumerate(active_channels.items()):
            diff = np.diff(arr)
            if len(diff) < window_samples:
                window_samples_ch = max(10, len(diff) // 2)
            else:
                window_samples_ch = window_samples

            # Compute windowed zero-crossing rate.
            # Mark only actual sign reversals (consistent with overall ZC):
            # a crossing occurs where consecutive non-zero signs differ,
            # ignoring zero-diff holds. abs(diff(signs))>0 incorrectly
            # counts sign→0 and 0→sign transitions as crossings.
            signs = np.sign(diff)
            signs = np.where(np.isfinite(signs), signs, 0.0)
            crossings_arr = np.zeros(len(signs))
            nz_idx = np.where(signs != 0)[0]
            if len(nz_idx) >= 2:
                reversed_nz = signs[nz_idx[1:]] != signs[nz_idx[:-1]]
                crossings_arr[nz_idx[1:][reversed_nz]] = 1.0
            crossings_arr = crossings_arr[:-1]  # align with cumsum window

            # Pad to maintain length
            if len(crossings_arr) < window_samples_ch:
                continue

            # Compute cumulative sum for windowed counting
            cumsum = np.concatenate([[0], np.cumsum(crossings_arr)])
            # Windowed crossing count
            windowed_count = (cumsum[window_samples_ch:]
                              - cumsum[:-window_samples_ch])

            # Convert to rate (Hz): crossings_in_window / window_duration
            if len(ts) > window_samples_ch:
                dt_window = np.diff(ts)
                # Mean dt for rate conversion
                mean_dt = float(np.nanmean(dt_window))
                if mean_dt > 0:
                    window_duration = mean_dt * window_samples_ch
                    zc_rate_windowed = windowed_count / window_duration
                else:
                    continue
            else:
                continue

            # Timestamps for windowed values (center of window)
            half_win = window_samples_ch // 2
            t_windowed = ts[half_win:half_win + len(zc_rate_windowed)]
            if len(t_windowed) != len(zc_rate_windowed):
                min_len = min(len(t_windowed), len(zc_rate_windowed))
                t_windowed = t_windowed[:min_len]
                zc_rate_windowed = zc_rate_windowed[:min_len]

            step = max(1, len(t_windowed) // 5000)
            fig.add_trace(go.Scattergl(
                x=t_windowed[::step], y=zc_rate_windowed[::step],
                mode='lines', name=ch,
                line=dict(color=colors[idx % len(colors)], width=1),
            ))

        # Add threshold lines
        self.add_threshold_hline(
            fig, self.thresholds.servo_dither_warn,
            _t("servo_dither.thresh_warn",
                val=self.thresholds.servo_dither_warn),
            color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig, self.thresholds.servo_dither_crit,
            _t("servo_dither.thresh_crit",
                val=self.thresholds.servo_dither_crit),
            color="#d9534f",
        )

        figures.append(fig)

        # -----------------------------------------------------------
        # 5. Motor channels
        # -----------------------------------------------------------
        if overall_status in ("warning", "critical"):
            # SPIN_MIN only helps if the dither is on a motor output
            motor_channels = set(self.motor_channels())
            dither_on_motors = any(
                ch in motor_channels for ch in worst_channels
            )
            if dither_on_motors:
                details.append(_t(
                    "servo_dither.spin_min_note",
                    param=("Q_M_SPIN_MIN" if self.vehicle.is_quadplane
                           else "MOT_SPIN_MIN")))

        # -----------------------------------------------------------
        # 6. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = _t("servo_dither.summary_critical")
            action_level = "physical_check"
            plain_summary = _t("servo_dither.critical_plain")
            action_text = _t("servo_dither.critical_action")
        elif overall_status == "warning":
            summary = _t("servo_dither.summary_warn")
            action_level = "physical_check"
            plain_summary = _t("servo_dither.warn_plain")
            action_text = _t("servo_dither.warn_action")
        else:
            summary = _t("servo_dither.summary_good",
                          n=len(active_channels))
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = _t("servo_dither.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.servo_dither"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
        )
