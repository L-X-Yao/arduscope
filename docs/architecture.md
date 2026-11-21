# Architecture

What depends on what, and where a change belongs. The product positioning is
in [POSITIONING.md](../POSITIONING.md); this file is about the code.

## Shape

ArduScope is a layered monolith: five entry points, one analysis pipeline,
32 auto-discovered analyzers, and a library they share.

```
entry points
  flight_report.py   batch_report.py   web_app.py
        │                  │              │
        └──────────────┬───┴──────────────┘
                       ▼
             src/arduscope/lib/report_session.py
                  ReportSession.run()
                       │
                       ▼
             src/arduscope/lib/pipeline.py  run_analysis()
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   src/arduscope/analyzers/*    lib primitives   src/arduscope/lib/agent/*
   (auto-found)   log_reader, results, thresholds,
                  i18n, vehicle_detect, ...
```

`multi_report.py` is the exception: it compares metrics across logs and uses
`src/arduscope/lib/multi_metrics.py`, not the full analyzer pipeline. `fleet_report.py`
uses `src/arduscope/lib/fleet_history.py`; both are entry points, not library layers.

## Rules

1. `src/arduscope/analyzers/*` may import `lib`; `lib` must not import `analyzers`, except
   for the function-local `discover_analyzers` imports in `src/arduscope/lib/pipeline.py`
   and `src/arduscope/lib/fleet_history.py`. Those three imports break the import cycle and
   are allowlisted. `tools/lint_project.py` check 8 enforces this rule.
2. `lib` and `analyzers` must not import an entry module. `analyzers` must not
   import `src/arduscope/lib/agent`. The same check enforces both.
3. Single-log analysis goes through `ReportSession` in
   `src/arduscope/lib/report_session.py`, which calls
   `src/arduscope/lib/pipeline.run_analysis()`, runs the diagnosis and archives
   the result. The batch, web and flight CLIs all use it. Do not add a second
   analyzer loop in an entry point.
4. `AnalysisResult` lives in `src/arduscope/lib/results.py`; `src/arduscope/analyzers/__init__.py`
   re-exports it for analyzer authors. Constants that both layers need go to
   `lib`, never to `analyzers`.
5. `web_app.py` owns the HTTP routes, uploads, settings and security. Mutable
   job and history state lives in `src/arduscope/web/state.py` (`WebState`,
   `JobStore`, `HistoryStore`). Page rendering is pure and lives in
   `src/arduscope/lib/web_pages.py`; upload-name and memory-budget helpers live
   in `src/arduscope/lib/web_limits.py`.
6. Reports: `src/arduscope/lib/html_report.py` builds the single-log page,
   `src/arduscope/lib/comparison_report.py` the multi-log page. Both read thresholds from
   `src/arduscope/lib/thresholds.py`; a report never invents a threshold.
7. The optional agent is `src/arduscope/lib/agent/`. It reads `PipelineResult` through
   read-only tools and cannot change a status, score, action level or
   suggestion.
8. A failure is never swallowed: the pipeline records an `AnalysisIssue`, the
   report shows a failed card and marks the run incomplete, and the fleet
   archive refuses an incomplete run. `ReportSession` does the same for a
   diagnosis failure and reports an archive error.

## Extending

- **New analyzer**: add a file in `src/arduscope/analyzers/`, subclass `BaseAnalyzer`, set
  `registry_key`, `required_messages` and `sort_order`, implement `analyze()`.
  It is registered in the CLI, web and reports automatically. Add its
  thresholds to `src/arduscope/lib/thresholds.py` with a source, and a row in
  `docs/suggestion_evidence.md` if it suggests a parameter.
- **New page**: add a pure builder to `src/arduscope/lib/web_pages.py`, a thin route in
  `web_app.py`. Keep request/state reads in the route, not the builder.
- **New entry point**: import `arduscope.lib.report_session` (or
  `arduscope.lib.pipeline` for a metrics-only tool), then add it to
  `pyproject.toml` (`py-modules`, `project.scripts`) and to
  `build_windows_package.py` (`INCLUDE_FILES`, `NUITKA_ENTRY_POINTS`).
  `tests/test_packaging.py` checks all three.

## Known debt

- Phases 7-9 of the 2026-11-21 architecture overhaul are not done: typed
  `ParamSuggestion` and a `ThresholdSource` data map, removal of
  `inspect.getsource`, moving `lib/agent/` to `arduscope/agent/`, a shared
  report theme/CSS, and a full ADR and contributor-doc rewrite. v0.13.1 stops
  at the end of Phase 6.
- `src/arduscope/lib/pipeline.py` is both the orchestration layer and part of
  `lib`; `ReportSession` is the application boundary, but it too lives in
  `lib`, and the pipeline's analyzer imports are the visible exception.
- `web_app.py` still holds the upload, settings and security routes; job and
  history state moved to `src/arduscope/web/state.py`. Extracting those routes
  is deferred.
- `tests/` mirrors the source area (`src/arduscope/analyzers/`, `src/arduscope/lib/`, `web/`, `agent/`,
  `cli/`, `tools/`, `integration/`, `sitl/`); shared helpers stay at the
  root. Put a new test in the folder of the code it exercises.
- `src/arduscope/analyzers/notch_tuning.py` and `src/arduscope/lib/html_report.py` are large because their
  domains are; check the layer before adding to them.

## Verify

```
python3 tools/lint_project.py      # locales, contracts, layering
python3 tools/check_analyzers.py   # analyzer contract and evidence source
python3 tools/check_i18n.py        # user-facing strings
python3 tools/ci_local.py          # docs and tests gate
```

`tools/release_gate.py` adds snapshots, SITL and the corpora. It takes about
20 minutes and needs the logs; it is a release gate, not a per-change gate.
