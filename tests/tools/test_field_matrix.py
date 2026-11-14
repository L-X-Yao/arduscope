"""
Field-matrix guardrail tests: the vendored message/field table, the
checker, and the aliases added for the dead reads it caught on first
run (PM.NLoop -> NL fleet-wide, ARSP.Primary -> Pri).
"""

from __future__ import annotations

from tests._repo import REPO_ROOT

import json
import os
import subprocess
import sys

import numpy as np
import pytest

from lib.log_reader import LogReader

pytestmark = pytest.mark.fast

_ROOT = str(REPO_ROOT)
_TABLE = os.path.join(_ROOT, "tools", "msg_field_table.json")


class TestFieldTable:

    def test_table_exists_and_covers_consumed_messages(self):
        with open(_TABLE, encoding="utf-8") as f:
            table = json.load(f)["messages"]
        assert len(table) > 200            # precondition: real harvest
        # Every message the analyzers rely on must be present.
        for msg in ("POWR", "MCU", "DSF", "PIQR", "PSCN", "QTUN",
                    "RATE", "XKF4", "BAT", "ARSP", "VIBE", "PM",
                    "MOTB", "STAT", "MODE", "IMU", "GPS"):
            assert msg in table, f"{msg} missing from field table"

    def test_messages_written_as_joined_literals_are_in_it(self):
        """BARO is: "Q" "B" "f" ..., "TimeUS," "I," "Alt," ..., across lines
        that end in a backslash. The harvest took one literal for each and
        left 14 messages out, so a check of BARO.Alt called it unknown."""
        with open(_TABLE, encoding="utf-8") as f:
            table = json.load(f)["messages"]

        assert "Alt" in table["BARO"]
        for msg in ("BARD", "DCM", "TEC3", "TEC4"):
            assert msg in table, f"{msg} missing from field table"

    def test_harvest_reads_literals_as_the_compiler_does(self):
        sys.path.insert(0, _ROOT)
        from tools.gen_msg_field_table import _as_the_compiler_reads_it

        source = ('{ LOG_X_MSG, sizeof(log_X), \\\n'
                  '  "XMSG", \\\n'
                  '  "Q"  "f", \\\n'
                  '  "TimeUS," "Alt" }')

        assert _as_the_compiler_reads_it(source) == (
            '{ LOG_X_MSG, sizeof(log_X), \n'
            '  "XMSG", \n'
            '  "Qf", \n'
            '  "TimeUS,Alt" }')

    def test_checker_passes_on_repo(self):
        r = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "tools",
                                          "check_msg_fields.py")],
            capture_output=True, text=True, cwd=_ROOT)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_checker_catches_bad_field(self, tmp_path):
        # End-to-end: a file referencing a nonexistent field must fail.
        bad = tmp_path / "bad.py"
        bad.write_text(
            "def f(reader):\n"
            "    return reader.get_messages_as_arrays("
            "'BAT', ['NoSuchField'])\n")
        sys.path.insert(0, _ROOT)
        from tools.check_msg_fields import scan_file
        table = {k: set(v) for k, v in
                 json.load(open(_TABLE))["messages"].items()}
        errors: list[str] = []
        scan_file(str(bad), table, set(), errors)
        assert errors and "NoSuchField" in errors[0]


class TestFieldMatrixAliases:
    """Aliases for the dead reads the matrix checker caught."""

    def test_pm_nloop_alias(self):
        # Fleet logs (April AND May 2026) log the loop count as "NL":
        # the "NLoop" read was all-NaN, silently disabling the
        # long-loop CPU check since it shipped.
        reader = LogReader.__new__(LogReader)
        result = {"NLoop": np.full(4, np.nan),
                  "NL": np.array([4000.0, 4000.0, 4000.0, 4000.0])}
        assert np.all(np.isnan(result["NLoop"]))   # precondition
        out = reader._apply_aliases("PM", ["NLoop"], result)
        np.testing.assert_array_equal(out["NLoop"], result["NL"])

    def test_arsp_primary_alias(self):
        reader = LogReader.__new__(LogReader)
        result = {"Primary": np.full(3, np.nan),
                  "Pri": np.array([0.0, 0.0, 1.0])}
        assert np.all(np.isnan(result["Primary"]))  # precondition
        out = reader._apply_aliases("ARSP", ["Primary"], result)
        np.testing.assert_array_equal(out["Primary"], result["Pri"])
