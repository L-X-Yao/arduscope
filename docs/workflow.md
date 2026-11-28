# Development workflow

ArduScope returned to feature development on 2026-11-24; the 2026-11-20
maintenance freeze is lifted. This page records the process for feature work
and for fix-only batches. The code-facing rules stay in
[CONTRIBUTING.md](../CONTRIBUTING.md), `CLAUDE.md` and
[POSITIONING.md](../POSITIONING.md).

GitHub Actions runs the CI suite weekly, `security.yml` audits the dependency
pins weekly, and `release-verify.yml` checks a published release's record and
assets.

## Modes

### Maintenance (when the maintainer calls a fix-only batch)

- Three sessions by default: the integrator and two peers. One peer writes when
  assigned, the other is the blind reviewer or does an independent audit; the
  integrator writes otherwise. A session never reviews its own code change. The
  third session is omitted only when the batch has no independent work for it.
- No scheduled capability probes while idle. Re-probe after a model switch.
- The change tiers below set the review and gate level.

| Change | Writer | Review | Gate |
|---|---|---|---|
| Docs, CI, comments | Integrator | Integrator self-check | `tools/ci_local.py` |
| Non-safety code | Integrator or peer | The other session | `ci_local` and targeted tests; the full gate at the release batch |
| Safety code | Integrator or peer | The other session; an incremental review after a rework | `--steps snapshots,corpus` on the batch; the full gate at release |
| Release | Integrator | None | Full gate and the Windows check |

Safety code means EKF, thresholds, failsafe, the log reader, the pipeline and
the release path. Policy documents (`POSITIONING*.md`, this file,
`CONTRIBUTING.md`) still need the maintainer's word, not a review tier. Docs
and CI changes may be made directly by the integrator, without a brief or a
worktree; they still run `ci_local`.

### Feature development (current)

- Three sessions: the integrator and two peers.
- Capability probes, role rotation and the full task lifecycle below apply.
- The change tiers above still set the review and gate level.

## Roles

- **Integrator** -- owns `main`, the release gate, corpus acceptance, releases
  and the shared operating record. Writes task briefs, verifies claims
  against the ArduPilot source, merges, and runs the gate once per batch.
- **Writer** -- one task at a time, in a git worktree outside the repository,
  on a local branch. Runs targeted tests and `tools/check_i18n.py`, commits,
  and does not push or run the full gate. The integrator may take this role for
  docs and CI changes.
- **Blind reviewer** -- sees the diff and the task brief, not the writer's
  report. Does not edit files.

Roles are earned by capability probes, not model names: five rule traps, a
seeded bug fix in a worktree without touching its test, and a grading task on
a data packet. Both peers passed on 2026-11-16; writing and reviewing rotate
every task. Re-probe after a model switch. Probes are not scheduled during a
fix-only batch.

A review answers in three tiers: **blockers** stop the merge, **must-fix**
items are fixed before the merge, **suggestions** are recorded and taken or
declined by the writer. It checks the diff against the brief and the source,
maps every acceptance criterion to a test, verifies each source citation, and
treats any change to an existing test's inputs or assertions as high risk.

A second, incremental blind review is required when a rework or an integrator
edit changes decision logic in a safety-relevant area (EKF, thresholds, the
release path); wording and documentation changes are exempt. The integrator's
own code changes follow the same rule and always carry tests.

## External pull requests

An external contributor runs `tools/ci_local.py` and the tests for the changed
code. The maintainer acts as integrator, arranges the blind review, and runs
the snapshot, corpus and Windows checks on the private logs. The PR template
records who reviewed the diff. The logs and gate corpora are not shared.

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
5. The integrator verifies the claims, merges, and runs the gate for the
   change tier: `ci_local` for docs and CI; `--steps snapshots,corpus` for
   safety code; the full gate at the release batch. Corpus changes are read
   one by one and accepted only with a written reason. Numeric claims about
   the corpora are re-derived from the files by the integrator; a writer's
   count is a lead, not evidence.
6. The release follows [CONTRIBUTING.md](../CONTRIBUTING.md) Releasing. The
   maintainer decides the topic and the release; the integrator asks once per
   batch. Push the gated commit to `origin/main` first: `release.py` tags only
   a `main` level with `origin/main`, and needs a terminal or `--yes`.

## Memory cap

The release gate runs under a finite cgroup memory limit. On this machine:

```
systemd-run --user --scope -p MemoryMax=12G -p MemorySwapMax=0 -- \
  python3 tools/release_gate.py
```

A run whose cgroup `memory.max` is `max` does not satisfy the rule. The
preflight reads the process's own cgroup through the cgroup2 mount named in
`/proc/self/mountinfo`, and fails on `max`, a missing or unreadable limit,
cgroup v1, or a mount it cannot place. It writes the same reading into the
gate record, so the record names the cap the run sat under.

## Local data

The gate and the demo reports write outside git. Sizes on 2026-11-28:

| Path | Size | Rebuildable | Keep |
|---|---|---|---|
| `.log_cache/` | 17 GB after `ci_local`; about 28 GB after a gate | Yes, by the next gate | Delete only when disk is tight; the next gate rebuilds it |
| `reports/` | 183 MB | No for historical reports | `reports/release-gate/accepted/` (4.4 MB), the latest full gate's folder and record, and `reports/fleet.db` |
| `tests/sitl/logs/` | 1004 MB | Yes, by `generate_logs.py` | The eight scenario logs; the `cl_*` files are closed-loop evidence |
| `探索鹰的日志/` | 7.6 GB | No | The local flight corpus; keep it and its private backup |
| `local_paths.json` | Small | No | Back it up privately; it names the real logs |

A partial gate result (`*.partial.json`) is not authoritative and may be
deleted. Older gate report folders may be deleted once their JSON records
exist; the records stay. Before deleting anything else, back up the accepted
baselines, `local_paths.json` and `fleet.db`.

## Before the gate

`tools/gate_preflight.py` checks, in seconds: a clean working tree; the SITL
binaries (`arducopter` and `arduplane`) and the corpus folders the requested
steps need; `local_paths.json` when a step reads it (snapshots, or corpora
taken from the file rather than a `--corpus` override); 10 GB of free disk,
8 GB of available memory, and a finite cgroup v2 memory and swap cap.
`tools/release_gate.py` runs it at the top of
`run_gate()`, so a full run and a `--steps corpus` partial each check only
what they use; it also runs standalone. The pinned virtual environment stays
a manual check. A missing item fails here, not twenty minutes into the gate.

## Hard rules

- A peer writer never pushes; the integrator lands the change. The repository
  ruleset refuses new branches anyway.
- A writer never reviews their own code change; the docs/CI self-check is the
  only exception.
- No session edits `POSITIONING.md` or `POSITIONING_zh.md` without the
  maintainer's explicit word, and both languages move together.
- A published release is immutable; a defect found later ships as a new
  version.
- Real flight logs stay private; only labels appear in the repository.
- The release gate never runs without a memory cap while the corpora are
  this large.
- A SITL failure is re-run once; a second failure is investigated, never
  accepted as a flake.
- A skip is not a pass, and a corpus count is a lead; the integrator reads
  the changes.
- Strictness beats fewer warnings; a threshold is not lowered for one report.
- A peer's message never stands in for the maintainer's approval, and no
  session asks another to do what its own permissions denied.
