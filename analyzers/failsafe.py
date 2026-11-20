"""
Failsafe Analyzer: safety margin assessment.

Evaluates how close each monitored metric came to its failsafe threshold,
using shaded margin areas (green -> yellow -> red gradient). Detects
triggered failsafes from ERR and STATUSTEXT messages.

Required: (uses pre-scan errors/events from reader)
Optional: BAT, GPS, XKF4, NKF4

Title: 安全裕度
"""

from __future__ import annotations

import re

import numpy as np
import plotly.graph_objects as go

from lib.ekf_failsafe import (
    ekf_failsafe_replay, over_threshold, plane_posvel_mask, sustained,
    vehicle_kind, window_touches_armed)
from lib.ekf_lanes import primary_lane_rows
from lib.i18n import t
from lib.np_helpers import safe_nanmax
from . import AnalysisResult, BaseAnalyzer


# ArduPilot LogErrorSubsystem values whose name carries the FAILSAFE_
# prefix — i.e. an actual failsafe fired. Values per AP_Logger/AP_Logger.h.
# EKFCHECK(16) and BARO(18) were previously listed but are *detection*
# subsystems, not failsafes: a baro glitch or EKF variance flag is not
# itself a failsafe (the EKF failsafe is subsys 17). ADSB(20),
# TERRAIN(23), SENSORS(26), LEAK(27), VIBE(29) and DEADRECKON(31) are
# real failsafes that were missing entirely.
_FS_SUBSYS_KEYS = {
    5: "failsafe.subsys_radio",
    6: "failsafe.subsys_battery",
    7: "failsafe.subsys_gps",
    8: "failsafe.subsys_gcs",
    9: "failsafe.subsys_fence",
    17: "failsafe.subsys_ekf",
    20: "failsafe.subsys_adsb",
    23: "failsafe.subsys_terrain",
    26: "failsafe.subsys_sensors",
    27: "failsafe.subsys_leak",
    29: "failsafe.subsys_vibe",
    31: "failsafe.subsys_deadreckon",
}

# STATUSTEXT names ArduPilot uses for its failsafes, mapped to the same
# i18n keys as the ERR subsystems. Sources: ArduCopter/events.cpp and
# ekf_check.cpp, ArduPlane/events.cpp and radio.cpp,
# ArduSub/failsafe.cpp, Rover/failsafe.cpp.
_TEXT_FAILSAFE_KEYS = {
    "radio": "failsafe.subsys_radio",
    "rc": "failsafe.subsys_radio",
    "rc short": "failsafe.subsys_radio",
    "rc long": "failsafe.subsys_radio",
    "short": "failsafe.subsys_radio",
    "throttle": "failsafe.subsys_radio",
    "gcs": "failsafe.subsys_gcs",
    "battery": "failsafe.subsys_battery",
    "ekf": "failsafe.subsys_ekf",
    "fence": "failsafe.subsys_fence",
    "terrain": "failsafe.subsys_terrain",
    "ads-b": "failsafe.subsys_adsb",
    "adsb": "failsafe.subsys_adsb",
    "leak": "failsafe.subsys_leak",
    "vibe": "failsafe.subsys_vibe",
    "vibration": "failsafe.subsys_vibe",
    "deadreckon": "failsafe.subsys_deadreckon",
    "dead reckoning": "failsafe.subsys_deadreckon",
    "sensors": "failsafe.subsys_sensors",
    "gps": "failsafe.subsys_gps",
    "radio + battery": "failsafe.subsys_radio_battery",
    "gcs + battery": "failsafe.subsys_gcs_battery",
}
# Text events may stand for more than one ERR subsystem (ArduCopter's
# combined "Radio + Battery" and "GCS + Battery" announcements).
_TEXT_DEDUPE_SUBSYS = {key: (subsys,)
                       for subsys, key in _FS_SUBSYS_KEYS.items()}
_TEXT_DEDUPE_SUBSYS["failsafe.subsys_radio_battery"] = (5, 6)
_TEXT_DEDUPE_SUBSYS["failsafe.subsys_gcs_battery"] = (8, 6)

# Text-only criticals in these subsystems point at the link, not at the
# battery/GPS/EKF margin text the generic action names.
_LINK_TEXT_KEYS = {
    "failsafe.subsys_radio",
    "failsafe.subsys_gcs",
    "failsafe.subsys_radio_battery",
    "failsafe.subsys_gcs_battery",
}
# ArduCopter announces a combined "Radio + Battery" or "GCS + Battery"
# failsafe, but clears it with the plain "Radio Failsafe Cleared" or
# "GCS Failsafe Cleared" message (ArduCopter/events.cpp). Pair the
# combined event under the link subsystem's clear.
_TEXT_STACK_KEYS = {
    "failsafe.subsys_radio_battery": "failsafe.subsys_radio",
    "failsafe.subsys_gcs_battery": "failsafe.subsys_gcs",
}

# The patterns cover the firmware's forms:
#   "Radio Failsafe", "Radio Failsafe - Continuing Landing",
#   "Radio + Battery Failsafe - Continuing Landing",
#   "RC Short Failsafe On", "RC Long Failsafe On: switched to RTL",
#   "GCS Failsafe On: switched to QRTL", "Throttle failsafe on",
#   "Failsafe: Terrain data missing", "Failsafe terrain triggered".
# Clear forms are matched first so "Radio Failsafe Cleared" is not an On.
_TEXT_CLEAR_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9 _+-]*?)\s+Failsafe\s+"
    r"(?:Cleared|Off)\b",
    re.IGNORECASE,
)
_TEXT_ON_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9 _+-]*?)\s+Failsafe"
    r"(?:\s+On)?(?:\s*[:\-].*)?$",
    re.IGNORECASE,
)
_TEXT_COLON_RE = re.compile(
    r"^Failsafe\s*:\s*(?P<name>[A-Za-z0-9][A-Za-z0-9 _+-]*)",
    re.IGNORECASE,
)
_TEXT_TRIGGERED_RE = re.compile(
    r"^Failsafe\s+(?P<name>[A-Za-z0-9][A-Za-z0-9 _+-]*?)\s+triggered\b",
    re.IGNORECASE,
)


def _text_failsafe_key(text: str) -> tuple[str, str] | None:
    """(i18n key, "on" | "clear") for a firmware failsafe line."""
    if not isinstance(text, str):
        return None
    text = " ".join(text.strip().split())
    match = _TEXT_CLEAR_RE.match(text)
    if match:
        key = _key_from_name(match.group("name"))
        return (key, "clear") if key else None
    for pattern in (_TEXT_ON_RE, _TEXT_COLON_RE, _TEXT_TRIGGERED_RE):
        match = pattern.match(text)
        if match:
            key = _key_from_name(match.group("name"))
            if key:
                return key, "on"
    return None


def _key_from_name(name: str) -> str | None:
    """The i18n key for a firmware name, including a trailing action."""
    name = name.strip().lower()
    if name in _TEXT_FAILSAFE_KEYS:
        return _TEXT_FAILSAFE_KEYS[name]
    for candidate in sorted(_TEXT_FAILSAFE_KEYS, key=len, reverse=True):
        if name.startswith(candidate + " "):
            return _TEXT_FAILSAFE_KEYS[candidate]
    return None


def _is_rover(reader) -> bool:
    vehicle_type = str(getattr(
        reader, "canonical_vehicle_type",
        getattr(reader, "vehicle_type", ""))).lower()
    if "rover" in vehicle_type:
        return True
    # A vendor banner renames the first token (`FC-Ground ... [ArduRover
    # V4.6.0]`); the official family in the banner is the evidence.
    for message in getattr(reader, "text_messages", []) or []:
        text = getattr(message, "text", "")
        if isinstance(text, str) and "ardurover" in text.lower():
            return True
    return False


def _pop_latest_open(open_by_key: dict[str, list[dict]]) -> dict | None:
    """The most recently started open event across subsystems."""
    latest = None
    latest_key = None
    for key, stack in open_by_key.items():
        if stack and (latest is None or stack[-1]["start"] > latest["start"]):
            latest = stack[-1]
            latest_key = key
    if latest is not None:
        open_by_key[latest_key].pop()
    return latest


def _text_failsafe_events(reader) -> list[dict]:
    """Pair firmware STATUSTEXT failsafe On/Cleared lines into events.

    Each On opens an event; clears close the most recent open event of the
    same subsystem, newest first. Two On lines for the same subsystem
    within one second are one event (`Throttle failsafe on` and
    `RC Short Failsafe On` are emitted together on Plane). An On with no
    clear is unresolved at the end of the log.

    Rover aggregates its failsafe bits and sends the clear with the last
    type that fired, so a Rover clear with no matching open event closes
    the most recent open event across subsystems.
    """
    events: list[dict] = []
    open_by_key: dict[str, list[dict]] = {}
    for message in getattr(reader, "text_messages", []) or []:
        text = getattr(message, "text", "")
        if not isinstance(text, str):
            continue
        parsed = _text_failsafe_key(text)
        if parsed is None:
            continue
        key, state = parsed
        when = float(getattr(message, "timestamp", 0.0))
        stack_key = _TEXT_STACK_KEYS.get(key, key)
        if state == "on":
            stack = open_by_key.setdefault(stack_key, [])
            if stack and 0.0 <= when - stack[-1]["start"] <= 1.0:
                # Keep the more specific name (`RC Short` over `Throttle`,
                # `Radio + Battery` over `Radio`).
                if len(text) > len(stack[-1]["text"]):
                    stack[-1]["text"] = text
                    stack[-1]["key"] = key
                continue
            event = {
                "key": key,
                "time": when,
                "start": when,
                "end": None,
                "unresolved": True,
                "text": text,
            }
            events.append(event)
            stack.append(event)
        else:
            stack = open_by_key.get(stack_key, [])
            event = stack.pop() if stack else None
            if event is None and _is_rover(reader):
                event = _pop_latest_open(open_by_key)
            if event is not None:
                event["end"] = when
                event["unresolved"] = False

    duration = float(getattr(reader, "get_flight_duration", lambda: 0.0)())
    for event in events:
        if event["end"] is None:
            event["end"] = duration
            event["unresolved"] = True
    return events


def _overlaps_armed(reader, start: float, end: float) -> bool:
    """True when the event's interval overlaps armed flight.

    The bounds are strict: an event that starts exactly at the disarm
    timestamp, or ends exactly at the arm timestamp, is not counted as
    armed. MSG and EV have no shared ordering, so the tie goes to the
    side that does not raise a false critical.
    """
    return any(start < armed_end and end > armed_start
               for armed_start, armed_end in reader.get_armed_intervals())


def _text_event_state(reader, event: dict) -> str:
    """Return "armed", "unarmed" or "unknown" for a STATUSTEXT event."""
    intervals = reader.get_armed_intervals()
    if not intervals:
        never_armed = getattr(reader, "never_armed", lambda: False)
        return "unarmed" if never_armed() else "unknown"
    if event["unresolved"]:
        # The log can end while the vehicle is still armed. The event is
        # then a point at the last row; an inclusive check against an
        # armed interval that reaches the end keeps it critical.
        duration = float(getattr(reader, "get_flight_duration",
                                 lambda: 0.0)())
        for armed_start, armed_end in intervals:
            if (armed_start <= event["start"] <= armed_end
                    and armed_end >= duration - 1e-6):
                return "armed"
    return ("armed" if _overlaps_armed(reader, event["start"], event["end"])
            else "unarmed")


class FailsafeAnalyzer(BaseAnalyzer):
    """
    Safety margin analyzer.

    Generates:
      - Combined margin plot with shaded green/yellow/red areas
      - Battery voltage margin (if BAT available)
      - GPS NSats margin (if GPS available)
      - EKF variance margin (if XKF4/NKF4 available)
    """

    registry_key = "failsafe"
    sort_order = 90
    # Always can run -- uses pre-scan data at minimum
    required_messages: list[str] = []  # custom can_run
    optional_messages = ["BAT", "GPS", "XKF4", "NKF4"]

    def can_run(self) -> bool:
        """Checks whatever failsafe evidence the log holds, so any flight log will do."""
        return True

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        margin_statuses = []
        self._ekf_single_over = False

        # -----------------------------------------------------------
        # 1. Check triggered failsafes from ERR and STATUSTEXT messages
        # -----------------------------------------------------------
        err_triggers = []
        for err in self.reader.errors:
            # ECode 0 == ERROR_RESOLVED (the failsafe cleared). Counting
            # it as a trigger double-counts every transient failsafe and
            # turns a log that only holds recovery records into a false
            # critical. Only the occurrence (ECode != 0) is a trigger.
            if err.ecode != 0 and err.subsys in _FS_SUBSYS_KEYS:
                err_triggers.append(
                    (err.subsys, float(err.timestamp), int(err.ecode)))

        # The same failsafe can be written as both ERR and STATUSTEXT;
        # the two lines are emitted together. The window covers logging
        # jitter only and does not grade anything. Each ERR line can
        # suppress at most one text episode, and the nearest one wins.
        text_events = _text_failsafe_events(self.reader)
        consumed_err: set[int] = set()
        kept_text = []
        for event in text_events:
            candidates = [
                (index, err_time)
                for index, (subsys, err_time, _code) in enumerate(err_triggers)
                if index not in consumed_err
                and subsys in _TEXT_DEDUPE_SUBSYS.get(event["key"], ())
                and abs(err_time - event["start"]) <= 10.0
            ]
            if candidates:
                index, _ = min(
                    candidates,
                    key=lambda item: abs(item[1] - event["start"]))
                consumed_err.add(index)
                continue
            kept_text.append(event)
        text_events = kept_text

        critical_events = [
            {"key": _FS_SUBSYS_KEYS[subsys], "time": when, "code": code,
             "source": "err", "end": when, "unresolved": False}
            for subsys, when, code in err_triggers
        ]
        unarmed_events = []
        for event in text_events:
            event["state"] = _text_event_state(self.reader, event)
            if event["state"] == "armed":
                event["source"] = "text"
                critical_events.append(event)
            else:
                unarmed_events.append(event)

        confirmed_unarmed = [event for event in unarmed_events
                             if event["state"] == "unarmed"]
        unknown_events = [event for event in unarmed_events
                          if event["state"] == "unknown"]

        if critical_events:
            details.append(t("failsafe.triggered_count",
                             count=len(critical_events)))
            for event in critical_events:
                name = t(event["key"])
                if event["source"] == "err":
                    line = t("failsafe.triggered_event",
                             name=name, time=f"{event['time']:.1f}",
                             code=event["code"])
                elif event["unresolved"]:
                    line = t("failsafe.triggered_text_unresolved",
                             name=name, time=f"{event['time']:.1f}")
                else:
                    line = t("failsafe.triggered_text_event",
                             name=name, time=f"{event['time']:.1f}",
                             end=f"{event['end']:.1f}")
                details.append(f"  - {line}")
            margin_statuses.append("critical")

        if confirmed_unarmed:
            details.append(t("failsafe.text_unarmed_count",
                             count=len(confirmed_unarmed)))
        if unknown_events:
            details.append(t("failsafe.text_unknown_count",
                             count=len(unknown_events)))
        for event in unarmed_events:
            if event["unresolved"]:
                line = t("failsafe.text_unarmed_unresolved",
                         name=t(event["key"]),
                         time=f"{event['start']:.1f}")
            else:
                line = t("failsafe.text_unarmed_event",
                         name=t(event["key"]),
                         time=f"{event['start']:.1f}")
            details.append(line)

        if not critical_events and not unarmed_events:
            details.append(t("failsafe.none_triggered"))

        # -----------------------------------------------------------
        # 2. Battery voltage margin
        # -----------------------------------------------------------
        if self.reader.has_message("BAT"):
            bat_fig, bat_status = self._battery_margin(details)
            if bat_fig is not None:
                figures.append(bat_fig)
            margin_statuses.append(bat_status)

        # -----------------------------------------------------------
        # 3. GPS NSats margin
        # -----------------------------------------------------------
        if self.reader.has_message("GPS"):
            gps_fig, gps_status = self._gps_margin(details)
            if gps_fig is not None:
                figures.append(gps_fig)
            margin_statuses.append(gps_status)

        # -----------------------------------------------------------
        # 4. EKF variance margin
        # -----------------------------------------------------------
        kf4_type = None
        if self.reader.has_message("XKF4"):
            kf4_type = "XKF4"
        elif self.reader.has_message("NKF4"):
            kf4_type = "NKF4"

        if kf4_type:
            ekf_fig, ekf_status = self._ekf_margin(kf4_type, details)
            if ekf_fig is not None:
                figures.append(ekf_fig)
            margin_statuses.append(ekf_status)

        # -----------------------------------------------------------
        # 5. Overall status
        # -----------------------------------------------------------
        if not margin_statuses:
            margin_statuses = ["info"]

        overall = "good"
        for s in margin_statuses:
            overall = self.worst_status(overall, s)

        # Evidence source per severity:
        # - critical: authoritative — actual failsafe fired OR a metric
        #   crossed the exact ArduPilot threshold during armed flight.
        # - warning: NSats dropped below pre-arm minimum (AHRS_GPS_MINSATS)
        #   during armed flight — pre-arm would have refused but we're
        #   already flying, so it's a navigation-degraded state.
        # All "margin < 20%" style speculative warnings have been removed
        # per POSITIONING.md — they were E-class judgment, not authority.
        # Engineer can still see the exact min_margin in details and
        # decide for themselves.
        if overall == "critical":
            evidence_source = t("failsafe.evidence_critical")
        elif overall == "warning":
            evidence_source = t("failsafe.evidence_warning")
        else:
            evidence_source = ""

        fs_ekf = self.reader.get_param("FS_EKF_THRESH", 0.8)

        suggestions: list[dict] = []
        if overall == "critical":
            summary = t("failsafe.summary_critical")
            plain_summary = t("failsafe.plain_critical")
            # No parameter suggestion here. The old code recommended
            # BATT_LOW_VOLT = 0.92 x observed peak voltage, which is both
            # unsourced and dangerous: on a full 4S pack that resolves to
            # 3.86 V/cell — well above the ~3.5 V/cell guidance — and would
            # itself provoke a false low-battery failsafe. It also fired on
            # *any* critical (EKF, GPS, ...), asserting a battery problem
            # unrelated to the actual trigger. A fired failsafe means "find
            # out why", not "raise the threshold"; battery-threshold advice
            # belongs to the battery analyzer, which has cell-count context.
            other_critical_margin = (
                margin_statuses.count("critical")
                > (1 if critical_events else 0))
            text_only = (bool(critical_events)
                         and not other_critical_margin
                         and all(event["source"] == "text"
                                 and event["key"] in _LINK_TEXT_KEYS
                                 for event in critical_events))
            if text_only:
                action_text = t("failsafe.action_text_critical")
            else:
                action_text = t(
                    "failsafe.action_critical",
                    fs_ekf=f"{fs_ekf:.2g}",
                )
            action_level = "physical_check"
        elif overall == "warning":
            summary = t("failsafe.summary_warning")
            plain_summary = t("failsafe.plain_warning")
            action_text = t("failsafe.action_warning")
            action_level = "physical_check"
        elif self._ekf_single_over:
            summary = t("failsafe.summary_good_ekf")
            plain_summary = t("failsafe.plain_good_ekf")
            action_text = ""
            action_level = "ok"
        elif unknown_events:
            summary = t("failsafe.summary_good_unknown",
                        count=len(unknown_events))
            plain_summary = t("failsafe.plain_good_unknown",
                              count=len(unknown_events))
            action_text = ""
            action_level = "ok"
        elif confirmed_unarmed:
            summary = t("failsafe.summary_good_unarmed",
                        count=len(confirmed_unarmed))
            plain_summary = t("failsafe.plain_good_unarmed",
                              count=len(confirmed_unarmed))
            action_text = ""
            action_level = "ok"
        else:
            summary = t("failsafe.summary_good")
            plain_summary = t("failsafe.plain_good")
            action_text = ""
            action_level = "ok"

        return self.make_result(
            title=t("analyzer.failsafe"),
            status=overall,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=suggestions,
        )

    # ------------------------------------------------------------------
    # Internal: margin computation helpers
    # ------------------------------------------------------------------

    def _battery_margin(
        self, details: list[str]
    ) -> tuple[go.Figure | None, str]:
        """Compute battery voltage margin to failsafe threshold."""
        bat = self.reader.get_messages_as_arrays("BAT", ["Inst", "Volt"])
        ts = bat["_timestamp"]
        if len(ts) == 0:
            return None, "good"

        inst = bat["Inst"]
        volt = bat["Volt"]

        # Use instance 0
        mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
        if not np.any(mask):
            mask = np.ones(len(ts), dtype=bool)

        t_v = ts[mask]
        v = volt[mask]

        # Filter to armed time only — pre-arm voltage (often 0V before
        # the BEC powers on) made min_margin go negative even on
        # perfectly fine flights. Same lesson as battery analyzer.
        armed = self.reader.get_armed_intervals()
        v_for_margin = v
        have_armed = False
        if armed:
            armed_mask = np.zeros(len(t_v), dtype=bool)
            for s, e in armed:
                armed_mask |= (t_v >= s) & (t_v <= e)
            if np.sum(armed_mask) > 10:
                v_for_margin = v[armed_mask]
                have_armed = True

        low_volt = self.thresholds.batt_low_volt
        if low_volt <= 0:
            low_volt = self.reader.get_param("BATT_LOW_VOLT", 0)
        if low_volt <= 0:
            details.append(t("failsafe.batt_low_volt_not_set"))
            return None, "good"

        margin = v_for_margin - low_volt
        min_margin = float(np.nanmin(margin))

        details.append(
            t("failsafe.batt_margin",
              min_margin=f"{min_margin:.2f}",
              low_volt=f"{low_volt:.1f}")
        )

        # Diagnostic semantics per POSITIONING.md:
        #   - critical: voltage crossed BATT_LOW_VOLT during armed flight
        #     (authoritative: ArduPilot triggers低电告警 at this exact threshold)
        #   - good: voltage stayed above the threshold
        # The "margin < 20% of threshold" check was removed — it was pure
        # engineering judgment (E-class), not authoritative. If the voltage
        # dropped close to threshold but didn't cross, the engineer can
        # see the exact min_margin in details and judge themselves.
        # Only a breach inside a known armed window is authoritative. With
        # no armed window, pre-arm 0V / boot / power-down samples push the
        # raw minimum below the threshold — we cannot claim a breach
        # "during armed flight", so do not escalate (min_margin is in the
        # details for the engineer).
        status = "good"
        if min_margin < 0 and have_armed:
            status = "critical"
        elif min_margin < 0:
            details.append(t("failsafe.margin_unarmed"))

        # Figure with shaded margin
        fig = self.make_figure(
            title=t("failsafe.fig_batt_margin"),
            xaxis_title=t("failsafe.time_s"),
            yaxis_title=t("failsafe.voltage_v"),
            height=350,
        )

        fig.add_trace(go.Scattergl(
            x=t_v, y=v,
            mode='lines', name=t("failsafe.voltage"),
            line=dict(color='#1f77b4', width=1.5),
        ))

        # Shaded area: voltage to threshold
        fig.add_trace(go.Scatter(
            x=np.concatenate([t_v, t_v[::-1]]),
            y=np.concatenate([v, np.full(len(t_v), low_volt)[::-1]]),
            fill='toself',
            fillcolor='rgba(92, 184, 92, 0.2)',
            line=dict(width=0),
            name=t("failsafe.margin_zone"),
            showlegend=True,
            hoverinfo='skip',
        ))

        self.add_threshold_hline(
            fig, low_volt,
            t("failsafe.batt_low_line", val=f"{low_volt:.1f}"),
            color="#d9534f",
        )

        return fig, status

    def _gps_margin(
        self, details: list[str]
    ) -> tuple[go.Figure | None, str]:
        """Compute GPS NSats margin to warning threshold."""
        gps = self.reader.get_messages_as_arrays("GPS", ["I", "NSats"])
        ts = gps["_timestamp"]
        if len(ts) == 0:
            return None, "good"

        inst = gps["I"]
        nsats = gps["NSats"]

        # Use instance 0
        mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
        if not np.any(mask):
            mask = np.ones(len(ts), dtype=bool)

        t_g = ts[mask]
        ns = nsats[mask]

        warn_nsats = self.thresholds.gps_nsats_warn
        # Filter to armed time only (pre-arm GPS acquiring doesn't count)
        armed = self.reader.get_armed_intervals()
        ns_for_margin = ns
        have_armed = False
        if armed:
            armed_mask = np.zeros(len(t_g), dtype=bool)
            for s, e in armed:
                armed_mask |= (t_g >= s) & (t_g <= e)
            if np.sum(armed_mask) > 10:
                ns_for_margin = ns[armed_mask]
                have_armed = True

        margin = ns_for_margin - warn_nsats
        min_margin = float(np.nanmin(margin)) if len(margin) > 0 else 0.0

        details.append(
            t("failsafe.gps_margin",
              min_margin=f"{min_margin:.0f}",
              warn_nsats=warn_nsats)
        )

        # Diagnostic semantics:
        #   - warning: NSats dropped below AHRS_GPS_MINSATS during armed
        #     flight (pre-arm would have refused; navigation degraded)
        #   - good: stayed above threshold
        # Without a known armed window, a cold-start NSats=0 would falsely
        # trip this, so only escalate inside an armed window.
        status = "good"
        if min_margin < 0 and have_armed:
            status = "warning"
        elif min_margin < 0:
            details.append(t("failsafe.margin_unarmed"))

        fig = self.make_figure(
            title=t("failsafe.fig_gps_margin"),
            xaxis_title=t("failsafe.time_s"),
            yaxis_title=t("failsafe.nsats"),
            height=300,
        )

        fig.add_trace(go.Scattergl(
            x=t_g, y=ns,
            mode='lines', name='NSats',
            line=dict(color='#2ca02c', width=1.5),
        ))

        self.add_threshold_hline(
            fig, warn_nsats,
            t("failsafe.warn_threshold", val=warn_nsats),
            color="#f0ad4e",
        )

        return fig, status

    def _ekf_margin(
        self, kf4_type: str, details: list[str]
    ) -> tuple[go.Figure | None, str]:
        """Evaluate ArduPilot's EKF failsafe on the logged variances."""
        kf4 = self.reader.get_messages_as_arrays(kf4_type, [
            "C", "SV", "SP", "SH", "SM", "PI", "SS",
        ])
        ts = kf4["_timestamp"]
        if len(ts) == 0:
            return None, "good"

        # AP evaluates the EKF failsafe on the primary lane only. XKF4/
        # NKF4 log one row per core (C column); a variance spike on a
        # non-primary lane must not read as a breach of FS_EKF_THRESH.
        core = kf4.get("C")
        if core is not None and not np.all(np.isnan(core)):
            core_mask = primary_lane_rows(core, kf4["PI"])
            ts = ts[core_mask]
            for _k in ("SV", "SP", "SH", "SM", "SS"):
                kf4[_k] = kf4[_k][core_mask]

        ekf_thresh = self.thresholds.ekf_variance_crit
        if self.reader.get_param("FS_EKF_THRESH", 0.8) <= 0:
            details.append(t("failsafe.ekf_disabled"))
            return None, "good"

        # Combined max variance, for the figure only: ArduPilot's test is
        # not a per-sensor threshold (see lib/ekf_failsafe.py).
        sv = np.asarray(kf4["SV"], dtype=float)
        sp = np.asarray(kf4["SP"], dtype=float)
        sh = np.asarray(kf4["SH"], dtype=float)
        sm = np.asarray(kf4["SM"], dtype=float)
        max_var = np.maximum(np.maximum(sv, sp), np.maximum(sh, sm))
        if all(np.all(np.isnan(a)) for a in (sv, sp, sh, sm)):
            details.append(t("failsafe.ekf_all_nan"))
            return None, "info"

        # Filter to armed flight only (pre-arm EKF initialization noise
        # would otherwise make every log look bad).
        active = np.ones(len(ts), dtype=bool)
        have_armed = False
        armed = self.reader.get_armed_intervals()
        if armed:
            armed_mask = np.zeros(len(ts), dtype=bool)
            for s, e in armed:
                armed_mask |= (ts >= s) & (ts <= e)
            if np.sum(armed_mask) > 10:
                active = armed_mask
                have_armed = True

        # Replay the firmware's own state machine where its shape is
        # known: Copter (variance filter, position gate, ten-check
        # counter) and QuadPlane (mode-gated counter). Other vehicles
        # keep the older continuous-second model below.
        kind = vehicle_kind(self.reader, self.vehicle)
        plane_disabled = kind == "plane"
        gate = None
        if kind == "quadplane":
            gate = plane_posvel_mask(self.reader, ts)
        over = None
        pos_loss = False
        if kind in ("copter", "quadplane"):
            armed = self.reader.get_armed_intervals()
            windows, pos_loss_list, over = ekf_failsafe_replay(
                self.reader, kf4, ts, kind, ekf_thresh, gate)
            triggered = False
            for window, pl in zip(windows, pos_loss_list):
                if window_touches_armed(window, ts, armed):
                    triggered = True
                    pos_loss = pos_loss or pl
        elif plane_disabled:
            triggered = False
        else:
            over = over_threshold(sv, sp, sm, ekf_thresh)
            gated = over & active
            held = sustained(gated, ts)
            triggered = bool(held.any())

        # The raw combination, before any filter and gate, for the
        # transient and unarmed detail lines below.
        raw_over = over_threshold(sv, sp, sm, ekf_thresh)

        max_sv = safe_nanmax(sv[active], default=0.0)
        max_sp = safe_nanmax(sp[active], default=0.0)
        max_sh = safe_nanmax(sh[active], default=0.0)
        max_sm = safe_nanmax(sm[active], default=0.0)
        single_over = max(max_sv, max_sp, max_sm) >= ekf_thresh
        self._ekf_single_over = single_over

        details.append(t(
            "failsafe.ekf_maxima",
            sv=f"{max_sv:.3f}", sp=f"{max_sp:.3f}", sh=f"{max_sh:.3f}",
            sm=f"{max_sm:.3f}", thresh=f"{ekf_thresh:.2f}"))
        if triggered:
            if kind in ("copter", "quadplane"):
                details.append(t("failsafe.ekf_triggered"))
            else:
                details.append(t("failsafe.ekf_triggered_sustained"))
            if pos_loss:
                # The replay fired without any variance breach inside the
                # window: Copter's has_position gate alone failed it.
                details.append(t("failsafe.ekf_position_gate"))
        elif plane_disabled:
            # A fixed-wing does not run the check at all; the line below
            # says so, and counter wording would only mislead.
            pass
        elif (raw_over & active).any():
            # The raw combination was reached without a trigger: on the
            # replay path the counter never got to ten (too brief, or
            # pushed back under by the filter); on the legacy path it
            # never held for the full second.
            met_inside = ((raw_over & active & gate).any()
                          if gate is not None else True)
            transient_key = ("failsafe.ekf_transient"
                             if kind in ("copter", "quadplane")
                             else "failsafe.ekf_transient_sustained")
            if met_inside:
                details.append(t(transient_key))
            elif single_over:
                details.append(t("failsafe.ekf_not_triggered"))
        elif single_over:
            details.append(t("failsafe.ekf_not_triggered"))
        if plane_disabled:
            details.append(t("failsafe.ekf_plane_disabled"))
        elif gate is not None and over is not None:
            inside = over & active & gate
            if (over & active).any() and not inside.any():
                details.append(t("failsafe.ekf_mode_gate"))
        if not have_armed and (raw_over.any() or single_over):
            details.append(t("failsafe.margin_unarmed"))

        # Critical only when ArduPilot's own test would have fired: the
        # ten-check counter reached its limit in the modes where the
        # vehicle runs it. A single variance over the threshold is a
        # health signal for the EKF and compass analyzers, not a failsafe.
        status = "critical" if triggered else "good"

        fig = self.make_figure(
            title=t("failsafe.fig_ekf_margin"),
            xaxis_title=t("failsafe.time_s"),
            yaxis_title=t("failsafe.variance"),
            height=350,
        )

        fig.add_trace(go.Scattergl(
            x=ts, y=max_var,
            mode='lines', name=t("failsafe.max_variance"),
            line=dict(color='#9467bd', width=1.5),
        ))

        # Shaded area: threshold to max_var
        fig.add_trace(go.Scatter(
            x=np.concatenate([ts, ts[::-1]]),
            y=np.concatenate([
                np.full(len(ts), ekf_thresh),
                max_var[::-1],
            ]),
            fill='toself',
            fillcolor='rgba(92, 184, 92, 0.2)',
            line=dict(width=0),
            name=t("failsafe.margin_zone"),
            showlegend=True,
            hoverinfo='skip',
        ))

        self.add_threshold_hline(
            fig, ekf_thresh,
            t("failsafe.ekf_thresh_line", val=f"{ekf_thresh:.2f}"),
            color="#d9534f",
        )

        return fig, status
