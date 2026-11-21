# ADR 0001: Architecture hardening, not a rewrite

- Status: accepted
- Date: 2026-11-21
- Starting point: v0.12.11, tag/release commit `071d0d7`

## Context

ArduScope is a layered monolith: five entry points, one single-log pipeline,
32 auto-discovered analyzers, and a library they share. The three-session
review found these implementation problems:

1. Silent degradation: analyzer import failures warn and continue; web
   metrics and AI failures are swallowed. A report can look complete while
   missing analysis.
2. The same armed-mask, primary-lane and per-log metric logic exists in
   several places, so one log can produce different numbers in the single
   report, the comparison report and the fleet report.
3. Entry modules import other entry modules; the lint check only scans
   `lib` and `analyzers`.
4. `web_app.py` mixes HTTP, state, uploads, history, settings and report
   orchestration.
5. `fleet.db` and `history.json` have no schema version or migration path.
6. `param_suggestions` is a list of dicts, and threshold sources are read
   from source comments with `inspect.getsource`.
7. Top-level package names (`lib`, `analyzers`, `locales`) are generic, and
   the architecture document does not describe the real entry graph.

The analyzers, thresholds and diagnostic decisions are valuable and are not
the problem. Rewriting them would lose evidence and calibration.

## Decision

Harden the architecture in phases, with behavior parity as the first
constraint:

- No analyzer, threshold or diagnostic-semantics rewrite.
- Failures become explicit `AnalysisIssue`s and failed cards, never silent.
- One implementation of armed masks, primary-lane selection and per-log
  metrics.
- Add an application boundary: `AnalysisRequest`, `AnalysisOutcome`,
  `ReportSession`, renderers.
- Split Web state into stores and an application service; keep Bottle routes
  thin.
- Add schema versions and migrations for fleet and web history data.
- Type `ParamSuggestion` and `ThresholdSource`; remove source-comment
  reflection.
- Move to a `src/arduscope` namespace, keeping the CLI command names.
- Share report theme/components without changing the visual design.

No microservices, ORM, queue, async rewrite or new product features. The
lightweight `multi_report` comparison path stays; only its duplicated metric
logic is centralized.

## Consequences

- Every phase is independently reviewable and releasable.
- The corpus and snapshots are the behavior contract: 561 logs, 424
  snapshots, SITL 55/41 and Windows 30/30.
- An unexpected corpus status/action/suggestion change stops the work.
- Feature work is frozen until the hardening phases finish.
- The target layout is `src/arduscope/{core,analysis,report,archive,web,agent,cli}`
  with dependency rules enforced by tests.
