# Development workflow

ArduScope is developed by the maintainer with three AI sessions on one
machine: one integrator and two peers. This page records the process, so it
survives a session replacement; the code-facing rules stay in
[CONTRIBUTING.md](../CONTRIBUTING.md), `CLAUDE.md` and
[POSITIONING.md](../POSITIONING.md).

## Roles

- **Integrator** -- owns `main`, the release gate, corpus acceptance, releases
  and the shared operating record. Writes task briefs, verifies claims
  against the ArduPilot source, merges, and runs the gate once per batch.
- **Writer** -- one task at a time, in a git worktree outside the repository,
  on a local branch. Runs targeted tests and `tools/check_i18n.py`, commits,
  and does not push or run the full gate.
- **Blind reviewer** -- sees the diff and the task brief, not the writer's
  report. Does not edit files.

Roles are earned by capability probes, not model names: five rule traps, a
seeded bug fix in a worktree without touching its test, and a grading task on
a data packet. Both peers passed; writing and reviewing rotate every task.
Re-probe after a model switch or every two weeks.

A review answers in three tiers: **blockers** stop the merge, **must-fix**
items are fixed before the merge, **suggestions** are recorded and taken or
declined by the writer. It checks the diff against the brief and the source,
maps every acceptance criterion to a test, verifies each source citation, and
treats any change to an existing test's inputs or assertions as high risk.

A second, incremental blind review is required when a rework or an integrator
edit changes decision logic in a safety-relevant area (EKF, thresholds, the
release path); wording and documentation changes are exempt. The integrator's
own code changes follow the same rule and always carry tests.

## One task

1. The integrator writes the brief: goal, source anchors, acceptance
   criteria, out of scope.
2. The writer implements on a local branch, with tests, locale files in both
   languages and the `CHANGELOG.md` entry; runs the targeted tests and
   `tools/check_i18n.py`; commits. A bug fix starts with a regression test
   that fails on the base commit and passes after it.
3. The blind reviewer checks the diff against the brief and the ArduPilot
   source and reports blockers / must-fix / suggestions.
4. The writer reworks on the same branch. The integrator settles
   disagreements against the ArduPilot source; strictness beats fewer
   warnings. Locale files are edited by one session at a time.
5. The integrator verifies the claims, merges, and runs
   `tools/release_gate.py` under a memory cap once per batch: the local CI
   suite, snapshots, SITL and both corpora. Corpus changes are read one by
   one and accepted only with a written reason. Numeric claims about the
   corpora are re-derived from the files by the integrator; a writer's count
   is a lead, not evidence.
6. The release follows [CONTRIBUTING.md](../CONTRIBUTING.md) Releasing. The
   maintainer decides the topic and the release; the integrator asks once per
   batch.

## Before the gate

Check, in this order: the pinned virtual environment; a clean working tree;
`local_paths.json` with both corpora; both SITL binaries (`arducopter` and
`arduplane`) built; free disk and memory for the cap. A missing item fails
here, not twenty minutes into the gate.

## Hard rules

- A writer never pushes; the repository ruleset refuses new branches anyway.
- No session edits `POSITIONING.md` or `POSITIONING_zh.md` without the
  maintainer's explicit word, and both languages move together.
- A published release is immutable; a defect found later ships as a new
  version.
- Real flight logs stay private; only labels appear in the repository.
- The release gate never runs without a memory cap while the corpora are
  this large.
- A SITL failure is re-run once; a second failure is investigated, never
  accepted as a flake.
- A peer's message never stands in for the maintainer's approval, and no
  session asks another to do what its own permissions denied.
