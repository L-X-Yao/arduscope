"""The arithmetic of tools/closed_loop.py. Flying it takes SITL."""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, str(TOOLS_DIR))

import closed_loop
from closed_loop import compare, parse_apply, rms, wrap_degrees, writable

pytestmark = pytest.mark.fast


class TestRms:
    def test_skips_what_is_not_finite(self):
        assert rms([3.0, np.nan, 3.0, np.inf]) == 3.0

    def test_nothing_to_measure(self):
        assert math.isnan(rms([np.nan]))


def test_angle_difference_takes_the_short_way_round():
    assert wrap_degrees(350.0) == -10.0
    assert wrap_degrees(-350.0) == 10.0


class TestWritable:
    def test_keeps_what_a_param_file_would_hold(self):
        rows = [{"param": "A", "suggested": 1.5}, {"param": "B", "suggested": "0.7"},
                {"param": "C", "suggested": "-"}, {"param": "D", "suggested": ""},
                {"param": "E", "suggested": None}]

        assert writable(rows) == {"A": 1.5, "B": 0.7}


class TestCompare:
    def test_mean_and_change(self):
        rows = compare([{"att_roll": 1.0}, {"att_roll": 3.0}], [{"att_roll": 3.0}])

        assert rows == [{"measure": "att_roll", "before": 2.0, "after": 3.0,
                         "change_pct": 50.0}]

    def test_a_flight_without_the_measure_is_left_out(self):
        # Rate rows can be NaN on a plane, and statistics chokes on those.
        rows = compare([{"rate_roll": 2.0}, {"rate_roll": float("nan")}],
                       [{"rate_roll": 1.0}])

        assert rows[0]["before"] == 2.0

    def test_nothing_that_is_not_a_measure(self):
        rows = compare([{"path": "a", "flagged": {}, "health": 49}],
                       [{"path": "b", "flagged": {}, "health": 100}])

        assert [r["measure"] for r in rows] == ["health"]

    def test_no_flights_after(self):
        assert compare([{"att_roll": 1.0}], []) == []

    def test_no_flights_before(self):
        assert compare([], [{"att_roll": 1.0}]) == []


class TestParseApply:
    def test_names_and_values(self):
        assert parse_apply(["A=1.5", "B=2"]) == {"A": 1.5, "B": 2.0}

    def test_rejects_a_name_alone(self):
        with pytest.raises(SystemExit):
            parse_apply(["A"])


def test_a_relative_sitl_dir_is_made_absolute(monkeypatch):
    """SITL starts in its work directory, where a relative path leads nowhere."""
    seen = []
    monkeypatch.setattr(closed_loop, "check_scenario",
                        lambda scenario, args: seen.append(args.sitl_dir))
    monkeypatch.setattr(sys, "argv", ["closed_loop.py", "--sitl-dir",
                                      "../ardupilot", "copter_normal"])
    closed_loop.main()
    assert seen == [os.path.abspath("../ardupilot")]


class TestMeasures:
    @staticmethod
    def _measures(monkeypatch, messages):
        class Reader:
            def __init__(self, path, preload_types):
                pass

            def has_message(self, name):
                return name in messages

            def get_messages_as_arrays(self, name, fields):
                data = {field: np.full(3, 5.0) for field in fields}
                data["_timestamp"] = np.arange(3.0)
                return data

        monkeypatch.setattr(closed_loop, "LogReader", Reader)
        monkeypatch.setattr(closed_loop, "motor_channels", lambda reader: [])
        monkeypatch.setattr(closed_loop, "stat_at",
                            lambda reader, ts, field: np.ones(len(ts)))
        return closed_loop.measures("flight.BIN")

    def test_a_copter_has_a_yaw_demand(self, monkeypatch):
        out = self._measures(monkeypatch, {"CTUN"})
        assert out["att_roll"] == 0.0
        assert out["att_yaw"] == 0.0

    def test_a_plane_has_no_yaw_demand(self, monkeypatch):
        out = self._measures(monkeypatch, {"TECS"})
        assert out["att_roll"] == 0.0
        assert "att_yaw" not in out


class TestNoise:
    RATE = 2048.0

    def _batch(self, hz, amplitude):
        t = np.arange(1024) / self.RATE
        wave = amplitude * np.sin(2 * np.pi * hz * t)
        return 0.0, self.RATE, wave, wave

    def test_a_tone_is_found_at_its_frequency_and_level(self):
        noise = closed_loop.band_noise(*closed_loop.spectrum(
            [self._batch(138.0, 8.0), self._batch(138.0, 8.0)]))
        assert noise["gyro_peak_hz"] == pytest.approx(138.0, abs=2.0)
        assert noise["gyro_peak"] == pytest.approx(8.0, rel=0.1)

    def test_a_tone_below_the_band_does_not_count(self):
        noise = closed_loop.band_noise(*closed_loop.spectrum(
            [self._batch(20.0, 8.0)]))
        assert noise["gyro_peak"] < 0.1

    def test_no_batches_no_spectrum(self):
        assert closed_loop.spectrum([]) is None


def test_set_changes_the_scenario_on_both_sides(monkeypatch):
    flown = []

    def fly(scenario, tag, repeats, sitl_dir):
        flown.append((tag, scenario["sitl_params"]))
        return [{"suggested": {"A": 2.0}, "flagged": {}}]

    monkeypatch.setattr(closed_loop, "fly", fly)
    args = closed_loop.argparse.Namespace(
        set=["B=1"], apply=[], repeats=1, sitl_dir="/ap")
    closed_loop.check_scenario({"name": "s", "sitl_params": {"C": 3}}, args)
    assert flown == [("base", {"C": 3, "B": 1.0}),
                     ("fix", {"C": 3, "B": 1.0, "A": 2.0})]


def test_the_fullest_set_of_suggestions_is_flown(monkeypatch):
    flown = []

    def fly(scenario, tag, repeats, sitl_dir):
        flown.append(scenario["sitl_params"])
        return [{"suggested": {}, "flagged": {}},
                {"suggested": {"A": 2.0}, "flagged": {}}]

    monkeypatch.setattr(closed_loop, "fly", fly)
    args = closed_loop.argparse.Namespace(
        set=[], apply=[], repeats=2, sitl_dir="/ap")
    closed_loop.check_scenario({"name": "s"}, args)
    assert flown[1] == {"A": 2.0}
