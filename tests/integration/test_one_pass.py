"""An analysis reads the log once.

A message type no analyzer declares is not loaded with the rest, and asking
for it reads the whole file again. Ten types were read that way: a Copter
log was opened 7 times in one analysis and a QuadPlane log 11 times.
"""

from __future__ import annotations

import pytest

import binlog
from analyzers.flight_profile import FlightProfileAnalyzer
from analyzers.log_health import LogHealthAnalyzer
from analyzers.pid_tuning import PIDTuningAnalyzer
from lib import log_reader as log_reader_module
from lib import pipeline
from lib.pipeline import run_analysis


@pytest.fixture
def opens(monkeypatch) -> list[str]:
    """Paths pymavlink was asked to open, in order."""
    seen: list[str] = []
    real = log_reader_module.mavutil.mavlink_connection

    def counting(path, *args, **kwargs):
        seen.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(log_reader_module.mavutil, "mavlink_connection",
                        counting)
    return seen


class TestPasses:
    def test_copter_log_is_opened_once(self, tmp_path, opens):
        path = binlog.flight_log("copter").save(tmp_path / "copter.bin")

        result = run_analysis(path)

        assert "flight_profile" in result.results_by_key  # precondition
        assert len(opens) == 1

    def test_quadplane_log_is_opened_twice(self, tmp_path, opens):
        """The second is for MODE as an array, which the first pass keeps
        as a list of mode changes."""
        path = binlog.flight_log("quadplane").save(tmp_path / "plane.bin")

        run_analysis(path)

        assert len(opens) == 2


@pytest.mark.fast
class TestDeclared:
    """The types below are read through a variable, which the field checker
    cannot follow."""

    def test_pid_tuning_declares_its_tables(self):
        tables = (PIDTuningAnalyzer._PID_MSGS + PIDTuningAnalyzer._PIQ_MSGS
                  + PIDTuningAnalyzer._PSC_AXES)
        assert len(tables) == 9  # precondition

        for message, _axis in tables:
            assert message in PIDTuningAnalyzer.optional_messages

    def test_flight_profile_declares_the_ekf_velocity(self):
        assert {"XKF1", "NKF1"} <= set(FlightProfileAnalyzer.optional_messages)

    def test_log_health_declares_the_logging_statistics(self):
        assert "DSF" in LogHealthAnalyzer.optional_messages

    def test_replay_frames_are_not_loaded(self):
        """log_health only asks whether RFRH is there, and it is the bulk
        of a replay log."""
        assert "RFRH" not in pipeline._collect_preload_types()
