"""The predefined agent workflow and its budgets.

The model starts from the offline results, checks every warning and
critical through read-only tools, and finishes by calling
``submit_analysis``. It cannot run code or read the .BIN directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from lib.i18n import get_locale, t

from .citations import Submission, ToolRecord, validate
from .config import AgentConfig
from .providers import ProviderError, Turn, make_provider
from .tools import TOOL_SPECS, ToolBox

SUBMIT_SPEC = {
    "name": "submit_analysis",
    "description": "Finish the analysis. Every observation must cite the "
                   "tool call numbers that support it.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "one sentence for the report card"},
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "cites": {"type": "array",
                                  "items": {"type": "integer"}},
                        "kind": {"type": "string",
                                 "enum": ["observation", "correlation",
                                          "data_gap"]},
                    },
                    "required": ["text", "cites", "kind"],
                },
            },
        },
        "required": ["summary", "observations"],
    },
}


class AgentError(RuntimeError):
    """The agent could not finish; the caller falls back."""

    def __init__(self, reason: str, partial=None):
        super().__init__(reason)
        self.reason = reason
        self.partial = partial


@dataclass
class AgentRun:
    submission: Submission
    records: list[ToolRecord]
    turns: int
    input_tokens: int
    output_tokens: int
    elapsed: float
    provider: str
    model: str


@dataclass
class _State:
    records: list[ToolRecord] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _system_prompt() -> str:
    language = "Chinese" if get_locale() == "zh" else "English"
    return t("agent.system_prompt", language=language)


def _first_message(pipeline, config: AgentConfig) -> str:
    info = pipeline.vehicle_info
    return t(
        "agent.first_message",
        vehicle=info.vehicle_type,
        firmware=info.firmware_version,
        duration=round(pipeline.reader.get_flight_duration(), 1),
        score=pipeline.health_score,
        count=len(pipeline.results_by_key),
        calls=config.max_tool_calls,
    )


def _tool_content(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _record_call(state: _State, toolbox: ToolBox, call,
                 on_event) -> ToolRecord:
    if toolbox.exhausted:
        payload = {"error": t("agent.error_tool_budget")}
    else:
        payload = toolbox.call(call.name, call.arguments)
    content = _tool_content(payload)
    record = ToolRecord(
        seq=len(state.records) + 1,
        name=call.name,
        arguments=dict(call.arguments or {}),
        content=content,
        is_error=isinstance(payload, dict) and "error" in payload,
    )
    state.records.append(record)
    if on_event:
        on_event({"stage": "tool", "call": record.seq,
                  "tool": record.name, "error": record.is_error,
                  "calls": len(state.records)})
    return record


def run_agent(pipeline, config: AgentConfig, provider=None,
              on_event=None) -> AgentRun:
    """Run the workflow. Raises AgentError with the reason on failure."""
    started = time.monotonic()
    deadline = started + max(30.0, float(config.max_seconds))
    provider = provider or make_provider(config)
    toolbox = ToolBox(pipeline, config)
    state = _State()

    messages: list[dict] = [
        {"role": "user", "content": _first_message(pipeline, config)},
    ]
    tools = list(TOOL_SPECS) + [SUBMIT_SPEC]
    asked_to_submit = False
    budget_hit = False

    for _turn in range(max(4, int(config.max_tool_calls) + 4)):
        if time.monotonic() > deadline:
            raise AgentError(t("agent.error_timeout"),
                             partial=state.records)
        try:
            reply: Turn = provider.complete(_system_prompt(), messages, tools)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise AgentError(f"{type(e).__name__}: {str(e)[:200]}",
                             partial=state.records) from e

        state.turns += 1
        state.input_tokens += reply.input_tokens
        state.output_tokens += reply.output_tokens

        if reply.tool_calls:
            messages.append({"role": "assistant", "text": reply.text,
                             "tool_calls": reply.tool_calls})
            submitted = None
            for call in reply.tool_calls:
                if call.name == "submit_analysis":
                    submitted = call.arguments
                    continue
                if (len(state.records) >= int(config.max_tool_calls)
                        or toolbox.exhausted):
                    budget_hit = True
                    payload = {"error": t("agent.error_tool_budget")}
                    content = _tool_content(payload)
                    record = ToolRecord(
                        seq=len(state.records) + 1, name=call.name,
                        arguments=dict(call.arguments or {}),
                        content=content, is_error=True)
                    state.records.append(record)
                    if on_event:
                        on_event({"stage": "tool", "call": record.seq,
                                  "tool": record.name, "error": True,
                                  "calls": len(state.records)})
                else:
                    record = _record_call(state, toolbox, call, on_event)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": f"[call {record.seq}] {record.content}",
                })
            if submitted is not None:
                return AgentRun(
                    submission=validate(submitted, state.records),
                    records=state.records, turns=state.turns,
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    elapsed=time.monotonic() - started,
                    provider=config.provider, model=config.model,
                )
            if budget_hit:
                messages.append({
                    "role": "user",
                    "content": t("agent.budget_exhausted"),
                })
                tools = [SUBMIT_SPEC]
            continue

        if reply.text:
            messages.append({"role": "assistant", "text": reply.text,
                             "tool_calls": []})
        if not asked_to_submit:
            asked_to_submit = True
            messages.append({"role": "user",
                             "content": t("agent.ask_submit")})
            continue
        raise AgentError(t("agent.error_no_submission"),
                         partial=state.records)

    raise AgentError(t("agent.error_turn_limit"), partial=state.records)
