"""
VTOL Transition Analyzer: QuadPlane transition monitoring.

Analyzes VTOL transitions (forward flight <-> hover) by monitoring
TMix (transition mix), altitude changes, and climb rate during transitions.
Only runs on ArduPlane QuadPlane logs.

Required: QTUN
Optional: ATT

Title: VTOL转换
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t
from lib.plane_phase import PLANE_Q_MODES
from . import AnalysisResult, BaseAnalyzer


# QTUN.Ast bits, from log_assistance_flags in ArduPlane/quadplane.cpp.
_AST_ACTIVE = 1
_AST_FORCED = 2
_AST_SPEED = 4
_AST_ALT = 8
_AST_ANGLE = 16

# QTUN stops with the motors, so a longer gap between rows is not assist time.
_QTUN_MAX_GAP_S = 1.0


class VTOLTransitionAnalyzer(BaseAnalyzer):
    """
    VTOL transition analyzer (QuadPlane only).

    Generates:
      - TMix (transition mix) vs time
      - Altitude during transitions
      - CRt (climb rate) during transitions
      - Transition event detection and altitude loss assessment
    """

    registry_key = "vtol_transition"
    sort_order = 140
    required_messages = ["QTUN"]
    optional_messages = ["ATT"]

    def can_run(self) -> bool:
        """Must have QTUN and be a QuadPlane."""
        if not self.reader.has_message("QTUN"):
            return False
        # Check if it's a QuadPlane (Q_ENABLE 1=enabled, 2=VTOL AUTO)
        return (
            self.vehicle.is_quadplane
            or self.reader.get_param("Q_ENABLE", 0) >= 1
        )

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read QTUN data
        # -----------------------------------------------------------
        qtun = self.reader.get_messages_as_arrays("QTUN", [
            "TMix", "Trn", "Alt", "DAlt", "CRt", "DCRt",
        ])
        t = qtun["_timestamp"]

        if len(t) == 0:
            return self.make_result(
                title=_t("analyzer.vtol_transition"),
                status="info",
                summary=_t("vtol_transition.empty"),
            )

        tmix = qtun["TMix"]
        trn = qtun["Trn"]
        alt = qtun["Alt"]
        dalt = qtun["DAlt"]
        crt = qtun["CRt"]

        # -----------------------------------------------------------
        # 2. Detect transition events
        # -----------------------------------------------------------
        transitions = self._detect_transitions(t, trn, alt)
        transitions += self._detect_back_transitions(t, alt)
        transitions.sort(key=lambda tr: tr['start'])
        details.append(_t("vtol_transition.event_count", count=len(transitions)))
        assist = self._assist_detail()
        if assist:
            details.append(assist)

        overall_status = "good"

        for i, tr in enumerate(transitions):
            tr_type = (_t("vtol_transition.to_hover")
                       if tr['direction'] == 'to_hover'
                       else _t("vtol_transition.to_fw"))
            details.append(
                _t("vtol_transition.detail",
                   idx=i+1, tr_type=tr_type,
                   start=f"{tr['start']:.1f}",
                   end=f"{tr['end']:.1f}",
                   dur=f"{tr['duration']:.1f}")
            )

            # Check altitude loss during transition
            alt_loss = tr.get('alt_loss', 0)
            if alt_loss > self.thresholds.vtol_alt_loss_crit:
                overall_status = self.worst_status(overall_status, "critical")
                details.append(
                    _t("vtol_transition.alt_loss_crit",
                       loss=f"{alt_loss:.1f}")
                )
            elif alt_loss > self.thresholds.vtol_alt_loss_warn:
                overall_status = self.worst_status(overall_status, "warning")
                details.append(
                    _t("vtol_transition.alt_loss_warn",
                       loss=f"{alt_loss:.1f}")
                )
            elif alt_loss > 0:
                details.append(
                    _t("vtol_transition.alt_loss",
                       loss=f"{alt_loss:.1f}")
                )

        # -----------------------------------------------------------
        # 3. Figure: TMix time series
        # -----------------------------------------------------------
        fig_tmix = self.make_figure(
            title=_t("vtol_transition.fig_tmix"),
            xaxis_title=_t("vtol_transition.time_s"),
            yaxis_title=_t("vtol_transition.tmix_axis"),
            height=350,
        )
        fig_tmix.add_trace(go.Scattergl(
            x=t, y=tmix,
            mode='lines', name='TMix',
            line=dict(color='#1f77b4', width=2),
        ))

        # Mark transition events
        for i, tr in enumerate(transitions):
            fig_tmix.add_vrect(
                x0=tr['start'], x1=tr['end'],
                fillcolor='rgba(255, 165, 0, 0.15)',
                line_width=0,
                annotation_text=f"T{i+1}",
                annotation_position="top left",
            )
        figures.append(fig_tmix)

        # -----------------------------------------------------------
        # 4. Figure: Altitude during full flight
        # -----------------------------------------------------------
        fig_alt = self.make_figure(
            title=_t("vtol_transition.fig_alt"),
            xaxis_title=_t("vtol_transition.time_s"),
            yaxis_title=_t("vtol_transition.alt_m"),
            height=400,
        )
        fig_alt.add_trace(go.Scattergl(
            x=t, y=dalt,
            mode='lines', name=_t("vtol_transition.desired_alt"),
            line=dict(color='rgba(31,119,180,0.5)', width=1),
        ))
        fig_alt.add_trace(go.Scattergl(
            x=t, y=alt,
            mode='lines', name=_t("vtol_transition.actual_alt"),
            line=dict(color='#d62728', width=1.5),
        ))

        # Mark transition events
        for i, tr in enumerate(transitions):
            fig_alt.add_vrect(
                x0=tr['start'], x1=tr['end'],
                fillcolor='rgba(255, 165, 0, 0.15)',
                line_width=0,
            )
        figures.append(fig_alt)

        # -----------------------------------------------------------
        # 5. Figure: Climb rate
        # -----------------------------------------------------------
        fig_crt = self.make_figure(
            title=_t("vtol_transition.fig_crt"),
            xaxis_title=_t("vtol_transition.time_s"),
            yaxis_title=_t("vtol_transition.climb_rate_ms"),
            height=350,
        )
        fig_crt.add_trace(go.Scattergl(
            x=t, y=crt,
            mode='lines', name='CRt',
            line=dict(color='#2ca02c', width=1),
        ))

        for i, tr in enumerate(transitions):
            fig_crt.add_vrect(
                x0=tr['start'], x1=tr['end'],
                fillcolor='rgba(255, 165, 0, 0.15)',
                line_width=0,
            )
        figures.append(fig_crt)

        # -----------------------------------------------------------
        # 6. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = _t("vtol_transition.crit_summary",
                         count=len(transitions))
        elif overall_status == "warning":
            summary = _t("vtol_transition.warn_summary",
                         count=len(transitions))
        elif len(transitions) > 0:
            summary = _t("vtol_transition.good_summary",
                         count=len(transitions))
        else:
            summary = _t("vtol_transition.no_events")

        # Transition issues are parameter-tuning problems (Q_TRANSITION_MS,
        # Q_TRAN_PIT_MAX, etc.), not hardware — adjust in MP.
        # Read current param values so we can give specific numbers.
        q_trans_ms = self.reader.get_param("Q_TRANSITION_MS", 0)
        q_tran_pit = self.reader.get_param("Q_TRAN_PIT_MAX", 0)

        param_advice = ""
        if overall_status in ("warning", "critical"):
            action_level = "physical_check"
            # Current values only: nothing authoritative says how far to
            # move these, so the report names no target.
            param_lines = []
            if q_trans_ms > 0:
                param_lines.append(
                    _t("vtol_transition.param_trans_ms",
                       current=int(q_trans_ms))
                )
            if q_tran_pit > 3:
                param_lines.append(
                    _t("vtol_transition.param_pit_max",
                       current=int(q_tran_pit))
                )
            param_advice = "；".join(param_lines) if param_lines else (
                _t("vtol_transition.default_advice")
            )

        if overall_status == "critical":
            plain_summary = _t("vtol_transition.crit_plain")
            action_text = _t("vtol_transition.crit_action",
                             advice=param_advice)
        elif overall_status == "warning":
            plain_summary = _t("vtol_transition.warn_plain")
            action_text = _t("vtol_transition.warn_action",
                             advice=param_advice)
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        evidence_source = ""
        if overall_status in ("warning", "critical"):
            evidence_source = _t("vtol_transition.evidence")

        return self.make_result(
            title=_t("analyzer.vtol_transition"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assist_detail(self) -> str | None:
        """How long Q_ASSIST held the aircraft up, and why."""
        qtun = self.reader.get_messages_as_arrays("QTUN", ["Ast"])
        ast = qtun["Ast"]
        if len(ast) < 2 or np.all(np.isnan(ast)):
            return None     # firmware from before the field
        flags = np.nan_to_num(ast).astype(int)[:-1]
        dt = np.minimum(np.diff(qtun["_timestamp"]), _QTUN_MAX_GAP_S)

        def seconds(bit: int) -> str:
            return f"{float(np.sum(dt[(flags & bit) != 0])):.1f}"

        active = (flags & _AST_ACTIVE) != 0
        if not np.any(active):
            return None
        periods = int(active[0]) + int(np.sum(active[1:] & ~active[:-1]))
        return _t("vtol_transition.assist_used", sec=seconds(_AST_ACTIVE),
                  count=periods, speed=seconds(_AST_SPEED),
                  alt=seconds(_AST_ALT), angle=seconds(_AST_ANGLE),
                  forced=seconds(_AST_FORCED))

    def _detect_transitions(
        self, t: np.ndarray, trn: np.ndarray, alt: np.ndarray,
    ) -> list[dict]:
        """Detect forward VTOL->FW transitions from QTUN.Trn.

        Trn is the ArduPlane transition state machine (transition.h):
        0=AIRSPEED_WAIT, 1=TIMER, 2=DONE, logged via
        get_log_transition_state(). The active forward transition is the
        TIMER(1) phase, completing at DONE(2). AIRSPEED_WAIT(0) is *also*
        the idle VTOL state, so neither TMix (the throttle/RPY mix, which
        sits ~0.1-0.5 the whole flight) nor an s<DONE band discriminates a
        transition — only TIMER runs do.

        Back transitions (FW->VTOL) just reset the state to AIRSPEED_WAIT
        with no timed phase, so Trn does not bound them; they come from
        _detect_back_transitions(). Returns dicts: start, end, direction,
        duration, alt_loss (max drop below the starting altitude).
        """
        n = len(t)
        if n < 10 or np.all(np.isnan(trn)):
            return []

        s = np.nan_to_num(trn, nan=2.0).astype(int)  # unknown -> DONE
        timer = s == 1
        if not np.any(timer):
            return []

        DONE = 2
        d = np.diff(timer.astype(int))
        starts = list(np.where(d == 1)[0] + 1)
        ends = list(np.where(d == -1)[0] + 1)
        if timer[0]:
            starts = [0] + starts
        if timer[-1]:
            ends = ends + [n - 1]

        transitions = []
        for a, b in zip(starts, ends):
            # Extend forward to the completing DONE sample so the window
            # covers the whole timed transition — but only briefly. A real
            # transition reaches DONE within a few seconds of the TIMER
            # phase; if it does not (an aborted/failed forward transition,
            # e.g. a tail-motor fault that never establishes forward
            # flight), keep the window at the TIMER run rather than
            # swallowing the following VTOL/descent segment as a multi-
            # minute "transition" with a huge altitude loss.
            e = b
            limit_t = float(t[b]) + 5.0
            while e < n - 1 and s[e] != DONE and float(t[e]) < limit_t:
                e += 1
            if s[min(e, n - 1)] != DONE:
                e = b
            seg = alt[a:e + 1]
            seg = seg[~np.isnan(seg)]
            alt_loss = float(seg[0] - np.min(seg)) if len(seg) > 1 else 0.0
            transitions.append({
                'start': float(t[a]),
                'end': float(t[e]),
                'duration': float(t[e] - t[a]),
                'direction': 'to_fw',
                'alt_loss': max(0.0, alt_loss),
            })
        return transitions

    def _detect_back_transitions(
        self, t: np.ndarray, alt: np.ndarray,
    ) -> list[dict]:
        """Detect FW->VTOL back transitions from mode changes.

        Trn does not show them, so the switch from a fixed-wing mode to a
        Q mode marks the start. ArduPlane limits pitch for Q_BACKTRANS_MS
        after the last fixed-wing mode (quadplane.cpp, default 3000 ms),
        which is taken as the window; 0 disables that envelope, so there
        is no window to judge. A back transition flown inside AUTO
        (VTOL land waypoint) has no mode change and is not seen here.
        """
        window_ms = self.reader.get_param("Q_BACKTRANS_MS", 3000)
        modes = self.reader.mode_list
        if window_ms <= 0 or len(t) < 2 or not modes:
            return []

        transitions = []
        for i in range(1, len(modes)):
            if (modes[i].mode_number not in PLANE_Q_MODES
                    or modes[i - 1].mode_number in PLANE_Q_MODES):
                continue
            start = float(modes[i].timestamp)
            end = start + window_ms / 1000.0
            if i + 1 < len(modes):
                end = min(end, float(modes[i + 1].timestamp))
            seg = alt[(t >= start) & (t <= end)]
            seg = seg[~np.isnan(seg)]
            alt_loss = float(seg[0] - np.min(seg)) if len(seg) > 1 else 0.0
            transitions.append({
                'start': start,
                'end': end,
                'duration': end - start,
                'direction': 'to_hover',
                'alt_loss': max(0.0, alt_loss),
            })
        return transitions
