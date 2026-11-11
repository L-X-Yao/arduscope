"""
Locks POSITIONING.md's "E-class 清零" rule into CI.

Every threshold field in lib/thresholds.py must carry an authority
label in its single-line comment: `# A:`, `# A(soft):`, `# B:`,
`# C:`, or `# D:`. `# E:` is only allowed for explicit waivers
(pid secondary / notch time-domain proxies) listed in the whitelist
below — block-level comments for those groups document why.

When adding a new threshold:
  - Cite the authority in a `# A:` / `# D:` comment.
  - If genuinely no authority exists, either add to the whitelist
    with justification or convert to a secondary signal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast


THRESHOLDS_FILE = (
    Path(__file__).parent.parent / "lib" / "thresholds.py"
)


# Fields where `# E:` is accepted because their block-level comment
# already documents the field as a secondary signal or proxy. These
# are not "unknown authority" — they are transparently-labelled
# auxiliary indicators. See POSITIONING.md §4.2.
E_CLASS_WAIVERS: dict[str, str] = {
    # -- pid_tuning secondary metrics --
    # Authoritative PID metric is METHODIC RATE.*out RMS (pid_rate_out_*).
    # These backup signals catch edge cases the primary metric misses.
    "pid_tracking_warn": "secondary: backup to METHODIC rate_out RMS",
    "pid_tracking_crit": "secondary: backup to METHODIC rate_out RMS",
    "pid_tracking_abs_warn": "secondary: absolute floor for ratio metric",
    "pid_tracking_abs_crit": "secondary: absolute floor for ratio metric",
    "pid_dterm_noise_warn": "secondary: D-term noise ratio",
    "pid_dterm_noise_crit": "secondary: D-term noise ratio",
    "pid_iterm_ratio_warn": "secondary: I-term dominance detector",
    "pid_iterm_monotonic_warn": "secondary: I-term monotonic trend",
    "pid_osc_crossings_warn": "secondary: zero-crossing rate",

    # -- notch_tuning time-domain proxies --
    # No source gives a limit for these. What a notch removes can only
    # be measured with the spectrum from before the filter beside the
    # one after it, which notch_tuning does not do, so these stand in.
    "notch_hover_error_warn": "proxy: no sourced limit",
    "notch_hover_error_crit": "proxy: no sourced limit",
    "notch_freq_drift_hz": "proxy: no sourced limit",
    "notch_ref_error": "proxy: no sourced limit",
    "notch_rate_max_crit": "proxy: no sourced limit",
    "notch_att_err_warn": "proxy: no sourced limit",
    "notch_att_err_crit": "proxy: no sourced limit",

    # -- Judgments that existed as literals in the analyzers. Values kept
    # as they were; no authoritative source is known for them yet.
    "airspeed_unhealthy_pct_warn": "no source: share of unhealthy samples",
    "airspeed_unhealthy_pct_crit": "no source: share of unhealthy samples",
    "airspeed_stuck_pct_warn": "no source: share of stuck samples",
    "rangefinder_quality_pct_warn": "no source: share of good readings",
    "gps_no_fix_frac_crit": "no source: duration of fix loss",
    "gps_high_hdop_frac_warn": "no source: duration of poor HDOP",
    "gps_low_nsats_frac_warn": "no source: duration of few satellites",
    "mission_completion_warn": "no source: waypoint completion",
    "mission_accuracy_warn": "no source: waypoint miss distance",
    "summary_error_count_warn": "no source: ERR event count",
    "wind_gust_ratio_warn": "no source: gust ratio",
    "wind_gust_min_mean": "no source: floor for the gust ratio",
}


# Matches ANY dataclass field line — `    name: type = value  [# comment]` —
# regardless of whether the comment carries an authority label. The label
# (if any) is extracted separately by LABEL_RE so that a field with a
# missing or malformed label is still parsed (label=None) and can be
# caught, instead of being silently invisible to the check.
FIELD_ANY = re.compile(
    r"^\s{4}([a-z_][a-z_0-9]*)\s*:\s*[A-Za-z_\[\]\|\.0-9 ]+"
    r"\s*=\s*[^#]+(?:#\s*(.*))?$"
)
# Extracts a valid authority label (A / A(soft) / B / C / D / E) from
# the start of the trailing comment.
LABEL_RE = re.compile(r"(A\(soft\)|[ABCDE])\s*:")


def _parse_fields() -> list[tuple[int, str, str | None]]:
    """Return [(line_no, field_name, label_or_None)] for every field.

    label is None when the field's comment carries no valid authority
    label — such fields are exactly what test_every_field_has_label
    must flag, so they are parsed rather than skipped.
    """
    fields = []
    in_class = False
    for lineno, line in enumerate(
        THRESHOLDS_FILE.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if line.startswith("class Thresholds"):
            in_class = True
            continue
        if not in_class:
            continue
        # Stop at first method definition (ends dataclass fields block)
        if line.startswith("    def ") or line.startswith("    @"):
            break
        m = FIELD_ANY.match(line)
        if not m:
            continue
        name = m.group(1)
        comment = (m.group(2) or "").strip()
        lm = LABEL_RE.match(comment)
        label = lm.group(1) if lm else None
        fields.append((lineno, name, label))
    return fields


class TestThresholdAuthority:
    """Every threshold field has an authority label."""

    def test_every_field_has_label(self):
        """Every threshold field carries a valid authority label."""
        fields = _parse_fields()
        # Reasonable lower bound so we know parsing worked.
        assert len(fields) > 30, (
            f"parsed only {len(fields)} fields — regex probably "
            "stopped matching. Check FIELD_ANY regex."
        )
        # A field parsed with label=None has a missing/malformed
        # authority comment — the exact violation this test guards.
        unlabelled = [
            (lineno, name) for lineno, name, label in fields
            if label is None
        ]
        if unlabelled:
            details = "\n".join(
                f"  line {ln}: {name}" for ln, name in unlabelled
            )
            pytest.fail(
                "threshold field(s) without an authority label "
                "(# A:/A(soft):/B:/C:/D:/E:):\n"
                f"{details}\n\n"
                "Every threshold MUST cite its source per CLAUDE.md "
                "invariant #4 / POSITIONING.md §4."
            )
        for _, name, label in fields:
            assert label in ("A", "A(soft)", "B", "C", "D", "E"), (
                f"{name}: unexpected label {label!r}"
            )

    def test_e_class_only_in_waiver_whitelist(self):
        """
        POSITIONING 锁定: no unlabelled E-class thresholds.
        Exceptions must be whitelisted with explicit justification.
        """
        fields = _parse_fields()
        e_fields = [
            (lineno, name) for lineno, name, label in fields
            if label == "E"
        ]
        unauthorised = [
            (lineno, name) for lineno, name in e_fields
            if name not in E_CLASS_WAIVERS
        ]
        if unauthorised:
            details = "\n".join(
                f"  line {ln}: {name}" for ln, name in unauthorised
            )
            pytest.fail(
                "E-class threshold(s) without waiver:\n"
                f"{details}\n\n"
                "To resolve: upgrade the label to A/D with an "
                "authoritative source, OR add the field to "
                "E_CLASS_WAIVERS in this test file with a short "
                "justification."
            )

    def test_no_dead_waivers(self):
        """Whitelist entries must correspond to real E-labeled fields."""
        fields = _parse_fields()
        e_names = {name for _, name, label in fields if label == "E"}
        dead = [
            name for name in E_CLASS_WAIVERS
            if name not in e_names
        ]
        assert not dead, (
            "E_CLASS_WAIVERS has stale entries (field no longer E-class "
            "or no longer exists): " + ", ".join(dead)
        )

    def test_authority_breakdown_sanity(self):
        """Inventory sanity: mostly A/D, small controlled E set."""
        fields = _parse_fields()
        counts = {"A": 0, "A(soft)": 0, "B": 0, "C": 0, "D": 0, "E": 0}
        for _, _, label in fields:
            if label is not None:  # None handled by test_every_field_has_label
                counts[label] += 1
        # Basic structural sanity: A + D dominate
        authoritative = counts["A"] + counts["A(soft)"] + counts["D"]
        assert authoritative >= counts["E"], (
            f"E-class ({counts['E']}) should not exceed "
            f"A+D ({authoritative}). Labels: {counts}"
        )
        # Report inventory when running verbose
        print(f"\nThreshold authority inventory: {counts}")


class TestTraceabilityTable:
    """docs/convergence_criteria.md claims to cover every field."""

    @staticmethod
    def _rows() -> dict[str, str]:
        doc = (Path(__file__).parent.parent / "docs"
               / "convergence_criteria.md").read_text(encoding="utf-8")
        return dict(re.findall(r"^\| `([a-z_0-9]+)` \| ([^|]+?) \|", doc, re.M))

    def test_every_field_is_listed(self):
        rows = self._rows()
        missing = [name for _, name, _ in _parse_fields() if name not in rows]
        assert not missing, f"not in convergence_criteria.md: {missing}"

    def test_listed_values_match_the_code(self):
        from dataclasses import fields

        from lib.thresholds import Thresholds
        defaults = {f.name: f.default for f in fields(Thresholds)}
        rows = self._rows()
        wrong = [name for name, text in rows.items()
                 if name in defaults
                 and float(text) != float(defaults[name])]
        assert not wrong, f"table disagrees with the code: {wrong}"

    def test_no_row_for_a_field_that_does_not_exist(self):
        names = {name for _, name, _ in _parse_fields()}
        assert not set(self._rows()) - names
