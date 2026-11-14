"""Power adds up over the packs that measure current, and nothing else.

BAT instances are not always packs. In the two log corpora 25 logs have
more than one instance and every extra one is a voltage-only rail. No log
there has a second current reading, so these cases are synthetic.
"""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.cruise_efficiency import CruiseEfficiencyAnalyzer
from analyzers.power import PowerAnalyzer
from lib.battery_packs import read_packs, sum_over_packs
from lib.i18n import t
from tests.test_synthetic_coverage import (
    SyntheticReader, _thresholds, _vehicle)

pytestmark = pytest.mark.fast

N = 200
DURATION_S = 100.0
TS = np.linspace(0.0, DURATION_S, N)


def _reader(instances: list[dict], params: dict | None = None,
            vehicle_type: str = "ArduCopter") -> SyntheticReader:
    """Interleaved BAT rows, one dict of constants per instance.

    A missing "curr" logs NaN, as AP does for a voltage-only monitor.
    """
    n_inst = len(instances)
    bat = {
        "_timestamp": np.repeat(TS, n_inst),
        "Inst": np.tile([float(i["inst"]) for i in instances], N),
        "Volt": np.tile([float(i["volt"]) for i in instances], N),
        "Curr": np.tile([float(i.get("curr", np.nan)) for i in instances], N),
    }
    if any("mah" in i for i in instances):
        ramp = np.linspace(0.0, 1.0, N)
        bat["CurrTot"] = np.column_stack(
            [ramp * i.get("mah", np.nan) for i in instances]).ravel()
    return SyntheticReader(messages={"BAT": bat}, params=params or {},
                           armed_intervals=[(0.0, DURATION_S)],
                           vehicle_type=vehicle_type)


def _power(reader):
    return PowerAnalyzer(reader, _thresholds(reader), _vehicle()).analyze()


def _cruise(reader):
    return CruiseEfficiencyAnalyzer(
        reader, _thresholds(reader), _vehicle(vt="ArduPlane")).analyze()


def _note_shown(result) -> bool:
    return any("(instances" in d or "实例" in d for d in result.details)


class TestReadPacks:
    def test_voltage_only_instance_is_not_a_pack(self):
        packs = read_packs(_reader([{"inst": 0, "volt": 50, "curr": 20},
                                    {"inst": 1, "volt": 24}]))
        assert [p.instance for p in packs] == [0]

    def test_pack_rows_stay_with_their_instance(self):
        packs = read_packs(_reader([{"inst": 0, "volt": 50, "curr": 20},
                                    {"inst": 1, "volt": 24, "curr": 5}]))
        assert [p.instance for p in packs] == [0, 1]
        assert len(packs[1].ts) == N
        assert set(packs[1].volt) == {24.0}
        assert set(packs[1].curr) == {5.0}

    def test_log_without_inst_is_one_pack(self):
        reader = SyntheticReader(messages={"BAT": {
            "_timestamp": TS, "Volt": np.full(N, 24.0),
            "Curr": np.full(N, 10.0)}})
        packs = read_packs(reader)
        assert len(packs) == 1
        assert len(packs[0].ts) == N

    def test_nothing_measures_current(self):
        assert read_packs(_reader([{"inst": 0, "volt": 50},
                                   {"inst": 1, "volt": 24}])) == []

    def test_empty_bat(self):
        reader = SyntheticReader(messages={"BAT": {
            "_timestamp": np.array([]), "Inst": np.array([]),
            "Volt": np.array([]), "Curr": np.array([])}})
        assert read_packs(reader) == []

    def test_sum_monitor_is_left_out(self):
        packs = read_packs(_reader(
            [{"inst": 0, "volt": 37, "curr": 9},
             {"inst": 1, "volt": 37, "curr": 3},
             {"inst": 2, "volt": 37, "curr": 12}],
            params={"BATT3_MONITOR": 10}))
        assert [p.instance for p in packs] == [0, 1]

    def test_sum_monitor_stays_when_it_holds_the_only_reading(self):
        packs = read_packs(_reader(
            [{"inst": 0, "volt": 37}, {"inst": 1, "volt": 37, "curr": 12}],
            params={"BATT2_MONITOR": 10}))
        assert [p.instance for p in packs] == [1]

    def test_instance_nine_uses_the_letter_prefix(self):
        packs = read_packs(_reader(
            [{"inst": 0, "volt": 37, "curr": 9},
             {"inst": 9, "volt": 37, "curr": 9}],
            params={"BATTA_MONITOR": 10}))
        assert [p.instance for p in packs] == [0]

    def test_instance_past_the_parameter_table_is_kept(self):
        packs = read_packs(_reader([{"inst": 0, "volt": 37, "curr": 9},
                                    {"inst": 40, "volt": 37, "curr": 9}]))
        assert [p.instance for p in packs] == [0, 40]


class TestSumOverPacks:
    def test_second_pack_lands_on_the_first_time_base(self):
        reader = _reader([{"inst": 0, "volt": 37, "curr": 9}])
        first = read_packs(reader)[0]
        other = read_packs(_reader([{"inst": 1, "volt": 37, "curr": 3}]))[0]
        other.ts = other.ts[::2]
        other.curr = other.curr[::2]

        total = sum_over_packs([first, other], [first.curr, other.curr])

        assert len(total) == len(first.ts)
        assert np.allclose(total, 12.0)

    def test_pack_without_two_finite_samples_adds_nothing(self):
        reader = _reader([{"inst": 0, "volt": 37, "curr": 9},
                          {"inst": 1, "volt": 37, "curr": 3}])
        first, other = read_packs(reader)
        other.curr = np.full(len(other.ts), np.nan)

        total = sum_over_packs([first, other], [first.curr, other.curr])

        assert np.allclose(total, 9.0)


class TestPowerAnalyzer:
    def test_power_adds_up_over_packs(self):
        # 37 V x 9 A = 333 W and 37 V x 3 A = 111 W.
        result = _power(_reader([{"inst": 0, "volt": 37, "curr": 9},
                                 {"inst": 1, "volt": 37, "curr": 3}]))

        assert any("444" in d for d in result.details)
        assert _note_shown(result)

    def test_energy_adds_up_from_curr_tot(self):
        # 1000 mAh and 500 mAh at 37 V: 37 Wh and 18.5 Wh.
        result = _power(_reader(
            [{"inst": 0, "volt": 37, "curr": 9, "mah": 1000},
             {"inst": 1, "volt": 37, "curr": 3, "mah": 500}]))

        assert any("55.5" in d for d in result.details)

    def test_energy_integrates_a_pack_without_curr_tot(self):
        # 444 W for 100 s.
        result = _power(_reader([{"inst": 0, "volt": 37, "curr": 9},
                                 {"inst": 1, "volt": 37, "curr": 3}]))

        assert any("12.3" in d for d in result.details)

    def test_sum_monitor_is_not_counted_twice(self):
        result = _power(_reader(
            [{"inst": 0, "volt": 37, "curr": 9},
             {"inst": 1, "volt": 37, "curr": 3},
             {"inst": 2, "volt": 37, "curr": 12}],
            params={"BATT3_MONITOR": 10}))

        assert any("444" in d for d in result.details)
        assert not any("888" in d for d in result.details)

    def test_voltage_rail_changes_nothing(self):
        single = _power(_reader([{"inst": 0, "volt": 50, "curr": 20}]))
        with_rail = _power(_reader([{"inst": 0, "volt": 50, "curr": 20},
                                    {"inst": 1, "volt": 24}]))

        assert with_rail.details == single.details
        assert not _note_shown(with_rail)

    def test_pack_on_a_later_instance_is_analyzed(self):
        result = _power(_reader([{"inst": 0, "volt": 50},
                                 {"inst": 1, "volt": 37, "curr": 9}]))

        assert result.summary != t("power.no_current")
        assert any("333" in d for d in result.details)

    def test_no_current_anywhere_says_so(self):
        result = _power(_reader([{"inst": 0, "volt": 50},
                                 {"inst": 1, "volt": 24}]))

        assert result.summary == t("power.no_current")

    def test_low_voltage_check_stays_on_instance_zero(self):
        # BATT_LOW_VOLT belongs to instance 0, so a healthy second pack
        # must not hide its low voltage, and a 24 V rail must not trip it.
        params = {"BATT_LOW_VOLT": 44.0, "BATT_CRT_VOLT": 0.0}
        low_first = _power(_reader(
            [{"inst": 0, "volt": 40}, {"inst": 1, "volt": 50, "curr": 9}],
            params=params))
        low_rail = _power(_reader(
            [{"inst": 0, "volt": 50, "curr": 9}, {"inst": 1, "volt": 24}],
            params=params))

        assert low_first.status == "warning"
        assert low_rail.status == "good"


class TestCruiseEfficiency:
    PLANE = "ArduPlane"

    def test_sum_monitor_is_not_counted_twice(self):
        result = _cruise(_reader(
            [{"inst": 0, "volt": 37, "curr": 9},
             {"inst": 1, "volt": 37, "curr": 3},
             {"inst": 2, "volt": 37, "curr": 12}],
            params={"BATT3_MONITOR": 10}, vehicle_type=self.PLANE))

        assert any("444.0 W" in d for d in result.details)
        assert _note_shown(result)

    def test_pack_on_a_later_instance_is_analyzed(self):
        result = _cruise(_reader(
            [{"inst": 0, "volt": 50}, {"inst": 1, "volt": 37, "curr": 9}],
            vehicle_type=self.PLANE))

        assert any("333.0 W" in d for d in result.details)
        assert not _note_shown(result)

    def test_voltage_rails_still_report_no_current(self):
        result = _cruise(_reader(
            [{"inst": 0, "volt": 50}, {"inst": 1, "volt": 24}],
            vehicle_type=self.PLANE))

        assert result.summary == t("cruise_efficiency.no_current")

    def test_empty_bat_still_reports_empty(self):
        reader = SyntheticReader(
            messages={"BAT": {"_timestamp": np.array([]),
                              "Inst": np.array([]), "Volt": np.array([]),
                              "Curr": np.array([])}},
            vehicle_type=self.PLANE)

        assert _cruise(reader).summary == t("cruise_efficiency.empty")
