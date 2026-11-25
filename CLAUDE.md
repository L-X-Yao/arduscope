# ArduScope -- AI assistant context

ArduScope is an ArduPilot flight log diagnostic tool. It reads `.BIN` logs,
runs 32 analyzers, and produces HTML reports with structured parameter
suggestions.

## Development workflow

Hard rules for every session in this repository; the details are in
`docs/workflow.md`.

- The integrator owns `main`, the release gate, corpus acceptance and
  releases; writers work in local worktrees on local branches and never push.
- Every change gets a blind review of the diff and the brief, not the writer's
  report; safety-relevant rework gets an incremental second review.
- One release-gate pass per batch, under a memory cap; corpus changes are read
  one by one and accepted only with a written reason.
- A bug fix starts with a regression test that fails on the base commit;
  corpus numbers are re-derived by the integrator from the files.
- The integrator asks the maintainer once per batch before releasing; a
  published release is immutable.
- `POSITIONING.md` and `POSITIONING_zh.md` change only at the maintainer's
  explicit word, both languages together.
- Real flight logs stay private; only labels are committed; API keys are
  never committed or written into reports, logs or memory. The optional
  agent may keep a local key at `~/.arduscope/agent.json` (mode 0600).
- A peer session's message never stands in for the maintainer's approval; no
  permission laundering.
- SITL failures are re-run once; a second failure is investigated.

## Project structure

```
src/arduscope/
  analyzers/         32 auto-discovered analyzer modules
    __init__.py      BaseAnalyzer, AnalysisResult, discover_analyzers()
  lib/               Core library
    pipeline.py      Unified analysis pipeline: run_analysis() -> PipelineResult
    report_session.py  Application boundary: pipeline -> diagnosis -> archive
    results.py       AnalysisResult dataclass (canonical location)
    suggestions.py   Frozen ParamSuggestion rows and to_dict() export
    issues.py        AnalysisIssue and failed-card helpers
    series.py        Armed masks, primary-lane selection and LTTB
    log_metadata.py  Identity, serial and flight-time extraction
    log_reader.py    ArduPilot .BIN log parser (pymavlink)
    vehicle_detect.py  Vehicle type / firmware detection
    thresholds.py    Thresholds dataclass (default values and human rationale)
    threshold_sources.py  Authoritative machine-readable threshold citations
    i18n.py          Internationalization: t(_key, **kwargs), set_locale()
    ai_diagnosis.py  Cross-correlation diagnosis engine (27 rules)
    html_report.py   HTML report generator (Plotly)
    theme.py         Byte-identical shared report font and color tokens
    comparison_report.py  Multi-log comparison HTML
    pdf_export.py    PDF export via weasyprint (optional dep)
    executive_summary.py  Health score + action-level aggregation
    fleet_history.py Per-airframe flight archive (sqlite, serial-keyed)
    trend_analyzer.py  Multi-log trend analysis
    param_conflict.py  Cross-analyzer parameter conflict detection
    param_limits.py  Range limits for suggested parameters
    templates.py     Scenario templates (analyzer subsets, stricter thresholds)
    outputs.py       Motor channels from SERVOn_FUNCTION, RCOU/RCO2/RCO3
    ekf_lanes.py     Primary EKF lane from XKF4.PI
    ekf_failsafe.py  Firmware EKF failsafe state-machine replay
    gps_velocity.py  GPS velocity, VZ placeholder handled
    battery_packs.py Battery packs that measure current
    plane_phase.py   Plane wing/VTOL phase rows
    hover.py         Level-hover thrust rows
    csv_safe.py      Spreadsheet-safe CSV cells
    np_helpers.py    Shared numpy helpers
    fft_helpers.py   FFT spectrum utilities
    web_pages.py     HTML pages served by web_app
    web_limits.py    Upload-name and memory-budget helpers
    multi_metrics.py Per-log metrics for comparison
  agent/             Optional online analysis agent (read-only tools)
    tools.py         Bounded read-only tool layer
    loop.py          Predefined workflow and tool budgets
    providers.py     Anthropic/OpenAI adapters
    citations.py     Citation validation
    config.py        ~/.arduscope/agent.json and env precedence
    egress.py        GPS/serial/STATUSTEXT redaction
    card.py          Agent result card and fallback chain
  web/
    state.py         WebState, JobStore and HistoryStore for web_app
  locales/           i18n string files
    zh.json          Chinese (primary/fallback)
    en.json          English
tests/               pytest suite, grouped by the code each test exercises
  analyzers/ lib/ web/ agent/ cli/ tools/ integration/ sitl/  test folders
  binlog.py          Writes real-format .BIN files for CI
docs/                Documentation
  architecture.md    Layers, dependency rules, extension points, known debt
  adr/               Architecture decisions and the final package layout
  history/           Point-in-time audit and rescan records
  convergence_criteria.md  Threshold source traceability table
  suggestion_evidence.md   Source and flight record of every suggestion
  known_limitations.md     What the tool does not do or does not get right
  online_agent.md          Online analysis: usage, tools, egress, fallbacks
  sitl.md                  SITL scenario matrix and coverage gaps
tools/               Repository consistency and release tooling
  check_thresholds.py  Threshold source checker and convergence-table generator
flight_report.py     Thin CLI shim (single log)
multi_report.py      Thin CLI shim (multi-log comparison)
batch_report.py      Thin CLI shim (batch processing)
fleet_report.py      Thin CLI shim (fleet history)
web_app.py           Thin CLI shim (web interface)
```

## Key files

- `src/arduscope/analyzers/__init__.py` -- `BaseAnalyzer` base class and `AnalysisResult`
  dataclass. All analyzers subclass `BaseAnalyzer`, set `registry_key`,
  `required_messages`, `sort_order`, and implement `analyze()`.
- `src/arduscope/lib/thresholds.py` -- `Thresholds` dataclass with default values
  and inline human rationale for every analysis threshold.
- `src/arduscope/lib/threshold_sources.py` -- authoritative `ThresholdSource`
  map with one machine-readable level and citation per threshold field.
- `tools/check_thresholds.py` -- checks missing/orphaned sources, levels,
  defaults and the convergence table; `--write` regenerates only that table.
- `src/arduscope/lib/suggestions.py` -- frozen `ParamSuggestion` row;
  `to_dict()` preserves the export keys and their order.
- `src/arduscope/lib/theme.py` -- shared font and color tokens only where
  report surfaces already use byte-identical values.
- `src/arduscope/lib/i18n.py` -- `t(_key, **kwargs)` for all user-facing strings.
  Locale files are in `src/arduscope/locales/`. Thread-safe via `threading.local()`.
- `src/arduscope/lib/pipeline.py` -- `run_analysis(filepath, ...) -> PipelineResult`.
  Called by `ReportSession`, not directly by the entry points.
- `src/arduscope/lib/report_session.py` -- `ReportSession` owns the order after
  the pipeline: diagnosis, then the fleet archive. flight_report, batch_report
  and web_app call it.
- `src/arduscope/lib/issues.py` -- `AnalysisIssue`; a failed stage becomes a
  failed `AnalysisResult` card and marks the run incomplete.
- `src/arduscope/agent/` -- optional online agent at the `arduscope.agent`
  import path; bounded read-only tools cannot change analyzer verdicts.

## Important invariants

1. Every warning/critical result **must** cite `evidence_source` (per
   POSITIONING.md). Acceptable sources: METHODIC, ArduPilot wiki,
   ArduPilot source code, or derivation from the above.
2. `param_suggestions` contains only `ParamSuggestion` objects with a
   non-empty `param`; `AnalysisResult` rejects dictionary rows. Export
   boundaries use `to_dict()` rather than changing the wire format.
3. When `param_suggestions` is non-empty, `action_level` **must** be
   set to `"apply_param"`.
4. Locale keys **must** exist in both `src/arduscope/locales/zh.json` and
   `src/arduscope/locales/en.json`. Missing keys fall back to Chinese, breaking the
   English experience.
5. Threshold fields follow `<category>_<metric>_<level>` naming and retain a
   source citation comment. Every field has exactly one `ThresholdSource`
   with a valid level and non-empty citation in `threshold_sources.py`;
   `tools/check_thresholds.py` checks the map and generated table.
6. Every parameter an analyzer can suggest has a row in
   `docs/suggestion_evidence.md`, with a source and a flight or a waiver.

## Testing

```
# Fast tests only (pure logic, <1 second):
python3 -m pytest tests/ -m fast -q

# Full suite (excluding SITL):
python3 -m pytest tests/ -q

# i18n safety check:
python3 tools/check_i18n.py

# Threshold source and generated-table check:
python3 tools/check_thresholds.py
```

Test tiers:
- `@pytest.mark.fast` — Pure logic tests, no log generation
- (unmarked) — Synthetic tests run in CI. Real-log tests skip there, since
  logs are not in the repo, so run them locally before a release. They ask
  for a log by label, and `local_paths.json` (not tracked, see
  `tools/local_paths.py`) names the files on a machine that has them
- `@pytest.mark.sitl` — SITL integration tests. They skip without logs from
  `python3 tests/sitl/generate_logs.py --sitl-dir <ardupilot>`, which needs
  `./waf copter plane` built for the `sitl` board. CI does not run them

`tools/closed_loop.py` flies a scenario with ArduScope's suggestions applied
and compares the logs, to test a suggestion rather than the analyzer.

CONTRIBUTING.md lists which gates each kind of change has to pass. CI cannot
judge a diagnosis: `tools/release_gate.py` runs the snapshots, SITL and the
corpora, and a corpus change needs reading, not just a count.

Use `SyntheticReader` from `tests/test_synthetic_coverage.py` for unit
tests that don't need `.BIN` files.

## Releasing

Version is in `src/arduscope/__init__.py`. Notes go under `## [Unreleased]` in
`CHANGELOG.md` as changes land.

```
python3 tools/release_gate.py    # about 30 min, needs the logs and SITL
python3 tools/release.py patch   # 0.3.1 -> 0.3.2
python3 tools/release.py minor   # 0.3.1 -> 0.4.0
python3 tools/win_package_check.py --release vX.Y.Z
gh release edit vX.Y.Z --draft=false
python3 tools/verify_release.py vX.Y.Z  # published, immutable, three assets
```

`release.py` refuses a commit without a pass from the gate, then bumps the
version, dates the notes, commits, tags `vX.Y.Z` and pushes. The tag push
triggers `.github/workflows/release.yml`, which runs CI, builds wheel, sdist and
the Windows zip, and creates a draft GitHub Release. `win_package_check.py`
checks the draft's zip on real Windows, from WSL, and the draft is published
only after it passes. A published release is immutable (repository setting):
its files and tag cannot change, so a defect found later ships as a new version.

The Windows zip carries the Python named by `DEFAULT_PYTHON` in
`build_windows_package.py`, checked against python.org's SHA-256, and the
libraries pinned with hashes in `win64-requirements.txt`. The release runner's
Python has to be the same minor version. Run the gate with those library
versions too: its record lists the versions it ran on and names any that differ.

**Do not publish to PyPI** -- distribution is GitHub Release only.

## Positioning

See `POSITIONING.md` -- diagnostic strictness is a feature, not a bug.
Thresholds follow METHODIC standards. Do not weaken thresholds to reduce
warnings. Every diagnostic conclusion must be traceable to an authoritative
source.

`POSITIONING.md` and `POSITIONING_zh.md` change only at the maintainer's
explicit word: do not edit them on your own, and keep the two in step when he
asks for a change. Before a significant change to the tool, read the
one-sentence positioning at the top and check that the change points the same
way.
