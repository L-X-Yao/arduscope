# Contributing to ArduScope

Feature work ended in November 2026, with one exception: the online analysis
agent, reopened on 2026-11-12 (see [POSITIONING.md](POSITIONING.md)). Outside
it, the maintainer fixes defects and adds no features. A pull request goes
through the gates under "Which gates a change has to pass". What follows
describes how the code is put together, for a fix and for anyone who extends a
copy of their own.

## Adding a new analyzer

1. Create a new file in `analyzers/`, e.g. `analyzers/my_check.py`.
2. Subclass `BaseAnalyzer` from `analyzers/__init__.py`.
3. Set three class attributes:
   - `registry_key` -- unique string identifier (used by `--skip`/`--only`).
   - `required_messages` -- list of ArduPilot message types the analyzer needs.
   - `sort_order` -- integer controlling report position (lower = earlier, default 100).
4. Implement `analyze()` and return an `AnalysisResult` via `self.make_result()`.
5. No registration boilerplate is needed. `discover_analyzers()` auto-discovers
   every `BaseAnalyzer` subclass with a non-empty `registry_key` in the
   `analyzers/` package at import time.

Minimal skeleton:

```python
from analyzers import BaseAnalyzer, AnalysisResult
from lib.i18n import t

class MyCheckAnalyzer(BaseAnalyzer):
    registry_key = "my_check"
    required_messages = ["MSG_TYPE"]
    sort_order = 150

    def analyze(self) -> AnalysisResult:
        # ... analysis logic ...
        return self.make_result(
            title=t("my_check.title"),
            status="good",
            summary=t("my_check.good"),
            evidence_source="METHODIC section / ArduPilot source / derivation",
        )
```

## Adding param_suggestions

Each suggestion is a dict with four keys:

```python
{"param": "NAME", "current": val, "suggested": val, "reason": t("key")}
```

Pass the list to `make_result(param_suggestions=[...])` and set
`action_level="apply_param"` whenever suggestions exist. The report
generator exports these to a `.param` file that Mission Planner can
import directly.

That file may get loaded on an aircraft, so a suggestion needs more than a
threshold does:

1. A source for the value: METHODIC, the ArduPilot wiki or its source. A rule
   derived from them counts. One that reads well does not.
2. A flight. `tools/closed_loop.py` flies a scenario with the suggestion applied
   and compares the logs. SITL can rule a suggestion out, it cannot prove one.
3. A row in `docs/suggestion_evidence.md`, per rule and not per parameter.
   `tools/check_suggestions.py` fails in CI until there is one.

Without a flight, keep it as a pointer in the report text and name no value.
Shipping it anyway takes a waiver in `tools/check_suggestions.py`, with the
reason. Read the Removed table in `docs/suggestion_evidence.md` before adding
back anything that looks missing.

## Adding thresholds

Add a new field to the `Thresholds` dataclass in `lib/thresholds.py`.

- Name the field as `<category>_<metric>_<level>` (e.g. `gps_hdop_warn`).
- Cite the source in an inline comment using the prefix convention:
  - `A:` -- authoritative (METHODIC doc, ArduPilot wiki, ArduPilot source code).
  - `A(soft):` -- an ArduPilot value used as a proxy, outside its own purpose.
  - `D:` -- derived from an authoritative source with stated reasoning.
  - `E:` -- engineering estimate (legacy/secondary, avoid for new thresholds).
- Every threshold must have a corresponding entry in
  `docs/convergence_criteria.md`.

Example:

```python
gps_hdop_warn: float = 1.5       # D: quality target, stricter than pre-arm 2.0
```

## Adding cross-correlation rules

Add a new `_Rule` instance to the list returned by `_build_cross_rules()`
in `lib/ai_diagnosis.py`:

```python
_Rule(
    name="unique_rule_name",
    conditions={
        "analyzer_key_a": {"warning", "critical"},
        "analyzer_key_b": {"warning", "critical"},
    },
    title=t("ai.cross.unique_rule_name.title"),
    diagnosis=t("ai.cross.unique_rule_name.diagnosis"),
    recommendation=t("ai.cross.unique_rule_name.recommendation"),
    priority="high",  # "high" | "medium" | "low"
),
```

The rule fires when ALL listed analyzers report one of the specified
statuses simultaneously.

## i18n

All user-facing strings go through `lib/i18n.py`. When adding a new key:

1. Add the key to **both** `locales/zh.json` and `locales/en.json`
   simultaneously. Missing keys in either locale will fall back to
   Chinese, which breaks the English experience.
2. Use `t("key", **kwargs)` in source code.
3. Format placeholders use Python `str.format()` syntax:
   `"max {value} m/s"` with `t("key", value=12.3)`.

## Testing

- Use the `SyntheticReader` pattern from `tests/test_synthetic_coverage.py`
  for unit tests. `SyntheticReader` is a duck-typed `LogReader` replacement
  that accepts pre-built numpy arrays -- no `.BIN` file needed.
- `tests/binlog.py` writes real-format `.BIN` files, which is how a test reaches
  the parser and the pipeline.
- Run the full suite:

```
python3 -m pytest tests/ -q
```

Real-log tests skip without the logs, which are not in the repository, and SITL
tests skip without `tests/sitl/logs`. A skip is not a pass: CI runs under half
of the suite for this reason.

The logs are the maintainer's. The tests ask for them by label, and a machine
that has them names the files in `local_paths.json`, which git ignores:
`tools/local_paths.py` describes it. With logs of your own you can still see
what a change moves: scan them before and after with
`python3 tools/baseline_run.py /path/to/logs -o before.json`, then compare the
two files with `python3 tools/baseline_diff.py before.json after.json`.

A regression test asserts its precondition before its invariant. With
`if result.param_suggestions:` around the assertion, the test passes without
running the code it was written for as soon as an input drifts under a
threshold. Put inputs well past a threshold, not on it.

Update snapshots with `tests/test_baseline_snapshots.py --update` only after
reading what changed.

## Which gates a change has to pass

CI checks names, formats and contracts. It has no flight logs and no SITL, so it
cannot say whether a diagnosis is right, and the defects that shipped in 0.9.x
passed all of it. Each row includes the rows above it.

| Change | Gate |
|---|---|
| Docs and tests | `python3 tools/ci_local.py` |
| Analyzer logic, a threshold | `python3 tools/release_gate.py --steps snapshots,corpus`, and read the changes. CI runs neither |
| A parameter suggestion | A source, a flight and a registry row, see Adding param_suggestions |
| Log reader, pipeline, web server, Windows package | `python3 tools/win_package_check.py --zip <zip> --version <x.y.z>`, on a build from `python3.14 build_windows_package.py`. The build compiles with a Python of the bundled minor version, 3.14 |
| A release | See Releasing |

The snapshots and the corpora are built on logs that are not public, so those
two steps run on the maintainer's machine. A pull request that changes analyzer
logic or a threshold should say what it expects to move, and is merged after the
maintainer has run them and read the changes.

`tools/release_gate.py` holds a shared `flock` on the SITL binaries while flying,
so a rebuild waits for it. Anything else that flies should take it too.

## Evidence for a change

- Check a claim against the code before repeating it. A finding marked closed is
  a claim: 49 of the 102 findings recorded as closed in July 2026 were still
  open in November.
- Read the corpus changes, not just the count. A clean status diff is not
  enough: the FFT, compass and D gain defects showed up in a scan and in no test.
- Before shipping a rule, scan the corpus for how often it fires on a healthy
  aircraft. 278 of the 296 axes the old D rule flagged were under METHODIC's limit.
- Before tightening a rule, test the true positive. The first D gate lost a real
  oscillation: that aircraft shook on the ground and never took off.
- A ledger finding such as `docs/rescan-2026-11-05.md` is closed by a commit that
  names its ID in the body.

## Releasing

1. Write the notes under `## [Unreleased]` in `CHANGELOG.md` as changes land.
2. Run `python3 tools/release_gate.py` on the commit to release. It takes about
   half an hour, and needs the logs and SITL. If corpus results moved it stops
   at review: read the diff, then run it with `--accept "why they are right"`.
   Run it with the library versions pinned in `win64-requirements.txt`, the
   ones the Windows package ships: its record lists the Python and library
   versions it ran on, and it names any library off those pins.
3. Run `python3 tools/release.py patch`. It refuses a commit without a pass,
   dates the notes, tags and pushes. The tag starts `release.yml`, which
   creates the release as a draft.
4. Run `python3 tools/win_package_check.py --release vX.Y.Z` on the draft's
   package. Linux will delete an open file, so only this finds a leaked handle.
5. When it passes, publish the draft: `gh release edit vX.Y.Z --draft=false`.
   A published release is immutable, a repository setting: its files and its
   tag cannot change, so a defect found after that goes out as a new version.

The Windows package carries the Python named by `DEFAULT_PYTHON` in
`build_windows_package.py`, and the libraries pinned with their hashes in
`win64-requirements.txt`. A rebuild of a tag ships the same files.

Distribution is GitHub Release only, never PyPI.

## Commit messages

Format: `<type>(<scope>): <subject>`

**type** (required, lowercase):

| Type | When to use |
|------|------------|
| `feat` | New analyzer, new diagnostic capability, new CLI flag |
| `fix` | Bug fix (wrong diagnosis, crash, incorrect output) |
| `refactor` | Code restructuring without behavior change |
| `docs` | Documentation only (README, CONTRIBUTING, CLAUDE.md) |
| `test` | Test additions or fixes |
| `chore` | Build, CI, dependencies, tooling, linting |
| `i18n` | Locale strings, translation updates |

**scope** (optional, in parentheses): the primary module affected.
Examples: `feat(vibration)`, `fix(pid_tuning)`, `i18n(locales)`, `chore(ruff)`.

**subject**: imperative mood, lowercase, no period, under 72 chars.

**Body** (optional): blank line after subject, wrap at 80 chars. Explain *why*, not *what*.

**No trailers**: no `Co-Authored-By`, no `Signed-off-by`, no AI attribution.

`tools/commit-msg-hook.sh` checks all of this. Install it with
`cp tools/commit-msg-hook.sh .git/hooks/commit-msg && chmod +x .git/hooks/commit-msg`.

Examples:
```
feat(gyro_saturation): add gyro saturation analyzer
fix(temperature): rename loop variable to avoid i18n shadow
refactor(html_report): extract threshold line drawing to helper
docs: update analyzer count 21 → 26
chore(ci): add GitHub Actions test workflow
i18n(locales): add servo_dither locale keys
```

Bad examples:
```
update stuff                          # no type, vague
feat: Add new feature.                # uppercase, period
FEAT(motor): fixed bug                # uppercase type, wrong type
feat(motor): add motor saturation detection and also fix the balance
  calculation and update the threshold  # too long, multiple changes
```

## Code style

- Linter: `ruff`, configured in `pyproject.toml`.
- Line length: 99 characters.
- Do not write comments that explain what code does. Code should be
  self-explanatory; comments are for *why* (threshold source citations,
  non-obvious design decisions, caveats).
- Run `python3 tools/lint_project.py` before committing to catch
  cross-file consistency issues (missing locale keys, count drift, etc.).
