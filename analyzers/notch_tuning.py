"""
Notch Filter Tuning Advisor: FFT-based vibration frequency detection,
hover throttle calibration check, and parameter recommendations.

Analyzes batch-sampled IMU data (ISBH/ISBD) or regular IMU data to
identify motor vibration frequencies, then generates notch filter
parameter recommendations.  Also checks Q_M_THST_HOVER accuracy and
attitude stability.

Required: RCOU
Optional: ISBH, ISBD, IMU, VIBE, ATT, RATE, CTUN, QTUN, ESC, PARM

Title: 调参建议
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.hover import airborne, hover_thrust
from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer


# How far a batch's measured rate can sit from the median and still share its
# frequency axis. 2% puts a 138 Hz peak under 3 Hz off.
_BATCH_RATE_TOLERANCE = 0.02

# INS_HNTCH_OPTS bit 4. The wiki no longer recommends the double notch, whose
# deepest cut sits either side of the centre. In SITL it left 0.18 deg/s of a
# 5 deg/s peak, single notch 0.17, triple 0.12.
_OPTS_TRIPLE_NOTCH = 16

# INS_HNTCH_HMNCS, the firmware default. With a 2nd harmonic in SITL this left
# 0.12 deg/s against 0.87 for the 1st alone, and without one it cost nothing.
# A filtered spectrum understates the harmonic, so it cannot be told from there.
_HMNCS_FIRST_AND_SECOND = "3"

# Rows of level hover it takes to call their median the hover throttle.
_HOVER_MIN_ROWS = 100

# The share of in-flight rows on which every ESC has to report RPM before its
# telemetry is offered as the notch source. The wiki ranks ESC telemetry first
# only where it is reliable.
_ESC_MIN_REPORTING = 0.30

# INS_HNTCH_FREQ under ESC tracking is the lowest motor frequency the notch
# follows: below it the centre stays put and the attenuation fades
# (HarmonicNotchFilter.cpp, set_center_frequency). The wiki sets it below the
# hover frequency. This percentile of what the ESCs report in flight is 0.83
# of their median on 172 corpus logs, 86 to 234 Hz. Flown through climbs and
# descents it left 0.41 deg/s of rate error, against 0.93 with FREQ at the
# spectrum's peak.
_ESC_LOW_PERCENTILE = 5

# How far the spectrum's peak may sit from a multiple of the ESC frequency
# and still be called that harmonic. It decides wording only.
_HARMONIC_TOLERANCE = 0.12


class NotchTuningAnalyzer(BaseAnalyzer):
    """
    Notch filter and hover throttle tuning advisor.

    Generates:
      - FFT spectrum with detected peak frequency
      - Notch filter coverage overlay
      - Hover throttle calibration check
      - Attitude oscillation assessment
      - Complete parameter recommendation table
    """

    registry_key = "notch_tuning"
    sort_order = 45  # after vibration (40), before pid_tuning (50)
    required_messages = ["RCOU"]
    optional_messages = ["ISBH", "ISBD", "IMU", "VIBE", "ATT", "RATE",
                         "CTUN", "QTUN", "ESC"]

    def analyze(self) -> AnalysisResult:
        details: list[str] = []
        figures: list[go.Figure] = []
        recommendations: list[dict] = []
        status = "good"

        # Motor noise takes motors. The wiki measures it on a flight
        # (common-imu-batchsampling), and a bench log has only sensor noise.
        if self.reader.never_armed():
            return self.make_result(
                title=t("analyzer.notch_tuning"),
                status="info",
                summary=t("common.never_armed"),
            )

        # Detect Q-prefix: QuadPlane uses "Q_", Copter uses ""
        is_quadplane = self.reader.get_param("Q_ENABLE", 0) >= 1
        q_prefix = "Q_" if is_quadplane else ""

        # Pre-compute shared state
        self._measured_peak_bw = None
        self._recommended_opts = 0
        self._actual_hover_throttle = self._compute_hover_throttle(q_prefix)
        self._armed_intervals = self.reader.get_armed_intervals()
        self._gyro_spectrum: tuple | None = None
        # Set when a peak calls for a notch and the log cannot set one up.
        self._no_notch_source = False

        # -----------------------------------------------------------
        # 1. Hover throttle check
        # -----------------------------------------------------------
        thr_status, thr_details, thr_recs = self._check_hover_throttle(
            q_prefix)
        status = self.worst_status(status, thr_status)
        details.extend(thr_details)
        recommendations.extend(thr_recs)

        # -----------------------------------------------------------
        # 2. FFT analysis -> peak frequency
        # -----------------------------------------------------------
        peak_freq, fft_figs, fft_details = self._fft_analysis()
        figures.extend(fft_figs)
        details.extend(fft_details)

        # -----------------------------------------------------------
        # 3. Notch filter check & recommendations
        # -----------------------------------------------------------
        notch_status, notch_details, notch_recs = self._check_notch_filter(
            q_prefix, peak_freq)
        status = self.worst_status(status, notch_status)
        details.extend(notch_details)
        recommendations.extend(notch_recs)

        # -----------------------------------------------------------
        # 4. Attitude stability check
        # -----------------------------------------------------------
        att_status, att_details = self._check_attitude_stability()
        status = self.worst_status(status, att_status)
        details.extend(att_details)

        # -----------------------------------------------------------
        # 5. Build recommendation table + .param text
        # -----------------------------------------------------------
        if recommendations:
            fig_table = self._build_recommendation_table(recommendations)
            figures.append(fig_table)
            param_text = self._build_param_text(recommendations)
            details.append(param_text)

        # -----------------------------------------------------------
        # 6. Summary + action level.
        # -----------------------------------------------------------
        n_recs = len(recommendations)
        if n_recs > 0 and status == "critical":
            summary = t("notch_tuning.summary_critical", n=n_recs)
            plain_summary = t("notch_tuning.critical_plain", n=n_recs)
            action_text = t("notch_tuning.critical_action")
            action_level = "apply_param"
        elif n_recs > 0:
            summary = t("notch_tuning.summary_suggest", n=n_recs)
            plain_summary = t("notch_tuning.suggest_plain", n=n_recs)
            action_text = t("notch_tuning.suggest_action")
            action_level = "apply_param"
        elif status in ("warning", "critical"):
            # A sub-check flagged an issue but produced no auto-adjustable
            # parameter (e.g. attitude oscillation with no usable notch
            # source, or HNTC2 already enabled). Do NOT claim PASS — that
            # contradicted the status badge and dropped the card from the
            # executive action items.
            summary = t("notch_tuning.summary_issue")
            plain_summary = t("notch_tuning.issue_plain")
            action_text = t("notch_tuning.issue_action")
            action_level = "physical_check"
        elif self._no_notch_source:
            # A peak with nothing to set a notch from is not a pass.
            summary = t("notch_tuning.summary_no_source")
            plain_summary = t("notch_tuning.no_source_plain")
            action_text = ""
            action_level = "ok"
        elif self._gyro_spectrum is None:
            # Nor is a log with no spectrum from a flight: a ground run,
            # or no fast gyro data. "Configured correctly" was said of an
            # aircraft that shook on the ground and never took off.
            status = self.worst_status(status, "info")
            summary = t("notch_tuning.summary_no_spectrum")
            plain_summary = t("notch_tuning.no_spectrum_plain")
            action_text = ""
            action_level = "ok"
        else:
            summary = t("notch_tuning.summary_pass")
            plain_summary = t("notch_tuning.pass_plain")
            action_text = ""
            action_level = "ok"

        # Convert internal recommendations to unified param_suggestions.
        def _to_num(val):
            try:
                return float(val)
            except (ValueError, TypeError):
                return val

        suggestions = []
        for r in recommendations:
            if r.get("new") not in (None, "-", ""):
                suggestions.append({
                    "param": r["param"],
                    "current": _to_num(r.get("old", "")),
                    "suggested": _to_num(r["new"]),
                    "reason": r.get("reason", ""),
                })

        # Evidence source
        if status in ("warning", "critical"):
            evidence_source = t("notch_tuning.evidence_warn")
        elif status == "info":
            evidence_source = t("notch_tuning.evidence_info")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.notch_tuning"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            param_suggestions=suggestions,
            evidence_source=evidence_source,
        )

    # ==================================================================
    # Hover throttle analysis
    # ==================================================================

    def _check_hover_throttle(self, q_prefix: str):
        """Compare configured THST_HOVER with actual hover throttle."""
        details = []
        recs = []
        status = "good"

        hover_param = (f"{q_prefix}M_THST_HOVER" if q_prefix
                        else "MOT_THST_HOVER")
        configured = self.reader.get_param(hover_param, 0.35)
        actual_throttle = self._actual_hover_throttle

        if actual_throttle < 0:
            # Not applicable (pure fixed-wing has no hover throttle).
            return status, details, recs
        if actual_throttle <= 0:
            details.append(t("notch_tuning.hover_too_short"))
            return status, details, recs

        details.append(
            t("notch_tuning.hover_compare",
              configured=f"{configured:.1%}",
              actual=f"{actual_throttle:.1%}")
        )

        error = abs(actual_throttle - configured)
        crit = self.thresholds.notch_hover_error_crit
        warn = self.thresholds.notch_hover_error_warn
        if error > crit:
            status = "critical"
            new_val = round(actual_throttle, 2)
            details.append(
                t("notch_tuning.hover_crit",
                  error=f"{error:.0%}", crit=f"{crit:.0%}",
                  configured=f"{configured:.0%}",
                  actual=f"{actual_throttle:.0%}")
            )
            recs.append({
                "param": hover_param,
                "old": f"{configured:.3f}",
                "new": f"{new_val:.2f}",
                "reason": t("notch_tuning.hover_rec_reason",
                            actual=f"{actual_throttle:.0%}"),
                "priority": t("notch_tuning.priority_high"),
            })
        elif error > warn:
            status = "warning"
            details.append(
                t("notch_tuning.hover_warn", error=f"{error:.0%}")
            )

        return status, details, recs

    # ==================================================================
    # FFT analysis
    # ==================================================================

    def _fft_analysis(self):
        """Perform FFT using ISBH/ISBD batch data or IMU fallback."""
        details = []
        figures = []
        peak_freq = None

        if self.reader.has_message("ISBH") and self.reader.has_message("ISBD"):
            peak_freq, figs, dets = self._fft_from_batch()
            figures.extend(figs)
            details.extend(dets)
        elif self.reader.has_message("IMU"):
            peak_freq, figs, dets = self._fft_from_imu()
            figures.extend(figs)
            details.extend(dets)
        else:
            details.append(t("notch_tuning.no_fft_data"))

        return peak_freq, figures, details

    def _fft_from_batch(self):
        """FFT from ISBH/ISBD batch-sampled data (highest fidelity)."""
        details = []
        batches = self.reader.isb_batches

        # Gyro batches taken while the motors carry the aircraft. ArduPilot's
        # IMU_SENSOR_TYPE enum is ACCEL=0, GYRO=1; the harmonic notch
        # (INS_HNTCH_*) filters the gyro signal, so we measure its effect
        # on gyro batches (type==1), not accel (type==0).
        gyro = {k: v for k, v in batches.items()
                if v["type"] == 1 and len(v["x"]) >= 32}
        keys = sorted(gyro)
        carried = self._carried(
            np.array([gyro[k]["time_s"] for k in keys], dtype=float))
        gyro_batches = {k: gyro[k] for k, kept in zip(keys, carried) if kept}

        if not gyro_batches:
            details.append(t("notch_tuning.no_gyro_batch"))
            return None, [], details

        # FFT each batch, grouped by length. Batches can differ in length or
        # rate, and one short batch must not decide the frequency axis:
        # averaging by bin index would mix frequencies, and trimming to the
        # shortest batch would cut the spectrum off below the real peak.
        spectra: dict[int, list] = {}
        for bid, b in sorted(gyro_batches.items()):
            smp_rate = b["smp_rate"]
            # BatchSampler stores multiplier * value, as int16, so widen
            # first or the arithmetic wraps.
            scale = b["mul"] or 1
            smp_cnt = b["smp_cnt"]
            x = np.array(b["x"][:smp_cnt], dtype=np.float64) / scale
            y = np.array(b["y"][:smp_cnt], dtype=np.float64) / scale
            z = np.array(b["z"][:smp_cnt], dtype=np.float64) / scale

            if len(x) < 64:
                continue

            N = len(x)
            window = np.hanning(N)
            amps = []
            for data in (x, y, z):
                data_c = data - np.mean(data)
                fft_vals = np.fft.rfft(data_c * window)
                amps.append(2.0 * np.abs(fft_vals[1:]) / np.sum(window))
            spectra.setdefault(N, []).append((smp_rate, amps))

        if not spectra:
            return None, [], [t("notch_tuning.fft_failed")]

        # Keep the length most batches share, the longer one on a tie. Each
        # batch logs its measured rate, so no two agree exactly: a log of 273
        # batches holds 271 rates, up to 1.3% apart. Same rate means close.
        n_samples, group = max(
            spectra.items(), key=lambda kv: (len(kv[1]), kv[0]))
        smp_rate = float(np.median([rate for rate, _ in group]))
        same_rate = [amps for rate, amps in group
                     if abs(rate - smp_rate) <= _BATCH_RATE_TOLERANCE * smp_rate]
        freqs = np.fft.rfftfreq(n_samples, 1.0 / smp_rate)[1:]  # skip DC
        avg_x, avg_y, avg_z = np.mean(same_rate, axis=0)

        # Cache spectrum for _measure_notch_attenuation
        self._gyro_spectrum = (freqs, avg_x, avg_y, avg_z)

        # Find peaks
        peak_freq = self._find_dominant_peak(freqs, avg_x, avg_y, avg_z)

        # Measure -3 dB peak width
        if peak_freq is not None:
            mask = (freqs >= 25.0) & (freqs <= 500.0)
            combined = avg_x[mask] ** 2 + avg_y[mask] ** 2 + avg_z[mask] ** 2
            self._measured_peak_bw = self._measure_peak_width(
                freqs[mask], combined)
            if self._measured_peak_bw is not None:
                self._recommended_opts = (0 if self._measured_peak_bw < 5
                                          else _OPTS_TRIPLE_NOTCH)

        if peak_freq is not None:
            bw_desc = ""
            if self._measured_peak_bw is not None:
                bw_desc = t("notch_tuning.peak_bw",
                            bw=f"{self._measured_peak_bw:.1f}")
            details.append(
                t("notch_tuning.fft_peak_batch",
                  freq=f"{peak_freq:.0f}",
                  n=len(group), bw=bw_desc)
            )
        else:
            details.append(t("notch_tuning.no_peak"))

        # Build FFT figure
        fig = self._build_fft_figure(freqs, avg_x, avg_y, avg_z,
                                     peak_freq,
                                     t("notch_tuning.sensor_gyro"),
                                     t("notch_tuning.source_batch",
                                       n=len(group)))
        return peak_freq, [fig], details

    def _fft_from_imu(self):
        """Fallback FFT from regular IMU log data.

        Primary-instance only: interleaved multi-IMU samples would inflate
        the estimated rate and add ghost peaks (see read_primary_imu).
        """
        details = []
        imu = self.read_primary_imu(["GyrX", "GyrY", "GyrZ"])
        ts = imu["_timestamp"]

        if len(ts) < 256:
            return None, [], [t("notch_tuning.imu_too_few")]

        dt_arr = np.diff(ts)
        dt_arr = dt_arr[dt_arr > 0]
        dt = float(np.median(dt_arr))
        fs = 1.0 / dt

        if fs < 100:
            return None, [], [t("notch_tuning.imu_rate_low",
                                 rate=f"{fs:.0f}")]

        # Keep the rows where the motors carry the aircraft
        carried = self._carried(ts)
        if not np.all(carried):
            if np.sum(carried) < 256:
                return None, [], [t("notch_tuning.imu_too_few")]
            ts = ts[carried]
            imu["GyrX"] = imu["GyrX"][carried]
            imu["GyrY"] = imu["GyrY"][carried]
            imu["GyrZ"] = imu["GyrZ"][carried]

        # Use up to 60 seconds from the middle
        n_samples = min(len(ts), int(60 * fs))
        start = (len(ts) - n_samples) // 2
        end = start + n_samples

        gyr_x = imu["GyrX"][start:end]
        gyr_y = imu["GyrY"][start:end]
        gyr_z = imu["GyrZ"][start:end]

        window = np.hanning(len(gyr_x))
        freqs = np.fft.rfftfreq(len(gyr_x), dt)[1:]

        amps = []
        for data in [gyr_x, gyr_y, gyr_z]:
            data_c = data - np.nanmean(data)
            data_c = np.nan_to_num(data_c)
            fft_vals = np.fft.rfft(data_c * window)
            amps.append(2.0 * np.abs(fft_vals[1:]) / np.sum(window))

        # Cache spectrum
        self._gyro_spectrum = (freqs, amps[0], amps[1], amps[2])

        peak_freq = self._find_dominant_peak(freqs, *amps)

        # Measure -3 dB peak width
        if peak_freq is not None:
            mask = (freqs >= 25.0) & (freqs <= 500.0)
            combined = (amps[0][mask] ** 2 + amps[1][mask] ** 2
                        + amps[2][mask] ** 2)
            self._measured_peak_bw = self._measure_peak_width(
                freqs[mask], combined)
            if self._measured_peak_bw is not None:
                self._recommended_opts = (0 if self._measured_peak_bw < 5
                                          else _OPTS_TRIPLE_NOTCH)

        if peak_freq is not None:
            bw_desc = ""
            if self._measured_peak_bw is not None:
                bw_desc = t("notch_tuning.peak_bw",
                            bw=f"{self._measured_peak_bw:.1f}")
            details.append(
                t("notch_tuning.fft_peak_imu",
                  freq=f"{peak_freq:.0f}",
                  rate=f"{fs:.0f}", bw=bw_desc)
            )

        fig = self._build_fft_figure(freqs, amps[0], amps[1], amps[2],
                                     peak_freq,
                                     t("notch_tuning.sensor_gyro"),
                                     t("notch_tuning.source_imu",
                                       rate=f"{fs:.0f}"))
        return peak_freq, [fig], details

    def _find_dominant_peak(self, freqs, amps_x, amps_y, amps_z,
                            min_freq=25.0, max_freq=500.0):
        """Find the single dominant vibration peak across all axes."""
        mask = (freqs >= min_freq) & (freqs <= max_freq)
        if not np.any(mask):
            return None

        f = freqs[mask]
        combined = (amps_x[mask] ** 2 + amps_y[mask] ** 2
                    + amps_z[mask] ** 2)

        peaks = []
        threshold = np.max(combined) * 0.15
        for i in range(1, len(combined) - 1):
            if (combined[i] > combined[i - 1]
                    and combined[i] > combined[i + 1]
                    and combined[i] > threshold):
                peaks.append((float(f[i]), float(combined[i])))

        if not peaks:
            return None

        peaks.sort(key=lambda p: p[1], reverse=True)
        peak_freq, peak_power = peaks[0]

        # The wiki looks for "a significant peak in noise". One that stands
        # less far above the floor than the least attenuation worth having
        # leaves a notch nothing to remove. With the motors at idle this is
        # the top of a floor that rises to the end of the band, near 490 Hz.
        # Against ESC RPM on 276 corpus logs, this and the armed filter took
        # away 7 suggestions, none within 10% of the motor frequency.
        floor = float(np.median(combined))
        if (floor > 0 and 10.0 * np.log10(peak_power / floor)
                < self.thresholds.notch_atten_crit):
            return None
        return peak_freq

    def _build_fft_figure(self, freqs, amps_x, amps_y, amps_z,
                          peak_freq, sensor_name, source_desc):
        """Build an FFT spectrum figure with optional notch overlay."""
        fig = self.make_figure(
            title=t("notch_tuning.fig_fft_title",
                    sensor=sensor_name, source=source_desc),
            xaxis_title=t("notch_tuning.freq_hz"),
            yaxis_title=t("notch_tuning.amplitude"),
            height=420,
        )

        for name, amps, color in [
            (t("notch_tuning.axis_x"), amps_x, "#ff4444"),
            (t("notch_tuning.axis_y"), amps_y, "#44aa44"),
            (t("notch_tuning.axis_z"), amps_z, "#4488ff"),
        ]:
            fig.add_trace(go.Scattergl(
                x=freqs, y=amps,
                mode="lines", name=name,
                line=dict(color=color, width=1.2),
            ))

        if peak_freq is not None:
            fig.add_vline(
                x=peak_freq, line_dash="solid", line_color="#ff0000",
                line_width=2, opacity=0.8,
                annotation_text=t("notch_tuning.peak_label",
                                   freq=f"{peak_freq:.0f}"),
                annotation_font_size=12,
                annotation_font_color="#ff0000",
            )

            bw = self._calc_notch_bw(peak_freq)
            fig.add_vrect(
                x0=peak_freq - bw / 2, x1=peak_freq + bw / 2,
                fillcolor="rgba(255, 0, 0, 0.08)",
                line_width=0,
                annotation_text=t("notch_tuning.notch_coverage",
                                   lo=f"{peak_freq - bw/2:.0f}",
                                   hi=f"{peak_freq + bw/2:.0f}"),
                annotation_position="top right",
                annotation_font_size=10,
                annotation_font_color="rgba(200,0,0,0.7)",
            )

        x_max = min(float(freqs[-1]), 500)
        fig.update_xaxes(range=[0, x_max])

        return fig

    # ==================================================================
    # Notch filter check
    # ==================================================================

    def _check_notch_filter(self, q_prefix, peak_freq):
        """Check if notch filter is configured and recommend settings."""
        details = []
        recs = []
        status = "good"

        enabled = self.reader.get_param("INS_HNTCH_ENABLE", 0)
        configured_freq = self.reader.get_param("INS_HNTCH_FREQ", 80)
        configured_bw = self.reader.get_param("INS_HNTCH_BW", 40)
        configured_mode = self.reader.get_param("INS_HNTCH_MODE", 0)
        configured_ref = self.reader.get_param("INS_HNTCH_REF", 0)

        if enabled >= 1:
            mode_names = {
                0: t("notch_tuning.mode_fixed"),
                1: t("notch_tuning.mode_throttle"),
                2: t("notch_tuning.mode_rpm"),
                3: t("notch_tuning.mode_esc"),
                4: t("notch_tuning.mode_fft"),
                5: t("notch_tuning.mode_rpm"),
            }
            mode_int = int(configured_mode)
            mode_name = mode_names.get(mode_int,
                                       t("notch_tuning.mode_unknown",
                                         val=mode_int))
            details.append(
                t("notch_tuning.notch_enabled",
                  freq=f"{configured_freq:.0f}",
                  bw=f"{configured_bw:.0f}",
                  mode=mode_name)
            )

            # A reference of zero switches tracking off in every mode
            # (AP_Vehicle::update_dynamic_notch): the notch sits at FREQ.
            tracking = mode_int != 0 and configured_ref != 0
            if mode_int != 0 and not tracking:
                status = self.worst_status(status, "warning")
                details.append(
                    t("notch_tuning.ref_zero_static", mode=mode_name))
                if mode_int in (2, 3, 5):
                    recs.append({
                        "param": "INS_HNTCH_REF",
                        "old": f"{configured_ref:.2f}",
                        "new": "1",
                        "reason": t("notch_tuning.ref_esc_reason"),
                        "priority": t("notch_tuning.priority_high"),
                    })

            if tracking:
                details.append(t("notch_tuning.atten_tracking"))
            else:
                self._report_attenuation(
                    details, configured_freq, configured_bw, peak_freq)

            # Check BW coverage
            bw_status, bw_dets, bw_recs = self._check_notch_bw_coverage(
                configured_freq, configured_bw)
            if bw_status != "good":
                status = self.worst_status(status, bw_status)
            details.extend(bw_dets)
            recs.extend(bw_recs)

            # --- Frequency comparison logic depends on MODE ---
            if mode_int in (2, 3, 4, 5):
                # Without tracking the reference above is the whole fix:
                # with it FREQ turns from the centre into a lower limit.
                if tracking:
                    dynamic_desc = {
                        2: t("notch_tuning.dynamic_rpm"),
                        3: t("notch_tuning.dynamic_esc"),
                        4: t("notch_tuning.dynamic_fft"),
                        5: t("notch_tuning.dynamic_rpm"),
                    }
                    details.append(
                        t("notch_tuning.dynamic_mode",
                          desc=dynamic_desc[mode_int])
                    )
                if tracking and mode_int == 3:
                    self._check_esc_floor(
                        details, recs, configured_freq, configured_bw)
                    if any(r["param"] == "INS_HNTCH_FREQ" for r in recs):
                        status = self.worst_status(status, "warning")
                if tracking and mode_int == 4 and peak_freq:
                    if configured_freq > peak_freq:
                        status = "warning"
                        details.append(
                            t("notch_tuning.fft_floor_high",
                              freq=f"{configured_freq:.0f}",
                              peak=f"{peak_freq:.0f}")
                        )
                        recs.append({
                            "param": "INS_HNTCH_FREQ",
                            "old": f"{configured_freq:.0f}",
                            "new": f"{max(10, int(peak_freq * 0.5)):.0f}",
                            "reason": t("notch_tuning.lower_floor_reason"),
                            "priority": t("notch_tuning.priority_mid"),
                        })

            elif peak_freq:
                # MODE 0/1: FREQ is meaningful for comparison
                freq_tol = self.thresholds.notch_freq_drift_hz
                freq_diff = abs(configured_freq - peak_freq)

                same_band = freq_diff <= max(freq_tol, peak_freq * 0.5)

                if same_band and freq_diff > freq_tol:
                    status = "warning"
                    details.append(
                        t("notch_tuning.freq_drift",
                          freq=f"{configured_freq:.0f}",
                          peak=f"{peak_freq:.0f}",
                          diff=f"{freq_diff:.0f}")
                    )
                    recs.append({
                        "param": "INS_HNTCH_FREQ",
                        "old": f"{configured_freq:.0f}",
                        "new": f"{peak_freq:.0f}",
                        "reason": t("notch_tuning.align_peak_reason"),
                        "priority": t("notch_tuning.priority_high"),
                    })
                elif same_band:
                    details.append(
                        t("notch_tuning.freq_match",
                          diff=f"{freq_diff:.0f}")
                    )
                else:
                    self._suggest_second_notch(
                        details, recs, configured_freq, peak_freq)
                    status = self.worst_status(status, "warning")

                if mode_int == 0 and same_band:
                    self._suggest_tracking(
                        details, recs, configured_freq, configured_bw,
                        configured_ref)

            # Check REF (only for throttle tracking mode). At 0 the notch
            # does not track at all, however near 0 the hover throttle is.
            if mode_int == 1 and self._actual_hover_throttle > 0:
                ref_err = abs(configured_ref - self._actual_hover_throttle)
                if (ref_err > self.thresholds.notch_ref_error
                        or configured_ref == 0):
                    details.append(
                        t("notch_tuning.ref_drift",
                          ref=f"{configured_ref:.2f}",
                          actual=f"{self._actual_hover_throttle:.2f}",
                          err=f"{ref_err:.0%}")
                    )
                    recs.append({
                        "param": "INS_HNTCH_REF",
                        "old": f"{configured_ref:.2f}",
                        "new": f"{self._actual_hover_throttle:.2f}",
                        "reason": t("notch_tuning.ref_match_reason"),
                        "priority": t("notch_tuning.priority_mid"),
                    })

        elif peak_freq:
            # ENABLE=0: not enabled at all.
            if self._is_vibration_problematic():
                status = "warning"
                details.append(
                    t("notch_tuning.notch_disabled_warn",
                      freq=f"{peak_freq:.0f}")
                )
            else:
                status = self.worst_status(status, "info")
                details.append(
                    t("notch_tuning.notch_disabled_info",
                      freq=f"{peak_freq:.0f}")
                )
            rows = self._notch_set("INS_HNTCH", peak_freq, details)
            recs.extend(rows)
            self._no_notch_source = not rows
        else:
            details.append(t("notch_tuning.notch_disabled_no_fft"))

        # PID FLTD consistency check
        if peak_freq:
            for axis, param_suffix in [("Roll", "RLL"), ("Pitch", "PIT")]:
                fltd_param = (f"{q_prefix}A_RAT_{param_suffix}_FLTD"
                              if q_prefix
                              else f"ATC_RAT_{param_suffix}_FLTD")
                fltd = self.reader.get_param(fltd_param, 0)
                if fltd > 0 and fltd > peak_freq * 0.8:
                    details.append(
                        t("notch_tuning.fltd_high",
                          axis=axis, param=fltd_param,
                          fltd=f"{fltd:.0f}",
                          peak=f"{peak_freq:.0f}")
                    )

        # FLTD = INS_GYRO_FILTER/2 is the wiki's starting value (Setting Up
        # for Tuning). Only the high side is a suggestion: 20 to 10 Hz cut
        # D-term noise 45% in SITL with tracking unchanged, but raising it
        # doubled D-term noise in all three scenarios, with a notch or without.
        gyro_filter = self.reader.get_param("INS_GYRO_FILTER", 0)
        if gyro_filter and gyro_filter > 0:
            recommended_fltd = round(gyro_filter / 2.0, 1)
            for axis, param_suffix in [("Roll", "RLL"), ("Pitch", "PIT")]:
                fltd_param = (f"{q_prefix}A_RAT_{param_suffix}_FLTD"
                              if q_prefix
                              else f"ATC_RAT_{param_suffix}_FLTD")
                fltd = self.reader.get_param(fltd_param, 0)
                if fltd <= 0:
                    continue
                deviation = abs(fltd - recommended_fltd) / recommended_fltd
                if deviation > 0.2 and fltd < recommended_fltd:
                    details.append(
                        t("notch_tuning.fltd_below_half",
                          axis=axis, param=fltd_param,
                          fltd=f"{fltd:.0f}",
                          rec=f"{recommended_fltd:.0f}")
                    )
                elif deviation > 0.2:
                    details.append(
                        t("notch_tuning.fltd_above_half",
                          axis=axis, param=fltd_param,
                          fltd=f"{fltd:.0f}",
                          rec=f"{recommended_fltd:.0f}")
                    )
                    recs.append({
                        "param": fltd_param,
                        "old": fltd,
                        "new": recommended_fltd,
                        "reason": t("notch_tuning.fltd_formula_reason",
                                    gyro=f"{gyro_filter:.0f}",
                                    div="2",
                                    rec=f"{recommended_fltd:.0f}"),
                        "priority": t("notch_tuning.priority_mid"),
                    })

        return status, details, recs

    # ==================================================================
    # Attitude stability check
    # ==================================================================

    def _check_attitude_stability(self):
        """Check attitude oscillation severity."""
        details = []
        status = "good"

        if not self.reader.has_message("ATT"):
            return status, details

        att = self.reader.get_messages_as_arrays("ATT", [
            "Roll", "Pitch", "DesRoll", "DesPitch",
        ])
        ts = att["_timestamp"]
        if len(ts) < 100:
            return status, details

        # Filter to armed intervals only
        armed = self._armed_intervals
        if armed:
            armed_mask = np.zeros(len(ts), dtype=bool)
            for start, end in armed:
                armed_mask |= (ts >= start) & (ts <= end)
            if np.sum(armed_mask) >= 100:
                ts = ts[armed_mask]
                for key in ["Roll", "Pitch", "DesRoll", "DesPitch"]:
                    att[key] = att[key][armed_mask]

        roll = att["Roll"]
        pitch = att["Pitch"]
        des_roll = att["DesRoll"]
        des_pitch = att["DesPitch"]

        roll_range = float(np.nanmax(roll) - np.nanmin(roll))
        pitch_range = float(np.nanmax(pitch) - np.nanmin(pitch))
        roll_err = float(np.nanmean(np.abs(roll - des_roll)))
        pitch_err = float(np.nanmean(np.abs(pitch - des_pitch)))

        details.append(
            t("notch_tuning.att_stability",
              roll_range=f"{roll_range:.1f}",
              pitch_range=f"{pitch_range:.1f}")
        )
        details.append(
            t("notch_tuning.att_tracking_err",
              roll_err=f"{roll_err:.2f}",
              pitch_err=f"{pitch_err:.2f}")
        )

        # Check RATE for oscillation speed
        max_rate_r = 0.0
        max_rate_p = 0.0
        if self.reader.has_message("RATE"):
            rate = self.reader.get_messages_as_arrays("RATE", ["R", "P"])
            rate_t = rate["_timestamp"]
            if len(rate_t) > 0 and armed:
                rate_mask = np.zeros(len(rate_t), dtype=bool)
                for a_start, a_end in armed:
                    rate_mask |= (rate_t >= a_start) & (rate_t <= a_end)
                if np.sum(rate_mask) > 0:
                    rate["R"] = rate["R"][rate_mask]
                    rate["P"] = rate["P"][rate_mask]
            if len(rate["R"]) > 0:
                max_rate_r = float(np.nanmax(np.abs(rate["R"])))
                max_rate_p = float(np.nanmax(np.abs(rate["P"])))
                details.append(
                    t("notch_tuning.rate_peak",
                      roll=f"{max_rate_r:.1f}",
                      pitch=f"{max_rate_p:.1f}")
                )

        # Assess severity.
        err_crit = self.thresholds.notch_att_err_crit
        err_warn = self.thresholds.notch_att_err_warn
        rate_crit = self.thresholds.notch_rate_max_crit
        max_err = max(roll_err, pitch_err)
        rate_high = (max_rate_r > rate_crit or max_rate_p > rate_crit)
        if max_err > err_crit:
            status = "critical"
            severity_reason = t("notch_tuning.severity_err_crit",
                                err=f"{max_err:.1f}", thresh=err_crit)
        elif max_err > err_warn and rate_high:
            status = "critical"
            severity_reason = t("notch_tuning.severity_err_rate",
                                err=f"{max_err:.1f}",
                                rate=f"{max(max_rate_r, max_rate_p):.0f}")
        elif max_err > err_warn:
            status = "warning"
            severity_reason = t("notch_tuning.severity_err_warn",
                                err=f"{max_err:.1f}", thresh=err_warn)
        else:
            severity_reason = ""

        if severity_reason:
            details.append(
                t("notch_tuning.att_deviation", reason=severity_reason)
            )

        return status, details

    def _suggest_second_notch(self, details, recs, configured_freq,
                              peak_freq):
        """Suggest INS_HNTC2 when HNTCH targets a different band."""
        details.append(
            t("notch_tuning.different_band",
              configured=f"{configured_freq:.0f}",
              peak=f"{peak_freq:.0f}")
        )
        if self.reader.get_param("INS_HNTC2_ENABLE", 0) >= 1:
            details.append(t("notch_tuning.hntc2_already_enabled"))
            return
        notes: list[str] = []
        rows = self._notch_set("INS_HNTC2", peak_freq, notes)
        if rows:
            details.append(t("notch_tuning.suggest_hntc2"))
        details.extend(notes)
        recs.extend(rows)

    def _suggest_tracking(self, details, recs, configured_freq,
                          configured_bw, configured_ref) -> None:
        """Offer a tracking mode for a fixed notch that sits on the motors."""
        esc = self._esc_notch_freq()
        if esc is not None:
            freq, low, median, _high = esc
            details.append(t("notch_tuning.mode0_with_esc"))
            wanted = [
                ("INS_HNTCH_MODE", "0", "3",
                 t("notch_tuning.esc_track_reason")),
                ("INS_HNTCH_REF", f"{configured_ref:.2f}", "1",
                 t("notch_tuning.ref_esc_reason")),
                ("INS_HNTCH_FREQ", f"{configured_freq:.0f}", f"{freq}",
                 t("notch_tuning.freq_esc_reason",
                   low=f"{low:.0f}", mid=f"{median:.0f}")),
                ("INS_HNTCH_BW", f"{configured_bw:.0f}",
                 f"{self._calc_notch_bw(freq)}",
                 t("notch_tuning.bw_esc_reason")),
            ]
            # ESC tracking turns FREQ into a lower limit, so its values
            # replace what the checks on the fixed notch asked for.
            recs[:] = [r for r in recs
                       if r["param"] not in ("INS_HNTCH_FREQ", "INS_HNTCH_BW")]
            for param, old, new, reason in wanted:
                if float(old) != float(new):
                    recs.append({
                        "param": param, "old": old, "new": new,
                        "reason": reason,
                        "priority": t("notch_tuning.priority_mid"),
                    })
        elif self._actual_hover_throttle > 0:
            details.append(t("notch_tuning.mode0_no_esc"))
            recs.append({
                "param": "INS_HNTCH_MODE",
                "old": "0", "new": "1",
                "reason": t("notch_tuning.throttle_track_reason"),
                "priority": t("notch_tuning.priority_mid"),
            })
            recs.append({
                "param": "INS_HNTCH_REF",
                "old": f"{configured_ref:.2f}",
                "new": f"{self._actual_hover_throttle:.2f}",
                "reason": t("notch_tuning.ref_match_reason"),
                "priority": t("notch_tuning.priority_mid"),
            })

    def _check_esc_floor(self, details, recs, configured_freq,
                         configured_bw) -> None:
        """Lower the limit of a notch that cannot follow the motors down.

        Below FREQ the centre stays put and the attenuation fades
        (HarmonicNotchFilter.cpp). Flown through climbs and descents with
        FREQ at the spectrum's peak, 119 Hz, the rate error was 0.93 deg/s,
        and 0.41 with it at the low end of the ESC frequency, 105 Hz.
        """
        esc = self._esc_notch_freq()
        if esc is None or configured_freq <= esc[0]:
            return
        freq, low, median, _high = esc
        details.append(
            t("notch_tuning.esc_floor_high",
              freq=f"{configured_freq:.0f}", low=f"{low:.0f}"))
        # The ratio of the two sets the notch's width at every frequency,
        # and it is the owner's choice: the wiki has 2, or 4 with a notch
        # per motor.
        bw = max(1, int(round(configured_bw * freq / configured_freq)))
        recs[:] = [r for r in recs
                   if r["param"] not in ("INS_HNTCH_FREQ", "INS_HNTCH_BW")]
        recs.append({
            "param": "INS_HNTCH_FREQ",
            "old": f"{configured_freq:.0f}", "new": f"{freq}",
            "reason": t("notch_tuning.freq_esc_reason",
                        low=f"{low:.0f}", mid=f"{median:.0f}"),
            "priority": t("notch_tuning.priority_mid"),
        })
        if bw != int(round(configured_bw)):
            recs.append({
                "param": "INS_HNTCH_BW",
                "old": f"{configured_bw:.0f}", "new": f"{bw}",
                "reason": t("notch_tuning.bw_ratio_reason"),
                "priority": t("notch_tuning.priority_mid"),
            })

    def _notch_set(self, prefix: str, peak_freq: float,
                   details: list[str]) -> list[dict]:
        """Every parameter that sets up a notch that is off, or none.

        ESC telemetry leads where it is reliable, as the wiki ranks it, and
        takes no frequency from the spectrum: the motors give it. Throttle
        tracking takes the hover throttle. A log with neither holds no
        flight to set a notch from, and a fixed-wing only Plane cannot use
        throttle tracking at all (INS_HNTCH_MODE).
        """
        second = prefix != "INS_HNTCH"
        high = t("notch_tuning.priority_high")
        mid = t("notch_tuning.priority_mid")
        esc = self._esc_notch_freq()
        if esc is not None:
            freq, slowest, median, fastest = esc
            details.append(
                self._peak_against_esc(peak_freq, slowest, fastest))
            rows = [
                (f"{prefix}_MODE", "3",
                 t("notch_tuning.esc_track_reason"), high),
                (f"{prefix}_FREQ", f"{freq}",
                 t("notch_tuning.freq_esc_reason",
                   low=f"{slowest:.0f}", mid=f"{median:.0f}"), high),
                (f"{prefix}_BW", f"{self._calc_notch_bw(freq)}",
                 t("notch_tuning.bw_esc_reason"), mid),
                (f"{prefix}_REF", "1",
                 t("notch_tuning.ref_esc_reason"), high),
            ]
        elif self._actual_hover_throttle > 0:
            bw = self._calc_notch_bw(peak_freq)
            rows = [
                (f"{prefix}_MODE", "1",
                 t("notch_tuning.throttle_track_reason"), high),
                (f"{prefix}_FREQ", f"{peak_freq:.0f}",
                 (t("notch_tuning.freq2_reason") if second
                  else t("notch_tuning.freq_reason")), high),
                (f"{prefix}_BW", f"{bw}",
                 t("notch_tuning.bw_reason",
                   lo=f"{peak_freq - bw / 2:.0f}",
                   hi=f"{peak_freq + bw / 2:.0f}"), mid),
                (f"{prefix}_REF", f"{self._actual_hover_throttle:.2f}",
                 t("notch_tuning.ref_reason"), high),
            ]
        else:
            details.append(t("notch_tuning.no_notch_source"))
            return []

        rows.insert(0, (f"{prefix}_ENABLE", "1",
                        (t("notch_tuning.enable2_reason") if second
                         else t("notch_tuning.enable_reason")), high))
        rows.append((f"{prefix}_HMNCS", _HMNCS_FIRST_AND_SECOND,
                     t("notch_tuning.hmncs_default"), mid))
        rows.append((f"{prefix}_OPTS", str(self._recommended_opts),
                     (t("notch_tuning.opts_single")
                      if self._recommended_opts == 0
                      else t("notch_tuning.opts_triple")), mid))
        return [{"param": param,
                 "old": "0" if param.endswith("_ENABLE") else "-",
                 "new": new, "reason": reason, "priority": priority}
                for param, new, reason, priority in rows]

    def _report_attenuation(self, details, configured_freq, configured_bw,
                            peak_freq) -> None:
        """Say what the filtered spectrum shows at a notch that stays put.

        A dip in the floor shows the filter cutting. A notch on a peak
        leaves a smaller peak and no dip, so the lack of one says nothing:
        flown, a notch that took 97.7% off its peak read 2 dB here and was
        called a failure. How much a notch removes takes the spectrum from
        before the filter as well, so nothing here grades it.
        """
        atten = self._measure_notch_attenuation(configured_freq, configured_bw)
        if atten is None:
            return
        method = atten["method"]
        if method == "nyquist_exceeded":
            details.append(
                t("notch_tuning.nyquist_exceeded",
                  freq=f"{configured_freq:.0f}",
                  nyquist=f"{atten['nyquist']:.0f}"))
            return
        if method == "pre_filter":
            details.append(t("notch_tuning.atten_pre_filter"))
            return
        if method == "mixed":
            details.append(
                t("notch_tuning.atten_mixed", opt=atten["options"]))
            return

        atten_db = atten["atten_db"]
        atten_warn = self.thresholds.notch_atten_warn
        atten_converge = self.thresholds.notch_atten_converge
        band_lo, band_hi = atten["in_band_hz"]
        if atten_db >= atten_converge:
            details.append(
                t("notch_tuning.atten_good",
                  db=f"{atten_db:.1f}", converge=atten_converge,
                  method=method))
        elif atten_db >= atten_warn:
            details.append(
                t("notch_tuning.atten_acceptable",
                  db=f"{atten_db:.1f}", warn=atten_warn,
                  converge=atten_converge, method=method))
        elif peak_freq is not None and band_lo <= peak_freq <= band_hi:
            details.append(
                t("notch_tuning.atten_residual",
                  peak=f"{peak_freq:.0f}",
                  lo=f"{band_lo:.0f}", hi=f"{band_hi:.0f}"))
        else:
            # No dip, but no vibration here either: a flat spectrum gives
            # a notch nothing to remove, so it proves nothing.
            details.append(
                t("notch_tuning.atten_no_peak",
                  db=f"{atten_db:.1f}", freq=f"{configured_freq:.0f}"))

    # ==================================================================
    # Notch BW coverage check
    # ==================================================================

    def _check_notch_bw_coverage(self, notch_freq, notch_bw):
        """Check if notch BW covers all peaks near its center frequency."""
        details = []
        recs = []
        status = "good"

        if not self.reader.has_message("RATE"):
            return status, details, recs

        rate = self.reader.get_messages_as_arrays(
            "RATE", ["R", "RDes", "P", "PDes", "Y", "YDes"])
        ts = rate["_timestamp"]
        if len(ts) < 512:
            return status, details, recs

        dt_arr = np.diff(ts)
        dt_arr = dt_arr[dt_arr > 0]
        dt = float(np.median(dt_arr))
        fs = 1.0 / dt
        if fs < 50:
            return status, details, recs

        # Filter to armed
        armed = self._armed_intervals
        roll_err = rate["RDes"] - rate["R"]
        pitch_err = rate["PDes"] - rate["P"]
        yaw_err = rate["YDes"] - rate["Y"]
        if armed:
            mask = np.zeros(len(ts), dtype=bool)
            for a_start, a_end in armed:
                mask |= (ts >= a_start) & (ts <= a_end)
            if np.sum(mask) >= 512:
                roll_err = roll_err[mask]
                pitch_err = pitch_err[mask]
                yaw_err = yaw_err[mask]

        n = min(len(roll_err), int(60 * fs))
        start = (len(roll_err) - n) // 2

        def _fft_amps(data_seg):
            seg = np.nan_to_num(data_seg - np.nanmean(data_seg))
            w = np.hanning(len(seg))
            return 2.0 * np.abs(np.fft.rfft(seg * w)) / np.sum(w)

        amps_r = _fft_amps(roll_err[start:start + n])
        amps_p = _fft_amps(pitch_err[start:start + n])
        amps_y = _fft_amps(yaw_err[start:start + n])
        amps = np.maximum(np.maximum(amps_r, amps_p), amps_y)
        freqs = np.fft.rfftfreq(n, dt)

        search_lo = max(2.0, notch_freq - notch_bw * 2)
        search_hi = notch_freq + notch_bw * 2
        mask_search = (freqs >= search_lo) & (freqs <= search_hi)
        f_s = freqs[mask_search]
        a_s = amps[mask_search]

        if len(a_s) < 5:
            return status, details, recs

        threshold = np.max(a_s) * 0.15
        peaks = []
        for i in range(1, len(a_s) - 1):
            if (a_s[i] > a_s[i - 1] and a_s[i] > a_s[i + 1]
                    and a_s[i] > threshold):
                peaks.append((float(f_s[i]), float(a_s[i])))

        if not peaks:
            return status, details, recs

        bw_lo = notch_freq - notch_bw / 2
        bw_hi = notch_freq + notch_bw / 2
        margin_min = 1.0

        uncovered = []
        for freq, amp in peaks:
            if freq < bw_lo - margin_min or freq > bw_hi + margin_min:
                continue
            margin = min(freq - bw_lo, bw_hi - freq)
            if margin < margin_min:
                uncovered.append((freq, amp, margin))

        if uncovered:
            uncovered.sort(key=lambda x: x[1], reverse=True)
            worst_freq, worst_amp, worst_margin = uncovered[0]

            needed_bw = notch_bw
            for freq, amp, margin in uncovered:
                dist = abs(freq - notch_freq) * 2 + margin_min * 2
                needed_bw = max(needed_bw, int(np.ceil(dist)))

            if worst_margin < 0:
                status = "warning"
                details.append(
                    t("notch_tuning.bw_uncovered",
                      bw=f"{notch_bw:.0f}",
                      freq=f"{worst_freq:.1f}",
                      lo=f"{bw_lo:.0f}", hi=f"{bw_hi:.0f}")
                )
            else:
                status = "warning"
                details.append(
                    t("notch_tuning.bw_tight",
                      bw=f"{notch_bw:.0f}",
                      freq=f"{worst_freq:.1f}",
                      margin=f"{worst_margin:.1f}",
                      lo=f"{bw_lo:.0f}", hi=f"{bw_hi:.0f}")
                )

            recs.append({
                "param": "INS_HNTCH_BW",
                "old": f"{notch_bw:.0f}",
                "new": f"{needed_bw}",
                "reason": t("notch_tuning.bw_rec_reason",
                            freq=f"{worst_freq:.0f}",
                            margin=f"{margin_min:.0f}"),
                "priority": t("notch_tuning.priority_mid"),
            })

        return status, details, recs

    # ==================================================================
    # Helpers
    # ==================================================================

    def _compute_hover_throttle(self, q_prefix: str) -> float:
        """Hover throttle as a 0-1 fraction, 0 if the log has no hover in
        it, -1 if the vehicle has none.

        Only multirotors (Copter, CTUN.ThO) and QuadPlanes (VTOL,
        QTUN.ThO) hover. On a pure fixed-wing Plane, CTUN.ThO is a 0-100
        output percentage with no hover meaning.

        It is the median over level hover, the gate the firmware learns
        the parameter inside. The median over every armed row above 5%
        throttle called a ground run a hover: 16% on an aircraft that
        hovers at 34%, read as a critical error with 0.16 to set. Motor
        PWM does not stand in for a missing throttle message either. It
        reads 0.1 to 0.2 off, where 0.12 is critical.
        """
        if self.reader.is_plane and q_prefix != "Q_":
            return -1.0
        thrust = hover_thrust(self.reader, self.thresholds,
                              min_rows=_HOVER_MIN_ROWS)
        return thrust if thrust is not None else 0.0

    def _is_vibration_problematic(self) -> bool:
        """Check whether actual time-domain vibration warrants a notch filter."""
        if not self.reader.has_message("VIBE"):
            return True
        vibe = self.reader.get_messages_as_arrays(
            "VIBE", ["VibeX", "VibeY", "VibeZ"])
        if len(vibe["_timestamp"]) == 0:
            return True

        ts = vibe["_timestamp"]
        armed = self.reader.get_armed_intervals()
        if armed:
            mask = np.zeros(len(ts), dtype=bool)
            for start, end in armed:
                mask |= (ts >= start) & (ts <= end)
            if np.sum(mask) > 100:
                vx = vibe["VibeX"][mask]
                vy = vibe["VibeY"][mask]
                vz = vibe["VibeZ"][mask]
            else:
                vx, vy, vz = vibe["VibeX"], vibe["VibeY"], vibe["VibeZ"]
        else:
            vx, vy, vz = vibe["VibeX"], vibe["VibeY"], vibe["VibeZ"]

        def _safe_max(arr):
            return float(np.nanmax(arr)) if len(arr) > 0 and not np.all(np.isnan(arr)) else 0.0

        max_vibe = max(_safe_max(vx), _safe_max(vy), _safe_max(vz))
        return max_vibe >= self.thresholds.vibe_warn

    def _flying(self, ts: np.ndarray) -> np.ndarray | None:
        """Rows of ts where the lift motors carry the aircraft in the air,
        None if the log cannot say."""
        return airborne(self.reader, self.thresholds, ts)

    def _carried(self, ts: np.ndarray) -> np.ndarray:
        """Rows of ts to measure motor noise on.

        The wiki measures it in flight, at the hover throttle. Armed alone
        let in the idle before takeoff, whose steady RPM gives the sharpest
        peak in the log, and on a QuadPlane the whole cruise, with the lift
        motors stopped: 31 Hz for an aircraft that hovers at 83. So the
        spectrum takes the rows where the lift motors carry the aircraft in
        the air. A log that does not show its throttle keeps the armed rows.
        """
        flying = self._flying(ts)
        if flying is not None:
            return flying
        inside = np.zeros(len(ts), dtype=bool)
        for start, end in self._armed_intervals:
            inside |= (ts >= start) & (ts <= end)
        return inside if self._armed_intervals else ~inside

    def _esc_flight_hz(self) -> tuple[float, float, float] | None:
        """(low, median, high) motor frequency the ESCs report in flight,
        in Hz.

        None when their telemetry cannot drive a notch. That is judged on
        the flight: over the whole log, a long wait on the ground with RPM
        at zero read as telemetry that drops out.
        """
        if not self.reader.has_message("ESC"):
            return None
        esc = self.reader.get_messages_as_arrays("ESC", ["RPM", "Instance"])
        rpm, instance = esc["RPM"], esc["Instance"]
        flying = self._flying(esc["_timestamp"])
        if flying is None or np.sum(flying) < 100:
            return None
        reporting = flying & (np.nan_to_num(rpm) > 0)
        known = np.isfinite(instance)
        motors = ([flying & (instance == i)
                   for i in np.unique(instance[known])]
                  if np.any(known) else [flying])
        for rows in motors:
            if (np.sum(rows) >= 20
                    and np.mean(reporting[rows]) < _ESC_MIN_REPORTING):
                return None
        if not np.any(reporting):
            return None
        hz = rpm[reporting] / 60.0
        return (float(np.percentile(hz, _ESC_LOW_PERCENTILE)),
                float(np.median(hz)),
                float(np.percentile(hz, 100 - _ESC_LOW_PERCENTILE)))

    def _esc_notch_freq(self) -> tuple[int, float, float, float] | None:
        """(FREQ, low, median, high) in Hz for a notch that follows the ESCs.

        The wiki puts FREQ below the hover frequency and above
        INS_GYRO_FILTER. None if ESC telemetry cannot be used, or the two
        leave no room.
        """
        measured = self._esc_flight_hz()
        if measured is None:
            return None
        low, median, high = measured
        gyro_filter = self.reader.get_param("INS_GYRO_FILTER", 0)
        freq = max(int(np.floor(low)), int(np.floor(gyro_filter)) + 1)
        if freq > median:
            return None
        return freq, low, median, high

    @staticmethod
    def _peak_against_esc(peak_freq: float, low: float, high: float) -> str:
        """Where the spectrum's peak sits against the motors' own frequency.

        The notch follows the motors, so a peak elsewhere stays. It is held
        against the range they flew, not its middle: the spectrum is a mean
        over the flight, and peaks where the motors spent longest.
        """
        peak, lo, hi = f"{peak_freq:.0f}", f"{low:.0f}", f"{high:.0f}"
        for harmonic in (1, 2, 3):
            if (harmonic * low * (1 - _HARMONIC_TOLERANCE) <= peak_freq
                    <= harmonic * high * (1 + _HARMONIC_TOLERANCE)):
                if harmonic == 1:
                    return t("notch_tuning.esc_peak_match",
                             peak=peak, lo=lo, hi=hi)
                text = t("notch_tuning.esc_peak_harmonic",
                         peak=peak, lo=lo, hi=hi, k=harmonic)
                if harmonic == 3:
                    text += t("notch_tuning.esc_third_harmonic")
                return text
        return t("notch_tuning.esc_peak_other", peak=peak, lo=lo, hi=hi)

    def _measure_peak_width(self, freqs, combined_amps):
        """Measure -3 dB width of the dominant peak."""
        if len(freqs) < 5 or len(combined_amps) < 5:
            return None
        peak_idx = int(np.argmax(combined_amps))
        peak_val = combined_amps[peak_idx]
        if peak_val <= 0:
            return None
        # -3 dB in power domain = ×0.5 (input is sum of squared amplitudes)
        threshold = peak_val * 0.5

        left_idx = peak_idx
        for i in range(peak_idx - 1, -1, -1):
            if combined_amps[i] < threshold:
                left_idx = i
                break
        else:
            left_idx = 0

        right_idx = peak_idx
        for i in range(peak_idx + 1, len(combined_amps)):
            if combined_amps[i] < threshold:
                right_idx = i
                break
        else:
            right_idx = len(combined_amps) - 1

        width = float(freqs[right_idx] - freqs[left_idx])
        if width <= 0:
            return None
        return width

    def _calc_notch_bw(self, peak_freq: float) -> int:
        """Half the centre frequency, as the wiki's notch setup pages say.

        Sized from the peak's width instead, the same noise in SITL kept 4
        times the residual peak and 3.6 times the motor output jitter.
        """
        return int(round(peak_freq / 2.0))

    def _measure_notch_attenuation(
        self, configured_freq: float, configured_bw: float,
    ) -> dict | None:
        """Measure notch filter attenuation in the gyro FFT spectrum."""
        if self._gyro_spectrum is None:
            return None

        # Batch samples are pre-filter unless INS_LOG_BAT_OPT says otherwise,
        # and a notch leaves no mark on pre-filter data. Bit 1 logs them
        # post-filter. With bit 2, or with bits 0 and 1 together, the sampler
        # alternates the two, told apart only by an offset on the instance
        # (Write_ISBH), which is not read here. IMU rows are always filtered.
        if self.reader.has_message("ISBD"):
            options = int(self.reader.get_param("INS_LOG_BAT_OPT", 0))
            if options & 4 or (options & 3) == 3:
                return {"method": "mixed", "options": options}
            if not options & 2:
                return {"method": "pre_filter"}

        freqs, ax, ay, az = self._gyro_spectrum

        nyquist = float(freqs[-1]) if len(freqs) > 0 else 0.0
        if configured_freq > nyquist:
            return {
                "method": "nyquist_exceeded",
                "nyquist": nyquist,
                "configured_freq": configured_freq,
            }

        combined = np.sqrt(ax ** 2 + ay ** 2 + az ** 2)

        f_lo = configured_freq - configured_bw / 2.0
        f_hi = configured_freq + configured_bw / 2.0

        in_mask = (freqs >= f_lo) & (freqs <= f_hi)
        ref_upper_mask = (
            (freqs >= f_hi + 0.5 * configured_bw)
            & (freqs <= f_hi + 1.5 * configured_bw)
        )
        ref_lower_mask = (
            (freqs >= f_lo - 1.5 * configured_bw)
            & (freqs <= f_lo - 0.5 * configured_bw)
        )

        in_band = combined[in_mask]
        ref_upper = combined[ref_upper_mask]
        ref_lower = combined[ref_lower_mask]

        if len(in_band) < 3 or (len(ref_upper) < 3 and len(ref_lower) < 3):
            return None

        in_min = float(np.min(in_band))
        if in_min <= 0:
            return None

        ref_samples = []
        if len(ref_upper) >= 3:
            ref_samples.append(float(np.median(ref_upper)))
        if len(ref_lower) >= 3:
            ref_samples.append(float(np.median(ref_lower)))
        ref_median = float(np.mean(ref_samples))
        if ref_median <= 0:
            return None

        atten_db = 20.0 * float(np.log10(ref_median / in_min))

        return {
            "atten_db": atten_db,
            "in_band_min": in_min,
            "ref_median": ref_median,
            "in_band_hz": (f_lo, f_hi),
            "method": "gyro FFT (ISBD batch)"
                      if self.reader.has_message("ISBD")
                      else "gyro FFT (IMU fallback)",
        }

    @staticmethod
    def _build_param_text(recommendations: list[dict]) -> str:
        """Build a copyable .param text block for the report details."""
        param_lines = []
        for r in recommendations:
            if r["new"] != "-":
                param_lines.append(f"{r['param']},{r['new']}")
        body = "\n".join(param_lines)
        return (
            "<br><b>" + t("notch_tuning.param_text_header")
            + f"</b><pre>\n{body}\n</pre>"
        )

    def _build_recommendation_table(self, recommendations):
        """Build a plotly table showing all parameter recommendations."""
        priority_order = {
            t("notch_tuning.priority_high"): 0,
            t("notch_tuning.priority_mid"): 1,
            t("notch_tuning.priority_low"): 2,
        }
        recommendations.sort(
            key=lambda r: priority_order.get(r["priority"], 9))

        fig = go.Figure(data=[go.Table(
            columnwidth=[50, 30, 30, 80, 20],
            header=dict(
                values=[
                    f"<b>{t('notch_tuning.col_param')}</b>",
                    f"<b>{t('notch_tuning.col_current')}</b>",
                    f"<b>{t('notch_tuning.col_suggested')}</b>",
                    f"<b>{t('notch_tuning.col_reason')}</b>",
                    f"<b>{t('notch_tuning.col_priority')}</b>",
                ],
                fill_color="#4472c4",
                font=dict(color="white", size=13,
                          family="PingFang SC, Microsoft YaHei, sans-serif"),
                align="left",
                height=32,
            ),
            cells=dict(
                values=[
                    [r["param"] for r in recommendations],
                    [r["old"] for r in recommendations],
                    [f"<b>{r['new']}</b>" for r in recommendations],
                    [r["reason"] for r in recommendations],
                    [r["priority"] for r in recommendations],
                ],
                fill_color=[
                    [("#fff3cd"
                      if r["priority"] == t("notch_tuning.priority_high")
                      else "#ffffff")
                     for r in recommendations]
                ] * 5,
                font=dict(size=12,
                          family="PingFang SC, Microsoft YaHei, sans-serif"),
                align="left",
                height=28,
            ),
        )])

        fig.update_layout(
            title=dict(
                text=t("notch_tuning.fig_table_title"),
                font=dict(size=14),
            ),
            height=max(200, 60 + 32 * len(recommendations)),
            margin=dict(l=10, r=10, t=40, b=10),
        )
        return fig
