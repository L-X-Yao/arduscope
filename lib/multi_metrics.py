"""Per-log metrics for the multi-log comparison report.

Kept out of multi_report.py so the web app and the history reports can
reuse them without importing the CLI module.
"""

from __future__ import annotations

from dataclasses import fields

import numpy as np

from lib.fleet_history import extract_flight_time, extract_identity
from lib.hover import hover_thrust
from lib.html_report import lttb_downsample
from lib.log_reader import LogReader
from lib.np_helpers import safe_nanmax
from lib.thresholds import Thresholds


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
