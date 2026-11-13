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
import os
import sys

# Ensure package importable when running as script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_SCRIPT_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from lib.comparison_report import (  # noqa: F401 - re-exported for tests
    METRIC_CONFIG,
    _render_param_diff_html,
    compute_param_diff,
    generate_html,
)
from lib.i18n import t
from lib.multi_metrics import extract_metrics  # noqa: F401 - re-exported for tests
from lib.pipeline import read_flight_log
from lib.thresholds import Thresholds


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
