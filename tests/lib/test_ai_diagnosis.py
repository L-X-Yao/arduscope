"""
Regression tests for the rule-based diagnosis engine (lib/ai_diagnosis).

Covers the four defects found in the 2026-07-24 review: severity
inversion on paired criticals, a rule whose text contradicted a rule
firing beside it, a source-free warning card, and analyzers the engine
had no template or rule for.
"""

from __future__ import annotations

from tests._repo import TOOLS_DIR

import sys

import pytest

from analyzers import discover_analyzers
from lib.ai_diagnosis import (
    _build_cross_rules,
    _build_individual_templates,
    _match_cross_rules,
    generate_diagnosis,
)
from lib.results import AnalysisResult
from lib.vehicle_detect import VehicleInfo

pytestmark = pytest.mark.fast


def _vehicle() -> VehicleInfo:
    return VehicleInfo(
        vehicle_type="ArduCopter", firmware_version="4.6.3",
        board_name="synthetic", frame_class=1, motor_count=4,
        imu_count=2, is_quadplane=False,
    )


def _res(key: str, status: str) -> AnalysisResult:
    return AnalysisResult(
        title=key, status=status, summary=f"{key} {status}",
        evidence_source="src" if status in ("warning", "critical") else None,
    )


def _diagnose(statuses: dict[str, str], score: int = 40) -> AnalysisResult:
    by_key = {k: _res(k, v) for k, v in statuses.items()}
    return generate_diagnosis(by_key, _vehicle(), score, 300.0)


def _matched_key_sets(statuses: dict[str, str]) -> list[set[str]]:
    by_key = {k: _res(k, v) for k, v in statuses.items()}
    return [set(m.related_keys) for m in _match_cross_rules(by_key)]


class TestSeverity:

    def test_paired_criticals_do_not_downgrade_the_card(self):
        # gps+ekf is a "medium" rule. Matching it hides both analyzers
        # from the individual list, so a static priority made two
        # criticals report lower than either one alone.
        one = _diagnose({"gps": "critical"})
        pair = _diagnose({"gps": "critical", "ekf": "critical"})
        assert one.status == "critical"
        assert pair.status == "critical", pair.summary

    def test_rule_priority_is_a_floor_not_a_ceiling(self):
        by_key = {k: _res(k, "warning") for k in ("gps", "ekf")}
        medium = _match_cross_rules(by_key)
        assert [m.priority for m in medium] == ["medium"]

        by_key["ekf"] = _res("ekf", "critical")
        raised = _match_cross_rules(by_key)
        assert [m.priority for m in raised] == ["high"]

    def test_warning_only_flight_stays_warning(self):
        assert _diagnose({"gps": "warning", "ekf": "warning"},
                         score=70).status == "warning"


class TestContradictingRules:

    def test_pid_rule_suppressed_while_vibration_is_flagged(self):
        # attitude_pid_tuning's text asserts vibration is normal, and
        # attitude_vibration_control carries the opposite case.
        sets = _matched_key_sets({"attitude": "warning",
                                  "vibration": "warning",
                                  "pid_tuning": "warning"})
        assert {"attitude", "vibration"} in sets
        assert {"attitude", "pid_tuning"} not in sets

    def test_pid_rule_fires_when_vibration_is_clean(self):
        sets = _matched_key_sets({"attitude": "warning",
                                  "vibration": "good",
                                  "pid_tuning": "warning"})
        assert {"attitude", "pid_tuning"} in sets

    def test_pid_rule_fires_when_vibration_analyzer_is_absent(self):
        # A missing analyzer is not evidence that vibration is bad.
        sets = _matched_key_sets({"attitude": "warning",
                                  "pid_tuning": "warning"})
        assert {"attitude", "pid_tuning"} in sets

    def test_specific_gps_rule_wins_over_the_generic_one(self):
        # gps_ekf and gps_glitch_ekf carried near-identical advice and
        # printed one after the other.
        both = _matched_key_sets({"gps": "warning", "gps_glitch": "warning",
                                  "ekf": "warning"})
        assert {"gps_glitch", "ekf"} in both
        assert {"gps", "ekf"} not in both

    def test_generic_gps_rule_still_fires_without_a_glitch(self):
        sets = _matched_key_sets({"gps": "warning", "ekf": "warning"})
        assert {"gps", "ekf"} in sets


class TestEvidenceSource:

    def test_flagged_card_cites_a_source(self):
        card = _diagnose({"gps": "critical"})
        assert card.status == "critical"
        assert card.evidence_source, "warning/critical card must cite a source"

    def test_clean_card_needs_no_source(self):
        card = _diagnose({"gps": "good"}, score=100)
        assert card.status == "good"
        assert not card.evidence_source


class TestCompassMotorInterference:
    """compass also warns on field variation and offsets."""

    @staticmethod
    def _matched(pct: float | None, **others: str) -> list[set[str]]:
        compass = _res("compass", "warning")
        if pct is not None:
            compass.evidence = [{"field": "motor_interference_pct",
                                 "value": pct, "threshold": 30.0}]
        by_key = {"compass": compass,
                  **{k: _res(k, v) for k, v in others.items()}}
        return [set(m.related_keys) for m in _match_cross_rules(by_key)]

    def test_measured_interference_is_enough(self):
        assert {"compass"} in self._matched(31.0)

    def test_motor_is_neither_needed_nor_hidden(self):
        assert self._matched(31.0, motor="warning") == [{"compass"}]

    def test_a_flagged_motor_does_not_stand_in_for_the_measurement(self):
        assert self._matched(14.0, motor="warning") == []

    def test_no_measurement_is_no_diagnosis(self):
        assert self._matched(None, motor="warning") == []


class TestCoverage:

    def test_rules_only_reference_real_analyzers(self):
        keys = {k for k, _cls in discover_analyzers()}
        for rule in _build_cross_rules():
            referenced = set(rule.conditions) | set(rule.excludes)
            unknown = referenced - keys
            assert not unknown, f"{rule.name} references {unknown}"

    def test_templates_only_reference_real_analyzers(self):
        keys = {k for k, _cls in discover_analyzers()}
        unknown = set(_build_individual_templates()) - keys
        assert not unknown, unknown

    @pytest.mark.parametrize("key", [
        "board_power", "ekf_vel_divergence", "throttle_collapse", "summary",
    ])
    def test_flagging_analyzers_have_an_individual_template(self, key):
        # These four can reach warning/critical; without a template the
        # card repeated the analyzer summary with no recommendation.
        assert _build_individual_templates().get(key)

    @pytest.mark.parametrize("key", [
        "ekf_vel_divergence", "board_power", "imu_consistency", "mission",
    ])
    def test_analyzer_participates_in_a_cross_rule(self, key):
        used = {k for r in _build_cross_rules() for k in r.conditions}
        assert key in used

    def test_individual_issue_carries_a_recommendation(self):
        card = _diagnose({"ekf_vel_divergence": "critical"})
        joined = " ".join(card.details)
        assert card.status == "critical"
        assert "ekf_vel_divergence critical" in joined


# Parameter names are validated repo-wide by tools/check_params.py
# against a table harvested from the ArduPilot source. It runs in CI;
# this keeps it in the fast suite too, because wrong parameter names
# were the most common defect in both 2026-07-24 audit passes and a
# wrong name fails silently in every place it can appear.
def test_no_unknown_parameter_names_anywhere():
    sys.path.insert(0, str(TOOLS_DIR))
    import check_params

    prefixes, leaves = check_params.load_table()
    unknown = []
    for name, sites in check_params.collect().items():
        if name in check_params.NOT_PARAMS or name in check_params.LEGACY_PARAMS:
            continue
        if not check_params.resolves(name, prefixes, leaves):
            unknown.append((name, sites[0]))
    assert not unknown, unknown
