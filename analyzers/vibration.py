"""
Vibration Analyzer: vibration level assessment and FFT spectrum.

Evaluates vibration severity from VIBE messages and optionally computes
FFT frequency spectrum from raw IMU accelerometer data.

Required: VIBE
Optional: IMU (for FFT)

Title: 振动分析
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.fft_helpers import do_fft, find_flight_window, find_peaks
from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer


class VibrationAnalyzer(BaseAnalyzer):
    """
    Vibration analysis.

    Generates:
      - VibeX/Y/Z time series with threshold lines (15/30/60)
      - Clip count summary
      - FFT spectrum from IMU data (if IMU rate >= 100 Hz)
    """

    registry_key = "vibration"
    sort_order = 40
    required_messages = ["VIBE"]
    optional_messages = ["IMU"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read VIBE data
        # -----------------------------------------------------------
        # VIBE is per-instance in ArduPilot 4.0+ (an "IMU" column with a
        # single cumulative "Clip" counter) and flat in pre-4.0
        # (Clip0/Clip1/Clip2). Reading VibeX/Y/Z straight interleaves IMU
        # instances (rows share timestamps), so reduce over the primary IMU
        # only; the clip channels map to the per-IMU counters. Reading the
        # pre-4.0 field names alone made total_clips always 0 on 4.0+ logs.
        vibe = self.reader.get_messages_as_arrays("VIBE", [
            "IMU", "VibeX", "VibeY", "VibeZ",
            "Clip", "Clip0", "Clip1", "Clip2",
        ])
        ts_all = vibe["_timestamp"]

        if len(ts_all) == 0:
            return self.make_result(
                title=t("analyzer.vibration"),
                status="info",
                summary=t("vibration.empty"),
            )

        imu_inst = vibe.get("IMU")
        have_inst = imu_inst is not None and not np.all(np.isnan(imu_inst))
        if have_inst:
            inst_int = np.nan_to_num(imu_inst, nan=-1).astype(int)
            primary = int(inst_int[inst_int >= 0].min())
            pmask = inst_int == primary
        else:
            inst_int = None
            pmask = np.ones(len(ts_all), dtype=bool)

        ts = ts_all[pmask]
        vibe_x = vibe["VibeX"][pmask]
        vibe_y = vibe["VibeY"][pmask]
        vibe_z = vibe["VibeZ"][pmask]

        # Clip channels as (timestamps, series, instance-number). 4.0+
        # splits the single Clip counter by IMU instance; pre-4.0 uses the
        # flat Clip0/1/2 fields at shared timestamps.
        clip_single = vibe["Clip"]
        if have_inst and not np.all(np.isnan(clip_single)):
            clip_channels = [
                (ts_all[inst_int == i], clip_single[inst_int == i], i)
                for i in sorted(set(inst_int[inst_int >= 0].tolist()))
            ]
        else:
            clip_channels = [
                (ts_all, vibe[f"Clip{i}"], i) for i in range(3)
                if not np.all(np.isnan(vibe[f"Clip{i}"]))
            ]

        # Filter vibe levels to armed intervals (exclude ground shocks).
        armed = self.reader.get_armed_intervals()
        if armed:
            armed_mask = np.zeros(len(ts), dtype=bool)
            for start, end in armed:
                armed_mask |= (ts >= start) & (ts <= end)
            if np.sum(armed_mask) > 100:
                ts = ts[armed_mask]
                vibe_x = vibe_x[armed_mask]
                vibe_y = vibe_y[armed_mask]
                vibe_z = vibe_z[armed_mask]

        # -----------------------------------------------------------
        # 2. Compute statistics
        # -----------------------------------------------------------
        def _safe_nanmax(arr):
            return float(np.nanmax(arr)) if len(arr) > 0 and not np.all(np.isnan(arr)) else 0.0

        def _safe_nanmean(arr):
            return float(np.nanmean(arr)) if len(arr) > 0 and not np.all(np.isnan(arr)) else 0.0

        max_vibe_x = _safe_nanmax(vibe_x)
        max_vibe_y = _safe_nanmax(vibe_y)
        max_vibe_z = _safe_nanmax(vibe_z)
        max_vibe = max(max_vibe_x, max_vibe_y, max_vibe_z)

        avg_vibe_x = _safe_nanmean(vibe_x)
        avg_vibe_y = _safe_nanmean(vibe_y)
        avg_vibe_z = _safe_nanmean(vibe_z)

        clip_totals = [self._clips_while_armed(ct, cc)
                       for ct, cc, _n in clip_channels]
        total_clip0 = clip_totals[0] if len(clip_totals) > 0 else 0
        total_clip1 = clip_totals[1] if len(clip_totals) > 1 else 0
        total_clip2 = clip_totals[2] if len(clip_totals) > 2 else 0
        total_clips = sum(clip_totals)

        details.append(
            t("vibration.vibe_axis_detail", axis="X",
              avg=f"{avg_vibe_x:.1f}", mx=f"{max_vibe_x:.1f}")
        )
        details.append(
            t("vibration.vibe_axis_detail", axis="Y",
              avg=f"{avg_vibe_y:.1f}", mx=f"{max_vibe_y:.1f}")
        )
        details.append(
            t("vibration.vibe_axis_detail", axis="Z",
              avg=f"{avg_vibe_z:.1f}", mx=f"{max_vibe_z:.1f}")
        )
        details.append(
            t("vibration.clip_count",
              c0=total_clip0, c1=total_clip1, c2=total_clip2,
              total=total_clips)
        )

        # -----------------------------------------------------------
        # 3. Status determination + action level.
        # Source: METHODIC tuning guide (10/15/20/30 m/s² thresholds)
        # -----------------------------------------------------------
        warn_thresh = self.thresholds.vibe_warn
        crit_thresh = self.thresholds.vibe_crit

        physical_check_steps = t("vibration.physical_check_steps")

        if max_vibe >= crit_thresh:
            status = "critical"
            summary = t("vibration.critical_summary",
                        val=f"{max_vibe:.1f}", thresh=crit_thresh)
            plain_summary = t("vibration.critical_plain")
            action_text = physical_check_steps
            action_level = "physical_check"
        elif max_vibe >= warn_thresh:
            status = "warning"
            summary = t("vibration.warn_summary",
                        val=f"{max_vibe:.1f}", thresh=warn_thresh)
            plain_summary = t("vibration.warn_plain")
            action_text = physical_check_steps
            action_level = "physical_check"
        else:
            status = "good"
            summary = t("vibration.good_summary", val=f"{max_vibe:.1f}")
            plain_summary = t("vibration.good_plain")
            action_text = ""
            action_level = "ok"

            # Cross-reference: VIBE time-domain RMS being normal does NOT
            # mean the drone has no vibration problem. Frequency-domain
            # motor noise (typical 50-200 Hz) often presents as visible
            # shake / blurry video / motor whine without significantly
            # raising VIBE RMS. If the harmonic notch is not enabled,
            # point at the notch_tuning section instead of
            # claiming everything is fine.
            #
            # The notch_tuning analyzer already does the FFT and produces
            # the parameter recommendations — we just need to make sure
            # vibration's "all good" plain summary doesn't mask it.
            hntch_enable = self.reader.get_param("INS_HNTCH_ENABLE", 0)
            if hntch_enable == 0:
                plain_summary = t("vibration.good_no_notch_plain",
                                  val=f"{max_vibe:.1f}")
                details.append(t("vibration.good_no_notch_detail"))

        # Clip count raises status — clipping means accelerometer
        # actually saturated, that's a stronger signal than just high RMS.
        if total_clips > self.thresholds.vibe_clip_warn:
            status = self.worst_status(status, "warning")
            details.append(
                t("vibration.clip_excess", total=total_clips)
            )
            if action_level == "ok":
                # Refresh summary too — otherwise the status badge flips to
                # warning while summary still says "vibration normal", which
                # the report renderer puts side-by-side.
                summary = t("vibration.warn_summary_clip", total=total_clips)
                plain_summary = t("vibration.clip_saturated_plain")
                action_text = physical_check_steps
                action_level = "physical_check"

        # No filter suggestion for clipping: clips are counted on the raw
        # sample, before INS_ACCEL_FILTER (AP_InertialSensor_Backend.cpp),
        # so only less vibration at the sensor can reduce them.

        # Enforce invariant: apply_param ↔ non-empty param_suggestions.
        if param_suggestions and action_level not in ("apply_param", "safety_stop"):
            action_level = "apply_param"

        # -----------------------------------------------------------
        # 4. Figure: Vibration time series
        # -----------------------------------------------------------
        fig_vibe = self.make_figure(
            title=t("vibration.fig_vibe_title"),
            xaxis_title=t("vibration.time_s"),
            yaxis_title=t("vibration.fig_vibe_yaxis"),
            height=400,
        )

        fig_vibe.add_trace(go.Scattergl(
            x=ts, y=vibe_x,
            mode='lines', name='VibeX',
            line=dict(color='#1f77b4', width=1),
        ))
        fig_vibe.add_trace(go.Scattergl(
            x=ts, y=vibe_y,
            mode='lines', name='VibeY',
            line=dict(color='#ff7f0e', width=1),
        ))
        fig_vibe.add_trace(go.Scattergl(
            x=ts, y=vibe_z,
            mode='lines', name='VibeZ',
            line=dict(color='#2ca02c', width=1),
        ))

        # Threshold lines
        self.add_threshold_hline(
            fig_vibe, warn_thresh,
            t("vibration.thresh_warn", val=warn_thresh), color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig_vibe, crit_thresh,
            t("vibration.thresh_crit", val=crit_thresh), color="#d9534f",
        )
        # 60 m/s^2 = severe
        self.add_threshold_hline(
            fig_vibe, 60.0,
            t("vibration.thresh_severe", val=60), color="#8b0000",
        )

        figures.append(fig_vibe)

        # -----------------------------------------------------------
        # 5. Figure: Clip count
        # -----------------------------------------------------------
        if total_clips > 0:
            fig_clip = self.make_figure(
                title=t("vibration.fig_clip_title"),
                xaxis_title=t("vibration.time_s"),
                yaxis_title=t("vibration.fig_clip_yaxis"),
                height=300,
            )
            _clip_colors = ['#1f77b4', '#ff7f0e', '#2ca02c']
            for clip_ts, clip_data, n in clip_channels:
                fig_clip.add_trace(go.Scattergl(
                    x=clip_ts, y=clip_data,
                    mode='lines', name=t("vibration.clip_trace", n=n),
                    line=dict(color=_clip_colors[n % len(_clip_colors)],
                              width=1),
                ))
            figures.append(fig_clip)

        # -----------------------------------------------------------
        # 6. Optional: FFT spectrum from IMU data
        # -----------------------------------------------------------
        if self.reader.has_message("IMU"):
            fft_figs = self._fft_spectrum()
            figures.extend(fft_figs)

        # Evidence source — vibration thresholds have strong authority
        if status == "critical":
            evidence_source = t("vibration.evidence_critical")
        elif status == "warning":
            evidence_source = t("vibration.evidence_warning")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.vibration"),
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _clips_while_armed(self, ts: np.ndarray, clip: np.ndarray) -> int:
        """How much the clip counter grew during armed flight.

        The counter runs from boot, so its value holds every bump from
        transport and handling, which say nothing about the flight. Each
        armed window counts its own growth. A log with no arm events is
        one window.
        """
        windows = (self.reader.get_armed_intervals()
                   or [(float(ts[0]), float(ts[-1]))])
        grown = 0.0
        for start, end in windows:
            counts = clip[(ts >= start) & (ts <= end)]
            counts = counts[np.isfinite(counts)]
            if len(counts) > 1:
                grown += float(counts[-1] - counts[0])
        return int(grown)

    def _get_imu_data_and_rate(self):
        """Load IMU data and compute sample rate. Returns (imu, dt, fs) or None.

        Primary-instance only: interleaved multi-IMU samples would corrupt
        the dt/rate estimate and the FFT (see BaseAnalyzer.read_primary_imu).
        """
        imu = self.read_primary_imu([
            "AccX", "AccY", "AccZ", "GyrX", "GyrY", "GyrZ",
        ])
        ts = imu["_timestamp"]
        if len(ts) < 256:
            return None

        dt_arr = np.diff(ts)
        dt_arr = dt_arr[dt_arr > 0]
        if len(dt_arr) == 0:
            return None

        dt_median = float(np.median(dt_arr))
        if dt_median <= 0 or (1.0 / dt_median) < 100:
            return None

        return imu, dt_median, 1.0 / dt_median

    def _find_flight_window(self, ts: np.ndarray, fs: float):
        """Thin wrapper around lib.fft_helpers.find_flight_window.

        Reads the primary-instance GyrX trace — the SAME instance filter
        as _get_imu_data_and_rate — so the returned indices align with
        that method's arrays.
        """
        imu = self.read_primary_imu(["GyrX"])
        return find_flight_window(imu["GyrX"], fs)

    def _do_fft(self, data: np.ndarray, dt: float):
        return do_fft(data, dt)

    def _find_peaks(self, freqs, amps, min_freq=3.0, max_freq=None,
                    n_peaks=3, min_prominence_ratio=0.15):
        return find_peaks(
            freqs, amps,
            min_freq=min_freq, max_freq=max_freq,
            n_peaks=n_peaks,
            min_prominence_ratio=min_prominence_ratio,
        )

    def _fft_spectrum(self) -> list[go.Figure]:
        """Compute and plot FFT spectrum from IMU gyro and accel data.

        Improvements over original:
        - Analyzes both gyroscope AND accelerometer
        - Auto-detects flight/hover phase (highest gyro activity)
        - Uses full flight window (up to 60s) instead of fixed 8192 samples
        - Marks detected peaks with annotations
        """
        result = self._get_imu_data_and_rate()
        if result is None:
            return []
        imu, dt_median, sample_rate = result
        ts = imu["_timestamp"]

        # Find flight window with highest activity
        start_idx, end_idx = self._find_flight_window(ts, sample_rate)
        fft_n = end_idx - start_idx
        window_sec = fft_n * dt_median

        figures = []

        # --- Gyroscope FFT ---
        fig_gyr = self.make_figure(
            title=t("vibration.fig_gyro_fft_title",
                    sec=f"{window_sec:.0f}", rate=f"{sample_rate:.0f}"),
            xaxis_title=t("vibration.freq_hz"),
            yaxis_title=t("vibration.amp_rads"),
            height=400,
        )

        gyr_peaks_all = []
        for axis_name, key, color in [
            (t("vibration.gyrx_label"), "GyrX", "#ff4444"),
            (t("vibration.gyry_label"), "GyrY", "#44aa44"),
            (t("vibration.gyrz_label"), "GyrZ", "#4488ff"),
        ]:
            segment = imu[key][start_idx:end_idx]
            freqs, amps = self._do_fft(segment, dt_median)

            fig_gyr.add_trace(go.Scattergl(
                x=freqs, y=amps,
                mode='lines', name=axis_name,
                line=dict(color=color, width=1.2),
            ))

            peaks = self._find_peaks(freqs, amps, max_freq=sample_rate / 2)
            for freq, amp in peaks:
                gyr_peaks_all.append((freq, amp, axis_name))

        # Annotate top peaks
        seen_freqs = set()
        for freq, amp, axis in sorted(gyr_peaks_all, key=lambda x: x[1],
                                       reverse=True):
            if not any(abs(freq - sf) < 3.0 for sf in seen_freqs):
                fig_gyr.add_vline(
                    x=freq, line_dash="dash", line_color="yellow",
                    line_width=1, opacity=0.7,
                    annotation_text=f"{freq:.1f} Hz",
                    annotation_font_size=10,
                )
                seen_freqs.add(freq)
            if len(seen_freqs) >= 3:
                break

        figures.append(fig_gyr)

        # --- Accelerometer FFT ---
        fig_acc = self.make_figure(
            title=t("vibration.fig_acc_fft_title",
                    sec=f"{window_sec:.0f}", rate=f"{sample_rate:.0f}"),
            xaxis_title=t("vibration.freq_hz"),
            yaxis_title=t("vibration.amp_ms2"),
            height=400,
        )

        acc_peaks_all = []
        for axis_name, key, color in [
            (t("vibration.accx_label"), "AccX", "#ff4444"),
            (t("vibration.accy_label"), "AccY", "#44aa44"),
            (t("vibration.accz_label"), "AccZ", "#4488ff"),
        ]:
            segment = imu[key][start_idx:end_idx]
            freqs, amps = self._do_fft(segment, dt_median)

            fig_acc.add_trace(go.Scattergl(
                x=freqs, y=amps,
                mode='lines', name=axis_name,
                line=dict(color=color, width=1.2),
            ))

            peaks = self._find_peaks(freqs, amps, max_freq=sample_rate / 2)
            for freq, amp in peaks:
                acc_peaks_all.append((freq, amp, axis_name))

        seen_freqs = set()
        for freq, amp, axis in sorted(acc_peaks_all, key=lambda x: x[1],
                                       reverse=True):
            if not any(abs(freq - sf) < 3.0 for sf in seen_freqs):
                fig_acc.add_vline(
                    x=freq, line_dash="dash", line_color="yellow",
                    line_width=1, opacity=0.7,
                    annotation_text=f"{freq:.1f} Hz",
                    annotation_font_size=10,
                )
                seen_freqs.add(freq)
            if len(seen_freqs) >= 3:
                break

        figures.append(fig_acc)

        return figures
