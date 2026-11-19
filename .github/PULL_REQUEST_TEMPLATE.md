# Pull request

## What changed

<!-- One or two sentences. Link the task or issue. -->

## Checks

- [ ] Targeted tests for the changed code, or `python3 -m pytest tests/ -q`
- [ ] `python3 tools/check_i18n.py`
- [ ] A blind review of the diff has run
- [ ] The release gate has run for this batch, if an analyzer, a locale file,
      a snapshot or the corpus changed

## Rules

- Real flight logs stay private: no log file, path, serial or aircraft
  identifier goes in this pull request.
- API keys are never written down, in code or in a report.
- `POSITIONING.md` and `POSITIONING_zh.md` change only at the maintainer's
  explicit word, both languages together.
