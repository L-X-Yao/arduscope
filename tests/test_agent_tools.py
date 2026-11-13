"""The online agent's tools: bounds, redaction, and what they return."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from lib.agent.config import AgentConfig
from lib.agent.tools import MAX_RESULT_BYTES, ToolBox, _json_size
from lib.pipeline import PipelineResult
from lib.results import AnalysisResult
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

VEHICLE = VehicleInfo(vehicle_type="ArduCopter", board_name="CubeOrange",
                      firmware_version="V4.6.3", frame_class=1,
                      motor_count=4, imu_count=2, is_quadplane=False)


def _pipeline(messages=None, params=None, results=None):
    ts = np.linspace(0, 100, 1000)
    messages = messages or {
        "ATT": {"_timestamp": ts, "Roll": np.sin(ts)},
        "GPS": {"_timestamp": ts,
                "Lat": np.full_like(ts, 35.1),
                "Lng": np.full_like(ts, 104.2)},
        "GYR": {"_timestamp": ts,
                "GyrX": np.sin(2 * np.pi * 4 * ts)},
    }
    reader = SyntheticReader(messages, params=params or {
        "BATT_CAPACITY": 5000, "ARMING_CHECK": 1})
    results = results or {
        "attitude": AnalysisResult(
            title="Attitude", status="warning", summary="roll",
            evidence=[{"field": "roll", "value": 20, "threshold": 15}],
            evidence_source="src"),
        "vibration": AnalysisResult(
            title="Vibration", status="critical", summary="vibe",
            evidence_source="src"),
    }
    return PipelineResult(
        reader=reader, vehicle_info=VEHICLE, thresholds=Thresholds(),
        results=list(results.values()), results_by_key=results,
        health_score=42, conflicts=[], gps_track=None,
    )


def _box(config=None):
    return ToolBox(_pipeline(), config or AgentConfig(
        provider="openai", model="m", api_key="k"))


def test_list_and_get_result():
    box = _box()
    listed = box.call("list_results", {})
    assert listed["count"] == 2
    assert {r["key"] for r in listed["results"]} == {"attitude", "vibration"}

    one = box.call("get_result", {"key": "attitude"})
    assert one["evidence_source"] == "src"
    assert one["evidence"][0]["field"] == "roll"

    missing = box.call("get_result", {"key": "nope"})
    assert "error" in missing


def test_gps_fields_are_stripped_by_default():
    box = _box()
    result = box.call("get_messages",
                      {"msg_type": "GPS", "fields": ["Lat", "Lng"]})
    assert "error" in result
    assert set(result["redacted_fields"]) == {"Lat", "Lng"}

    allowed = _box(AgentConfig(provider="openai", model="m", api_key="k",
                               allow_gps=True))
    result = allowed.call("get_messages",
                          {"msg_type": "GPS", "fields": ["Lat", "Lng"],
                           "max_points": 3})
    assert result["n_total"] == 1000
    assert len(result["points"]) == 3
    assert result["points"][0]["Lat"] == pytest.approx(35.1)


def test_message_points_are_capped():
    box = _box()
    result = box.call("get_messages",
                      {"msg_type": "ATT", "fields": ["Roll"],
                       "max_points": 9999})
    assert 0 < len(result["points"]) <= 500


def test_params_by_prefix_and_name():
    box = _box()
    by_prefix = box.call("get_params", {"prefix": "BATT"})
    assert by_prefix["matched"] == 1
    assert by_prefix["params"]["BATT_CAPACITY"] == 5000

    by_name = box.call("get_params", {"names": ["ARMING_CHECK", "NOPE"]})
    assert by_name["params"] == {"ARMING_CHECK": 1}
    assert by_name["missing"] == ["NOPE"]


def test_statustext_stays_local_until_allowed():
    pipeline = _pipeline()
    pipeline.reader.text_messages = [
        SimpleNamespace(timestamp=5.0, text="CubeOrange 001A002B 3030510C 31323334"),
    ]
    box = ToolBox(pipeline, AgentConfig(provider="openai", model="m",
                                        api_key="k"))
    result = box.call("get_events", {"kind": "text"})
    assert result["sent"] is False
    assert result["rows"] == []
    assert result["count"] == 1

    allowed = ToolBox(pipeline, AgentConfig(
        provider="openai", model="m", api_key="k",
        allow_text=True, allow_serial=False))
    result = allowed.call("get_events", {"kind": "text"})
    assert result["rows"][0]["text"] == "CubeOrange [serial]"


def test_statustext_quoted_in_details_follows_the_text_opt_in():
    pipeline = _pipeline(results={
        "summary": AnalysisResult(
            title="Summary", status="good", summary="log",
            details=["系统消息: 1 条", "[5.0s] EKF3 IMU0 is using GPS"],
            evidence_source="src"),
    })
    pipeline.reader.text_messages = [
        SimpleNamespace(timestamp=5.0, text="EKF3 IMU0 is using GPS"),
    ]
    payload = ToolBox(pipeline, AgentConfig(
        provider="openai", model="m", api_key="k")).call(
            "get_result", {"key": "summary"})
    assert payload["details"] == ["系统消息: 1 条"]
    assert payload["redacted_text_lines"] == 1
    assert payload["egress_note"]

    allowed = ToolBox(pipeline, AgentConfig(
        provider="openai", model="m", api_key="k", allow_text=True))
    payload = allowed.call("get_result", {"key": "summary"})
    assert len(payload["details"]) == 2
    assert "redacted_text_lines" not in payload


def test_spectrum_finds_the_injected_frequency():
    box = _box()
    result = box.call("get_spectrum",
                      {"msg_type": "GYR", "field": "GyrX", "top_n": 3})
    assert result["sample_rate_hz"] == pytest.approx(10.0, abs=0.1)
    assert result["peaks"]
    assert result["peaks"][0]["freq_hz"] == pytest.approx(4.0, abs=0.2)


def test_threshold_returns_value_and_source():
    box = _box()
    result = box.call("get_threshold", {"field": "vibe_warn"})
    assert result["value"] == 15
    assert "METHODIC" in result["source"]


def test_every_result_fits_the_limit():
    box = _box()
    result = box.call("get_messages",
                      {"msg_type": "ATT", "fields": ["Roll"],
                       "max_points": 500})
    assert _json_size(result) <= MAX_RESULT_BYTES


def test_total_budget_stops_tools():
    box = _box()
    box.total_bytes = 10 * MAX_RESULT_BYTES
    box.exhausted = True
    result = box.call("list_results", {})
    assert "error" in result
