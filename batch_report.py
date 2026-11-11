#!/usr/bin/env python3
"""
ArduScope Batch Flight Log Analysis.

Scans a directory for .BIN log files, analyzes each one, and generates:
  - Individual HTML report per log
  - Summary CSV with health scores and key metrics
  - HTML overview table with color-coded scores and report links
  - Optional multi-log comparison report (--compare)

Usage:
    python3 batch_report.py /path/to/logs/
    python3 batch_report.py /path/to/logs/ --template copter-factory
    python3 batch_report.py /path/to/logs/ --compare --recursive
    python3 batch_report.py /path/to/logs/ -o /path/to/output/
"""

from __future__ import annotations

import argparse
import html as _html
import os
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import quote

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from lib.csv_safe import SafeWriter
from lib.i18n import t
from lib.html_report import HtmlReportGenerator
from lib.ai_diagnosis import generate_diagnosis
from lib.pipeline import run_analysis
from lib.templates import get_template
from multi_report import (
    extract_metrics, METRIC_CONFIG,
    generate_html as generate_multi_html,
)


# =====================================================================
# Directory scanning
# =====================================================================

def scan_for_bins(directory: str, recursive: bool = False) -> list[str]:
    """Find all .BIN files in directory, sorted by name."""
    bins = []
    if recursive:
        for root, _, files in os.walk(directory):
            for f in files:
                if f.upper().endswith('.BIN'):
                    bins.append(os.path.join(root, f))
    else:
        for f in os.listdir(directory):
            if f.upper().endswith('.BIN'):
                bins.append(os.path.join(directory, f))
    bins.sort()
    return bins


def log_labels(bins: list[str], root: str) -> dict[str, str]:
    """The name each log is shown and filed under.

    That is the filename where it is unique. ArduPilot numbers logs per
    SD card, so --recursive can find 00000012.BIN in several folders, and
    one output directory then meant each report overwrote the last. Those
    keep their path under root, and their report mirrors that folder.
    Compared without case, as the output may be on Windows.
    """
    names = [os.path.basename(path) for path in bins]
    seen = Counter(name.casefold() for name in names)
    return {path: name if seen[name.casefold()] == 1
            else os.path.relpath(path, root)
            for path, name in zip(bins, names)}


# =====================================================================
# Single-file analysis (reuses existing library components)
# =====================================================================

def analyze_one(
    filepath: str,
    output_dir: str,
    template=None,
    no_ai: bool = False,
    no_map: bool = False,
    no_fleet_record: bool = False,
    label: str | None = None,
) -> dict:
    """
    Analyze a single .BIN file and generate an HTML report.

    label is the name from log_labels(), by default the filename.

    Returns a result dict with keys: filename, filepath, report_path,
    vehicle_type, firmware, board, duration, health_score, metrics, params,
    error (None if success), archive_warning, elapsed.
    """
    filename = label or os.path.basename(filepath)
    result = {
        "filename": filename,
        "filepath": filepath,
        "report_path": None,
        "vehicle_type": "",
        "firmware": "",
        "board": "",
        "duration": 0.0,
        "health_score": 0,
        "metrics": {},
        "params": {},
        "n_warnings": 0,
        "n_criticals": 0,
        "error": None,
        "archive_warning": None,
        "elapsed": 0.0,
    }

    t0 = time.time()
    try:
        pipeline = run_analysis(
            filepath,
            template=template,
            include_gps=not no_map,
        )

        reader = pipeline.reader
        vehicle_info = pipeline.vehicle_info
        analysis_results = pipeline.results
        results_by_key = pipeline.results_by_key
        gps_track = pipeline.gps_track

        result["vehicle_type"] = vehicle_info.vehicle_type
        result["firmware"] = vehicle_info.firmware_version
        result["board"] = vehicle_info.board_name
        result["duration"] = reader.get_flight_duration()
        result["params"] = dict(reader.params)
        result["health_score"] = pipeline.health_score
        result["n_warnings"] = sum(
            1 for r in analysis_results if r.status == "warning"
        )
        result["n_criticals"] = sum(
            1 for r in analysis_results if r.status == "critical"
        )

        # AI diagnosis
        if not no_ai and results_by_key:
            try:
                diagnosis = generate_diagnosis(
                    results_by_key, vehicle_info, pipeline.health_score,
                    reader.get_flight_duration(),
                )
                analysis_results.insert(0, diagnosis)
            except Exception:
                pass

        # Extract metrics
        try:
            result["metrics"] = extract_metrics(reader, pipeline.thresholds)
        except Exception:
            pass

        # Fleet history (archive + trend card); never blocks the batch
        if not no_fleet_record:
            try:
                from lib import fleet_history
                fleet_db = os.path.join(output_dir, "fleet.db")
                serial, _n = fleet_history.record(
                    fleet_db, pipeline, filepath)
                hist = fleet_history.build_history_result(
                    fleet_history.query_history(fleet_db, serial), serial)
                if hist is not None:
                    analysis_results.append(hist)
            except Exception as e:
                result["archive_warning"] = t("fleet.record_failed", error=e)

        # Generate HTML report
        base = os.path.splitext(filename)[0]
        report_name = f"{base}_report.html"
        report_path = os.path.join(output_dir, report_name)

        generator = HtmlReportGenerator(
            vehicle_info=vehicle_info,
            results=analysis_results,
            log_filename=filename,
            flight_duration=reader.get_flight_duration(),
            health_score=pipeline.health_score,
            gps_track=gps_track,
        )
        generator.write(report_path)
        result["report_path"] = report_path

    except Exception as e:
        result["error"] = str(e)

    result["elapsed"] = round(time.time() - t0, 1)
    return result


# =====================================================================
# Summary outputs
# =====================================================================

def _report_link(result: dict, summary_path: str) -> str:
    """Path of a log's report as seen from a summary file."""
    if not result["report_path"]:
        return ""
    return os.path.relpath(result["report_path"],
                           os.path.dirname(os.path.abspath(summary_path)))


def write_summary_csv(results: list[dict], output_path: str):
    """Write a summary CSV with one row per log."""
    metric_keys = list(METRIC_CONFIG.keys())
    headers = [
        t("batch.csv_filename"), t("batch.csv_vehicle"), t("batch.csv_firmware"),
        t("batch.csv_board"), t("batch.csv_duration"), t("batch.csv_score"),
        t("batch.csv_warnings"), t("batch.csv_criticals"),
        t("batch.csv_elapsed"), t("batch.csv_report_file"),
    ] + [t(METRIC_CONFIG[k]["label_key"]) for k in metric_keys] + [
        t("batch.csv_error"),
    ]

    with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = SafeWriter(f)
        writer.writerow(headers)
        for r in results:
            # A failed log has no score. 0 would read as the worst flight
            # of the batch, and nothing else in the row would say otherwise.
            failed = bool(r["error"])
            row = [
                r["filename"],
                r["vehicle_type"],
                r["firmware"],
                r["board"],
                "" if failed else f"{r['duration']:.0f}",
                "" if failed else r["health_score"],
                "" if failed else r["n_warnings"],
                "" if failed else r["n_criticals"],
                r["elapsed"],
                _report_link(r, output_path),
            ]
            for k in metric_keys:
                val = r["metrics"].get(k)
                if val is not None:
                    fmt = METRIC_CONFIG[k]["format"]
                    row.append(f"{val:{fmt}}")
                else:
                    row.append("")
            row.append(r["error"] or "")
            writer.writerow(row)


def write_summary_html(results: list[dict], output_path: str,
                       compare_path: str | None = None):
    """Write a standalone HTML overview table."""
    n_total = len(results)
    n_ok = sum(1 for r in results if r["health_score"] >= 80 and not r["error"])
    n_warn = sum(1 for r in results
                 if 50 <= r["health_score"] < 80 and not r["error"])
    n_bad = sum(1 for r in results
                if r["health_score"] < 50 and not r["error"])
    n_err = sum(1 for r in results if r["error"])
    avg_score = (
        round(sum(r["health_score"] for r in results if not r["error"])
              / max(n_total - n_err, 1))
    )

    def _score_color(s):
        if s >= 80:
            return "#67c23a"
        if s >= 50:
            return "#e6a23c"
        return "#f56c6c"

    def _fmt_duration(s):
        m, sec = divmod(int(s), 60)
        return f"{m}m{sec}s" if m else f"{sec}s"

    # Table rows
    rows = []
    for i, r in enumerate(results, 1):
        if r["error"]:
            rows.append(
                f'<tr class="row-err"><td>{i}</td>'
                f'<td>{_html.escape(r["filename"])}</td>'
                f'<td colspan="7" style="color:#f56c6c">'
                f'{t("batch.error_prefix")}: {_html.escape(str(r["error"]))}</td></tr>'
            )
            continue
        sc = r["health_score"]
        sc_color = _score_color(sc)
        vibe = r["metrics"].get("max_vibe")
        ekf = r["metrics"].get("max_ekf_var")
        volt = r["metrics"].get("min_batt_volt")
        att = r["metrics"].get("att_rms_error")
        link = ""
        if r["report_path"]:
            rname = quote(_report_link(r, output_path).replace(os.sep, "/"))
            link = f'<a href="{rname}" target="_blank">{t("batch.view_link")}</a>'
        _v = lambda v, fmt: f'<td>{v:{fmt}}</td>' if v is not None else '<td>—</td>'
        rows.append(
            f'<tr>'
            f'<td>{i}</td>'
            f'<td class="fname">{_html.escape(r["filename"])}</td>'
            f'<td>{_html.escape(r["vehicle_type"])}</td>'
            f'<td>{_fmt_duration(r["duration"])}</td>'
            f'<td style="color:{sc_color};font-weight:700">{sc}</td>'
            + _v(vibe, '.1f')
            + _v(ekf, '.3f')
            + _v(volt, '.1f')
            + _v(att, '.2f')
            + f'<td>{link}</td>'
            f'</tr>'
        )

    compare_link = ""
    if compare_path:
        cname = quote(os.path.basename(compare_path))
        compare_link = (
            f'<p style="margin-top:12px"><a href="{cname}" '
            f'target="_blank" class="btn">{t("batch.view_comparison")}</a></p>'
        )

    html = f"""<!DOCTYPE html>
<html lang="{t('_lang_code')}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{t("batch.page_title")}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
      background:#f5f6fa;color:#333;line-height:1.6}}
.header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;
         padding:24px;text-align:center}}
.header h1{{font-size:22px;margin-bottom:8px}}
.header p{{font-size:14px;color:rgba(255,255,255,.7)}}
.stats{{display:flex;gap:16px;justify-content:center;margin-top:12px;flex-wrap:wrap}}
.stat{{background:rgba(255,255,255,.15);padding:6px 16px;border-radius:6px;font-size:13px}}
.container{{max-width:1200px;margin:24px auto;padding:0 16px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
       overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);font-size:13px}}
th{{background:#f7f8fa;padding:10px 8px;text-align:center;font-weight:600;
    border-bottom:2px solid #eee;position:sticky;top:0}}
td{{padding:8px;text-align:center;border-bottom:1px solid #f0f0f0}}
.fname{{text-align:left;font-family:monospace;font-size:12px;max-width:300px;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.row-err td{{background:#fff5f5}}
a{{color:#409eff;text-decoration:none}}
a:hover{{text-decoration:underline}}
.btn{{display:inline-block;background:#409eff;color:#fff;padding:8px 20px;
      border-radius:6px;font-size:14px}}
.btn:hover{{background:#337ecc;color:#fff;text-decoration:none}}
.footer{{text-align:center;padding:20px;color:#999;font-size:12px}}
</style>
</head>
<body>
<div class="header">
    <h1>{t("batch.page_title")}</h1>
    <p>{t("batch.header_subtitle", count=n_total, timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
    <div class="stats">
        <span class="stat">{t("batch.stat_avg_score")}: {avg_score}</span>
        <span class="stat" style="color:#67c23a">{t("batch.stat_ok")}: {n_ok}</span>
        <span class="stat" style="color:#e6a23c">{t("batch.stat_warn")}: {n_warn}</span>
        <span class="stat" style="color:#f56c6c">{t("batch.stat_bad")}: {n_bad}</span>
        {'<span class="stat" style="color:#f56c6c">' + t("batch.stat_failed") + ': ' + str(n_err) + '</span>' if n_err else ''}
    </div>
</div>
<div class="container">
    <table>
        <thead><tr>
            <th>#</th><th>{t("batch.col_filename")}</th><th>{t("batch.col_vehicle")}</th><th>{t("batch.col_duration")}</th><th>{t("batch.col_score")}</th>
            <th>{t("batch.col_vibe")}</th><th>{t("batch.col_ekf")}</th><th>{t("batch.col_voltage")}</th><th>{t("batch.col_att_err")}</th><th>{t("batch.col_report")}</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
    </table>
    {compare_link}
</div>
<footer class="footer">{t("batch.footer")}</footer>
</body>
</html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)


# =====================================================================
# Main
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ArduScope batch flight log analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "directory",
        help="Directory containing .BIN log files",
    )
    parser.add_argument(
        "-o", "--output-dir",
        help="Output directory for reports (default: same as input)",
    )
    parser.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="Scan subdirectories recursively",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Also generate a multi-log comparison report",
    )
    parser.add_argument(
        "--template",
        help="Scenario template to apply to all logs",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Disable AI diagnosis",
    )
    parser.add_argument(
        "--no-map",
        action="store_true",
        help="Disable GPS map in reports",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open summary in browser",
    )
    parser.add_argument(
        "--lang",
        choices=["zh", "en"],
        default="zh",
        help="Report language (default: zh)",
    )
    parser.add_argument(
        "--no-fleet-record",
        action="store_true",
        help="Do not archive flights into the fleet history db",
    )
    args = parser.parse_args()

    from lib.i18n import set_locale
    set_locale(args.lang)

    if not os.path.isdir(args.directory):
        print(t("cli.err_not_dir", path=args.directory), file=sys.stderr)
        sys.exit(1)

    # Scan
    bins = scan_for_bins(args.directory, args.recursive)
    if not bins:
        print(t("cli.no_bin_files", path=args.directory), file=sys.stderr)
        sys.exit(1)

    print(t("cli.found_bins", n=len(bins)))

    # Output directory
    output_dir = args.output_dir or args.directory
    os.makedirs(output_dir, exist_ok=True)

    # Template
    template = None
    if args.template:
        template = get_template(args.template)
        if template is None:
            print(t("cli.err_unknown_template", name=args.template), file=sys.stderr)
            sys.exit(1)
        print(t("cli.template_using", name=template.display_name))

    # Analyze each file
    results = []
    labels = log_labels(bins, args.directory)
    total_t0 = time.time()
    for i, filepath in enumerate(bins, 1):
        print(t("batch.progress", i=i, n=len(bins), name=labels[filepath]),
              end=" ", flush=True)

        r = analyze_one(
            filepath, output_dir,
            template=template, no_ai=args.no_ai, no_map=args.no_map,
            no_fleet_record=args.no_fleet_record, label=labels[filepath],
        )
        results.append(r)

        if r["error"]:
            print(t("batch.progress_err", error=r["error"]))
        else:
            sc = r["health_score"]
            icon = "OK" if sc >= 80 else "!!" if sc >= 50 else "XX"
            print(t("batch.progress_ok", icon=icon, score=sc,
                     elapsed=r["elapsed"]))
        if r["archive_warning"]:
            print(f"  [warn] {r['archive_warning']}", file=sys.stderr)

    total_elapsed = round(time.time() - total_t0, 1)
    print(t("cli.analysis_complete", n=len(results), elapsed=total_elapsed))

    # Summary CSV
    csv_path = os.path.join(output_dir, "batch_summary.csv")
    write_summary_csv(results, csv_path)
    print(t("cli.summary_csv", path=csv_path))

    # Multi-log comparison (optional)
    compare_path = None
    if args.compare and len(results) >= 2:
        good = [r for r in results if not r["error"]]
        if len(good) >= 2:
            print(t("cli.generating_comparison"))
            # Failed logs stay in: dropping one would let the verdict
            # stand for a flight nobody could read.
            log_names = [r["filename"] for r in results]
            all_metrics = [r["metrics"] for r in results]
            all_params = [r["params"] for r in results]
            compare_path = os.path.join(output_dir, "batch_comparison.html")
            html = generate_multi_html(log_names, all_metrics, all_params)
            with open(compare_path, 'w', encoding='utf-8') as f:
                f.write(html)
            print(t("cli.comparison_report", path=compare_path))

    # Summary HTML
    summary_path = os.path.join(output_dir, "batch_overview.html")
    write_summary_html(results, summary_path, compare_path)
    print(t("cli.overview_page", path=summary_path))

    # Open
    if not args.no_open:
        import webbrowser
        from pathlib import Path
        url = Path(os.path.abspath(summary_path)).as_uri()
        webbrowser.open(url)


if __name__ == "__main__":
    main()
