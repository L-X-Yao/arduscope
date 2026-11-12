"""The two API adapters, against fake SDK modules (no network)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from lib.agent.config import AgentConfig
from lib.agent.providers import (AnthropicAdapter, NoToolSupport,
                                 OpenAIAdapter, ProviderError, ToolCall)

pytestmark = pytest.mark.fast

CONFIG = AgentConfig(provider="openai", model="m", api_key="k")

TOOLS = [{"name": "list_results", "description": "list",
          "parameters": {"type": "object", "properties": {}}}]


class _FakeMessages:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.reply


def _anthropic_reply():
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="checking"),
            SimpleNamespace(type="tool_use", id="t1", name="list_results",
                            input={}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def test_anthropic_adapter_converts_tools_and_reply(monkeypatch):
    messages = _FakeMessages(reply=_anthropic_reply())
    fake = SimpleNamespace(Anthropic=lambda **kw: SimpleNamespace(
        messages=messages))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    adapter = AnthropicAdapter(CONFIG)
    turn = adapter.complete("sys", [{"role": "user", "content": "hi"}],
                            TOOLS)
    assert turn.text == "checking"
    assert turn.tool_calls == [ToolCall(id="t1", name="list_results",
                                        arguments={})]
    assert turn.input_tokens == 10
    request = messages.requests[0]
    assert request["tools"][0]["input_schema"] == {"type": "object",
                                                   "properties": {}}


def test_anthropic_adapter_groups_tool_results(monkeypatch):
    messages = _FakeMessages(reply=SimpleNamespace(
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn", usage=None))
    fake = SimpleNamespace(Anthropic=lambda **kw: SimpleNamespace(
        messages=messages))
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    adapter = AnthropicAdapter(CONFIG)
    adapter.complete(
        "sys",
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "text": "",
             "tool_calls": [ToolCall(id="t1", name="list_results",
                                     arguments={})]},
            {"role": "tool", "tool_call_id": "t1", "name": "list_results",
             "content": "{}"},
            {"role": "tool", "tool_call_id": "t2", "name": "get_vehicle",
             "content": "{}"},
        ],
        TOOLS,
    )
    converted = messages.requests[0]["messages"]
    assert converted[-1]["role"] == "user"
    assert len(converted[-1]["content"]) == 2
    assert converted[-1]["content"][0]["type"] == "tool_result"


def test_anthropic_tool_refusal_is_its_own_error(monkeypatch):
    messages = _FakeMessages(error=ValueError("tools are not supported"))
    fake = SimpleNamespace(Anthropic=lambda **kw: SimpleNamespace(
        messages=messages))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    with pytest.raises(NoToolSupport):
        AnthropicAdapter(CONFIG).complete("s", [], TOOLS)


def test_openai_adapter_converts_tools_and_reply(monkeypatch):
    reply = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(
                    id="c1",
                    function=SimpleNamespace(
                        name="list_results",
                        arguments=json.dumps({})),
                )],
            ))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3),
    )
    messages = _FakeMessages(reply=reply)
    fake = SimpleNamespace(OpenAI=lambda **kw: SimpleNamespace(
        chat=SimpleNamespace(completions=messages)))
    monkeypatch.setitem(sys.modules, "openai", fake)

    adapter = OpenAIAdapter(CONFIG)
    turn = adapter.complete("sys", [{"role": "user", "content": "hi"}],
                            TOOLS)
    assert turn.tool_calls == [ToolCall(id="c1", name="list_results",
                                        arguments={})]
    assert turn.input_tokens == 7
    request = messages.requests[0]
    assert request["tools"][0]["function"]["name"] == "list_results"
    assert request["messages"][0]["role"] == "system"


def test_openai_api_error_is_a_provider_error(monkeypatch):
    messages = _FakeMessages(error=ConnectionError("refused"))
    fake = SimpleNamespace(OpenAI=lambda **kw: SimpleNamespace(
        chat=SimpleNamespace(completions=messages)))
    monkeypatch.setitem(sys.modules, "openai", fake)
    with pytest.raises(ProviderError):
        OpenAIAdapter(CONFIG).complete("s", [], TOOLS)


def test_client_construction_error_is_a_provider_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("missing credentials")

    fake = SimpleNamespace(OpenAI=boom)
    monkeypatch.setitem(sys.modules, "openai", fake)
    with pytest.raises(ProviderError):
        OpenAIAdapter(CONFIG)


def test_openai_sends_an_output_cap(monkeypatch):
    reply = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="done", tool_calls=None))],
        usage=None)
    messages = _FakeMessages(reply=reply)
    fake = SimpleNamespace(OpenAI=lambda **kw: SimpleNamespace(
        chat=SimpleNamespace(completions=messages)))
    monkeypatch.setitem(sys.modules, "openai", fake)
    OpenAIAdapter(CONFIG).complete("s", [], TOOLS)
    assert messages.requests[0]["max_tokens"] > 0


def test_openai_retries_with_the_other_cap_name(monkeypatch):
    reply = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="done", tool_calls=None))],
        usage=None)

    class _TwoStep(_FakeMessages):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            if "max_tokens" in kwargs:
                raise ValueError("max_tokens is not supported")
            return reply

    messages = _TwoStep()
    fake = SimpleNamespace(OpenAI=lambda **kw: SimpleNamespace(
        chat=SimpleNamespace(completions=messages)))
    monkeypatch.setitem(sys.modules, "openai", fake)
    turn = OpenAIAdapter(CONFIG).complete("s", [], TOOLS)
    assert turn.text == "done"
    assert "max_completion_tokens" in messages.requests[1]
