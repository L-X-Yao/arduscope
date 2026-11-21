# Architecture baseline, 2026-11-21

Read-only baseline before the architecture hardening phases. Starting point:
v0.12.11, tag/release commit `071d0d7`, code commit `4dd885d`.

## Gate and package baseline

| Measure | Value |
|---|---|
| Full gate commit | `4dd885d` |
| `ci_local` | 11/11, 486 s |
| Snapshots | 424 passed, 134 s |
| SITL | 55 passed, 41 skipped, 171 s |
| Corpus | 43 eagle + 518 test, 0 changes, 273 s |
| Windows package | 30/30 |
| Tests collected | 3,842 |
| Analyzers | 32 |
| Locale keys | 1,914 in each language |
| Python code | 60,624 lines across 197 tracked files |
| Current release | v0.12.11, immutable, 2026-11-21T06:06Z |

The v0.12.11 assets are wheel 493,169 B, sdist 462,040 B and Windows zip
87,992,878 B.

## Architecture facts recorded

- Five entry points: `flight_report.py`, `multi_report.py`,
  `batch_report.py`, `fleet_report.py`, `web_app.py`.
- `lib/pipeline.py::run_analysis` is the only single-log pipeline.
- 32 analyzers are auto-discovered; 38 modules live under `lib/`.
- `web_app.py` owns mutable job, upload, history and settings state.
- `multi_report` uses `lib/multi_metrics` and bypasses the full pipeline.
- `fleet_history` stores the fleet in sqlite next to the report directory.
- Real-log and SITL tests run only in the local gate.
- `fleet.db` and `history.json` have no schema version.
- `discover_analyzers` warns and continues when an analyzer module fails to
  import.
- `web_app.py` swallows metrics and AI diagnosis exceptions.

## Guardrails

- No analyzer, threshold or diagnostic-semantics change during hardening.
- Every phase must run targeted tests and `ci_local`; phases that touch
  diagnostic or report data also run `snapshots,corpus`.
- An unexpected corpus status/action/suggestion change stops the plan.
- The first architecture test marks the current entry-to-entry imports as
  xfail until the application boundary removes them.
