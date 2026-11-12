"""The agent loop: tool calls, budgets, submission and fallbacks."""

from __future__ import annotations

import numpy as np
import pytest

from lib.agent import card as card_mod
from lib.agent.card import analyze_with_agent
from lib.agent.config import AgentConfig
from lib.agent.loop import AgentError, SUBMIT_SPEC, run_agent
from lib.agent.providers import NoToolSupport, ToolCall, Turn
from lib.pipeline import PipelineResult
from lib.results import AnalysisResult
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

VEHICLE = VehicleInfo(vehicle_type="ArduCopter", board_name="CubeOrange",
                      firmware_version="V4.6.3", frame_class=1,
                      motor_count=4, imu_count=2, is_quadplane=False)
CONFIG = AgentConfig(provider="openai", model="fake-model", api_key="k",
                     max_tool_calls=4, max_seconds=30)


def _pipeline():
    ts = np.linspace(0, 100, 100)
    reader = SyntheticReader({"ATT": {"_timestamp": ts,
                                      "Roll": np.sin(ts)}})
    results = {
        "attitude": AnalysisResult(title="Attitude", status="critical",
                                   summary="roll", evidence_source="src"),
    }
    return PipelineResult(reader, VEHICLE, Thresholds(),
                          list(results.values()), results, 42, [], None)


class _FakeProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.seen_tools = []

    def complete(self, system, messages, tools):
        self.seen_tools.append([t["name"] for t in tools])
        if not self.turns:
            return Turn(text="nothing left")
        return self.turns.pop(0)


def _submission(text="there is 1 result", cites=(1,)):
    return Turn(tool_calls=[ToolCall(
        id="s1", name="submit_analysis",
        arguments={"summary": "one card",
                   "observations": [{"text": text, "cites": list(cites),
                                     "kind": "observation"}]})])


def test_a_complete_run_submits_citations():
    provider = _FakeProvider([
        Turn(tool_calls=[ToolCall(id="a", name="list_results",
                                  arguments={})]),
        _submission(),
    ])
    run = run_agent(_pipeline(), CONFIG, provider=provider)
    assert run.submission.unverified_count == 0
    assert len(run.records) == 1
    assert run.records[0].name == "list_results"
    assert run.input_tokens == 0


def test_budget_turns_extra_calls_into_errors():
    config = AgentConfig(provider="openai", model="fake-model", api_key="k",
                         max_tool_calls=1, max_seconds=30)
    provider = _FakeProvider([
        Turn(tool_calls=[
            ToolCall(id="a", name="list_results", arguments={}),
            ToolCall(id="b", name="get_vehicle", arguments={}),
        ]),
        _submission(cites=(1,)),
    ])
    run = run_agent(_pipeline(), config, provider=provider)
    assert len(run.records) == 2
    assert run.records[0].is_error is False
    assert run.records[1].is_error is True
    assert "submit_analysis" in provider.seen_tools[-1]


def test_text_without_a_submission_raises():
    provider = _FakeProvider([Turn(text="here is a report"),
                              Turn(text="still no submission")])
    with pytest.raises(AgentError):
        run_agent(_pipeline(), CONFIG, provider=provider)


def test_card_takes_status_from_the_offline_results():
    provider = _FakeProvider([
        Turn(tool_calls=[ToolCall(id="a", name="list_results",
                                  arguments={})]),
        _submission(),
    ])
    original = card_mod.run_agent
    card_mod.run_agent = lambda pipeline, config, on_event=None: run_agent(
        pipeline, config, provider=provider, on_event=on_event)
    try:
        card = analyze_with_agent(_pipeline(), CONFIG)
    finally:
        card_mod.run_agent = original
    assert card.status == "critical"
    assert card.provider == "agent:openai"
    assert card.param_suggestions == []
    assert "fake-model" in card.evidence_source


def test_no_tool_support_falls_back_to_single_shot(monkeypatch):
    fallback = AnalysisResult(title="rule", status="warning",
                              evidence_source="src")
    monkeypatch.setattr(card_mod, "run_agent",
                        lambda *a, **k: (_ for _ in ()).throw(
                            NoToolSupport("no tools here")))
    monkeypatch.setattr(card_mod, "generate_diagnosis",
                        lambda *a, **k: fallback)
    card = analyze_with_agent(_pipeline(), CONFIG)
    assert card is fallback
    assert "no tools here" in card.details[0]


def test_submit_spec_is_offered_with_the_tools():
    provider = _FakeProvider([
        Turn(tool_calls=[ToolCall(id="a", name="list_results",
                                  arguments={})]),
        _submission(),
    ])
    run_agent(_pipeline(), CONFIG, provider=provider)
    assert SUBMIT_SPEC["name"] in provider.seen_tools[0]


def test_an_unverified_summary_is_marked_on_the_card():
    from lib.agent.citations import Observation, Submission
    from lib.agent.card import _build_card
    from lib.agent.loop import AgentRun
    run = AgentRun(
        submission=Submission(summary="the peak was 99", observations=[
            Observation(text="vibration is high", cites=[1])],
            unverified_count=0, summary_unverified=True),
        records=[], turns=1, input_tokens=0, output_tokens=0,
        elapsed=0.0, provider="openai", model="m")
    card = _build_card(run, _pipeline(), CONFIG)
    assert "the peak was 99" in card.summary
    assert card.summary != "the peak was 99"


def test_an_unverified_submission_is_sent_back_once():
    provider = _FakeProvider([
        Turn(tool_calls=[ToolCall(id="a", name="list_results",
                                  arguments={})]),
        _submission(text="there are 99 results", cites=(1,)),
        _submission(text="there is 1 result", cites=(1,)),
    ])
    run = run_agent(_pipeline(), CONFIG, provider=provider)
    assert run.submission.unverified_count == 0
    assert run.turns == 3


def _empty_submission():
    return Turn(tool_calls=[ToolCall(
        id="s0", name="submit_analysis",
        arguments={"summary": "", "observations": []})])


def test_an_empty_submission_is_sent_back_once():
    provider = _FakeProvider([
        Turn(tool_calls=[ToolCall(id="a", name="list_results",
                                  arguments={})]),
        _empty_submission(),
        _submission(text="there is 1 result", cites=(1,)),
    ])
    run = run_agent(_pipeline(), CONFIG, provider=provider)
    assert run.submission.observations
    assert run.turns == 3


def test_two_empty_submissions_fall_back():
    provider = _FakeProvider([
        Turn(tool_calls=[ToolCall(id="a", name="list_results",
                                  arguments={})]),
        _empty_submission(),
        _empty_submission(),
    ])
    with pytest.raises(AgentError):
        run_agent(_pipeline(), CONFIG, provider=provider)
