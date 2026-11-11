"""
GPS Glitch Analyzer: detects GPS position jumps unexplainable by vehicle speed.

Computes inter-sample haversine distance and compares against expected max
distance (speed * dt * 2x safety factor).  Flags glitches when actual
distance exceeds both the expected distance and a minimum 5 m floor.

Required: GPS

Title: GPS 跳变
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t
from . import AnalysisResult, BaseAnalyzer


class GPSGlitchAnalyzer(BaseAnalyzer):
    """
    GPS glitch analyzer.

    Generates:
      - Inter-sample distance time series with glitch threshold overlay
    """

    registry_key = "gps_glitch"
    sort_order = 72
    required_messages = ["GPS"]

    def analyze(self) -> AnalysisResult:
        details: list[str] = []
        figures: list[go.Figure] = []

        # -----------------------------------------------------------
        # 1. Read GPS data (instance 0 only)
        # -----------------------------------------------------------
        gps = self.reader.get_messages_as_arrays("GPS", [
            "I", "Lat", "Lng", "Spd", "Status",
        ])
        ts = gps["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=_t("analyzer.gps_glitch"),
                status="info",
                summary=_t("gps_glitch.empty"),
            )

        # Filter to the primary GPS instance (lowest index present), not a
        # hardcoded instance 0 — a log whose only receiver is instance 1
        # would otherwise be skipped entirely.
        inst = gps["I"]
        if not np.all(np.isnan(inst)):
            primary = int(np.nanmin(inst))
            mask0 = np.nan_to_num(inst, nan=-1).astype(int) == primary
        else:
            mask0 = np.ones(len(ts), dtype=bool)

        ts = ts[mask0]
        lat = gps["Lat"][mask0]
        lng = gps["Lng"][mask0]
        spd = gps["Spd"][mask0]
        status = gps["Status"][mask0]

        # Drop no-fix samples before measuring jumps. A Status < 3 (no 3D
        # fix) or a (0,0) placeholder position is not a real location, so
        # the jump to/from it registers as a huge phantom glitch and drove
        # a false critical. AP GPS Status: 0/1 = no fix, 2 = 2D, >=3 = 3D.
        fix_ok = (~np.isnan(lat) & ~np.isnan(lng)
                  & (np.abs(lat) > 1e-7) & (np.abs(lng) > 1e-7))
        if not np.all(np.isnan(status)):
            fix_ok &= np.nan_to_num(status, nan=0).astype(int) >= 3
        ts = ts[fix_ok]
        lat = lat[fix_ok]
        lng = lng[fix_ok]
        spd = spd[fix_ok]

        if len(ts) < 2:
            return self.make_result(
                title=_t("analyzer.gps_glitch"),
                status="info",
                summary=_t("gps_glitch.insufficient"),
            )

        # -----------------------------------------------------------
        # 2. Compute inter-sample distance (haversine approx)
        # -----------------------------------------------------------
        dlat = np.diff(lat)
        dlng = np.diff(lng)
        # Approximate: 1 degree latitude ~ 111000 m
        # Longitude correction: cos(lat) * 111000
        cos_lat = np.cos(np.radians(lat[:-1]))
        actual_dist = np.sqrt(
            (dlat * 111000.0) ** 2 + (dlng * cos_lat * 111000.0) ** 2
        )

        # -----------------------------------------------------------
        # 3. Compute expected max distance = Spd * dt * 2
        # -----------------------------------------------------------
        dt = np.diff(ts)
        # Use speed at the start of each interval
        expected_dist = spd[:-1] * dt * 2.0

        # -----------------------------------------------------------
        # 4. Flag glitches
        # -----------------------------------------------------------
        min_dist = self.thresholds.gps_glitch_min_dist
        glitch_mask = actual_dist > np.maximum(expected_dist, min_dist)

        # Flight only. Multipath on the ground is not a flight fault, and
        # long clean ground time would dilute real in-flight jumps below
        # the threshold. A pair must sit inside one armed window, or the
        # landing spot and the next takeoff spot read as a jump.
        armed = self.get_armed_mask(ts)
        starts = [start for start, _ in self.reader.get_armed_intervals()]
        same_window = (np.searchsorted(starts, ts[:-1], side="right")
                       == np.searchsorted(starts, ts[1:], side="right"))

        # Filter out NaN entries and non-positive dt (backward/duplicate
        # timestamps are clock anomalies, not GPS glitches).
        valid = (~np.isnan(actual_dist) & ~np.isnan(expected_dist)
                 & (dt > 0) & armed[:-1] & armed[1:] & same_window)
        n_valid = int(np.sum(valid))
        if n_valid == 0:
            return self.make_result(
                title=_t("analyzer.gps_glitch"),
                status="info",
                summary=_t("common.never_armed"
                           if self.reader.never_armed()
                           else "gps_glitch.insufficient"),
            )

        n_glitches = int(np.sum(glitch_mask & valid))
        glitch_frac = n_glitches / n_valid

        # -----------------------------------------------------------
        # 5. Determine status
        # -----------------------------------------------------------
        warn_thresh = self.thresholds.gps_glitch_warn
        crit_thresh = self.thresholds.gps_glitch_crit

        # One jump past EK3_GLITCH_RAD is enough, however small a share of
        # the samples it is: at that distance the EKF stops rejecting the GPS
        # and resets to it (AP_NavEKF3.cpp). 0 swaps the radius for clipping,
        # which leaves the default as the yardstick. Flight only: the corpora's
        # six such jumps were all a fix settling on the ground.
        glitch_radius = self.reader.get_param("EK3_GLITCH_RAD", 25.0) or 25.0
        speed_limit = np.maximum(spd[:-1], spd[1:]) * dt * 2.0
        jumps = valid & (actual_dist > np.maximum(speed_limit, glitch_radius))
        worst_jump = float(np.max(actual_dist[jumps], initial=0.0))
        jump_past_radius = bool(np.any(jumps))

        if glitch_frac > crit_thresh:
            status = "critical"
        elif glitch_frac > warn_thresh or jump_past_radius:
            status = "warning"
        else:
            status = "good"

        details.append(
            _t("gps_glitch.sample_count", count=n_valid)
        )
        details.append(
            _t("gps_glitch.glitch_count",
               count=n_glitches,
               pct=f"{glitch_frac * 100:.2f}")
        )
        jump_text = _t("gps_glitch.summary_jump", dist=f"{worst_jump:.0f}",
                       radius=f"{glitch_radius:.0f}")
        if jump_past_radius:
            details.append(jump_text)

        # -----------------------------------------------------------
        # 6. Figure: distance between samples with threshold
        # -----------------------------------------------------------
        t_mid = (ts[:-1] + ts[1:]) / 2.0
        fig = self.make_figure(
            title=_t("gps_glitch.fig_title"),
            xaxis_title=_t("gps_glitch.time_s"),
            yaxis_title=_t("gps_glitch.distance_m"),
            height=350,
        )

        # Normal samples
        fig.add_trace(go.Scattergl(
            x=t_mid[valid & ~glitch_mask],
            y=actual_dist[valid & ~glitch_mask],
            mode='markers',
            marker=dict(size=2, color='#1f77b4'),
            name=_t("gps_glitch.normal"),
        ))

        # Glitch samples
        glitch_valid = valid & glitch_mask
        if np.any(glitch_valid):
            fig.add_trace(go.Scattergl(
                x=t_mid[glitch_valid],
                y=actual_dist[glitch_valid],
                mode='markers',
                marker=dict(size=4, color='red'),
                name=_t("gps_glitch.glitch"),
            ))

        # Threshold line
        self.add_threshold_hline(
            fig, min_dist,
            _t("gps_glitch.min_threshold", val=min_dist),
            color="#f0ad4e",
        )

        figures.append(fig)

        # -----------------------------------------------------------
        # 7. Summary and action
        # -----------------------------------------------------------
        if status == "critical":
            summary = _t("gps_glitch.summary_critical",
                         pct=f"{glitch_frac * 100:.1f}")
            action_level = "physical_check"
            plain_summary = _t("gps_glitch.plain_critical")
            action_text = _t("gps_glitch.action_critical")
            evidence_source = _t("gps_glitch.evidence")
        elif status == "warning":
            by_share = glitch_frac > warn_thresh
            summary = (_t("gps_glitch.summary_warning",
                          pct=f"{glitch_frac * 100:.1f}")
                       if by_share else jump_text)
            action_level = "physical_check"
            plain_summary = _t("gps_glitch.plain_warning")
            action_text = _t("gps_glitch.action_warning")
            evidence_source = _t("gps_glitch.evidence" if by_share
                                 else "gps_glitch.evidence_jump")
        else:
            summary = _t("gps_glitch.summary_good")
            action_level = "ok"
            plain_summary = ""
            action_text = ""
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.gps_glitch"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
        )
