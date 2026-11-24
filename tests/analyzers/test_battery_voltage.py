"""Armed, sustained and valid battery-voltage grading for both analyzers."""

from __future__ import annotations

import numpy as np
import pytest

from arduscope.analyzers.battery import BatteryAnalyzer
from arduscope.analyzers.power import PowerAnalyzer
from arduscope.lib.thresholds import Thresholds
from tests.test_synthetic_coverage import SyntheticReader, _vehicle

pytestmark = pytest.mark.fast
ANALYZERS = [BatteryAnalyzer, PowerAnalyzer]


def _reader(ts, volt, params=None, armed=((0.0, 100.0),)):
    ts = np.asarray(ts, dtype=float)
    values = {
        "BATT_LOW_VOLT": 11.0,
        "BATT_CRT_VOLT": 10.0,
        "BATT_LOW_TIMER": 10.0,
        "BATT_MONITOR": 4,
        "BATT_ARM_VOLT": 0.0,
        "BATT_LOW_MAH": 0.0,
        "BATT_CRT_MAH": 0.0,
    }
    values.update(params or {})
    reader = SyntheticReader(messages={"BAT": {
        "_timestamp": ts,
        "Inst": np.zeros(len(ts)),
        "Volt": np.asarray(volt, dtype=float),
        "Curr": np.full(len(ts), 5.0),
        "CurrTot": np.linspace(0.0, 100.0, len(ts)),
        "Rem": np.full(len(ts), 50.0),
    }}, params=values, armed_intervals=list(armed))
    return reader


def _run(analyzer, reader):
    return analyzer(reader, Thresholds(), _vehicle()).analyze()


def _dip(ts, start, end, low=9.0, high=12.0):
    ts = np.asarray(ts, dtype=float)
    return np.where((ts >= start) & (ts <= end), low, high)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_twelve_seconds_below_critical_is_critical(analyzer):
    reader = _reader(np.arange(21.0), _dip(np.arange(21.0), 2, 14))
    result = _run(analyzer, reader)
    assert result.status == "critical"
    assert any("LOW_TIMER" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_missing_low_timer_uses_the_firmware_default(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 13))
    del reader.params["BATT_LOW_TIMER"]
    assert _run(analyzer, reader).status == "critical"


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_short_voltage_dip_is_not_critical(analyzer):
    ts = np.linspace(0.0, 2.0, 21)
    reader = _reader(ts, _dip(ts, 1.0, 1.2))
    assert _run(analyzer, reader).status == "good"


@pytest.mark.parametrize("seconds,expected", [(9, "good"), (11, "critical")])
@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_only_persistence_beyond_the_timer_is_critical(
        analyzer, seconds, expected):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 2 + seconds))
    assert _run(analyzer, reader).status == expected


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_zero_low_timer_disables_voltage_grading(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 14), {"BATT_LOW_TIMER": 0.0})
    result = _run(analyzer, reader)
    assert result.status == "good"
    assert any("BATT_LOW_TIMER" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_zero_timer_wins_over_an_invalid_configuration(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 18), {
        "BATT_LOW_TIMER": 0.0, "BATT_CRT_VOLT": 13.0})
    result = _run(analyzer, reader)
    assert result.status == "good"
    assert any("BATT_LOW_TIMER" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_no_armed_interval_is_not_a_voltage_critical(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 18), armed=[])
    reader._armed = []
    result = _run(analyzer, reader)
    assert result.status == "good"
    assert any("armed" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_disabled_monitor_is_not_a_voltage_critical(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 18), {"BATT_MONITOR": 0})
    result = _run(analyzer, reader)
    assert result.status == "good"
    assert any("BATT_MONITOR" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_impossible_arming_voltage_is_a_configuration_warning(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 18), {"BATT_ARM_VOLT": 13.0})
    result = _run(analyzer, reader)
    assert result.status == "warning"
    assert any("BATT_ARM_VOLT" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_critical_threshold_above_pack_max_is_a_configuration_warning(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 18), {"BATT_CRT_VOLT": 13.0})
    result = _run(analyzer, reader)
    assert result.status == "warning"
    assert result.status != "critical"
    assert any("BATT_CRT_VOLT" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_low_threshold_near_pack_max_is_a_configuration_warning(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 18), {"BATT_LOW_VOLT": 11.9})
    result = _run(analyzer, reader)
    assert result.status == "warning"
    assert any("BATT_LOW_VOLT" in detail for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_flat_subvolt_sensor_is_not_a_battery_critical(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, np.full(len(ts), 0.25))
    result = _run(analyzer, reader)
    assert result.status == "warning"
    assert any("sensor" in detail.lower() or "传感器" in detail
               for detail in result.details)


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_telemetry_gaps_do_not_join_low_voltage_segments(analyzer):
    ts = np.array([0, 1, 2, 3, 14, 15, 16, 17, 28, 29, 30, 31, 32], dtype=float)
    volt = np.array([9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 12], dtype=float)
    reader = _reader(ts, volt)
    assert _run(analyzer, reader).status == "good"


@pytest.mark.parametrize("analyzer", ANALYZERS)
def test_sustained_low_voltage_without_critical_is_a_warning(analyzer):
    ts = np.arange(21.0)
    reader = _reader(ts, _dip(ts, 2, 14, low=10.5))
    result = _run(analyzer, reader)
    assert result.status == "warning"
    assert result.status != "critical"
