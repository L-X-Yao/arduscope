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
             lib/pipeline.py  run_analysis()
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   analyzers/*    lib primitives   lib/agent/*
   (auto-found)   log_reader, results, thresholds,
                  i18n, vehicle_detect, ...
```

`multi_report.py` is the exception: it compares metrics across logs and uses
`lib/multi_metrics.py`, not the full analyzer pipeline. `fleet_report.py`
uses `lib/fleet_history.py`; both are entry points, not library layers.

## Rules

1. `analyzers/*` may import `lib`; `lib` must not import `analyzers`, except
   for the function-local `discover_analyzers` imports in `lib/pipeline.py`
   and `lib/fleet_history.py`. Those three imports break the import cycle and
   are allowlisted. `tools/lint_project.py` check 8 enforces this rule.
2. `lib` and `analyzers` must not import an entry module. `analyzers` must not
   import `lib/agent`. The same check enforces both.
3. Single-log analysis goes through `lib/pipeline.run_analysis()`. The batch,
   web and flight CLIs all call it. Do not add a second analyzer loop in an
   entry point.
4. `AnalysisResult` lives in `lib/results.py`; `analyzers/__init__.py`
   re-exports it for analyzer authors. Constants that both layers need go to
   `lib`, never to `analyzers`.
5. `web_app.py` owns mutable HTTP state: jobs, uploads, history, settings and
   fleet routes. Page rendering is pure and lives in `lib/web_pages.py`;
   upload-name and memory-budget helpers live in `lib/web_limits.py`.
6. Reports: `lib/html_report.py` builds the single-log page,
   `lib/comparison_report.py` the multi-log page. Both read thresholds from
   `lib/thresholds.py`; a report never invents a threshold.
7. The optional agent is `lib/agent/`. It reads `PipelineResult` through
   read-only tools and cannot change a status, score, action level or
   suggestion.

## Extending

- **New analyzer**: add a file in `analyzers/`, subclass `BaseAnalyzer`, set
  `registry_key`, `required_messages` and `sort_order`, implement `analyze()`.
  It is registered in the CLI, web and reports automatically. Add its
  thresholds to `lib/thresholds.py` with a source, and a row in
  `docs/suggestion_evidence.md` if it suggests a parameter.
- **New page**: add a pure builder to `lib/web_pages.py`, a thin route in
  `web_app.py`. Keep request/state reads in the route, not the builder.
- **New entry point**: import `lib.pipeline`, then add it to
  `pyproject.toml` (`py-modules`, `project.scripts`) and to
  `build_windows_package.py` (`INCLUDE_FILES`, `NUITKA_ENTRY_POINTS`).
  `tests/test_packaging.py` checks all three.

## Known debt

- Top-level package names are generic: `lib`, `analyzers`, `locales` install
  at the root of site-packages. Give ArduScope its own virtual environment,
  or use the Windows package. S9-03/D7 deferred a rename; it is a 2-3 day
  change across imports, entry points, build scripts and docs.
- `lib/pipeline.py` is both the orchestration layer and part of `lib`; the
  analyzer imports are the visible exception.
- `web_app.py` still holds the stateful HTTP core; presentation was moved out,
  not the job/upload/history state.
- `tests/` is flat. With long-term iteration, grouping it by source area is
  the next maintenance task.
- `analyzers/notch_tuning.py` and `lib/html_report.py` are large because their
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
