"""
Log Health Analyzer: loop timing, CPU load, and message timing integrity.

Evaluates system performance by monitoring PM (Performance Monitor) messages
for loop time, CPU load, free memory, and detecting ATT message timing gaps.

Required: PM
Optional: ATT (for message timing gap detection)

Title: 日志健康
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


# A loop over this many periods counts in PM.NLon (PerfInfo::set_loop_rate).
_LONG_LOOP_PERIODS = 1.2

_COPTER_DEFAULT_LOG_BITMASK = 176126
_PLANE_DEFAULT_LOG_BITMASK = 0xFFFF


class LogHealthAnalyzer(BaseAnalyzer):
    """
    Log health analyzer.

    Generates:
      - MaxT (max loop time) vs time with warning line at 20ms
      - Load (CPU %) vs time with 80% line
      - Mem (free memory) vs time
      - ATT message timing gap detection
    """

    registry_key = "log_health"
    sort_order = 130
    required_messages = ["PM"]
    # RFRH is only asked whether it is there. It is not read, and it is the
    # bulk of a replay log.
    optional_messages = ["ATT", "DSF", "POS", "CTUN"]

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read PM data
        # -----------------------------------------------------------
        pm = self.reader.get_messages_as_arrays("PM", [
            "NLon", "NLoop", "MaxT", "Mem", "Load",
        ])
        ts = pm["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.log_health"),
                status="info",
                summary=t("log_health.pm_empty"),
            )

        max_t = pm["MaxT"]   # in microseconds
        n_lon = pm["NLon"]   # long loops count per PM reporting window
        n_loop = pm["NLoop"] # total loops in same window
        mem = pm["Mem"]       # free memory in bytes
        # Per-mille since the field exists: load_average() * 1000
        # (AP_Scheduler::Log_Write_Performance). Telling the scale from
        # the values cannot work, a quiet log never goes over 100.
        load = pm["Load"] / 10.0
        valid_load = load[~np.isnan(load)]
        has_load = len(valid_load) > 0

        overall_status = "good"

        # -----------------------------------------------------------
        # 2. MaxT analysis (convert us to ms)
        # -----------------------------------------------------------
        max_t_ms = max_t / 1000.0  # us -> ms
        valid_maxt = max_t_ms[~np.isnan(max_t_ms)]
        if len(valid_maxt) > 0:
            peak_loop = float(np.max(valid_maxt))
            avg_loop = float(np.mean(valid_maxt))
        else:
            peak_loop = 0.0
            avg_loop = 0.0

        details.append(t("log_health.max_loop_time", val=f"{peak_loop:.1f}"))
        details.append(t("log_health.avg_loop_time", val=f"{avg_loop:.1f}"))

        sched_rate = self.reader.get_param("SCHED_LOOP_RATE", 400)
        expected_ms = 1000.0 / sched_rate
        crit_ms = expected_ms * self.thresholds.loop_time_crit
        warn_ms = expected_ms * self.thresholds.loop_time_warn
        long_loop_ms = expected_ms * _LONG_LOOP_PERIODS

        # Long-loop fraction is the meaningful CPU pressure metric.
        # Single MaxT peaks fire on every flight (background tasks like
        # SD flush, MAVLink burst, EKF init briefly stall the loop).
        # The PM message already aggregates "long loops" via NLon/NLoop;
        # we compute the percentage and gate the status on that.
        valid_pm = ~np.isnan(n_loop) & (n_loop > 0)
        if np.any(valid_pm):
            total_loops = float(np.nansum(n_loop[valid_pm]))
            total_long = float(np.nansum(n_lon[valid_pm]))
            long_frac = total_long / total_loops if total_loops > 0 else 0.0
        else:
            long_frac = 0.0

        details.append(
            t("log_health.long_loop_frac",
              pct=f"{long_frac*100:.2f}",
              threshold=f"{long_loop_ms:.1f}")
        )

        # Thresholds: a few stray spikes are normal; sustained CPU
        # pressure is the real problem.
        long_warn = self.thresholds.log_long_loop_warn
        long_crit = self.thresholds.log_long_loop_crit
        if long_frac >= long_crit:
            overall_status = self.worst_status(overall_status, "critical")
            details.append(
                t("log_health.long_loop_crit",
                  pct=f"{long_frac*100:.1f}",
                  threshold=f"{long_loop_ms:.1f}")
            )
        elif long_frac >= long_warn:
            overall_status = self.worst_status(overall_status, "warning")
            details.append(
                t("log_health.long_loop_warn",
                  pct=f"{long_frac*100:.1f}",
                  threshold=f"{long_loop_ms:.1f}",
                  peak=f"{peak_loop:.1f}")
            )
        elif peak_loop > crit_ms:
            # Peak high but isolated → just an info note, not a flag
            details.append(
                t("log_health.isolated_spike", peak=f"{peak_loop:.1f}")
            )

        # -----------------------------------------------------------
        # 3. CPU load analysis
        # -----------------------------------------------------------
        # A Load row covers the last 32 loops (AP_Scheduler _spare_ticks),
        # so a row logged while parked is a stall at some boot or file
        # open, not CPU pressure in flight. Only armed rows are graded.
        graded = self.get_armed_mask(ts)
        graded_load = load[graded & ~np.isnan(load)]
        shown_load = graded_load if len(graded_load) else valid_load
        if has_load:
            details.append(t("log_health.cpu_load",
                             avg=f"{float(np.mean(shown_load)):.1f}",
                             max=f"{float(np.max(shown_load)):.1f}"))
        else:
            details.append(t("log_health.cpu_load_unavailable"))

        max_load = float(np.max(graded_load)) if len(graded_load) else 0.0
        if max_load > self.thresholds.cpu_load_crit:
            overall_status = self.worst_status(overall_status, "critical")
            details.append(
                t("log_health.cpu_load_crit",
                  val=f"{max_load:.1f}",
                  threshold=self.thresholds.cpu_load_crit)
            )
        elif max_load > self.thresholds.cpu_load_warn:
            overall_status = self.worst_status(overall_status, "warning")
            details.append(
                t("log_health.cpu_load_warn",
                  val=f"{max_load:.1f}",
                  threshold=self.thresholds.cpu_load_warn)
            )

        # -----------------------------------------------------------
        # 4. Memory analysis
        # -----------------------------------------------------------
        valid_mem = mem[~np.isnan(mem)]
        if len(valid_mem) > 0 and np.any(valid_mem > 0):
            min_mem = float(np.nanmin(valid_mem[valid_mem > 0]))
            details.append(
                t("log_health.min_free_mem",
                  bytes=f"{min_mem:.0f}",
                  kb=f"{min_mem/1024:.1f}")
            )
            if min_mem < self.thresholds.log_min_free_mem:
                overall_status = self.worst_status(overall_status, "warning")
                details.append(t("log_health.low_memory"))

        # -----------------------------------------------------------
        # 5. Figure: MaxT time series
        # -----------------------------------------------------------
        fig_maxt = self.make_figure(
            title=t("log_health.fig_maxt"),
            xaxis_title=t("log_health.time_s"),
            yaxis_title=t("log_health.loop_time_ms"),
            height=350,
        )
        fig_maxt.add_trace(go.Scattergl(
            x=ts, y=max_t_ms,
            mode='lines', name=t("log_health.trace_maxt"),
            line=dict(color='#d62728', width=1),
        ))
        self.add_threshold_hline(
            fig_maxt, crit_ms, t("log_health.threshold_crit", val=f"{crit_ms:.1f}"), color="#d9534f",
        )
        self.add_threshold_hline(
            fig_maxt, warn_ms, t("log_health.threshold_warn", val=f"{warn_ms:.1f}"), color="#f0ad4e",
        )
        figures.append(fig_maxt)

        # -----------------------------------------------------------
        # 6. Figure: CPU load time series
        # -----------------------------------------------------------
        fig_load = self.make_figure(
            title=t("log_health.fig_cpu_load"),
            xaxis_title=t("log_health.time_s"),
            yaxis_title=t("log_health.load_pct"),
            height=300,
        )
        fig_load.add_trace(go.Scattergl(
            x=ts, y=load,
            mode='lines', name=t("log_health.trace_load"),
            line=dict(color='#ff7f0e', width=1),
        ))
        self.add_threshold_hline(
            fig_load, self.thresholds.cpu_load_warn,
            t("log_health.threshold_warn", val=f"{self.thresholds.cpu_load_warn}%"),
            color="#f0ad4e",
        )
        self.add_threshold_hline(
            fig_load, self.thresholds.cpu_load_crit,
            t("log_health.threshold_crit", val=f"{self.thresholds.cpu_load_crit}%"),
            color="#d9534f",
        )
        figures.append(fig_load)

        # -----------------------------------------------------------
        # 7. Figure: Free memory time series
        # -----------------------------------------------------------
        if len(valid_mem) > 0 and np.any(valid_mem > 0):
            fig_mem = self.make_figure(
                title=t("log_health.fig_free_mem"),
                xaxis_title=t("log_health.time_s"),
                yaxis_title=t("log_health.free_mem_kb"),
                height=300,
            )
            fig_mem.add_trace(go.Scattergl(
                x=ts, y=mem / 1024.0,
                mode='lines', name=t("log_health.trace_mem"),
                line=dict(color='#2ca02c', width=1),
            ))
            figures.append(fig_mem)

        # -----------------------------------------------------------
        # 8. ATT message timing gap detection
        # -----------------------------------------------------------
        if self.reader.has_message("ATT"):
            gap_fig, gap_count = self._detect_timing_gaps()
            if gap_fig is not None:
                figures.append(gap_fig)
            if gap_count > 0:
                details.append(t("log_health.att_timing_gaps", count=gap_count))
                if gap_count > 10:
                    overall_status = self.worst_status(
                        overall_status, "warning"
                    )

        # -----------------------------------------------------------
        # 8.5 Logger dropout (DSF) + LOG_REPLAY bloat
        # -----------------------------------------------------------
        dropout_fired = False
        replay_suggestion = None
        if self.reader.has_message("DSF"):
            dsf = self.reader.get_messages_as_arrays("DSF", ["Dp", "FMn"])
            dp = dsf["Dp"]
            dp_ok = np.isfinite(dp)
            if np.any(dp_ok):
                # Dp is cumulative since logging started
                dp_final = int(np.nanmax(dp[dp_ok]))
                if dp_final >= self.thresholds.log_dropped_msgs_warn:
                    dropout_fired = True
                    overall_status = self.worst_status(
                        overall_status, "warning")
                    fmn = dsf["FMn"]
                    fmn_ok = np.isfinite(fmn)
                    fmn_min = (f"{int(np.nanmin(fmn[fmn_ok]))}"
                               if np.any(fmn_ok) else "?")
                    details.append(t("log_health.dsf_dropped_warn",
                                     count=dp_final, fmn=fmn_min))
                elif dp_final > 0:
                    # Boot/arming transients routinely drop a handful
                    # of messages — factual note, not an alarm.
                    details.append(t("log_health.dsf_dropped_info",
                                     count=dp_final))
        # LOG_REPLAY streams (R* messages) multiply the log size and can
        # starve the logger buffer — the classic cause of the dropout
        # above. RFRH is the replay frame header, present whenever the
        # replay stream is enabled.
        if self.reader.has_message("RFRH"):
            details.append(t("log_health.replay_detected"))
            log_replay = self.reader.get_param("LOG_REPLAY", 0)
            if log_replay >= 1:
                replay_suggestion = {
                    "param": "LOG_REPLAY",
                    "current": int(log_replay),
                    "suggested": 0,
                    "reason": t("log_health.replay_suggest_reason"),
                }

        # -----------------------------------------------------------
        # 9. Abrupt log end while armed (power interruption forensics)
        # -----------------------------------------------------------
        abrupt_status, abrupt_details = self._detect_abrupt_end()
        if abrupt_status is not None:
            overall_status = self.worst_status(overall_status, abrupt_status)
            details.extend(abrupt_details)

        # -----------------------------------------------------------
        # 10. Summary
        # -----------------------------------------------------------
        if abrupt_status is not None:
            summary = t("log_health.abrupt_summary")
        elif overall_status == "critical":
            summary = t("log_health.summary_critical")
        elif dropout_fired:
            summary = t("log_health.dropout_summary")
        elif overall_status == "warning":
            summary = t("log_health.summary_warning")
        else:
            summary = t("log_health.summary_good")

        # Log health issues are usually parameter-fixable (LOG_BITMASK,
        # scheduling load from features) or SD card related (physical).
        log_bitmask = self.reader.get_param("LOG_BITMASK", 0)
        bitmask_info = ""
        if log_bitmask > 0:
            bitmask_info = t("log_health.bitmask_info", val=int(log_bitmask))

        suggestions = []
        # "Log less" can only mean going back to the vehicle's default, and
        # only from a mask that holds all of it and more, or the change
        # would add logging. The defaults differ per vehicle
        # (ArduCopter/config.h, ArduPlane/config.h): Plane logs everything.
        if self.reader.is_copter:
            recommended_bitmask = _COPTER_DEFAULT_LOG_BITMASK
        elif self.reader.is_plane:
            recommended_bitmask = _PLANE_DEFAULT_LOG_BITMASK
        else:
            recommended_bitmask = None
        logs_above_default = (
            recommended_bitmask is not None
            and int(log_bitmask) != recommended_bitmask
            and int(log_bitmask) & recommended_bitmask == recommended_bitmask)
        if abrupt_status is not None:
            # Power/SD forensics — a LOG_BITMASK tweak is irrelevant here.
            # replay_suggestion is deliberately NOT attached: the
            # physical_check action level must keep suggestions empty
            # (AnalysisResult invariant), and power forensics dominate.
            action_level = "physical_check"
            plain_summary = t("log_health.abrupt_plain")
            action_text = t("log_health.abrupt_action")
        elif dropout_fired and overall_status == "warning":
            # Logger dropped messages: SD speed / logging volume issue,
            # not a LOG_BITMASK-recommendation situation.
            plain_summary = t("log_health.dropout_plain")
            action_text = t("log_health.dropout_action")
            if replay_suggestion is not None:
                suggestions.append(replay_suggestion)
                action_level = "apply_param"
            else:
                action_level = "physical_check"
        elif overall_status == "critical":
            action_level = "apply_param"
            plain_summary = t("log_health.plain_critical")
            action_text = t(
                "log_health.action_critical",
                bitmask_info=bitmask_info,
                recommended=(recommended_bitmask if recommended_bitmask
                             is not None else t("log_health.vehicle_default")),
            )
            if logs_above_default:
                suggestions.append({
                    "param": "LOG_BITMASK",
                    "current": int(log_bitmask),
                    "suggested": recommended_bitmask,
                    "reason": t("log_health.suggest_reason"),
                })
            if replay_suggestion is not None:
                suggestions.append(replay_suggestion)
        elif overall_status == "warning":
            action_level = "apply_param"
            plain_summary = t("log_health.plain_warning")
            action_text = t(
                "log_health.action_warning",
                bitmask_info=bitmask_info,
            )
            if logs_above_default:
                suggestions.append({
                    "param": "LOG_BITMASK",
                    "current": int(log_bitmask),
                    "suggested": recommended_bitmask,
                    "reason": t("log_health.suggest_reason"),
                })
            if replay_suggestion is not None:
                suggestions.append(replay_suggestion)
        elif replay_suggestion is not None:
            # Healthy log, but the replay debug stream is still on:
            # surface the LOG_REPLAY=0 fix (status stays good/info).
            action_level = "apply_param"
            plain_summary = ""
            action_text = t("log_health.replay_action")
            suggestions.append(replay_suggestion)
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""
        if action_level == "apply_param" and not suggestions:
            action_level = "physical_check"

        evidence_source = ""
        if abrupt_status is not None:
            evidence_source = t("log_health.abrupt_evidence")
        elif overall_status == "critical":
            evidence_source = t("log_health.evidence")
        elif dropout_fired:
            evidence_source = t("log_health.dropout_evidence")
        elif overall_status == "warning":
            evidence_source = t("log_health.evidence")

        return self.make_result(
            title=t("analyzer.log_health"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            param_suggestions=suggestions,
            evidence_source=evidence_source,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_abrupt_end(self) -> tuple[str | None, list[str]]:
        """
        Detect a log that stops while the vehicle is still armed.

        ArduPilot keeps logging until disarm and always writes the
        DISARMED event, so an armed interval that reaches the end of the
        log means logging was killed externally: power-path
        interruption (connector/solder/pack weld), SD card failure, or
        a crash that separated the battery. This is the "log dies
        mid-sentence" signature — the last records look completely
        normal, there is no error cascade and no landing.

        Returns (status_or_None, detail_lines). Status is "critical"
        when the vehicle was clearly airborne at cutoff, "warning"
        when armed on the ground or altitude is unknown.
        """
        t_start, t_end = self.reader.time_range
        log_end = t_end - t_start if t_end > t_start else 0.0

        armed = self.reader.get_armed_intervals()
        if armed:
            # Armed intervals are already relative to log start
            last_armed_end = armed[-1][1]
            if (log_end - last_armed_end
                    > self.thresholds.log_abrupt_end_gap_s):
                return None, []      # clean disarm before log end
            # Even with a small gap, a matched DISARMED event means the
            # pilot disarmed cleanly (then logging/power stopped) — NOT a
            # power-path interruption. get_armed_intervals synthesizes an
            # end at log_end when no disarm exists, so the gap alone can't
            # tell them apart. The last arm or disarm event is the final
            # state; counting them cannot see the order.
            events = getattr(self.reader, "events", []) or []
            marks = [ev.event_id for ev in events
                     if ev.event_id in (10, 11)]
            if marks and marks[-1] == 11:
                return None, []      # cleanly disarmed
        else:
            # Logging starts at arming, so the ARMED event can miss the
            # log entirely (race at logger open) — a power-cut fragment
            # then carries NO arm/disarm events at all. Fall back: no
            # DISARMED event anywhere + airborne at the end = armed at
            # cutoff. Without flight evidence, make no claim. POWR.Safety
            # has already spoken if it says the vehicle stayed disarmed.
            if self.reader.never_armed():
                return None, []
            events = getattr(self.reader, "events", []) or []
            if any(ev.event_id == 11 for ev in events):
                return None, []      # a clean disarm exists somewhere
            end_alt = self._altitude_at_cutoff(log_end)
            if (end_alt is None
                    or end_alt <= self.thresholds.log_abrupt_airborne_alt_m):
                return None, []
            last_armed_end = log_end

        details = [t("log_health.abrupt_detected",
                     end=f"{last_armed_end:.1f}")]

        # Airborne at cutoff?
        status = "warning"
        last_alt = self._altitude_at_cutoff(last_armed_end)
        if (last_alt is not None
                and last_alt > self.thresholds.log_abrupt_airborne_alt_m):
            status = "critical"
            details.append(t("log_health.abrupt_airborne",
                             alt=f"{last_alt:.1f}"))

        # Battery trend in the final seconds: a healthy voltage right up
        # to the cutoff points at the power PATH (contact/weld/connector),
        # not the cell. Interruption can strike at ANY load — do not
        # assume it only happens at high current.
        if self.reader.has_message("BAT"):
            # Primary battery only — volt[-1]/curr[-1] on interleaved
            # instances would read whichever monitor logged last (e.g. a
            # secondary 0V monitor), flipping the power-path vs
            # cell-exhaustion diagnosis.
            bat = self.read_primary_instance("BAT", ["Volt", "Curr"], "Inst")
            bts = bat["_timestamp"]
            tail = bts >= (last_armed_end - 5.0)
            if np.any(tail):
                volt = np.nan_to_num(bat["Volt"][tail], nan=0.0)
                curr = np.nan_to_num(bat["Curr"][tail], nan=0.0)
                last_v, last_i = float(volt[-1]), float(curr[-1])
                details.append(t("log_health.abrupt_last_bat",
                                 volt=f"{last_v:.1f}", curr=f"{last_i:.1f}"))
                low_v = self.thresholds.batt_low_volt
                if low_v > 0 and last_v < low_v:
                    details.append(t("log_health.abrupt_volt_sagging"))
                else:
                    details.append(t("log_health.abrupt_volt_healthy"))

        return status, details

    def _altitude_at_cutoff(self, cutoff: float) -> float | None:
        """Last height in the 2 s before `cutoff`, None if nothing logged it.

        POS.RelHomeAlt leads: every vehicle writes it and it counts from
        home. Plane's CTUN has no Alt at all, and Copter's counts from the
        EKF origin, so it is only the fallback for logs without POS.
        """
        for msg, field in (("POS", "RelHomeAlt"), ("CTUN", "Alt")):
            if not self.reader.has_message(msg):
                continue
            data = self.reader.get_messages_as_arrays(msg, [field])
            tail = data[field][data["_timestamp"] >= cutoff - 2.0]
            tail = tail[np.isfinite(tail)]
            if len(tail):
                return float(tail[-1])
        return None

    def _detect_timing_gaps(self) -> tuple[go.Figure | None, int]:
        """
        Detect ATT message timing gaps > 3x median interval.

        Returns (figure, gap_count).
        """
        att = self.reader.get_messages_as_arrays("ATT", ["Roll"])
        ts = att["_timestamp"]

        if len(ts) < 10:
            return None, 0

        # A timestamp that repeats or steps back is clock_health's finding.
        # Its interval is left out here, and so is its place on the time
        # axis: with the axis left whole, every later point was drawn early,
        # and over 2000 points the report could not be written at all.
        steps = np.diff(ts)
        forward = steps > 0
        dt = steps[forward]
        if len(dt) == 0:
            return None, 0

        median_dt = float(np.median(dt))
        if median_dt <= 0:
            return None, 0

        threshold = median_dt * 3
        gap_mask = dt > threshold
        gap_count = int(np.sum(gap_mask))

        if gap_count == 0:
            return None, 0

        # Figure showing timing intervals with gaps highlighted
        fig = self.make_figure(
            title=t("log_health.fig_att_timing",
                     median=f"{median_dt*1000:.1f}",
                     threshold=f"{threshold*1000:.1f}"),
            xaxis_title=t("log_health.time_s"),
            yaxis_title=t("log_health.interval_ms"),
            height=300,
        )

        dt_ms = dt * 1000
        t_mid = ((ts[:-1] + ts[1:]) / 2)[forward]

        # Two traces, not one with a colour for each point: the report
        # thins a trace to 2000 points and leaves its colours as they were,
        # so the red ones landed on whatever points came first.
        fig.add_trace(go.Scattergl(
            x=t_mid[~gap_mask], y=dt_ms[~gap_mask],
            mode='markers',
            marker=dict(size=2, color='#1f77b4'),
            name=t("log_health.interval"),
        ))
        fig.add_trace(go.Scattergl(
            x=t_mid[gap_mask], y=dt_ms[gap_mask],
            mode='markers',
            marker=dict(size=5, color='red'),
            name=t("log_health.interval_gap"),
        ))

        self.add_threshold_hline(
            fig, threshold * 1000,
            t("log_health.3x_median", val=f"{threshold*1000:.1f}"),
            color="#d9534f",
        )

        return fig, gap_count
