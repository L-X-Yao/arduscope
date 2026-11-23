# ADR 0002: Architecture hardening completion

- Status: accepted
- Date: 2026-11-23
- Supersedes: the target layout in [ADR 0001](0001-architecture-hardening.md)

## Context

ADR 0001 chose hardening over a rewrite. Phases 0-6 shipped in v0.13.0 and
v0.13.1: the `src/arduscope` namespace, visible failure handling, shared
metric helpers, the `ReportSession` application boundary, web state stores,
and versioned persistence. Phases 7-9 finish the remaining type, source-map,
package, theme and documentation work.

The target directory split in ADR 0001 was aspirational. The implemented
application boundary and dependency checks now provide the separation that
matters, so another directory migration would add churn without a
user-visible benefit.

## Decision

1. `ParamSuggestion` is a frozen typed row in
   `src/arduscope/lib/suggestions.py`. Export boundaries call `to_dict()` and
   keep the existing `param`, `current`, `suggested`, `reason` wire format.
2. Threshold citations live in `src/arduscope/lib/threshold_sources.py`.
   `tools/check_thresholds.py` checks the map against `Thresholds` and the
   generated [convergence table](../convergence_criteria.md); the table is not
   edited by hand.
3. The online agent lives at `src/arduscope/agent/` (`arduscope.agent`), not
   under `lib`. There is no compatibility shim at the old import path.
4. Report surfaces share only byte-identical values through
   `src/arduscope/lib/theme.py`. Per-surface palettes and other literals stay
   per-surface; generated CSS remains unchanged.
5. The final layout is the actual layered monolith:

   ```text
   src/arduscope/{analyzers,lib,web,agent,locales}
   flight_report.py
   multi_report.py
   batch_report.py
   fleet_report.py
   web_app.py
   ```

   The five root modules remain thin entry shims and keep the existing CLI
   names. `ReportSession` is the single-log application boundary;
   `multi_report` keeps its metrics-only comparison path. The
   `core/analysis/report/archive` target in ADR 0001 is not pursued.

## Consequences

Behavior parity remains the contract. The acceptance measures are the 561
private corpus logs (43 eagle and 518 test), 424 snapshots, the SITL matrix
and the 30/30 Windows package check, as recorded in the
[architecture baseline](../history/architecture-baseline-2026-11-21.md).
Those are release-gate measures, not a claim that this documentation commit
reran them.

The remaining debt is deliberately limited to the items in
[Architecture: Known debt](../architecture.md#known-debt): the Bottle routes
in `web_app.py`, the pipeline and application boundary living in `lib`, and
large domain modules. These do not justify another layout rewrite. Route
extraction and module splits need an independent maintenance reason.

## Implementation record

The batch history is linear. These are integrated phase tips and release
commits, not invented merge commits.

| Phase | Date | Integrated evidence |
|---|---|---|
| 0. Baseline and import guard | 2026-11-21 | `853e2b9` |
| 1. `src/arduscope` namespace and packaging | 2026-11-21 | `67a1f18` through `6937f55` |
| 2. Visible analysis failures | 2026-11-21 | `acfb8f0`, review fix `83da86b` |
| 3. Shared armed, primary-lane and metric helpers | 2026-11-21 | `94c2271`, export fix `8c03e13` |
| Checkpoint A: v0.13.0 | 2026-11-21 | `05529ac` |
| 4. `ReportSession` application boundary | 2026-11-21 | `ec9425a`, failure-order fix `152220a` |
| 5. `WebState`, job and history stores | 2026-11-21 | `a7c415d`, size-bound fix `6052936` |
| 6. Versioned fleet and web persistence | 2026-11-21 | `ec0fc5b`, read-only fallback `9810771` |
| Checkpoint B: v0.13.1 | 2026-11-21 | `b39f9aa` |
| 7. Typed suggestions and threshold sources | 2026-11-23 | `f623e72`, `8e0e9b3`; reviewed tip `ba201e5` |
| 8. Agent namespace and shared report tokens | 2026-11-23 | `4bf55ab`, `4e96190`; reviewed tip `8d07a70` |
| 9. Architecture and contributor documentation | 2026-11-23 | This ADR and its companion documentation commit; no runtime checkpoint |

ADR 0001 remains the historical record of the original plan. This ADR closes
that plan with the layout that was actually reviewed and merged.
