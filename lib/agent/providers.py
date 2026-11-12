"""API adapters for tool-calling models.

Two protocols cover the vendors we can test: Anthropic's Messages API and
OpenAI's chat completions. A service that speaks neither falls back to the
single-shot summary in ``lib.ai_diagnosis``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import AgentConfig


class ProviderError(RuntimeError):
    """The request failed. The caller falls back and says why."""


class NoToolSupport(ProviderError):
    """The service does not accept tool definitions."""


class NoProviderPackage(ProviderError):
    """The SDK named by the provider is not installed."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def _looks_like_tool_refusal(message: str) -> bool:
    text = str(message).lower()
    return ("tool" in text or "function" in text) and any(
        phrase in text for phrase in (
            "not supported", "unsupported", "does not support",
            "unknown parameter", "unrecognized", "invalid parameter",
        ))


class _Adapter:
    def complete(self, system: str, messages: list, tools: list) -> Turn:
        raise NotImplementedError


class AnthropicAdapter(_Adapter):
    def __init__(self, config: AgentConfig):
        try:
            import anthropic
        except ImportError as e:
            raise NoProviderPackage("anthropic") from e
        kwargs = {"api_key": config.resolved_key(), "timeout": 120.0}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = anthropic.Anthropic(**kwargs)
        self._model = config.model

    def complete(self, system: str, messages: list, tools: list) -> Turn:
        converted = []
        pending_results = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                pending_results.append({
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": message["content"],
                })
                continue
            if pending_results:
                converted.append({"role": "user",
                                  "content": pending_results})
                pending_results = []
            if role == "user":
                converted.append({"role": "user",
                                  "content": [{"type": "text",
                                               "text": message["content"]}]})
            else:
                blocks = []
                if message.get("text"):
                    blocks.append({"type": "text", "text": message["text"]})
                for call in message.get("tool_calls", []):
                    blocks.append({"type": "tool_use", "id": call.id,
                                   "name": call.name,
                                   "input": call.arguments})
                converted.append({"role": "assistant", "content": blocks})
        if pending_results:
            converted.append({"role": "user", "content": pending_results})

        api_tools = [{"name": tool["name"],
                      "description": tool["description"],
                      "input_schema": tool["parameters"]}
                     for tool in tools]
        try:
            reply = self._client.messages.create(
                model=self._model,
                max_tokens=16000,
                system=system,
                messages=converted,
                tools=api_tools,
            )
        except Exception as e:  # noqa: BLE001 - SDK raises many types
            if _looks_like_tool_refusal(e):
                raise NoToolSupport(str(e)[:300]) from e
            raise ProviderError(str(e)[:300]) from e

        turn = Turn(stop_reason=getattr(reply, "stop_reason", "") or "")
        usage = getattr(reply, "usage", None)
        if usage is not None:
            turn.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            turn.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        for block in getattr(reply, "content", []) or []:
            kind = getattr(block, "type", None)
            if kind == "text":
                turn.text += getattr(block, "text", "") or ""
            elif kind == "tool_use":
                turn.tool_calls.append(ToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=dict(getattr(block, "input", {}) or {}),
                ))
        return turn


class OpenAIAdapter(_Adapter):
    def __init__(self, config: AgentConfig):
        try:
            import openai
        except ImportError as e:
            raise NoProviderPackage("openai") from e
        kwargs = {"api_key": config.resolved_key(), "timeout": 120.0}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = openai.OpenAI(**kwargs)
        self._model = config.model

    def complete(self, system: str, messages: list, tools: list) -> Turn:
        converted = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "user":
                converted.append({"role": "user",
                                  "content": message["content"]})
            elif role == "tool":
                converted.append({"role": "tool",
                                  "tool_call_id": message["tool_call_id"],
                                  "content": message["content"]})
            else:
                calls = [{
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name,
                                 "arguments": json.dumps(
                                     call.arguments, ensure_ascii=False)},
                } for call in message.get("tool_calls", [])]
                entry = {"role": "assistant",
                         "content": message.get("text") or None}
                if calls:
                    entry["tool_calls"] = calls
                converted.append(entry)

        api_tools = [{"type": "function",
                      "function": {"name": tool["name"],
                                   "description": tool["description"],
                                   "parameters": tool["parameters"]}}
                     for tool in tools]
        try:
            reply = self._client.chat.completions.create(
                model=self._model,
                messages=converted,
                tools=api_tools,
            )
        except Exception as e:  # noqa: BLE001 - SDK raises many types
            if _looks_like_tool_refusal(e):
                raise NoToolSupport(str(e)[:300]) from e
            raise ProviderError(str(e)[:300]) from e

        choices = getattr(reply, "choices", None) or []
        if not choices:
            raise ProviderError("no choices in the reply")
        message = choices[0].message
        turn = Turn(stop_reason=getattr(choices[0], "finish_reason", "") or "",
                    text=(getattr(message, "content", None) or ""))
        usage = getattr(reply, "usage", None)
        if usage is not None:
            turn.input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            turn.output_tokens = int(
                getattr(usage, "completion_tokens", 0) or 0)
        for call in getattr(message, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            raw = getattr(function, "arguments", "") or "{}"
            try:
                arguments = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                arguments = {"_raw": raw}
            turn.tool_calls.append(ToolCall(
                id=str(getattr(call, "id", "")),
                name=str(getattr(function, "name", "")),
                arguments=arguments if isinstance(arguments, dict) else {},
            ))
        return turn


def make_provider(config: AgentConfig) -> _Adapter:
    if config.provider == "anthropic":
        return AnthropicAdapter(config)
    if config.provider == "openai":
        return OpenAIAdapter(config)
    raise ProviderError(f"unknown provider: {config.provider!r}")


def test_connection(config: AgentConfig) -> tuple[bool, str]:
    """One tiny request. Returns (ok, message shown to the user)."""
    adapter = make_provider(config)
    try:
        if isinstance(adapter, AnthropicAdapter):
            adapter._client.messages.create(
                model=config.model, max_tokens=1,
                messages=[{"role": "user", "content": "ping"}])
        else:
            try:
                adapter._client.chat.completions.create(
                    model=config.model, max_tokens=1,
                    messages=[{"role": "user", "content": "ping"}])
            except Exception as e:  # noqa: BLE001
                # Some reasoning models refuse max_tokens; the service is
                # still reachable, which is what this check asks.
                if "max_tokens" not in str(e):
                    raise
            else:
                return True, "ok"
            adapter._client.chat.completions.create(
                model=config.model,
                messages=[{"role": "user", "content": "ping"}])
        return True, "ok"
    except NoProviderPackage:
        raise
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:300]}"
