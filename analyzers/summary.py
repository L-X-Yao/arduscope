"""
Summary Analyzer: flight overview and event timeline.

Uses pre-scanned metadata (modes, errors, events, text messages) to
produce a high-level flight summary without needing a second log pass.

Title: 飞行概览
"""

from __future__ import annotations

import html as _html

import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


# ArduPilot event IDs (EV message). Values are the LogEvent enum in
# ArduPilot libraries/AP_Logger/AP_Logger.h — the previous table was
# mis-mapped from ID 15 up (e.g. 18 shown as "Flip Start" is actually
# LAND_COMPLETE), which mislabelled routine landings as flips and real
# GPS loss as a flip. Corrected against the authoritative enum.
EVENT_NAMES = {
    10: "Armed",
    11: "Disarmed",
    15: "Auto Armed",
    17: "Land Complete Maybe",
    18: "Land Complete",
    19: "Lost GPS",
    21: "Flip Start",
    22: "Flip End",
    25: "Set Home",
    26: "Set Simple On",
    27: "Set Simple Off",
    28: "Not Landed",
    29: "Set Super Simple On",
    30: "AutoTune Initialised",
    31: "AutoTune Off",
    32: "AutoTune Restart",
    33: "AutoTune Success",
    34: "AutoTune Failed",
    35: "AutoTune Reached Limit",
    36: "AutoTune Pilot Testing",
    37: "AutoTune Saved Gains",
    38: "Save Trim",
    39: "Save Waypoint",
    41: "Fence Enable",
    42: "Fence Disable",
    43: "Acro Trainer Off",
    44: "Acro Trainer Leveling",
    45: "Acro Trainer Limited",
    46: "Gripper Grab",
    47: "Gripper Release",
    49: "Parachute Disabled",
    50: "Parachute Enabled",
    51: "Parachute Released",
    52: "Landing Gear Deployed",
    53: "Landing Gear Retracted",
    54: "Motors Emergency Stopped",
    55: "Motors Emergency Stop Cleared",
    56: "Motors Interlock Disabled",
    57: "Motors Interlock Enabled",
    58: "Rotor Runup Complete",
    59: "Rotor Speed Below Critical",
    60: "EKF Alt Reset",
    61: "Land Cancelled By Pilot",
    62: "EKF Yaw Reset",
    63: "Avoidance ADSB Enable",
    64: "Avoidance ADSB Disable",
    65: "Avoidance Proximity Enable",
    66: "Avoidance Proximity Disable",
    67: "GPS Primary Changed",
    71: "ZigZag Store A",
    72: "ZigZag Store B",
    73: "Land Repo Active",
    74: "Standby Enable",
    75: "Standby Disable",
    76: "Fence Alt Max Enable",
    77: "Fence Alt Max Disable",
    78: "Fence Circle Enable",
    79: "Fence Circle Disable",
    80: "Fence Alt Min Enable",
    81: "Fence Alt Min Disable",
    82: "Fence Polygon Enable",
    83: "Fence Polygon Disable",
    85: "EK3 Sources Primary",
    86: "EK3 Sources Secondary",
    87: "EK3 Sources Tertiary",
    90: "Airspeed Primary Changed",
    163: "Surfaced",
    164: "Not Surfaced",
    165: "Bottomed",
    166: "Not Bottomed",
}

# ArduPilot error subsystem IDs (ERR message). Values are the
# LogErrorSubsystem enum in libraries/AP_Logger/AP_Logger.h. The
# previous table drifted from ID 18 up: 18 (BARO) was labelled a
# failsafe, 21 (TERRAIN) collided with 23, and 25 (THRUST_LOSS_CHECK,
# a crash-critical signal) was shown as "Failsafe Leak". 27-31 were
# missing entirely. Corrected against the authoritative enum.
ERROR_SUBSYS_NAMES = {
    1: "Main",
    2: "Radio",
    3: "Compass",
    4: "Optical Flow",
    5: "Failsafe Radio",
    6: "Failsafe Battery",
    7: "Failsafe GPS",
    8: "Failsafe GCS",
    9: "Failsafe Fence",
    10: "Flight Mode",
    11: "GPS",
    12: "Crash Check",
    13: "Flip",
    14: "AutoTune",
    15: "Parachute",
    16: "EKF Check",
    17: "Failsafe EKF/INAV",
    18: "Baro",
    19: "CPU",
    20: "Failsafe ADS-B",
    21: "Terrain",
    22: "Navigation",
    23: "Failsafe Terrain",
    24: "EKF Primary",
    25: "Thrust Loss Check",
    26: "Failsafe Sensors",
    27: "Failsafe Leak",
    28: "Pilot Input",
    29: "Failsafe Vibe",
    30: "Internal Error",
    31: "Failsafe Deadreckon",
}


class SummaryAnalyzer(BaseAnalyzer):
    """
    Flight overview analyzer.

    Generates:
      - Flight info text (firmware, duration, modes, error/event counts)
      - Mode timeline bar chart
      - Event/error timeline scatter plot
    """

    registry_key = "summary"
    sort_order = 10
    # No required_messages -- uses only pre-scanned data
    required_messages: list[str] = []  # custom can_run
    optional_messages: list[str] = []

    def can_run(self) -> bool:
        """Reads only what the pre-scan collected, so any flight log will do."""
        return True

    def analyze(self) -> AnalysisResult:
        details = self._build_details()
        figures = []

        mode_fig = self._mode_timeline()
        if mode_fig is not None:
            figures.append(mode_fig)

        event_fig = self._event_timeline()
        if event_fig is not None:
            figures.append(event_fig)

        # Determine status
        if len(self.reader.errors) > self.thresholds.summary_error_count_warn:
            status = "warning"
            summary = t("summary.many_errors", count=len(self.reader.errors))
        elif len(self.reader.errors) > 0:
            status = "info"
            summary = t("summary.some_errors", count=len(self.reader.errors))
        else:
            status = "good"
            summary = t("summary.no_errors")

        evidence_source = ""
        if status == "warning":
            evidence_source = t("summary.evidence")

        return self.make_result(
            title=t("analyzer.summary"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            evidence_source=evidence_source,
        )

    def _build_details(self) -> list[str]:
        """Build detail strings from pre-scan metadata."""
        details = []

        # Firmware info
        details.append(
            t("summary.firmware",
              vtype=_html.escape(self.reader.vehicle_type),
              version=_html.escape(self.reader.firmware_version))
        )
        details.append(t("summary.board", board=_html.escape(self.vehicle.board_name)))

        # Duration
        duration = self.reader.get_flight_duration()
        if duration > 0:
            m, s = divmod(int(duration), 60)
            details.append(t("summary.log_duration", m=m, s=s))

        # Armed intervals
        armed = self.reader.get_armed_intervals()
        if armed:
            for i, (start, end) in enumerate(armed):
                d = end - start
                details.append(
                    t("summary.armed_segment",
                      idx=i+1, start=f"{start:.1f}",
                      end=f"{end:.1f}", dur=f"{d:.1f}")
                )
        else:
            details.append(t("summary.no_arm_events"))

        # Mode summary
        if self.reader.mode_list:
            mode_names = [mc.mode_name for mc in self.reader.mode_list]
            unique_modes = list(dict.fromkeys(mode_names))  # preserve order
            details.append(t("summary.flight_modes", modes=', '.join(unique_modes)))

        # Error summary
        if self.reader.errors:
            details.append(t("summary.error_count", count=len(self.reader.errors)))
            # Group by subsystem
            subsys_counts: dict[str, int] = {}
            for err in self.reader.errors:
                name = ERROR_SUBSYS_NAMES.get(err.subsys, f"Subsys#{err.subsys}")
                subsys_counts[name] = subsys_counts.get(name, 0) + 1
            for name, count in subsys_counts.items():
                details.append(
                    f"  - {t('summary.subsys_error', name=name, count=count)}"
                )

        # Event summary
        if self.reader.events:
            details.append(t("summary.event_count", count=len(self.reader.events)))

        # Key text messages (first 10)
        if self.reader.text_messages:
            details.append(t("summary.text_msg_count", count=len(self.reader.text_messages)))
            for msg in self.reader.text_messages[:10]:
                details.append(
                    f"  {t('summary.text_msg_line', time=f'{msg.timestamp:.1f}', text=_html.escape(msg.text))}"
                )

        # Frame / motor info
        if self.vehicle.motor_count > 0:
            details.append(
                t("summary.motor_count",
                  count=self.vehicle.motor_count,
                  frame_class=self.vehicle.frame_class)
            )
        details.append(t("summary.imu_count", count=self.vehicle.imu_count))

        return details

    def _mode_timeline(self) -> go.Figure | None:
        """Create a horizontal bar chart showing mode durations."""
        if not self.reader.mode_list:
            return None

        total_duration = self.reader.get_flight_duration()
        if total_duration <= 0:
            return None

        # Build intervals: each mode lasts until the next mode change
        intervals = []
        for i, mc in enumerate(self.reader.mode_list):
            start = mc.timestamp
            if i + 1 < len(self.reader.mode_list):
                end = self.reader.mode_list[i + 1].timestamp
            else:
                end = total_duration
            intervals.append((mc.mode_name, start, end))

        # Color palette for modes
        mode_colors = {}
        palette = [
            '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
            '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
        ]
        unique_modes = list(dict.fromkeys(m[0] for m in intervals))
        for i, mode in enumerate(unique_modes):
            mode_colors[mode] = palette[i % len(palette)]

        fig = self.make_figure(
            title=t("summary.mode_timeline"),
            xaxis_title=t("summary.time_s"),
            height=max(150, 60 + 30 * len(intervals)),
        )

        _seen_modes = set()
        for mode_name, start, end in intervals:
            fig.add_trace(go.Bar(
                x=[end - start],
                y=[t("summary.mode_label")],
                base=[start],
                orientation='h',
                name=mode_name,
                marker_color=mode_colors[mode_name],
                text=f"{mode_name} ({end - start:.0f}s)",
                textposition="inside",
                hovertemplate=(
                    f"{mode_name}<br>"
                    f"{t('summary.hover_start')}: {start:.1f}s<br>"
                    f"{t('summary.hover_end')}: {end:.1f}s<br>"
                    f"{t('summary.hover_duration')}: {end - start:.1f}s"
                    "<extra></extra>"
                ),
                showlegend=mode_name not in _seen_modes,
            ))
            _seen_modes.add(mode_name)

        fig.update_layout(
            barmode='stack',
            yaxis=dict(visible=False),
            height=120,
        )
        return fig

    def _event_timeline(self) -> go.Figure | None:
        """Create a scatter plot of events and errors on a timeline."""
        if not self.reader.events and not self.reader.errors:
            return None

        fig = self.make_figure(
            title=t("summary.event_timeline"),
            xaxis_title=t("summary.time_s"),
            height=250,
        )

        # Events as blue dots
        if self.reader.events:
            times = [ev.timestamp for ev in self.reader.events]
            labels = [
                EVENT_NAMES.get(ev.event_id, f"Event#{ev.event_id}")
                for ev in self.reader.events
            ]
            fig.add_trace(go.Scatter(
                x=times,
                y=[1] * len(times),
                mode='markers+text',
                name=t("summary.events"),
                marker=dict(size=10, color='#5bc0de', symbol='circle'),
                text=labels,
                textposition='top center',
                textfont=dict(size=9),
                hovertemplate="%{text}<br>" + t("summary.hover_time") + ": %{x:.1f}s<extra></extra>",
            ))

        # Errors as red triangles
        if self.reader.errors:
            times = [err.timestamp for err in self.reader.errors]
            labels = [
                f"{ERROR_SUBSYS_NAMES.get(err.subsys, f'Subsys#{err.subsys}')} "
                f"(code={err.ecode})"
                for err in self.reader.errors
            ]
            fig.add_trace(go.Scatter(
                x=times,
                y=[0] * len(times),
                mode='markers+text',
                name=t("summary.errors"),
                marker=dict(size=12, color='#d9534f', symbol='triangle-up'),
                text=labels,
                textposition='bottom center',
                textfont=dict(size=9),
                hovertemplate="%{text}<br>" + t("summary.hover_time") + ": %{x:.1f}s<extra></extra>",
            ))

        fig.update_layout(
            yaxis=dict(
                tickvals=[0, 1],
                ticktext=[t("summary.errors"), t("summary.events")],
                range=[-0.5, 1.8],
            ),
            showlegend=True,
        )
        return fig
