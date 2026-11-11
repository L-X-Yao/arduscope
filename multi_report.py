#!/usr/bin/env python3
"""
ArduScope Multi-Log Comparison Report Generator.

Takes multiple .BIN log files, extracts key metrics from each, and generates
an HTML report with a summary comparison table and trend charts.

Usage:
    python3 multi_report.py log1.BIN log2.BIN log3.BIN
    python3 multi_report.py -o comparison.html *.BIN
"""

import argparse
import html
import math
import os
import sys
from dataclasses import fields
from datetime import datetime

import numpy as np

# Ensure package importable when running as script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from lib.fleet_history import (
    extract_flight_time,
    extract_identity,
    format_flight_time,
)
from lib.hover import hover_thrust
from lib.html_report import lttb_downsample
from lib.i18n import t
from lib.log_reader import LogReader
from lib.np_helpers import safe_nanmax
from lib.pipeline import read_flight_log
from lib.thresholds import Thresholds
from lib.trend_analyzer import (
    analyze_trends,
    TrendReport,
    TREND_IMPROVING,
    TREND_REGRESSING,
    TREND_STABLE,
    TREND_CONVERGED,
    TREND_INSUFFICIENT,
)

try:
    import plotly.graph_objects as go
    import plotly.io as pio
    import plotly.offline
except ImportError:
    print("Error: plotly is required. Install with: pip install plotly",
          file=sys.stderr)
    sys.exit(1)


# Fewest armed samples worth narrowing to, as vibration.py and ekf.py use:
# below this the analyzers read the whole log, and the same log has to
# give the same number in both reports.
_VIBE_ARMED_FLOOR = 100
_EKF_ARMED_FLOOR = 10


def _armed_rows(reader: LogReader, ts: np.ndarray, rows: np.ndarray,
                floor: int) -> np.ndarray:
    """rows narrowed to armed time, if more than floor samples remain."""
    armed = np.zeros(len(ts), dtype=bool)
    for start, end in reader.get_armed_intervals():
        armed |= (ts >= start) & (ts <= end)
    narrowed = rows & armed
    return narrowed if np.sum(narrowed) > floor else rows


def _lowest_instance_rows(inst: np.ndarray) -> np.ndarray:
    """Rows of the lowest instance, or every row if the column is empty."""
    if np.all(np.isnan(inst)):
        return np.ones(len(inst), dtype=bool)
    return inst == np.nanmin(inst)


def _primary_core_rows(core: np.ndarray, primary: np.ndarray) -> np.ndarray:
    """Rows of the EKF core flying the vehicle at each sample.

    PI names it and can change in flight. The failsafe only ever sees that
    core, so a backup lane's variance says nothing about convergence.
    """
    if not np.all(np.isnan(primary)):
        return core == primary
    return _lowest_instance_rows(core)


def _clip_counters(vibe: dict) -> list[tuple[np.ndarray, np.ndarray]]:
    """(timestamps, counter) per IMU, in whichever layout the log uses.

    Firmware 4.0+ logs one row per IMU with a single Clip column; older
    firmware logs one row carrying Clip0/1/2.
    """
    ts, inst, clip = vibe["_timestamp"], vibe["IMU"], vibe["Clip"]
    if not np.all(np.isnan(inst)) and not np.all(np.isnan(clip)):
        return [(ts[inst == i], clip[inst == i])
                for i in np.unique(inst[~np.isnan(inst)])]
    return [(ts, vibe[f]) for f in ("Clip0", "Clip1", "Clip2")
            if not np.all(np.isnan(vibe[f]))]


def _armed_clips(reader: LogReader, vibe: dict) -> float | None:
    """Clip events while armed, summed over IMUs. None if not logged.

    The counter runs from boot, so its value alone also counts what
    happened on the ground: handling, or a hard landing after disarm.
    """
    counters = _clip_counters(vibe)
    if not counters:
        return None
    ts = vibe["_timestamp"]
    windows = reader.get_armed_intervals() or [(ts[0], ts[-1])]
    total = 0.0
    for times, counts in counters:
        for start, end in windows:
            inside = counts[(times >= start) & (times <= end)]
            inside = inside[~np.isnan(inside)]
            if len(inside) > 0:
                total += float(inside.max() - inside.min())
    return total


def _vehicle_family(reader: LogReader) -> str | None:
    """Vehicle family, which a vendor's renamed banner does not change."""
    if reader.is_copter:
        return "ArduCopter"
    if reader.is_plane:
        return "ArduPlane"
    return None if reader.vehicle_type == "Unknown" else reader.vehicle_type


def extract_metrics(reader: LogReader,
                    thresholds: Thresholds) -> dict[str, float | None]:
    """
    Extract key metrics from a single log file for comparison.

    Returns dict with metric name -> value (None if unavailable).
    """
    metrics: dict[str, float | None] = {}
    metrics["flight_time"] = extract_flight_time(reader)
    metrics["serial"] = extract_identity(reader)[0]
    metrics["vehicle"] = _vehicle_family(reader)
    metrics["firmware"] = (None if reader.firmware_version == "Unknown"
                           else reader.firmware_version)

    # Web and history reports are built later from these metrics alone, and
    # the EKF limit depends on the aircraft's FS_EKF_THRESH.
    metrics["converge"] = {
        f.name: getattr(thresholds, f.name) for f in fields(Thresholds)
        if f.name.startswith("converge_")
    }

    # -- Max vibration + cumulative clip count --
    if reader.has_message("VIBE"):
        vibe = reader.get_messages_as_arrays("VIBE", [
            "IMU", "VibeX", "VibeY", "VibeZ",
            "Clip", "Clip0", "Clip1", "Clip2",
        ])
        if len(vibe["_timestamp"]) > 0:
            rows = _armed_rows(reader, vibe["_timestamp"],
                               _lowest_instance_rows(vibe["IMU"]),
                               _VIBE_ARMED_FLOOR)
            metrics["max_vibe"] = safe_nanmax(
                vibe["VibeX"][rows], vibe["VibeY"][rows], vibe["VibeZ"][rows],
            )
            metrics["max_clip"] = _armed_clips(reader, vibe)
        else:
            metrics["max_vibe"] = None
            metrics["max_clip"] = None
    else:
        metrics["max_vibe"] = None
        metrics["max_clip"] = None

    # -- Max EKF variance --
    kf4_type = None
    if reader.has_message("XKF4"):
        kf4_type = "XKF4"
    elif reader.has_message("NKF4"):
        kf4_type = "NKF4"

    if kf4_type:
        kf4 = reader.get_messages_as_arrays(kf4_type, [
            "C", "PI", "SV", "SP", "SH", "SM",
        ])
        if len(kf4["_timestamp"]) > 0:
            rows = _armed_rows(reader, kf4["_timestamp"],
                               _primary_core_rows(kf4["C"], kf4["PI"]),
                               _EKF_ARMED_FLOOR)
            metrics["max_ekf_var"] = safe_nanmax(
                kf4["SV"][rows], kf4["SP"][rows],
                kf4["SH"][rows], kf4["SM"][rows],
            )
        else:
            metrics["max_ekf_var"] = None
    else:
        metrics["max_ekf_var"] = None

    # -- Hover thrust --
    metrics["mean_hover_thrust"] = hover_thrust(reader, thresholds)

    # -- Min battery voltage (armed only — pre-arm 0V would skew it) --
    if reader.has_message("BAT"):
        bat = reader.get_messages_as_arrays("BAT", ["Inst", "Volt"])
        t_bat = bat["_timestamp"]
        inst = bat["Inst"]
        volt = bat["Volt"]
        # Instance 0
        mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
        if not np.any(mask):
            mask = np.ones(len(volt), dtype=bool)
        # Filter to armed time so pre-arm 0V doesn't dominate min_volt
        armed = reader.get_armed_intervals()
        if armed:
            armed_mask = np.zeros(len(t_bat), dtype=bool)
            for s, e in armed:
                armed_mask |= (t_bat >= s) & (t_bat <= e)
            mask = mask & armed_mask
        valid = volt[mask]
        valid = valid[~np.isnan(valid)]
        if len(valid) > 0:
            metrics["min_batt_volt"] = float(np.min(valid))
        else:
            metrics["min_batt_volt"] = None
    else:
        metrics["min_batt_volt"] = None

    # -- Attitude RMS error (armed only to avoid ground-time dilution) --
    if reader.has_message("ATT"):
        att = reader.get_messages_as_arrays("ATT", [
            "DesRoll", "Roll", "DesPitch", "Pitch",
        ])
        att_t = att["_timestamp"]
        if len(att_t) > 0:
            roll_err = att["DesRoll"] - att["Roll"]
            pitch_err = att["DesPitch"] - att["Pitch"]
            armed = reader.get_armed_intervals()
            if armed:
                armed_mask = np.zeros(len(att_t), dtype=bool)
                for s, e in armed:
                    armed_mask |= (att_t >= s) & (att_t <= e)
                if np.sum(armed_mask) > 10:
                    roll_err = roll_err[armed_mask]
                    pitch_err = pitch_err[armed_mask]
            axes = [e for e in (roll_err, pitch_err)
                    if not np.all(np.isnan(e))]
            metrics["att_rms_error"] = (
                max(float(np.sqrt(np.nanmean(e ** 2))) for e in axes)
                if axes else None
            )
        else:
            metrics["att_rms_error"] = None
    else:
        metrics["att_rms_error"] = None

    # -- Flight duration --
    metrics["flight_duration"] = reader.get_flight_duration()

    # -- Error count --
    metrics["error_count"] = float(len(reader.errors))

    # -- Max wind speed (from EKF) --
    kf2_type = None
    if reader.has_message("XKF2"):
        kf2_type = "XKF2"
    elif reader.has_message("NKF2"):
        kf2_type = "NKF2"

    if kf2_type:
        kf2 = reader.get_messages_as_arrays(kf2_type, ["VWN", "VWE"])
        if len(kf2["_timestamp"]) > 0:
            ws = np.sqrt(kf2["VWN"]**2 + kf2["VWE"]**2)
            metrics["max_wind_speed"] = safe_nanmax(ws)
        else:
            metrics["max_wind_speed"] = None
    else:
        metrics["max_wind_speed"] = None

    # -- Max IMU temperature --
    if reader.has_message("IMU"):
        imu = reader.get_messages_as_arrays("IMU", ["T"])
        if len(imu["_timestamp"]) > 0 and not np.all(np.isnan(imu["T"])):
            metrics["max_imu_temp"] = float(np.nanmax(imu["T"]))
        else:
            metrics["max_imu_temp"] = None
    else:
        metrics["max_imu_temp"] = None

    # -- Max RATE.*out RMS (METHODIC PID quality metric) --
    # Only meaningful if the vehicle actually flew. A ground test would
    # produce small RATE.*out values (controller has nothing to fight)
    # and falsely indicate "well tuned" — same trap as pid_tuning.py.
    metrics["max_rate_out"] = None
    armed = reader.get_armed_intervals()
    metrics["flew"] = None      # stays None when the log cannot say
    if armed and reader.has_message("POS"):
        pos = reader.get_messages_as_arrays("POS", ["Alt"])
        pos_t = pos["_timestamp"]
        if len(pos_t) > 0:
            pos_mask = np.zeros(len(pos_t), dtype=bool)
            for s, e in armed:
                pos_mask |= (pos_t >= s) & (pos_t <= e)
            alt_armed = pos["Alt"][pos_mask]
            alt_armed = alt_armed[~np.isnan(alt_armed)]
            if len(alt_armed) > 10:
                metrics["flew"] = bool(
                    np.max(alt_armed) - np.min(alt_armed) > 2.0)
    if metrics["flew"] and reader.has_message("RATE"):
        rate = reader.get_messages_as_arrays(
            "RATE", ["ROut", "POut", "YOut"],
        )
        rate_t = rate["_timestamp"]
        if len(rate_t) > 0 and armed:
            rate_mask = np.zeros(len(rate_t), dtype=bool)
            for s, e in armed:
                rate_mask |= (rate_t >= s) & (rate_t <= e)
            rms_per_axis = []
            for ch in ["ROut", "POut", "YOut"]:
                vals = rate[ch][rate_mask]
                vals = vals[~np.isnan(vals)]
                if len(vals) > 10:
                    rms_per_axis.append(
                        float(np.sqrt(np.mean(vals ** 2)))
                    )
            if rms_per_axis:
                metrics["max_rate_out"] = max(rms_per_axis)

    # -- Hover thrust error (|measured - configured|) --
    # Convergence requires the configured MOT_THST_HOVER to match
    # what the vehicle actually used. Source: METHODIC.
    if reader.is_quadplane:
        mot_thst = reader.get_param("Q_M_THST_HOVER",
                   reader.get_param("MOT_THST_HOVER", 0.0))
    else:
        mot_thst = reader.get_param("MOT_THST_HOVER", 0.0)
    measured = metrics.get("mean_hover_thrust")
    if measured is not None and mot_thst and mot_thst > 0:
        metrics["hover_thr_err"] = abs(measured - mot_thst)
    else:
        metrics["hover_thr_err"] = None

    # =====================================================================
    # Intra-flight time-series (for overlay charts in multi-log report)
    # =====================================================================
    _TS_POINTS = 200  # LTTB target — keeps HTML small

    time_series: dict[str, dict[str, list[float]]] = {}

    # -- vibe_ts: max vibration envelope over time --
    try:
        if reader.has_message("VIBE"):
            vibe = reader.get_messages_as_arrays(
                "VIBE", ["VibeX", "VibeY", "VibeZ"],
            )
            vt = vibe["_timestamp"]
            if len(vt) > 0:
                envelope = np.maximum(
                    np.abs(vibe["VibeX"]),
                    np.maximum(np.abs(vibe["VibeY"]), np.abs(vibe["VibeZ"])),
                )
                t0 = vt[0]
                ds_x, ds_y = lttb_downsample(
                    vt - t0, envelope, _TS_POINTS,
                )
                time_series["vibe_ts"] = {
                    "t": ds_x.tolist(), "y": ds_y.tolist(),
                }
    except Exception:
        pass

    # -- ekf_ts: EKF velocity variance (SV) over time --
    try:
        kf4 = None
        if reader.has_message("XKF4"):
            kf4 = reader.get_messages_as_arrays("XKF4", ["SV"])
        elif reader.has_message("NKF4"):
            kf4 = reader.get_messages_as_arrays("NKF4", ["SV"])
        if kf4 is not None and len(kf4["_timestamp"]) > 0:
            kt = kf4["_timestamp"]
            t0 = float(kt[0])
            ds_x, ds_y = lttb_downsample(
                kt - t0, kf4["SV"], _TS_POINTS,
            )
            time_series["ekf_ts"] = {
                "t": ds_x.tolist(), "y": ds_y.tolist(),
            }
    except Exception:
        pass

    # -- batt_ts: battery voltage over time --
    try:
        if reader.has_message("BAT"):
            bat = reader.get_messages_as_arrays("BAT", ["Inst", "Volt"])
            bt = bat["_timestamp"]
            inst = bat["Inst"]
            volt = bat["Volt"]
            mask = np.nan_to_num(inst, nan=-1).astype(int) == 0
            if not np.any(mask):
                mask = np.ones(len(volt), dtype=bool)
            bv = volt[mask]
            btime = bt[mask]
            if len(btime) > 0:
                t0 = btime[0]
                ds_x, ds_y = lttb_downsample(
                    btime - t0, bv, _TS_POINTS,
                )
                time_series["batt_ts"] = {
                    "t": ds_x.tolist(), "y": ds_y.tolist(),
                }
    except Exception:
        pass

    # -- att_ts: attitude roll tracking error over time --
    try:
        if reader.has_message("ATT"):
            att = reader.get_messages_as_arrays(
                "ATT", ["DesRoll", "Roll"],
            )
            at = att["_timestamp"]
            if len(at) > 0:
                roll_err = np.abs(att["DesRoll"] - att["Roll"])
                t0 = at[0]
                ds_x, ds_y = lttb_downsample(
                    at - t0, roll_err, _TS_POINTS,
                )
                time_series["att_ts"] = {
                    "t": ds_x.tolist(), "y": ds_y.tolist(),
                }
    except Exception:
        pass

    if time_series:
        metrics["time_series"] = time_series

    return metrics


# Metric display configuration
METRIC_CONFIG = {
    "max_vibe": {
        "label_key": "multi.metric_max_vibe",
        "format": ".1f",
        "color": "#d62728",
    },
    "max_clip": {
        "label_key": "multi.metric_max_clip",
        "format": ".0f",
        "color": "#c0392b",
    },
    "max_rate_out": {
        "label_key": "multi.metric_max_rate_out",
        "format": ".3f",
        "color": "#e67e22",
    },
    "hover_thr_err": {
        "label_key": "multi.metric_hover_thr_err",
        "format": ".3f",
        "color": "#f39c12",
    },
    "max_ekf_var": {
        "label_key": "multi.metric_max_ekf_var",
        "format": ".3f",
        "color": "#9467bd",
    },
    "mean_hover_thrust": {
        "label_key": "multi.metric_mean_hover_thrust",
        "format": ".3f",
        "color": "#ff7f0e",
    },
    "min_batt_volt": {
        "label_key": "multi.metric_min_batt_volt",
        "format": ".2f",
        "color": "#2ca02c",
    },
    "att_rms_error": {
        "label_key": "multi.metric_att_rms_error",
        "format": ".2f",
        "color": "#1f77b4",
    },
    "flight_duration": {
        "label_key": "multi.metric_flight_duration",
        "format": ".0f",
        "color": "#8c564b",
    },
    "error_count": {
        "label_key": "multi.metric_error_count",
        "format": ".0f",
        "color": "#e377c2",
    },
    "max_wind_speed": {
        "label_key": "multi.metric_max_wind_speed",
        "format": ".1f",
        "color": "#17becf",
    },
    "max_imu_temp": {
        "label_key": "multi.metric_max_imu_temp",
        "format": ".1f",
        "color": "#bcbd22",
    },
}


# =====================================================================
# Parameter diff
# =====================================================================

# Category prefix mapping (first prefix match wins)
_PARAM_CATEGORY_KEYS: list[tuple[str, list[str]]] = [
    ("multi.pcat_pid", ["ATC_RAT_", "ATC_", "PSC_", "Q_A_RAT_", "Q_A_"]),
    ("multi.pcat_motor", ["MOT_", "Q_M_"]),
    ("multi.pcat_battery", ["BATT"]),
    ("multi.pcat_safety", ["FS_", "ARMING_"]),
    ("multi.pcat_gps", ["GPS_"]),
    ("multi.pcat_compass", ["COMPASS_"]),
    ("multi.pcat_imu", ["INS_"]),
    ("multi.pcat_ekf", ["EK2_", "EK3_"]),
    ("multi.pcat_rc", ["RC", "FLTMODE"]),
    ("multi.pcat_nav", ["NAV", "WP_", "RTL_", "LAND_", "LOIT_"]),
    ("multi.pcat_vtol", ["Q_"]),
    ("multi.pcat_log", ["LOG_"]),
    ("multi.pcat_servo", ["SERVO"]),
]


def _categorize_param(name: str) -> str:
    """Return the category locale key for a parameter name."""
    for cat_key, prefixes in _PARAM_CATEGORY_KEYS:
        for pfx in prefixes:
            if name.startswith(pfx):
                return cat_key
    # Fallback: use first word before '_'
    idx = name.find('_')
    return name[:idx] if idx > 0 else t("multi.pcat_other")


def compute_param_diff(
    all_params: list[dict[str, float]],
) -> list[dict]:
    """
    Find parameters that changed between consecutive logs.

    A log with no parameters at all (it failed to parse) is stepped over:
    that is missing data, not every parameter disappearing. A parameter
    missing from a log that has others is a real change.

    Returns list of dicts, each:
        {
            "name": "ATC_RAT_RLL_P",
            "category": "multi.pcat_pid",
            "values": [0.135, 0.180, 0.180],  # one per log, None if absent
            "transitions": [0],  # i: log i + 1 holds the new value
        }
    Sorted by category then name.
    """
    if len(all_params) < 2:
        return []

    # Collect all param names that exist in any log
    all_names: set[str] = set()
    for params in all_params:
        all_names.update(params.keys())

    with_data = [i for i, params in enumerate(all_params) if params]
    changed = []
    for name in sorted(all_names):
        values = [p.get(name) for p in all_params]
        transitions = []
        for i, j in zip(with_data, with_data[1:]):
            v_prev, v_next = values[i], values[j]
            if v_prev is None and v_next is None:
                continue
            if v_prev is None or v_next is None:
                transitions.append(j - 1)
            elif abs(v_prev - v_next) > 1e-9:
                transitions.append(j - 1)
        if transitions:
            changed.append({
                "name": name,
                "category": _categorize_param(name),
                "values": values,
                "transitions": transitions,
            })

    # Sort by category then name
    cat_order = {cat: i for i, (cat, _) in enumerate(_PARAM_CATEGORY_KEYS)}
    changed.sort(key=lambda p: (cat_order.get(p["category"], 999), p["name"]))
    return changed


def _render_param_diff_html(
    log_names: list[str],
    changed_params: list[dict],
) -> str:
    """Render parameter diff section HTML."""
    if not changed_params:
        return f'<p style="color:#999;text-align:center">{t("multi.param_no_diff")}</p>'

    # Summary by category
    cat_counts: dict[str, int] = {}
    for p in changed_params:
        cat_counts[p["category"]] = cat_counts.get(p["category"], 0) + 1

    summary_tags = " ".join(
        f'<span class="cat-tag">{html.escape(t(cat))}: {cnt}</span>'
        for cat, cnt in cat_counts.items()
    )
    summary_html = (
        f'<p style="margin-bottom:12px">'
        f'{t("multi.param_changed_count", count=len(changed_params))}'
        f' &nbsp; {summary_tags}</p>'
    )

    # Table
    header_cells = "".join(f"<th>{html.escape(n)}</th>" for n in log_names)
    rows = []
    prev_cat = ""
    for p in changed_params:
        # Category separator row
        if p["category"] != prev_cat:
            prev_cat = p["category"]
            rows.append(
                f'<tr class="cat-row"><td colspan="{len(log_names) + 2}">'
                f'{html.escape(t(prev_cat))}</td></tr>'
            )

        cells = ""
        for i, val in enumerate(p["values"]):
            # Check if this specific cell is the "new" side of a transition
            is_new = (i - 1) in p["transitions"] if i > 0 else False
            cls = ' class="val-changed"' if is_new else ""
            if val is None:
                cells += f"<td{cls}>—</td>"
            elif not math.isfinite(val):
                cells += f"<td{cls}>{val}</td>"
            elif val == int(val) and abs(val) < 1e8:
                cells += f"<td{cls}>{int(val)}</td>"
            elif abs(val) < 0.001 and val != 0:
                cells += f"<td{cls}>{val:.6f}</td>"
            else:
                cells += f"<td{cls}>{val:.4g}</td>"

        rows.append(
            f'<tr><td class="pname">{html.escape(p["name"])}</td>'
            f'<td class="pcat">{html.escape(t(p["category"]))}</td>'
            f'{cells}</tr>'
        )

    table_html = f"""
    <table class="comparison-table param-table">
        <thead>
            <tr><th>{t("multi.col_param")}</th><th>{t("multi.col_category")}</th>{header_cells}</tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>
    """

    return summary_html + table_html


def _render_param_charts(
    log_names: list[str],
    changed_params: list[dict],
) -> str:
    """Render evolution charts for most-changed parameter categories."""
    if not changed_params:
        return ""

    # Pick top categories by count (max 4)
    cat_params: dict[str, list[dict]] = {}
    for p in changed_params:
        cat_params.setdefault(p["category"], []).append(p)

    top_cats = sorted(cat_params.keys(),
                      key=lambda c: len(cat_params[c]), reverse=True)[:4]

    charts = ""
    palette = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
               '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']

    for cat in top_cats:
        params = cat_params[cat]
        if len(params) > 10:
            # Too many params — show only those with biggest relative change
            def _max_change(p):
                vals = [v for v in p["values"] if v is not None]
                if len(vals) < 2 or max(abs(v) for v in vals) == 0:
                    return 0
                return max(vals) - min(vals)
            params = sorted(params, key=_max_change, reverse=True)[:10]

        fig = go.Figure()
        for j, p in enumerate(params):
            y_vals = []
            x_labels = []
            for i, val in enumerate(p["values"]):
                if val is not None:
                    y_vals.append(val)
                    x_labels.append(log_names[i])
            if len(y_vals) >= 2:
                fig.add_trace(go.Scatter(
                    x=x_labels, y=y_vals,
                    mode='lines+markers',
                    name=p["name"],
                    line=dict(color=palette[j % len(palette)], width=2),
                    marker=dict(size=6),
                ))

        if not fig.data:
            continue

        fig.update_layout(
            title=dict(text=t("multi.param_evolution", cat=t(cat)), font=dict(size=14)),
            xaxis_title=t("multi.axis_log"),
            height=320,
            margin=dict(l=60, r=30, t=50, b=80),
            template="plotly_white",
            font=dict(
                family="PingFang SC, Microsoft YaHei, Noto Sans SC, sans-serif",
                size=12,
            ),
            xaxis=dict(tickangle=-45),
            legend=dict(font=dict(size=10)),
        )

        fig_html = pio.to_html(
            fig, include_plotlyjs=False, full_html=False,
            config={'responsive': True, 'displayModeBar': True,
                    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
                    'displaylogo': False},
        )
        charts += f'<div class="chart-container">{fig_html}</div>\n'

    return charts


# =====================================================================
# Intra-flight time-series overlay charts
# =====================================================================

# Config for overlay chart types: dict key in time_series -> display info
_OVERLAY_CONFIG = [
    ("vibe_ts", "trend.overlay_vibe", "#d62728", "m/s²"),
    ("ekf_ts", "trend.overlay_ekf", "#9467bd", ""),
    ("batt_ts", "trend.overlay_batt", "#2ca02c", "V"),
    ("att_ts", "trend.overlay_att", "#1f77b4", "°"),
]

# Per-log trace colors (up to 10 logs; wraps for more)
_TRACE_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def _render_overlay_charts(
    log_names: list[str],
    all_metrics: list[dict],
) -> str:
    """
    Render intra-flight time-series overlay charts.

    For each metric type (vibe, ekf, batt, att), superimpose the
    downsampled time-series from every log on a single chart.
    """
    sections = []
    for ts_key, label_key, _default_color, unit in _OVERLAY_CONFIG:
        # Collect traces from all logs that have this time-series
        traces = []
        for i, metrics in enumerate(all_metrics):
            ts_data = metrics.get("time_series")
            if not isinstance(ts_data, dict):
                continue
            series = ts_data.get(ts_key)
            if not series:
                continue
            t_arr = series.get("t", [])
            y_arr = series.get("y", [])
            if not t_arr or not y_arr:
                continue
            traces.append((log_names[i], t_arr, y_arr, i))

        if not traces:
            continue

        fig = go.Figure()
        for name, t_arr, y_arr, idx in traces:
            color = _TRACE_PALETTE[idx % len(_TRACE_PALETTE)]
            fig.add_trace(go.Scatter(
                x=t_arr, y=y_arr,
                mode='lines',
                name=name,
                line=dict(color=color, width=1.5),
                opacity=0.85,
            ))

        label = t(label_key)
        y_label = f"{label} ({unit})" if unit else label
        fig.update_layout(
            title=dict(text=label, font=dict(size=14)),
            xaxis_title=t("trend.overlay_time"),
            yaxis_title=y_label,
            height=320,
            margin=dict(l=60, r=30, t=50, b=60),
            template="plotly_white",
            font=dict(
                family=(
                    "PingFang SC, Microsoft YaHei, "
                    "Noto Sans SC, sans-serif"
                ),
                size=12,
            ),
            legend=dict(font=dict(size=10)),
            hovermode="x unified",
        )

        fig_html = pio.to_html(
            fig, include_plotlyjs=False, full_html=False,
            config={'responsive': True, 'displayModeBar': True,
                    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
                    'displaylogo': False},
        )
        sections.append(f'<div class="chart-container">{fig_html}</div>\n')

    return "".join(sections)


# =====================================================================
# HTML report generation
# =====================================================================

def _direction_icon(direction: str) -> str:
    """Map a TrendAnalyzer direction to a single-glyph icon."""
    return {
        TREND_IMPROVING: "↗",
        TREND_REGRESSING: "↘",
        TREND_STABLE: "→",
        TREND_CONVERGED: "✅",
        TREND_INSUFFICIENT: "·",
    }.get(direction, "?")


def _direction_class(direction: str) -> str:
    """CSS class name for a trend direction."""
    return {
        TREND_IMPROVING: "trend-improving",
        TREND_REGRESSING: "trend-regressing",
        TREND_STABLE: "trend-stable",
        TREND_CONVERGED: "trend-converged",
        TREND_INSUFFICIENT: "trend-insufficient",
    }.get(direction, "trend-stable")


def _render_trend_report(report: TrendReport) -> str:
    """
    Render the trend section at the top of the multi-log report.
    Every metric has an auto-generated interpretation.
    """
    # Headline card
    if report.convergence.fully_converged:
        header_class = "trend-header trend-header-converged"
    elif any(m.direction == TREND_REGRESSING for m in report.metrics):
        header_class = "trend-header trend-header-warning"
    else:
        header_class = "trend-header trend-header-progress"

    headline_html = f"""
    <div class="{header_class}">
        <h2>{html.escape(report.headline)}</h2>
        <p class="trend-next-step">{html.escape(report.next_step_text)}</p>
    </div>
    """

    # Per-metric interpretation rows
    rows_html = []
    for m in report.metrics:
        # Skip metrics with no data at all
        if all(v is None for v in m.values):
            continue
        icon = _direction_icon(m.direction)
        css = _direction_class(m.direction)
        rows_html.append(f"""
        <div class="trend-row {css}">
            <span class="trend-icon">{icon}</span>
            <span class="trend-text">{html.escape(m.interpretation)}</span>
        </div>
        """)

    interpretation_html = (
        '<div class="trend-interpretation">'
        + ''.join(rows_html)
        + '</div>'
    )

    notices_html = "".join(
        f'<p class="trend-notice">{html.escape(notice)}</p>'
        for notice in report.notices
    )

    return f"""
    <div class="section section-trend">
        <h2>{t("multi.trend_title")}</h2>
        {headline_html}
        {notices_html}
        {interpretation_html}
    </div>
    """


def _latest_thresholds(all_metrics: list[dict]) -> Thresholds:
    """Convergence limits of the newest log that recorded them."""
    thresholds = Thresholds()
    for metrics in reversed(all_metrics):
        limits = metrics.get("converge")
        if limits:
            for name, value in limits.items():
                setattr(thresholds, name, value)
            break
    return thresholds


def _in_flight_order(log_names: list[str], all_metrics: list[dict],
                     all_params: list[dict] | None):
    """The three parallel lists, oldest flight first.

    Trends compare each log with the one before it, and no caller's order
    can be trusted for that: names sort log_10 before log_9, uploads
    finish in any order, the history page lists newest first. A log with
    no GPS time cannot be placed, so it keeps its slot and the dated logs
    sort around it.
    """
    dated = [i for i, m in enumerate(all_metrics)
             if m.get("flight_time") is not None]
    order = list(range(len(all_metrics)))
    by_time = sorted(dated, key=lambda i: all_metrics[i]["flight_time"])
    for slot, source in zip(dated, by_time):
        order[slot] = source
    return ([log_names[i] for i in order],
            [all_metrics[i] for i in order],
            [all_params[i] for i in order] if all_params else all_params)


def _flight_date_row(all_metrics: list[dict]) -> str:
    """Table row that shows which order the report settled on."""
    cells = ""
    for metrics in all_metrics:
        when = metrics.get("flight_time")
        cells += f"<td>{format_flight_time(when) if when else 'N/A'}</td>"
    return f"<tr><td><strong>{t('multi.row_flight_date')}</strong></td>{cells}</tr>"


def generate_html(
    log_names: list[str],
    all_metrics: list[dict[str, float | None]],
    all_params: list[dict[str, float]] | None = None,
) -> str:
    """Generate the comparison HTML report."""

    plotly_js = plotly.offline.get_plotlyjs()
    log_names, all_metrics, all_params = _in_flight_order(
        log_names, all_metrics, all_params)

    # Build the trend report at the top of the page.
    trend_report = analyze_trends(
        all_metrics, log_names, _latest_thresholds(all_metrics))
    trend_html = _render_trend_report(trend_report)

    # Build comparison table
    table_rows = [_flight_date_row(all_metrics)]
    for metric_key, config in METRIC_CONFIG.items():
        label = t(config['label_key'])
        row = f"<tr><td><strong>{label}</strong></td>"
        for metrics in all_metrics:
            val = metrics.get(metric_key)
            if val is not None:
                fmt = config["format"]
                row += f"<td>{val:{fmt}}</td>"
            else:
                row += "<td>N/A</td>"
        row += "</tr>"
        table_rows.append(row)

    header_cells = "".join(
        f"<th>{html.escape(name)}</th>" for name in log_names
    )
    table_html = f"""
    <table class="comparison-table">
        <thead>
            <tr><th>{t("multi.col_metric")}</th>{header_cells}</tr>
        </thead>
        <tbody>
            {''.join(table_rows)}
        </tbody>
    </table>
    """

    # Build trend charts
    charts_html = ""
    for metric_key, config in METRIC_CONFIG.items():
        values = []
        labels = []
        for i, metrics in enumerate(all_metrics):
            val = metrics.get(metric_key)
            if val is not None:
                values.append(val)
                labels.append(log_names[i])

        if not values:
            continue

        label = t(config['label_key'])
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=labels, y=values,
            mode='lines+markers',
            name=label,
            line=dict(color=config['color'], width=2),
            marker=dict(size=8),
        ))
        fig.update_layout(
            title=dict(text=label, font=dict(size=14)),
            xaxis_title=t("multi.axis_log"),
            yaxis_title=label,
            height=300,
            margin=dict(l=60, r=30, t=50, b=80),
            template="plotly_white",
            font=dict(
                family=(
                    "PingFang SC, Microsoft YaHei, "
                    "Noto Sans SC, sans-serif"
                ),
                size=12,
            ),
            xaxis=dict(tickangle=-45),
        )

        fig_html = pio.to_html(
            fig, include_plotlyjs=False, full_html=False,
            config={'responsive': True, 'displayModeBar': True,
                    'modeBarButtonsToRemove': ['lasso2d', 'select2d'],
                    'displaylogo': False},
        )
        charts_html += f'<div class="chart-container">{fig_html}</div>\n'

    # Parameter diff section
    param_charts_html = ""
    if sum(1 for params in all_params or [] if params) >= 2:
        changed = compute_param_diff(all_params)
        param_diff_html = _render_param_diff_html(log_names, changed)
        param_charts_html = _render_param_charts(log_names, changed)
    else:
        param_diff_html = (f'<p style="color:#999;text-align:center">'
                           f'{t("multi.param_unknown")}</p>')

    # Time-series overlay charts
    overlay_charts_html = _render_overlay_charts(log_names, all_metrics)

    # Feedback channel — same prefilled-message pattern as single-log
    feedback_prefill = "\n".join([
        t("multi.feedback_prefill_header"),
        "",
        t("multi.feedback_prefill_comparing", count=len(log_names)),
        *[f"  - {n}" for n in log_names],
        "",
        t("multi.feedback_prefill_issue"),
        t("multi.feedback_prefill_placeholder"),
        "",
        t("multi.feedback_prefill_expect"),
        "",
    ])
    feedback_prefill_attr = html.escape(feedback_prefill, quote=True)
    feedback_html = f"""
<div class="feedback-section">
    <h3>{t("multi.feedback_title")}</h3>
    <p class="feedback-hint">
        {t("multi.feedback_desc")}
    </p>
    <textarea id="feedback-text">{feedback_prefill_attr}</textarea>
    <div class="feedback-actions">
        <button onclick="(function(){{
            var ta = document.getElementById('feedback-text');
            function done() {{
                var s = document.getElementById('feedback-status');
                s.textContent = '{t("multi.feedback_copied")}';
                s.classList.add('shown');
                setTimeout(function(){{ s.classList.remove('shown'); }}, 4000);
            }}
            function legacy() {{
                ta.select();
                if (document.execCommand('copy')) done();
            }}
            ta.select();
            if (navigator.clipboard) navigator.clipboard.writeText(ta.value).then(done, legacy);
            else legacy();
        }})()\">{t("multi.feedback_copy_btn")}</button>
        <button data-prefill="{feedback_prefill_attr}" onclick="document.getElementById('feedback-text').value = this.dataset.prefill" style="background:#999;">{t("multi.feedback_reset_btn")}</button>
        <span id="feedback-status" class="feedback-status"></span>
    </div>
</div>
"""

    return f"""<!DOCTYPE html>
<html lang="{t('_lang_code')}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{t("multi.page_title")}</title>
<script>{plotly_js}</script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans SC",
                 -apple-system, BlinkMacSystemFont, sans-serif;
    background: #f5f6fa;
    color: #333;
    line-height: 1.6;
}}
.header {{
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    color: #fff;
    padding: 24px 0;
    text-align: center;
}}
.header h1 {{ font-size: 24px; margin-bottom: 8px; }}
.header p {{ font-size: 14px; color: rgba(255,255,255,0.7); }}
.container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px;
}}
.section {{
    background: #fff;
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}}
.section h2 {{
    font-size: 16px;
    margin-bottom: 16px;
    color: #555;
}}
.comparison-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}}
.comparison-table th, .comparison-table td {{
    padding: 8px 12px;
    text-align: center;
    border-bottom: 1px solid #eee;
}}
.comparison-table th {{
    background: #f7f8fa;
    font-weight: 600;
}}
.comparison-table td:first-child {{
    text-align: left;
}}
.chart-container {{ margin: 12px 0; }}
.footer {{
    text-align: center;
    padding: 20px;
    color: #999;
    font-size: 12px;
}}
/* Parameter diff styles */
.param-table .pname {{ font-family: monospace; font-size: 12px; text-align: left; }}
.param-table .pcat {{ font-size: 11px; color: #999; }}
.param-table .val-changed {{
    background: #fff3cd; font-weight: 600; color: #856404;
}}
.param-table .cat-row {{
    background: #e9ecef; font-weight: 600; font-size: 12px; color: #495057;
}}
.param-table .cat-row td {{ text-align: left; padding: 6px 12px; }}
.cat-tag {{
    display: inline-block; background: #e9ecef; padding: 2px 8px;
    border-radius: 3px; font-size: 12px; color: #555; margin: 2px;
}}
/* v2 trend report */
.section-trend {{ border-left: 6px solid #5cb85c; }}
.trend-header {{
    padding: 16px 20px;
    border-radius: 6px;
    margin-bottom: 16px;
}}
.trend-header h2 {{ font-size: 18px; margin: 0 0 6px 0; color: #222; }}
.trend-header .trend-next-step {{ font-size: 14px; color: #555; margin: 0; }}
.trend-header-converged {{ background: #d6f0d6; }}
.trend-header-progress  {{ background: #d6e8f9; }}
.trend-header-warning   {{ background: #fff3cd; }}
.trend-interpretation {{ display: flex; flex-direction: column; gap: 8px; }}
.trend-row {{
    display: flex; gap: 12px; align-items: flex-start;
    padding: 8px 12px; border-radius: 4px;
    background: #fafafa;
}}
.trend-row .trend-icon {{
    font-size: 18px; flex-shrink: 0; min-width: 24px; text-align: center;
}}
.trend-row .trend-text {{ font-size: 14px; color: #444; }}
.trend-improving    {{ border-left: 3px solid #5cb85c; }}
.trend-regressing   {{ border-left: 3px solid #d9534f; background: #fff5f5; }}
.trend-stable       {{ border-left: 3px solid #999; }}
.trend-converged    {{ border-left: 3px solid #5cb85c; background: #f0faf0; }}
.trend-insufficient {{ border-left: 3px solid #ccc; opacity: 0.7; }}
.trend-notice {{
    margin-bottom: 16px; padding: 12px 16px;
    background: #fff8e1; border-left: 3px solid #ffc107;
    border-radius: 4px; font-size: 13px; color: #555;
}}
.feedback-section {{
    background: #fff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 20px;
    margin: 24px 0;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}}
.feedback-section h3 {{
    font-size: 16px; margin: 0 0 8px 0; color: #333;
}}
.feedback-section p.feedback-hint {{
    font-size: 13px; color: #666; margin: 0 0 12px 0; line-height: 1.6;
}}
.feedback-section textarea {{
    width: 100%; min-height: 100px; padding: 10px 12px;
    font-family: inherit; font-size: 13px;
    border: 1px solid #ccc; border-radius: 4px;
    box-sizing: border-box; resize: vertical;
}}
.feedback-section .feedback-actions {{
    display: flex; gap: 8px; margin-top: 10px; align-items: center;
}}
.feedback-section button {{
    background: #5cb85c; color: #fff; border: none;
    padding: 8px 18px; font-size: 13px; border-radius: 4px;
    cursor: pointer; font-family: inherit;
}}
.feedback-section button:hover {{ background: #4cae4c; }}
.feedback-section .feedback-status {{
    font-size: 12px; color: #5cb85c; margin-left: 8px;
    transition: opacity .3s; opacity: 0;
}}
.feedback-section .feedback-status.shown {{ opacity: 1; }}
@media print {{ .feedback-section {{ display: none !important; }} }}
@media (max-width: 768px) {{
    .container {{ padding: 12px; }}
    .comparison-table {{ display: block; overflow-x: auto; }}
}}
@media (max-width: 480px) {{
    .container {{ padding: 8px; }}
    h1 {{ font-size: 18px; }}
}}
</style>
</head>
<body>

<div class="header">
    <h1>{t("multi.page_title")}</h1>
    <p>{t("multi.header_subtitle", count=len(log_names), timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
</div>

<div class="container">
    {trend_html}

    <div class="section">
        <h2>{t("multi.section_comparison")}</h2>
        {table_html}
    </div>

    <div class="section">
        <h2>{t("multi.section_charts")}</h2>
        {charts_html}
    </div>

    {'<div class="section"><h2>' + t("trend.overlay_title") + '</h2>' + overlay_charts_html + '</div>' if overlay_charts_html else ''}

    {'<div class="section"><h2>' + t("multi.section_param_diff") + '</h2>' + param_diff_html + '</div>' if param_diff_html else ''}

    {'<div class="section"><h2>' + t("multi.section_param_trends") + '</h2>' + param_charts_html + '</div>' if param_charts_html else ''}

    {feedback_html}
</div>

<footer class="footer">
    <p>{t('multi.footer')}</p>
</footer>

</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description="ArduScope multi-log comparison report generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "logfiles",
        nargs="+",
        help="ArduPilot .BIN log files to compare",
    )
    parser.add_argument(
        "-o", "--output",
        default="multi_report.html",
        help="Output HTML file path (default: multi_report.html)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the report in a browser after generation",
    )
    parser.add_argument(
        "--lang",
        choices=["zh", "en"],
        default="zh",
        help="Report language (default: zh)",
    )
    args = parser.parse_args()

    from lib.i18n import set_locale
    set_locale(args.lang)

    # Validate files
    for f in args.logfiles:
        if not os.path.isfile(f):
            print(t("cli.err_file_not_found", path=f), file=sys.stderr)
            sys.exit(1)

    print(t("cli.analyzing_n", n=len(args.logfiles)))

    log_names = []
    all_metrics = []
    all_params = []

    for i, logfile in enumerate(args.logfiles):
        name = os.path.basename(logfile)
        log_names.append(name)
        print(t("cli.progress", i=i+1, n=len(args.logfiles), name=name), end=" ")

        try:
            reader = read_flight_log(logfile)
            thresholds = Thresholds.from_params(
                reader.params, reader.canonical_vehicle_type
            )
            metrics = extract_metrics(reader, thresholds)
            all_metrics.append(metrics)
            all_params.append(dict(reader.params))
            print(t("cli.progress_ok", vehicle=reader.vehicle_type, params=len(reader.params)))
        except Exception as e:
            print(t("cli.err_parse", error=e))
            all_metrics.append({})
            all_params.append({})

    # Parameter diff summary
    if len(all_params) >= 2:
        changed = compute_param_diff(all_params)
        print(t("cli.param_changes", n=len(changed)))
    else:
        changed = []

    # Generate report
    print(t("cli.generating_report", path=args.output))
    html_output = generate_html(log_names, all_metrics, all_params)

    os.makedirs(
        os.path.dirname(os.path.abspath(args.output)) or '.', exist_ok=True
    )
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(html_output)

    file_size = os.path.getsize(args.output)
    print(t("cli.done_file_size", size=f"{file_size / 1024:.0f}"))

    if not args.no_open:
        import webbrowser
        from pathlib import Path
        url = Path(os.path.abspath(args.output)).as_uri()
        print(t("cli.opening_browser", url=url))
        webbrowser.open(url)


if __name__ == "__main__":
    main()
