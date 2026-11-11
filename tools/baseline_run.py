#!/usr/bin/env python3
"""
Run every analyzer against every log in a given directory tree and
emit a flat JSON baseline: one record per (log, analyzer) with the
status / action_level / summary / warning count.

Purpose:
  - Establish a ground-truth snapshot of what the diagnostic tool
    currently reports on real flight logs.
  - Serve as reference data when calibrating or re-tuning thresholds
    (POSITIONING.md mandates METHODIC + real-log calibration, no
    personal-experience tweaks).
  - Detect regressions: re-run after any analyzer change and diff
    the output JSON.

Usage:
    python3 tools/baseline_run.py /path/to/logs_root -o baseline.json
    python3 tools/baseline_run.py /path/to/logs_root --pattern "*.bin"
    python3 tools/baseline_run.py /path/to/logs_root --jobs 6 --disk-cache

--jobs N analyzes N logs at once, largest first, one process per log so
memory is returned between logs. --disk-cache keeps parsed logs in
ARDUSCOPE_LOG_CACHE (default .log_cache/), which makes repeat runs far
cheaper; budget roughly 2x the size of the corpus on disk.

Output JSON schema:
    {
      "generated_at": "2026-04-15T20:50:00",
      "tool_version": "<git sha>",
      "logs": [
        {
          "relpath": "2026-04/flight1.BIN",
          "size_mb": 462.1,
          "vehicle_type": "ArduPlane",
          "firmware": "...",
          "duration_s": 300.5,
          "analyzers": {
            "ekf":   {"status": "warning", "action": "physical_check",
                      "summary": "..."},
            ...
          },
          "status_counts": {"good": 14, "warning": 3, ...},
          "error": null
        },
        ...
      ]
    }
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Make lib/ and analyzers/ importable when run from anywhere
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LA_DIR = os.path.dirname(_SCRIPT_DIR)
if _LA_DIR not in sys.path:
    sys.path.insert(0, _LA_DIR)

from lib.log_reader import LogReader  # noqa: E402
from lib.thresholds import Thresholds  # noqa: E402
from lib.vehicle_detect import detect_vehicle  # noqa: E402
from lib.pipeline import _get_preload_types  # noqa: E402
from analyzers import AnalysisResult, discover_analyzers  # noqa: E402


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _analyze_one(logfile: str, registry, disk_cache: bool = False) -> dict:
    """Run every analyzer on the log. Return a flat dict suitable for JSON."""
    t_start = time.time()
    out = {
        "analyzers": {},
        "status_counts": {"good": 0, "info": 0, "warning": 0, "critical": 0},
        "error": None,
    }
    try:
        if disk_cache:
            reader = LogReader(logfile, disk_cache=True)
        else:
            reader = LogReader(logfile, preload_types=_get_preload_types())
    except Exception as e:
        out["error"] = f"LogReader failed: {e}"
        return out

    out["vehicle_type"] = reader.vehicle_type
    out["firmware"] = reader.firmware_version
    out["duration_s"] = round(reader.get_flight_duration(), 1)

    try:
        vehicle_info = detect_vehicle(reader)
        thresholds = Thresholds.from_params(
            reader.params, reader.canonical_vehicle_type)
    except Exception as e:
        out["error"] = f"vehicle/thresholds failed: {e}"
        return out

    for key, cls in registry:
        try:
            analyzer = cls(reader, thresholds, vehicle_info)
            if not analyzer.can_run():
                out["analyzers"][key] = {
                    "status": "skip",
                    "action": "ok",
                    "summary": "缺少必需消息类型",
                }
                continue
            result: AnalysisResult = analyzer.analyze()
            status = result.status or "info"
            out["analyzers"][key] = {
                "status": status,
                "action": result.action_level or "ok",
                "summary": (result.summary or "")[:200],
                "n_param_suggestions": len(result.param_suggestions or []),
                "suggestions": sorted(
                    f"{p['param']}={p['suggested']}"
                    for p in _all_suggestions(result)),
                # POSITIONING contract: the pipeline keeps such a result
                # and flags it, so this is where the bug surfaces.
                "missing_evidence": (status in ("warning", "critical")
                                     and not result.evidence_source),
            }
            if status in out["status_counts"]:
                out["status_counts"][status] += 1
        except Exception as e:
            out["analyzers"][key] = {
                "status": "error",
                "action": "ok",
                "summary": f"exception: {type(e).__name__}: {str(e)[:100]}",
            }

    out["elapsed_s"] = round(time.time() - t_start, 1)
    return out


def _all_suggestions(result: AnalysisResult) -> list[dict]:
    """Suggestions that reach the .param file: parent and sub-results."""
    found = list(result.param_suggestions or [])
    for sub in result.sub_results or []:
        found.extend(sub.param_suggestions or [])
    return found


def _worker(job: tuple[str, str, bool]) -> dict:
    """Pool entry point: analyze one log in a fresh process."""
    logfile, rel, disk_cache = job
    size_mb = os.path.getsize(logfile) / (1024 * 1024)
    entry = {"relpath": rel, "size_mb": round(size_mb, 1)}
    entry.update(_analyze_one(logfile, discover_analyzers(), disk_cache))
    return entry


def _report(i: int, total: int, entry: dict) -> None:
    sc = entry["status_counts"]
    badge = (f"crit={sc['critical']} warn={sc['warning']} "
             f"info={sc['info']} good={sc['good']}")
    err = f" ERROR: {entry['error']}" if entry["error"] else ""
    print(f"[{i}/{total}] {entry['relpath']} ({entry['size_mb']} MB) "
          f"{badge}{err}  ({entry.get('elapsed_s', 0)}s)", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs_root", help="Directory to recursively scan")
    ap.add_argument("-o", "--output", default="baseline.json",
                    help="Output JSON file (default: baseline.json)")
    ap.add_argument("--pattern", default="*.bin,*.BIN",
                    help="Comma-separated globs (default: '*.bin,*.BIN')")
    ap.add_argument("--max-logs", type=int, default=0,
                    help="Cap to N logs (0 = no cap, all)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="Logs analyzed in parallel (default: 1)")
    ap.add_argument("--disk-cache", action="store_true",
                    help="Reuse parsed logs across runs (see module docs)")
    args = ap.parse_args()

    root = Path(args.logs_root).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    patterns = [p.strip() for p in args.pattern.split(",") if p.strip()]
    logs: list[Path] = []
    for pat in patterns:
        logs.extend(root.rglob(pat))
    logs = sorted(set(logs))
    if args.max_logs > 0:
        logs = logs[: args.max_logs]

    registry = discover_analyzers()
    print(f"找到 {len(logs)} 份日志 × {len(registry)} 个分析器 = "
          f"{len(logs) * len(registry)} 条待生成")

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tool_version": _git_sha(),
        "logs_root": str(root),
        "n_logs": len(logs),
        "n_analyzers": len(registry),
        "analyzers": [k for k, _ in registry],
        "logs": [],
    }

    # Largest first: the long jobs start early, so the pool drains evenly.
    jobs = [(str(log), str(log.relative_to(root)), args.disk_cache)
            for log in sorted(logs, key=lambda p: -p.stat().st_size)]
    entries = []
    if args.jobs > 1:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(args.jobs, maxtasksperchild=1) as pool:
            for i, entry in enumerate(
                    pool.imap_unordered(_worker, jobs), 1):
                entries.append(entry)
                _report(i, len(jobs), entry)
    else:
        for i, job in enumerate(jobs, 1):
            entries.append(_worker(job))
            _report(i, len(jobs), entries[-1])
    result["logs"] = sorted(entries, key=lambda e: e["relpath"])

    out_path = Path(args.output).resolve()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 基线已写入 {out_path}")


if __name__ == "__main__":
    main()
