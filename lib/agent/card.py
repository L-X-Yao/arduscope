"""Build the report card from the agent's submission, or fall back."""

from __future__ import annotations

import html as _html

from lib.ai_diagnosis import generate_diagnosis
from lib.i18n import t
from lib.results import AnalysisResult

from .config import AgentConfig
from .egress import egress_categories
from .loop import AgentError, run_agent
from .providers import NoProviderPackage, NoToolSupport, ProviderError

def _worst_status(results_by_key: dict) -> str:
    """The card's severity is the offline analyzers' verdict."""
    statuses = {r.status for r in results_by_key.values()}
    if "critical" in statuses:
        return "critical"
    return "warning" if "warning" in statuses else "good"


def _kind_label(kind: str) -> str:
    return {
        "correlation": t("agent.kind_correlation"),
        "data_gap": t("agent.kind_data_gap"),
    }.get(kind, t("agent.kind_observation"))


def _egress_line(config: AgentConfig) -> str:
    names = {
        "results": t("agent.egress_results"),
        "parameters": t("agent.egress_parameters"),
        "messages": t("agent.egress_messages"),
        "gps": t("agent.egress_gps"),
        "serial": t("agent.egress_serial"),
        "text": t("agent.egress_text"),
    }
    categories = egress_categories(config.allow_gps, config.allow_serial,
                                   config.allow_text)
    return t("agent.egress_sent",
             categories=", ".join(names[c] for c in categories))


def _build_details(run, config: AgentConfig) -> list[str]:
    details = [
        t("agent.generated_by", provider=run.provider, model=run.model,
          turns=run.turns, calls=len(run.records)),
        t("agent.usage", input=run.input_tokens, output=run.output_tokens,
          seconds=round(run.elapsed, 1)),
    ]
    if run.records:
        details.append(t("agent.tools_called", tools=", ".join(sorted(
            {record.name for record in run.records}))))
    for observation in run.submission.observations:
        cites = ", ".join(f"T{c}" for c in observation.cites)
        mark = (f"<em>{_html.escape(t('agent.unverified_mark'))}</em> "
                if observation.unverified else "")
        details.append(
            f"<strong>[{_html.escape(_kind_label(observation.kind))}]</strong> "
            f"{mark}{_html.escape(observation.text)}"
            + (f" <code>{_html.escape(cites)}</code>" if cites else ""))
    if run.submission.unverified_count:
        details.append(t("agent.unverified_section",
                         count=run.submission.unverified_count))
    details.append(_html.escape(_egress_line(config)))
    return details


def _build_card(run, pipeline, config: AgentConfig) -> AnalysisResult:
    status = _worst_status(pipeline.results_by_key)
    summary = (run.submission.summary[:300]
               or t("agent.summary_fallback",
                    count=len(run.submission.observations)))
    if run.submission.summary_unverified:
        summary = f"[{t('agent.unverified_mark')}] {summary}"
    return AnalysisResult(
        title=t("agent.card_title"),
        status=status,
        summary=summary,
        details=_build_details(run, config),
        figures=[],
        sub_results=[],
        evidence_source=t("agent.evidence",
                          model=run.model, provider=run.provider),
        provider=f"agent:{run.provider}",
    )


def _fallback(pipeline, config: AgentConfig, reason: str,
              on_event=None) -> AnalysisResult:
    """Single-shot summary first, then the rule engine, visibly."""
    if on_event:
        on_event({"stage": "fallback", "reason": reason})
    card = generate_diagnosis(
        pipeline.results_by_key,
        pipeline.vehicle_info,
        pipeline.health_score,
        pipeline.reader.get_flight_duration(),
        provider=config.provider,
        api_key=config.resolved_key(),
        model=config.model,
        base_url=config.base_url,
    )
    note = t("agent.fallback_note", reason=reason)
    card.details.insert(0, _html.escape(note))
    return card


def analyze_with_agent(pipeline, config: AgentConfig,
                       on_event=None) -> AnalysisResult:
    """Run the agent; on any failure fall back and say why."""
    try:
        run = run_agent(pipeline, config, on_event=on_event)
    except NoToolSupport as e:
        return _fallback(pipeline, config,
                         t("agent.fallback_no_tools", error=str(e)[:200]),
                         on_event)
    except NoProviderPackage as e:
        return _fallback(pipeline, config,
                         t("agent.fallback_no_package", package=str(e)),
                         on_event)
    except ProviderError as e:
        return _fallback(pipeline, config,
                         t("agent.fallback_provider", error=str(e)[:200]),
                         on_event)
    except AgentError as e:
        return _fallback(pipeline, config, e.reason, on_event)
    except Exception as e:  # noqa: BLE001 - the agent must never break a report
        # A defect anywhere in the loop must cost the model narrative, not
        # the whole report: the offline card is still there to fall back to.
        return _fallback(
            pipeline, config,
            t("agent.fallback_unexpected",
              error=f"{type(e).__name__}: {str(e)[:200]}"),
            on_event)
    return _build_card(run, pipeline, config)
