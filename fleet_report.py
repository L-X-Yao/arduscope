#!/usr/bin/env python3
"""
Fleet history CLI — inspect the per-airframe archive (fleet.db).

Usage:
    python3 fleet_report.py --db reports/fleet.db --list
    python3 fleet_report.py --db reports/fleet.db 001A002B-3030510C-31323334
    python3 fleet_report.py --db reports/fleet.db 3号机 -o trend.html
    python3 fleet_report.py --db reports/fleet.db --set-alias \\
            001A002B-3030510C-31323334 3号机
    python3 fleet_report.py --db reports/fleet.db --backfill D:\\logs

The archive is written automatically by flight_report / web_app /
batch_report (disable with --no-fleet-record). --backfill seeds it
from historical logs: analyze + archive only, no HTML; sha1 dedup
skips already-archived logs, so re-runs are cheap and safe. Design:
docs/fleet_history_design.md.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from html import escape

# Ensure the package is importable when running as a script.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from lib.i18n import set_locale, t
from lib import fleet_history


def _print_list(db_path: str) -> None:
    frames = fleet_history.list_airframes(db_path)
    if not frames:
        print(t("fleet.cli_empty", db=db_path))
        return
    print(f"{'serial':30} {'alias':12} {'board':20} "
          f"{'flights':>7} last")
    print("-" * 84)
    for f in frames:
        print(f"{f['serial']:30} {f['alias']:12} {f['board']:20} "
              f"{f['n_flights']:>7} {f['last_date']}")


def _render_html(rows, serial: str, out_path: str) -> None:
    result = fleet_history.build_history_result(rows, serial)
    if result is None:
        sys.exit(t("fleet.cli_too_few"))
    fig_html = result.figures[0].to_html(
        full_html=False, include_plotlyjs="inline")
    lines = "\n".join(
        f"<li><code>{escape(d)}</code></li>" for d in result.details)
    title = escape(result.title)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:PingFang SC,Microsoft YaHei,sans-serif;
max-width:1100px;margin:24px auto;padding:0 16px}}
li{{margin:4px 0;list-style:none}}</style></head>
<body><h2>{title}</h2><p>{escape(result.summary)}</p>
{fig_html}<ul>{lines}</ul></body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(t("fleet.cli_written", path=out_path))


def _run_backfill(db_path: str, sources: list[str]) -> None:
    missing = [s for s in sources if not os.path.exists(s)]
    if missing:
        sys.exit(t("fleet.cli_backfill_missing", path=missing[0]))
    n_found = len(fleet_history.iter_log_files(sources))
    if not n_found:
        sys.exit(t("fleet.cli_backfill_none"))
    print(t("fleet.cli_backfill_start", n=n_found, db=db_path), flush=True)

    def _progress(i, n, path, event, detail):
        name = os.path.basename(path)[:44]
        if event == "record":
            print(f"[{i}/{n}] {name} -> {detail}", flush=True)
        elif event == "skip":
            print(t("fleet.cli_backfill_skip", i=i, n=n, name=name),
                  flush=True)
        else:
            print(t("fleet.cli_backfill_fail", i=i, n=n, name=name,
                    err=detail[:100]), flush=True)

    t0 = time.time()
    stats = fleet_history.backfill(db_path, sources, progress=_progress)
    print(t("fleet.cli_backfill_done",
            recorded=stats["recorded"], skipped=stats["skipped"],
            failed=stats["failed"], total=stats["total"],
            minutes=f"{(time.time() - t0) / 60:.1f}"))
    if stats["failed"]:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ArduScope fleet history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("key", nargs="?",
                        help="airframe serial, serial prefix, or alias")
    parser.add_argument("--db", default="fleet.db",
                        help="fleet database path (default: ./fleet.db)")
    parser.add_argument("--list", action="store_true",
                        help="list all airframes")
    parser.add_argument("--set-alias", nargs=2,
                        metavar=("SERIAL", "ALIAS"),
                        help="assign a human-friendly name")
    parser.add_argument("--backfill", nargs="+", metavar="PATH",
                        help="analyze + archive every .BIN under the given"
                             " files/dirs (sha1-deduped, re-run safe)")
    parser.add_argument("-o", "--output",
                        help="write the trend page to this HTML file")
    parser.add_argument("-n", "--limit", type=int, default=50,
                        help="flights to include (default 50)")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh")
    args = parser.parse_args()

    set_locale(args.lang)

    if args.backfill:
        _run_backfill(args.db, args.backfill)
        return

    if not os.path.isfile(args.db) and not args.set_alias:
        sys.exit(t("fleet.cli_no_db", db=args.db))

    if args.set_alias:
        serial, alias = args.set_alias
        try:
            known = fleet_history.set_alias(args.db, serial, alias)
        except fleet_history.AliasTaken as e:
            sys.exit(str(e))
        if known:
            print(t("fleet.cli_alias_set", serial=serial, alias=alias))
        else:
            sys.exit(t("fleet.cli_alias_unknown", serial=serial))
        return

    if args.list or not args.key:
        _print_list(args.db)
        return

    serial = fleet_history.resolve_serial(args.db, args.key)
    if serial is None:
        sys.exit(t("fleet.cli_not_found", key=args.key))

    rows = fleet_history.query_history(args.db, serial, limit=args.limit)
    if args.output:
        _render_html(rows, serial, args.output)
        return

    print(t("fleet.cli_header", serial=serial, n=len(rows)))
    for r in rows:
        mets = r["metrics"]

        def _f(key):
            v = mets.get(key)
            return f"{v}" if v is not None else "-"

        print(f"  {r['flight_date'] or '-':16} "
              f"{r['log_name'][:36]:36} "
              f"score={r['health_score']:>3} "
              f"dur={(r['duration_s'] or 0) / 60:5.1f}min "
              f"vibeX={_f('vibe_max_x'):>6} "
              f"vmin={_f('batt_v_min'):>6} "
              f"sag={_f('vcc_sag')}")


if __name__ == "__main__":
    main()
