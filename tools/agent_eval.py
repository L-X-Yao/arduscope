#!/usr/bin/env python3
"""Run the online agent against the eight SITL scenarios.

This is the acceptance check for the agent: a fault scenario has to be named
in every run, a normal scenario has to stay free of invented faults, and an
observation whose numbers cannot be traced to a tool call fails the run.

Usage:
    OPENAI_API_KEY=... python3 tools/agent_eval.py \
        --provider openai --model deepseek-flash \
        --base-url https://api.deepseek.com --repeats 3

The key is read from ANTHROPIC_API_KEY or OPENAI_API_KEY, never from argv.
Results go to reports/agent-eval/<timestamp>.json unless --out names a file.
The log files are the ones tests/sitl/generate_logs.py writes.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.agent.config import AgentConfig  # noqa: E402
from lib.agent.loop import run_agent  # noqa: E402
from lib.i18n import set_locale  # noqa: E402
from lib.pipeline import run_analysis  # noqa: E402

DEFAULT_LOG_DIR = os.path.join(_ROOT, "tests", "sitl", "logs")
DEFAULT_OUT_DIR = os.path.join(_ROOT, "reports", "agent-eval")

# Terms that count as naming the injected fault. A normal scenario has none.
FAULT_TERMS = {
    "copter_normal": [],
    "copter_high_vibration": ["vibrat", "motor", "noise", "振动", "电机"],
    "copter_gps_glitch": ["gps", "glitch", "satellite", "jump", "卫星", "跳变"],
    "copter_compass_interference": ["compass", "magnet", "interference",
                                    "罗盘", "磁"],
    "plane_tecs_tuning": ["tecs", "tuning", "airspeed", "调参", "空速"],
    "copter_battery_aging": ["battery", "voltage", "sag", "cell",
                             "电池", "电压"],
    "copter_motor_noise": ["vibrat", "noise", "notch", "振动", "噪声"],
    "copter_motor_noise_climbs": ["vibrat", "noise", "notch",
                                  "振动", "噪声"],
}

# Terms that must not appear in a normal scenario's observations.
FALSE_POSITIVE_TERMS = ["failure", "crash", "critical fault", "injected"]


def _texts(observations) -> list[str]:
    return [
        (observation.text if hasattr(observation, "text")
         else observation.get("text", "")).lower()
        for observation in observations
    ]


def _detected(observations, terms) -> bool | None:
    if not terms:
        return None
    texts = _texts(observations)
    return any(any(term in text for term in terms) for text in texts)


def _false_positive(observations) -> bool:
    texts = _texts(observations)
    return any(any(term in text for term in FALSE_POSITIVE_TERMS)
               for text in texts)


def run_one(pipeline, config: AgentConfig) -> dict:
    started = time.time()
    try:
        run = run_agent(pipeline, config)
    except Exception as e:  # noqa: BLE001 - the report keeps the failure
        return {"ok": False, "elapsed": round(time.time() - started, 1),
                "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "elapsed": round(time.time() - started, 1),
        "turns": run.turns,
        "calls": len(run.records),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "summary": run.submission.summary,
        "summary_unverified": run.submission.summary_unverified,
        "unverified": run.submission.unverified_count,
        "observations": [
            {"text": o.text, "cites": o.cites, "kind": o.kind,
             "unverified": o.unverified, "reason": o.reason}
            for o in run.submission.observations
        ],
        "tools": [{"seq": r.seq, "name": r.name, "error": r.is_error}
                  for r in run.records],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", choices=["openai", "anthropic"],
                        default="openai")
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--only", default="",
                        help="comma-separated scenario names; default all")
    parser.add_argument("--out", default="")
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    set_locale("en")
    config = AgentConfig(provider=args.provider, model=args.model,
                         base_url=args.base_url,
                         max_tool_calls=args.max_calls,
                         max_seconds=args.max_seconds)
    if not config.is_configured():
        print("No API key in the environment", file=sys.stderr)
        return 1

    out = args.out or os.path.join(
        DEFAULT_OUT_DIR,
        datetime.datetime.now().strftime("agent-eval-%Y%m%d-%H%M%S.json"))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)

    names = ([name.strip() for name in args.only.split(",") if name.strip()]
             or list(FAULT_TERMS))
    unknown = [name for name in names if name not in FAULT_TERMS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")

    records = []
    for name in names:
        terms = FAULT_TERMS[name]
        path = os.path.join(args.log_dir, name + ".BIN")
        if not os.path.isfile(path):
            print(f"missing log: {path}", file=sys.stderr)
            return 1
        pipeline = run_analysis(path)
        offline = {key: result.status
                   for key, result in pipeline.results_by_key.items()
                   if result.status in ("warning", "critical")}
        for attempt in range(args.repeats):
            record = {"scenario": name, "attempt": attempt + 1,
                      "offline": offline}
            record.update(run_one(pipeline, config))
            record["detected"] = _detected(
                record.get("observations", []), terms)
            record["false_positive"] = (
                _false_positive(record.get("observations", []))
                if not terms else None)
            records.append(record)
            # Write after every run, so an interrupted evaluation still
            # leaves the runs it finished.
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"provider": args.provider, "model": args.model,
                           "repeats": args.repeats, "records": records},
                          f, ensure_ascii=False, indent=1)
            print(f"{name} #{attempt + 1}: "
                  f"{'ok' if record['ok'] else record.get('error')} "
                  f"detected={record['detected']} "
                  f"unverified={record.get('unverified')} "
                  f"calls={record.get('calls')}", flush=True)

    with open(out, "w", encoding="utf-8") as f:
        json.dump({"provider": args.provider, "model": args.model,
                   "repeats": args.repeats, "records": records},
                  f, ensure_ascii=False, indent=1)

    failures = []
    failed_runs = 0
    for record in records:
        problems = []
        if not record["ok"]:
            problems.append(record["error"])
        else:
            if record["unverified"]:
                problems.append(f"{record['unverified']} unverified")
            if record.get("summary_unverified"):
                problems.append("the summary has an untraced number")
            if record["detected"] is False:
                problems.append("the injected fault was not named")
        if problems:
            failed_runs += 1
            failures.append(f"{record['scenario']} #{record['attempt']}: "
                            + "; ".join(problems))

    print(f"\n{len(records) - failed_runs} of {len(records)} runs passed")
    for failure in failures:
        print(f"  FAIL {failure}")
    print(f"report: {out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
