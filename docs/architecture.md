# Architecture

What depends on what, and where a change belongs. The product positioning is
in [POSITIONING.md](../POSITIONING.md); this file is about the code.
[ADR 0002](adr/0002-architecture-hardening-completion.md) records the final
layout and closes the earlier directory-split target.

## Shape

ArduScope is a layered monolith: five entry points, one single-log analysis
pipeline, 32 auto-discovered analyzers, and shared library packages.

```
flight_report.py     batch_report.py     web_app.py
        │                  │                │
        └──────────────────┼────────────────┘
                           ▼
             src/arduscope/lib/report_session.py
                       ReportSession.run()
                           │
                           ▼
             src/arduscope/lib/pipeline.py
                       run_analysis()
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
   src/arduscope/analyzers/*     lib primitives
        (auto-found)             log_reader, results, thresholds,
                                 i18n, vehicle_detect, ...

multi_report.py  ───────────────> lib/multi_metrics.py
fleet_report.py  ───────────────> lib/fleet_history.py
arduscope/agent/* ──────────────> PipelineResult (read-only tools)
```

`multi_report.py` compares metrics without running the full analyzer pipeline.
`fleet_report.py` reads the fleet archive. The optional agent consumes the
finished pipeline result; it is not an analyzer and cannot change a verdict.
The final packages are `analyzers`, `lib`, `web`, `agent` and `locales` under
`src/arduscope`, with the five root entry modules.

## Rules

1. `src/arduscope/analyzers/*` may import `lib`; `lib` must not import
   `analyzers`, except for the function-local `discover_analyzers` imports in
   `src/arduscope/lib/pipeline.py` and `src/arduscope/lib/fleet_history.py`.
   Those three imports break the import cycle and are allowlisted.
   `tools/lint_project.py` check 8 enforces this rule.
2. `lib` and `analyzers` must not import an entry module. `analyzers` must not
   import `src/arduscope/agent`. The same check enforces both and still
   recognizes the old agent path.
3. Single-log analysis goes through `ReportSession` in
   `src/arduscope/lib/report_session.py`, which calls
   `src/arduscope/lib/pipeline.run_analysis()`, runs the diagnosis and archives
   the result. The batch, web and flight entry points all use it. Do not add a
   second analyzer loop in an entry point.
4. `AnalysisResult` lives in `src/arduscope/lib/results.py`;
   `src/arduscope/analyzers/__init__.py` re-exports it for analyzer authors.
   `ParamSuggestion` is a frozen typed row in `src/arduscope/lib/suggestions.py`;
   `AnalysisResult` rejects dictionary rows. Exports call `to_dict()` so the
   external format stays unchanged. Constants that both layers need go to
   `lib`, never to `analyzers`.
5. `web_app.py` owns the HTTP routes, uploads, settings and security. Mutable
   job and history state lives in `src/arduscope/web/state.py` (`WebState`,
   `JobStore`, `HistoryStore`). Page rendering is pure and lives in
   `src/arduscope/lib/web_pages.py`; upload-name and memory-budget helpers live
   in `src/arduscope/lib/web_limits.py`.
6. Reports: `src/arduscope/lib/html_report.py` builds the single-log page and
   `src/arduscope/lib/comparison_report.py` the multi-log page. Only values
   that are byte-identical across report surfaces move to
   `src/arduscope/lib/theme.py`; per-surface palettes stay local.
7. Threshold values stay in `src/arduscope/lib/thresholds.py`. Their
   machine-readable levels and citations live in
   `src/arduscope/lib/threshold_sources.py`, with one entry per field.
   `tools/check_thresholds.py` checks that map and the generated convergence
   table; reports never invent a threshold.
8. The optional agent is `src/arduscope/agent/`. It reads `PipelineResult`
   through read-only tools and cannot change a status, score, action level or
   suggestion.
9. A failure is never swallowed: the pipeline records an `AnalysisIssue`, the
   report shows a failed card and marks the run incomplete, and the fleet
   archive refuses an incomplete run. `ReportSession` does the same for a
   diagnosis failure and reports an archive error.

## Extending

- **New analyzer**: add a file in `src/arduscope/analyzers/`, subclass
  `BaseAnalyzer`, set `registry_key`, `required_messages` and `sort_order`, and
  implement `analyze()`. It is registered in the CLI, web and reports
  automatically. Add thresholds and their `ThresholdSource` entries, regenerate
  the convergence table, and add a row in `docs/suggestion_evidence.md` if the
  analyzer suggests a parameter.
- **New page**: add a pure builder to `src/arduscope/lib/web_pages.py` and a
  thin route in `web_app.py`. Keep request/state reads in the route, not the
  builder.
- **New entry point**: import `arduscope.lib.report_session` (or
  `arduscope.lib.pipeline` for a metrics-only tool), then add it to
  `pyproject.toml` (`py-modules`, `project.scripts`) and to
  `build_windows_package.py` (`INCLUDE_FILES`, `NUITKA_ENTRY_POINTS`).
  `tests/tools/test_packaging.py` checks all three.
- **New test**: `tests/` mirrors the source area (`analyzers/`, `lib/`,
  `web/`, `agent/`, `cli/`, `tools/`, `integration/`, `sitl/`); shared helpers
  stay at the root. Put the test in the folder of the code it exercises.

## Known debt

- `web_app.py` still holds the upload, settings and security routes. State and
  page rendering now have boundaries, so extracting those routes is deferred
  until it helps an independent change.
- `src/arduscope/lib/pipeline.py` is the orchestration layer and
  `ReportSession` is the application boundary; both intentionally remain in
  `lib`. Their separation is enforced by the call graph and dependency tests,
  not by new `core`, `analysis`, `report` or `archive` directories. The
  function-local analyzer imports are the visible cycle-breaking exception.
- `src/arduscope/analyzers/notch_tuning.py` and
  `src/arduscope/lib/html_report.py` remain large because their domains are.
  Check the layer before adding to them; split them only for an independent
  maintenance reason, not to finish a directory plan.

## Verify

```
python3 tools/lint_project.py       # locales, contracts, layering
python3 tools/check_analyzers.py    # analyzer contract and evidence source
python3 tools/check_thresholds.py   # threshold fields, sources and table
python3 tools/check_suggestions.py  # parameter evidence registry
python3 tools/check_i18n.py         # user-facing strings
python3 tools/ci_local.py           # docs and tests gate
```

`tools/release_gate.py` adds snapshots, SITL and the corpora. It is a release
gate, not a per-change gate, and needs the private logs.
