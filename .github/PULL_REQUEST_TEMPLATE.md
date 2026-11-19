# Pull request

## What changed

<!-- One or two sentences. Link the task or issue. -->

## Checks

- [ ] Targeted tests for the changed code, or `python3 -m pytest tests/ -q`
- [ ] `python3 tools/check_i18n.py`
- [ ] A blind review of the diff has run
- [ ] The gate in [CONTRIBUTING.md](../CONTRIBUTING.md#which-gates-a-change-has-to-pass)
      was run for what this changes: `ci_local` for docs and tests, the
      snapshots and corpora for analyzer logic or a threshold,
      `win_package_check` for the log reader, pipeline, web server or Windows
      package
- [ ] A new or changed parameter suggestion has a source, a flight or waiver,
      and a row in `docs/suggestion_evidence.md`

## Rules

- Real flight logs stay private: no log file, path, serial or aircraft
  identifier goes in this pull request.
- API keys are never written down, in code or in a report.
- `POSITIONING.md` and `POSITIONING_zh.md` change only at the maintainer's
  explicit word, both languages together.
