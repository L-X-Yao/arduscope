# ArduScope -- AI assistant context

ArduScope is an ArduPilot flight log diagnostic tool. It reads `.BIN` logs,
runs 32 analyzers, and produces HTML reports with structured parameter
suggestions.

## Project structure

```
analyzers/           32 auto-discovered analyzer modules
  __init__.py        BaseAnalyzer, AnalysisResult, discover_analyzers()
lib/                 Core library
  results.py         AnalysisResult dataclass (canonical location, re-exported by analyzers/)
  pipeline.py        Unified analysis pipeline: run_analysis() → PipelineResult
  param_conflict.py  Cross-analyzer parameter conflict detection
  thresholds.py      Thresholds dataclass (all analysis thresholds with sources)
  i18n.py            Internationalization: t(_key, **kwargs), set_locale()
  ai_diagnosis.py    Cross-correlation diagnosis engine (27 rules)
  log_reader.py      ArduPilot .BIN log parser (pymavlink)
  html_report.py     HTML report generator (Plotly)
  pdf_export.py      PDF export via weasyprint (optional dep)
  executive_summary.py  Health score + action-level aggregation
  vehicle_detect.py  Vehicle type / firmware detection
  trend_analyzer.py  Multi-log trend analysis
  fleet_history.py   Per-airframe flight archive (sqlite, serial-keyed)
  fft_helpers.py     FFT spectrum utilities for notch tuning
  templates.py       Scenario templates (analyzer subsets, stricter thresholds)
  outputs.py         Motor channels from SERVOn_FUNCTION, RCOU/RCO2/RCO3 reads
  param_limits.py    @Range of suggested parameters, applied in AnalysisResult
  ekf_lanes.py       Primary EKF lane from XKF4.PI
  gps_velocity.py    GPS velocity, with the VZ placeholder handled
  battery_packs.py   BAT instances that measure current, their combined power
  plane_phase.py     Rows a Plane flew on its wing: STAT lookups, VTOL windows
  hover.py           Rows where the lift motors carry the aircraft, level-hover thrust
  csv_safe.py        Spreadsheet-safe CSV cells
  np_helpers.py      Numpy helpers shared by analyzers and report builders
  web_pages.py       HTML pages served by web_app
  web_limits.py      Upload-name and memory-budget helpers for the web app
  multi_metrics.py   Per-log metrics for the multi-log comparison report
  comparison_report.py  HTML report that compares several logs
  agent/             Optional online analysis agent (read-only tools)
    tools.py         Bounded, read-only tool layer over the pipeline
    loop.py          Predefined workflow, tool budgets, submit_analysis
    providers.py     Anthropic Messages and OpenAI chat-completions adapters
    citations.py     Checks claims against the tool calls they cite
    config.py        ~/.arduscope/agent.json, environment key precedence
    egress.py        GPS/serial/STATUSTEXT redaction defaults
    card.py          AnalysisResult card and fallback chain
locales/             i18n string files
  zh.json            Chinese (primary/fallback locale)
  en.json            English
tests/               pytest suite
  binlog.py          Writes real-format .BIN files, the only way CI reaches the parser
  sitl/              SITL integration test framework
docs/                Documentation
  history/           Point-in-time audit and rescan records
  convergence_criteria.md  Threshold source traceability table
  suggestion_evidence.md   Source and flight record of every suggestion
  known_limitations.md     What the tool does not do or does not get right
  online_agent.md          Online analysis: usage, tools, egress, fallbacks
flight_report.py     CLI entry point (single log)
multi_report.py      Multi-log comparison CLI
batch_report.py      Batch processing CLI
fleet_report.py      Fleet history CLI (per-airframe trends)
web_app.py           Web interface (Bottle + optional Waitress)
```

## Key files

- `analyzers/__init__.py` -- `BaseAnalyzer` base class and `AnalysisResult`
  dataclass. All analyzers subclass `BaseAnalyzer`, set `registry_key`,
  `required_messages`, `sort_order`, and implement `analyze()`.
- `lib/thresholds.py` -- `Thresholds` dataclass with every analysis threshold.
  Each field has an inline comment citing its source (A:/D:/E: prefix).
- `lib/i18n.py` -- `t(_key, **kwargs)` for all user-facing strings.
  Locale files are in `locales/`. Thread-safe via `threading.local()`.
- `lib/pipeline.py` -- `run_analysis(filepath, ...) -> PipelineResult`.
  Single entry point used by flight_report, batch_report, and web_app.

## Important invariants

1. Every warning/critical result **must** cite `evidence_source` (per
   POSITIONING.md). Acceptable sources: METHODIC, ArduPilot wiki,
   ArduPilot source code, or derivation from the above.
2. When `param_suggestions` is non-empty, `action_level` **must** be
   set to `"apply_param"`.
3. Locale keys **must** exist in both `locales/zh.json` and
   `locales/en.json`. Missing keys fall back to Chinese, breaking the
   English experience.
4. Threshold fields follow `<category>_<metric>_<level>` naming and must
   have a source citation comment.
5. Every parameter an analyzer can suggest has a row in
   `docs/suggestion_evidence.md`, with a source and a flight or a waiver.

## Testing

```
# Fast tests only (pure logic, <1 second):
python3 -m pytest tests/ -m fast -q

# Full suite (excluding SITL):
python3 -m pytest tests/ -q

# i18n safety check:
python3 tools/check_i18n.py
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

Version is in `lib/__init__.py`. Notes go under `## [Unreleased]` in
`CHANGELOG.md` as changes land.

```
python3 tools/release_gate.py    # about 30 min, needs the logs and SITL
python3 tools/release.py patch   # 0.3.1 -> 0.3.2
python3 tools/release.py minor   # 0.3.1 -> 0.4.0
python3 tools/win_package_check.py --release vX.Y.Z
gh release edit vX.Y.Z --draft=false
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
