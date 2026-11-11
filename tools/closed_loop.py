#!/usr/bin/env python3
"""
Check ArduScope's parameter suggestions by flying them.

Flies a SITL scenario, loads what ArduScope suggests for that flight, flies
again, and compares measures read straight from the logs, so the verdict does
not depend on ArduScope grading its own advice. SITL is not an aircraft: this
can rule a suggestion out, but it cannot prove one on a real airframe.

Usage:
    python3 tools/closed_loop.py --sitl-dir ../ardupilot copter_normal
    python3 tools/closed_loop.py --sitl-dir ../ardupilot --repeats 3 \
        plane_tecs_tuning copter_battery_aging
    python3 tools/closed_loop.py --sitl-dir ../ardupilot copter_high_vibration \
        --apply ATC_RAT_RLL_I=0.108 --apply ATC_RAT_YAW_I=0.014

--apply flies these parameters in place of the suggestions, to test one alone.
--set changes the scenario on both sides, to test a suggestion on another
aircraft. With no scenario named, every scenario flies. Logs go to tests/sitl/logs under
a cl_ prefix, which the regression tests ignore. Flights take the SITL ports,
so only one run can go at a time.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
import warnings

import numpy as np
from pymavlink import mavutil

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "tests", "sitl")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import generate_logs  # noqa: E402
from lib.log_reader import LogReader  # noqa: E402
from lib.outputs import motor_channels, read_outputs  # noqa: E402
from lib.param_conflict import merge_suggestions  # noqa: E402
from lib.pipeline import run_analysis  # noqa: E402
from lib.plane_phase import FLIGHT_STAGE_NORMAL, stat_at  # noqa: E402

# Height above home that counts as off the ground, in metres.
_AIRBORNE_M = 2.0
_MEASURED_TYPES = ["ATT", "RATE", "CTUN", "POS", "TECS", "STAT", "IMU",
                   "PIDR", "PIDP", "RCOU", "ESC"]
# Where motor noise sits, and how long to wait for it to settle after takeoff.
_NOISE_BAND_HZ = (60.0, 400.0)
_NOISE_SETTLE_S = 10.0


def rms(values) -> float:
    """Root mean square of the finite values, NaN if there are none."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return float(np.sqrt(np.mean(values ** 2))) if len(values) else float("nan")


def wrap_degrees(angle):
    """Fold an angle difference into -180 to 180."""
    return (np.asarray(angle, dtype=float) + 180.0) % 360.0 - 180.0


def _airborne(reader) -> tuple[float, float] | None:
    pos = reader.get_messages_as_arrays("POS", ["RelHomeAlt"])
    up = pos["_timestamp"][np.nan_to_num(pos["RelHomeAlt"]) > _AIRBORNE_M]
    return (float(up[0]), float(up[-1])) if len(up) > 1 else None


def gyro_batches(path: str):
    """(time_s, rate_hz, x, y) of each batch from the first gyro, in deg/s."""
    log = mavutil.mavlink_connection(path, robust_parsing=True)
    head, xs, ys = None, [], []
    while (m := log.recv_match(type=["ISBH", "ISBD"])) is not None:
        if m.get_type() == "ISBD":
            if head is not None and m.N == head.N:
                xs.append(np.array(m.x, dtype=float) / head.mul)
                ys.append(np.array(m.y, dtype=float) / head.mul)
            continue
        if head is not None and xs:
            yield _batch(head, xs, ys)
        head = m if m.type == 1 and m.instance == 0 else None
        xs, ys = [], []
    if head is not None and xs:
        yield _batch(head, xs, ys)


def _batch(head, xs: list, ys: list) -> tuple:
    return (head.SampleUS / 1e6, float(head.smp_rate),
            np.degrees(np.concatenate(xs)), np.degrees(np.concatenate(ys)))


def spectrum(batches) -> tuple | None:
    """Mean amplitude spectrum of roll and pitch gyro over the batches."""
    total, freqs, count = None, None, 0
    for _time, rate, x, y in batches:
        window = np.hanning(len(x))
        amp = sum(np.abs(np.fft.rfft((axis - axis.mean()) * window))
                  for axis in (x, y)) / 2 * 2 / window.sum()
        if total is None:
            total, freqs = np.zeros_like(amp), np.fft.rfftfreq(len(x), 1 / rate)
        if len(amp) == len(total):
            total += amp
            count += 1
    return (freqs, total / count) if count else None


def band_noise(freqs, amp) -> dict:
    """Largest peak and overall level in the motor noise band, in deg/s."""
    band = (freqs >= _NOISE_BAND_HZ[0]) & (freqs <= _NOISE_BAND_HZ[1])
    return {"gyro_peak": float(np.max(amp[band])),
            "gyro_peak_hz": float(freqs[band][np.argmax(amp[band])]),
            "gyro_band": float(np.sqrt(np.sum(amp[band] ** 2)))}


def measures(path: str) -> dict:
    """Tracking and noise figures from the log, with no ArduScope verdict."""
    reader = LogReader(path, preload_types=_MEASURED_TYPES)
    window = _airborne(reader)
    if window is None:
        return {"flew": 0.0}
    start, end = window
    out = {"flew": end - start}

    def rows(data):
        return (data["_timestamp"] >= start) & (data["_timestamp"] <= end)

    att = reader.get_messages_as_arrays(
        "ATT", ["DesRoll", "Roll", "DesPitch", "Pitch", "DesYaw", "Yaw"])
    m = rows(att)
    out["att_roll"] = rms((att["DesRoll"] - att["Roll"])[m])
    out["att_pitch"] = rms((att["DesPitch"] - att["Pitch"])[m])
    # Plane logs DesYaw as a 0 placeholder (ArduPlane/Log.cpp), so there is no
    # yaw demand to track there.
    if not reader.has_message("TECS"):
        out["att_yaw"] = rms(wrap_degrees(att["DesYaw"] - att["Yaw"])[m])

    rate = reader.get_messages_as_arrays(
        "RATE", ["RDes", "R", "PDes", "P", "YDes", "Y"])
    m = rows(rate)
    out["rate_roll"] = rms((rate["RDes"] - rate["R"])[m])
    out["rate_pitch"] = rms((rate["PDes"] - rate["P"])[m])
    out["rate_yaw"] = rms((rate["YDes"] - rate["Y"])[m])

    if reader.has_message("TECS"):
        tecs = reader.get_messages_as_arrays(
            "TECS", ["hdem", "h", "spdem", "sp"])
        ts = tecs["_timestamp"]
        m = ((stat_at(reader, ts, "Stage") == FLIGHT_STAGE_NORMAL)
             & (stat_at(reader, ts, "isFlying") > 0.5))
        out["tecs_height"] = rms((tecs["hdem"] - tecs["h"])[m])
        out["tecs_speed"] = rms((tecs["spdem"] - tecs["sp"])[m])
    elif reader.has_message("CTUN"):
        ctun = reader.get_messages_as_arrays("CTUN", ["DAlt", "Alt"])
        out["alt_track"] = rms((ctun["DAlt"] - ctun["Alt"])[rows(ctun)])

    # What a filter or a lower gain is for, next to what it costs above.
    for message, axis in (("PIDR", "roll"), ("PIDP", "pitch")):
        if reader.has_message(message):
            pid = reader.get_messages_as_arrays(message, ["D"])
            out[f"d_{axis}"] = rms(pid["D"][rows(pid)])
    channels = motor_channels(reader)
    if channels:
        outputs = read_outputs(reader, channels)
        m = rows(outputs)
        steps = [rms(np.diff(outputs[ch][m])) for ch in channels]
        out["motor_jitter"] = float(np.nanmean(steps)) / np.sqrt(2.0)
    if reader.has_message("ESC"):
        esc = reader.get_messages_as_arrays("ESC", ["RPM"])
        out["esc_hz"] = float(np.nanmedian(esc["RPM"][rows(esc)])) / 60.0
    if reader.has_message("ISBH"):
        settled = [b for b in gyro_batches(path)
                   if start + _NOISE_SETTLE_S <= b[0] <= end - _NOISE_SETTLE_S]
        found = spectrum(settled)
        if found is not None:
            out.update(band_noise(*found))
    return out


def writable(merged: list[dict]) -> dict:
    """The rows of merge_suggestions() a .param file would hold."""
    return {s["param"]: float(s["suggested"]) for s in merged
            if s.get("suggested") not in (None, "", "-")}


def verdict(path: str) -> dict:
    """What ArduScope says about the log, and what it would change."""
    result = run_analysis(path)
    merged, _conflicts = merge_suggestions(list(result.results_by_key.values()))
    return {
        "health": result.health_score,
        "flagged": {key: r.status for key, r in result.results_by_key.items()
                    if r.status in ("warning", "critical")},
        "suggested": writable(merged),
    }


def fly(scenario: dict, tag: str, repeats: int, sitl_dir: str) -> list[dict]:
    """Fly the scenario. A flight that fails is reported and left out."""
    flights = []
    for i in range(repeats):
        run = copy.deepcopy(scenario)
        run["name"] = f"cl_{scenario['name']}_{tag}{i}"
        try:
            path = generate_logs.run_scenario(run, sitl_dir)
        except generate_logs.ScenarioFailed as e:
            print(f"   {run['name']} FAILED: {e}", flush=True)
            continue
        flights.append({"path": path, **measures(path), **verdict(path)})
    return flights


def compare(before: list[dict], after: list[dict]) -> list[dict]:
    """Mean of each measure both sides have, with the change in percent."""
    rows = []
    for key in before[0] if before else []:
        a = [f[key] for f in before if isinstance(f.get(key), (int, float))
             and np.isfinite(f[key])]
        b = [f[key] for f in after if isinstance(f.get(key), (int, float))
             and np.isfinite(f[key])]
        if not a or not b:
            continue
        mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
        rows.append({"measure": key, "before": mean_a, "after": mean_b,
                     "change_pct": ((mean_b - mean_a) / mean_a * 100
                                    if mean_a else float("nan"))})
    return rows


def parse_apply(pairs: list[str]) -> dict:
    """["NAME=1.5"] as {"NAME": 1.5}."""
    params = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--apply wants NAME=VALUE, got {pair!r}")
        params[name] = float(value)
    return params


def check_scenario(scenario: dict, args) -> dict | None:
    print(f"== {scenario['name']}", flush=True)
    scenario = {**scenario, "sitl_params": {**scenario.get("sitl_params", {}),
                                            **parse_apply(args.set)}}
    before = fly(scenario, "base", args.repeats, args.sitl_dir)
    if not before:
        print("   the baseline never flew", flush=True)
        return None
    # Flights can differ, and the first is no better a judge than the rest.
    params = parse_apply(args.apply) or max(
        (f["suggested"] for f in before), key=len)
    print(f"   applying {params}", flush=True)
    if any(f["suggested"] != before[0]["suggested"] for f in before):
        print("   flights disagree on the suggestions:",
              [f["suggested"] for f in before], flush=True)
    if not params:
        return {"scenario": scenario["name"], "params": {}, "before": before}

    changed = copy.deepcopy(scenario)
    changed["sitl_params"] = {**changed.get("sitl_params", {}), **params}
    after = fly(changed, "fix", args.repeats, args.sitl_dir)
    print(f"   flights: before {len(before)}/{args.repeats}, "
          f"after {len(after)}/{args.repeats}", flush=True)
    rows = compare(before, after)
    for row in rows:
        print(f"     {row['measure']:12s} {row['before']:9.3f} -> "
              f"{row['after']:9.3f}  {row['change_pct']:+7.1f}%", flush=True)
    print(f"     flagged before {before[0]['flagged']}", flush=True)
    print(f"     flagged after  {after[0]['flagged'] if after else None}",
          flush=True)
    return {"scenario": scenario["name"], "params": params, "before": before,
            "after": after, "compare": rows}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fly ArduScope's suggestions in SITL and compare")
    parser.add_argument("scenarios", nargs="*",
                        help="names from tests/sitl/scenarios.json")
    parser.add_argument("--sitl-dir", required=True, type=os.path.abspath,
                        help="ArduPilot tree with build/sitl binaries")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--apply", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="fly this in place of the suggestions")
    parser.add_argument("--set", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="fly this before and after")
    parser.add_argument("--json", help="also write the results to this file")
    args = parser.parse_args()

    warnings.filterwarnings("ignore")
    known = generate_logs.load_scenarios()
    unknown = set(args.scenarios) - {s["name"] for s in known}
    if unknown:
        parser.error(f"unknown scenarios: {sorted(unknown)}")
    wanted = [s for s in known
              if not args.scenarios or s["name"] in args.scenarios]
    report = [r for r in (check_scenario(s, args) for s in wanted) if r]
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    return 0 if len(report) == len(wanted) else 1


if __name__ == "__main__":
    sys.exit(main())
