# Pull request

## What changed

<!-- One or two sentences. Link the task or issue. -->

## Checks

- [ ] Targeted tests for the changed code, or `python3 -m pytest tests/ -q`
- [ ] `python3 tools/check_i18n.py`
- [ ] A blind review of the diff has run (maintainer task), or the reviewer is
      named below (external PR)
- [ ] The gate in [CONTRIBUTING.md](../CONTRIBUTING.md#which-gates-a-change-has-to-pass)
      was run for what this changes; external PRs say what ran locally and
      what the maintainer must run
- [ ] A new or changed parameter suggestion has a source, a flight or waiver,
      and a row in `docs/suggestion_evidence.md`

## Reviewer

Maintainer tasks: the blind review is part of the workflow. External PRs: name
the reviewer here; the maintainer arranges it and runs the private snapshot,
corpus and Windows checks.

## Rules

- Real flight logs stay private: no log file, path, serial or aircraft
  identifier goes in this pull request.
- API keys are never written down, in code or in a report.
- `POSITIONING.md` and `POSITIONING_zh.md` change only at the maintainer's
  explicit word, both languages together.
