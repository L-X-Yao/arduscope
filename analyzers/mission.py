"""
Mission Execution Analyzer: waypoint accuracy and mission completion.

Analyzes AUTO mode mission execution by comparing actual flight path
against commanded waypoints.

Required: CMD
Optional: GPS, POS

Title: 任务执行
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


# MAV_CMD IDs for navigation waypoints
_NAV_CMDS = {16, 17, 18, 19, 20, 21, 22, 31, 82, 83, 84, 85, 92, 93, 189}


class MissionAnalyzer(BaseAnalyzer):
    """
    Mission execution analyzer.

    Generates:
      - Waypoint map with actual track overlay
      - Waypoint accuracy (cross-track error)
      - Mission completion status
    """

    registry_key = "mission"
    sort_order = 155
    required_messages = ["CMD"]
    optional_messages = ["GPS", "POS"]

    def can_run(self) -> bool:
        """Need CMD messages and GPS or POS for track."""
        if not self.reader.has_message("CMD"):
            return False
        return (self.reader.has_message("GPS")
                or self.reader.has_message("POS"))

    def _wp_radius_m(self) -> tuple[float, str]:
        """(waypoint radius in metres, the parameter it came from).

        AC_WPNav moved to SI units upstream: Copter's `WPNAV_RADIUS` (cm)
        became `WP_RADIUS_M` (m). Logs arrive from both generations, so
        read the current name first and fall back. Plane's `WP_RADIUS`
        has always been metres.
        """
        if self.vehicle.is_copter:
            radius_m = self.reader.get_param("WP_RADIUS_M", 0.0)
            if radius_m > 0:
                return radius_m, "WP_RADIUS_M"
            return self.reader.get_param("WPNAV_RADIUS", 200) / 100.0, \
                "WPNAV_RADIUS"
        return self.reader.get_param("WP_RADIUS", 30), "WP_RADIUS"

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read CMD (mission items)
        # -----------------------------------------------------------
        cmd = self.reader.get_messages_as_arrays("CMD", [
            "CNum", "CId", "Lat", "Lng", "Alt",
        ])
        t_cmd = cmd["_timestamp"]

        if len(t_cmd) == 0:
            return self.make_result(
                title=t("analyzer.mission"),
                status="info",
                summary=t("mission.no_data"),
            )

        cmd_num = cmd["CNum"]
        cmd_id = cmd["CId"]
        wp_lat = cmd["Lat"]
        wp_lng = cmd["Lng"]

        # Filter to navigation waypoints with valid coordinates
        nav_mask = []
        for cid, lat, lng in zip(cmd_id, wp_lat, wp_lng):
            try:
                is_nav = int(cid) in _NAV_CMDS
            except (ValueError, TypeError):
                nav_mask.append(False)
                continue
            nav_mask.append(is_nav and not (abs(lat) < 0.001 and abs(lng) < 0.001))
        nav_mask = np.array(nav_mask)

        nav_lats = wp_lat[nav_mask]
        nav_lngs = wp_lng[nav_mask]
        nav_nums = cmd_num[nav_mask]

        total_cmds = len(cmd_num)
        nav_wps = len(nav_lats)
        details.append(t("mission.total_cmds", count=total_cmds))
        details.append(t("mission.nav_wps", count=nav_wps))

        if nav_wps == 0:
            return self.make_result(
                title=t("analyzer.mission"),
                status="info",
                summary=t("mission.no_nav_wps", count=total_cmds),
            )

        status = "good"

        # -----------------------------------------------------------
        # 2. Read actual flight track
        # -----------------------------------------------------------
        if self.reader.has_message("POS"):
            pos = self.reader.get_messages_as_arrays("POS", [
                "Lat", "Lng", "Alt",
            ])
            track_t = pos["_timestamp"]
            track_lat = pos["Lat"]
            track_lng = pos["Lng"]
            track_alt = pos["Alt"]
        else:
            # Primary GPS instance only — a dual-GPS log interleaves both
            # receivers at shared timestamps, biasing every waypoint
            # distance in the fallback track.
            gps = self.read_primary_instance(
                "GPS", ["Lat", "Lng", "Alt"], "I")
            track_t = gps["_timestamp"]
            track_lat = gps["Lat"]
            track_lng = gps["Lng"]
            track_alt = gps["Alt"]

        valid_track = (
            ~np.isnan(track_lat) & ~np.isnan(track_lng)
            & (np.abs(track_lat) > 0.001)
        )
        track_t = track_t[valid_track]
        track_lat = track_lat[valid_track]
        track_lng = track_lng[valid_track]
        track_alt = track_alt[valid_track]

        if len(track_lat) < 10:
            return self.make_result(
                title=t("analyzer.mission"),
                status="info",
                summary=t("mission.insufficient_track"),
            )

        # -----------------------------------------------------------
        # 3. Check AUTO mode time
        # -----------------------------------------------------------
        auto_duration = 0.0
        for mc in self.reader.mode_list:
            if mc.mode_name in ('AUTO', 'Auto', '10', '3'):
                auto_duration += 1  # approximate
        has_auto = auto_duration > 0
        if has_auto:
            details.append(t("mission.auto_detected", count=int(auto_duration)))
        else:
            details.append(t("mission.no_auto"))
            status = "info"

        # -----------------------------------------------------------
        # 4. Waypoint proximity analysis
        # -----------------------------------------------------------
        wp_radius, wp_param_name = self._wp_radius_m()

        wp_distances = []
        for i in range(nav_wps):
            wlat = float(nav_lats[i])
            wlng = float(nav_lngs[i])

            # Distance from each track point to this waypoint (meters)
            dlat = (track_lat - wlat) * 111320
            dlng = (track_lng - wlng) * 111320 * np.cos(np.radians(wlat))
            dists = np.sqrt(dlat**2 + dlng**2)

            min_dist = float(np.nanmin(dists))
            wp_distances.append(min_dist)

        wp_distances = np.array(wp_distances)

        if len(wp_distances) > 0:
            mean_acc = float(np.mean(wp_distances))
            max_acc = float(np.max(wp_distances))
            reached = int(np.sum(wp_distances < wp_radius))

            details.append(
                t("mission.wp_accuracy",
                  mean=f"{mean_acc:.1f}", max=f"{max_acc:.1f}")
            )
            details.append(
                t("mission.wp_reached",
                  radius=f"{wp_radius:.0f}",
                  reached=reached, total=nav_wps)
            )

            if has_auto:
                completion_pct = reached / nav_wps * 100
                details.append(t("mission.completion_pct", pct=f"{completion_pct:.0f}"))

                if completion_pct < self.thresholds.mission_completion_warn:
                    status = self.worst_status(status, "warning")
                if mean_acc > self.thresholds.mission_accuracy_warn:
                    status = self.worst_status(status, "warning")
                    details.append(t("mission.low_accuracy",
                                     mean=f"{mean_acc:.1f}",
                                     max=f"{max_acc:.1f}",
                                     pct=f"{completion_pct:.0f}"))

        # -----------------------------------------------------------
        # 5. Figure: Map with waypoints and track
        # -----------------------------------------------------------
        fig_map = self.make_figure(
            title=t("mission.fig_map"),
            xaxis_title=t("mission.longitude"),
            yaxis_title=t("mission.latitude"),
            height=500,
        )

        # Actual track
        fig_map.add_trace(go.Scattergl(
            x=track_lng, y=track_lat,
            mode='lines', name=t("mission.actual_track"),
            line=dict(color='#1f77b4', width=1),
        ))

        # Waypoints
        wp_labels = [f"WP{int(n)}" if np.isfinite(n) else f"WP?" for n in nav_nums]
        fig_map.add_trace(go.Scatter(
            x=nav_lngs, y=nav_lats,
            mode='markers+text', name=t("mission.waypoints"),
            marker=dict(color='#d62728', size=10, symbol='diamond'),
            text=wp_labels,
            textposition='top center',
            textfont=dict(size=10),
        ))

        # Waypoint lines
        fig_map.add_trace(go.Scatter(
            x=nav_lngs, y=nav_lats,
            mode='lines', name=t("mission.planned_route"),
            line=dict(color='#d62728', width=1, dash='dash'),
            showlegend=True,
        ))

        fig_map.update_layout(yaxis_scaleanchor="x", yaxis_scaleratio=1)
        figures.append(fig_map)

        # -----------------------------------------------------------
        # 6. Figure: Waypoint accuracy bar chart
        # -----------------------------------------------------------
        if len(wp_distances) > 0:
            fig_acc = self.make_figure(
                title=t("mission.fig_accuracy"),
                xaxis_title=t("mission.waypoint"),
                yaxis_title=t("mission.closest_dist_m"),
                height=350,
            )
            colors = [
                '#5cb85c' if d < 10 else '#f0ad4e' if d < 30 else '#d9534f'
                for d in wp_distances
            ]
            fig_acc.add_trace(go.Bar(
                x=wp_labels, y=wp_distances,
                marker_color=colors,
                name=t("mission.closest_dist_m"),
            ))
            self.add_threshold_hline(
                fig_acc, 10, t("mission.thresh_excellent", val=10), color="#5cb85c", dash="dot",
            )
            self.add_threshold_hline(
                fig_acc, 30, t("mission.thresh_reached", val=30), color="#f0ad4e", dash="dot",
            )
            figures.append(fig_acc)

        # -----------------------------------------------------------
        # 7. Summary + actionable advice
        # -----------------------------------------------------------
        if not has_auto:
            summary = t("mission.no_auto_summary", count=nav_wps)
        elif len(wp_distances) > 0:
            summary = t("mission.result_summary",
                        total=nav_wps, reached=reached,
                        mean=f"{mean_acc:.1f}")
        else:
            summary = t("mission.wp_only_summary", count=nav_wps)

        # Action advice based on status
        # The radius gets a pointer, not a value: nothing authoritative says
        # how far to move it, and the value also has to come out in this
        # parameter's own units (WPNAV_RADIUS cm, WP_RADIUS_M and WP_RADIUS m).
        if status == "warning":
            action_level = "physical_check"
            low_completion = (has_auto and len(wp_distances) > 0
                              and completion_pct < 50)
            kind = "low_completion" if low_completion else "low_accuracy"
            plain_summary = t(f"mission.{kind}_plain")
            action_text = t(f"mission.{kind}_action", param=wp_param_name,
                            radius=f"{wp_radius:g}")
        else:
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        evidence_source = ""
        if status == "warning":
            evidence_source = t("mission.evidence")

        return self.make_result(
            title=t("analyzer.mission"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            evidence_source=evidence_source,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
        )
