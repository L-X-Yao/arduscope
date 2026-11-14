"""batch_report's files: one report per log, and failures that read as such."""

from __future__ import annotations

from tests._repo import REPO_ROOT

import csv
import os
import re
import subprocess
import sys

import pytest

import batch_report
import binlog
from lib.i18n import t

_ROOT = str(REPO_ROOT)


def _result(**changes) -> dict:
    return {
        "filename": "a.BIN", "filepath": "a.BIN", "report_path": None,
        "vehicle_type": "ArduCopter", "firmware": "V4.6.3", "board": "x",
        "duration": 120.0, "health_score": 87, "metrics": {}, "params": {},
        "n_warnings": 1, "n_criticals": 0, "error": None, "elapsed": 1.5,
        **changes,
    }


@pytest.mark.fast
class TestLogLabels:
    def test_unique_filename_stays_as_it_is(self):
        paths = [os.path.join("root", "x", "a.BIN"),
                 os.path.join("root", "y", "b.BIN")]
        labels = batch_report.log_labels(paths, "root")
        assert labels == {paths[0]: "a.BIN", paths[1]: "b.BIN"}

    def test_shared_filename_gets_its_folder(self):
        paths = [os.path.join("root", "x", "00000012.BIN"),
                 os.path.join("root", "y", "00000012.BIN")]
        labels = batch_report.log_labels(paths, "root")
        assert labels == {paths[0]: os.path.join("x", "00000012.BIN"),
                          paths[1]: os.path.join("y", "00000012.BIN")}

    def test_case_only_difference_counts_as_shared(self):
        # One file on Windows, whose output directory could not hold both.
        paths = [os.path.join("root", "x", "LOG.BIN"),
                 os.path.join("root", "y", "log.bin")]
        labels = batch_report.log_labels(paths, "root")
        assert len({label.casefold() for label in labels.values()}) == 2


class TestRecursiveCollision:
    @pytest.fixture(scope="class")
    @classmethod
    def run(cls, tmp_path_factory):
        root = tmp_path_factory.mktemp("batch")
        for folder, seconds in (("flightA", 30.0), ("flightB", 50.0)):
            (root / "logs" / folder).mkdir(parents=True)
            binlog.flight_log("copter", duration_s=seconds).save(
                root / "logs" / folder / "00000012.BIN")
        out = root / "out"
        result = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "batch_report.py"),
             str(root / "logs"), "-r", "-o", str(out), "--compare",
             "--no-open", "--no-map", "--no-ai", "--no-fleet-record",
             "--lang", "en"],
            capture_output=True, text=True, timeout=240, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        return out

    def test_each_log_keeps_its_own_report(self, run):
        for folder in ("flightA", "flightB"):
            report = run / folder / "00000012_report.html"
            assert report.is_file()
            assert folder in report.read_text(encoding="utf-8")

    def test_csv_points_each_row_at_its_own_report(self, run):
        with open(run / "batch_summary.csv", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        linked = {row[0]: row[9] for row in rows[1:]}
        assert linked == {
            os.path.join("flightA", "00000012.BIN"):
                os.path.join("flightA", "00000012_report.html"),
            os.path.join("flightB", "00000012.BIN"):
                os.path.join("flightB", "00000012_report.html"),
        }

    def test_overview_links_resolve(self, run):
        page = (run / "batch_overview.html").read_text(encoding="utf-8")
        links = re.findall(r'href="([^"]+_report\.html)"', page)
        assert sorted(links) == ["flightA/00000012_report.html",
                                 "flightB/00000012_report.html"]

    def test_comparison_columns_can_be_told_apart(self, run):
        page = (run / "batch_comparison.html").read_text(encoding="utf-8")
        table = page[page.index('<table class="comparison-table">'):]
        heads = re.findall(r"<th>([^<]+00000012\.BIN)</th>", table)
        assert len(set(heads)) == 2


class TestSummaryCsv:
    def _rows(self, tmp_path, results):
        path = tmp_path / "summary.csv"
        batch_report.write_summary_csv(results, str(path))
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        return rows[0], rows[1:]

    def test_failed_log_says_why_and_has_no_score(self, tmp_path):
        failed = _result(filename="bad.BIN", vehicle_type="", firmware="",
                         board="", duration=0.0, health_score=0, n_warnings=0,
                         error="Log file is empty: bad.BIN")
        headers, rows = self._rows(tmp_path, [failed])
        row = dict(zip(headers, rows[0]))
        assert row[t("batch.csv_error")] == "Log file is empty: bad.BIN"
        assert row[t("batch.csv_score")] == ""
        assert row[t("batch.csv_warnings")] == ""
        assert row[t("batch.csv_duration")] == ""

    def test_zero_score_of_a_real_flight_stays_zero(self, tmp_path):
        headers, rows = self._rows(
            tmp_path, [_result(health_score=0, n_criticals=5)])
        row = dict(zip(headers, rows[0]))
        assert row[t("batch.csv_score")] == "0"
        assert row[t("batch.csv_error")] == ""
