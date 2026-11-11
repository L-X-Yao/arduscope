"""
AI-powered flight diagnosis engine.

Cross-correlates findings from multiple analyzers to produce intelligent
diagnostic narratives with actionable recommendations.

Uses a rule-based expert system with cross-correlation analysis.
No external API dependency required.
"""

from __future__ import annotations

import html as _html
import sys
from dataclasses import dataclass, field

from .results import AnalysisResult
from lib.i18n import t
from lib.vehicle_detect import VehicleInfo


# =====================================================================
# Cross-correlation rules
# =====================================================================
# Each rule fires when ALL listed analyzers show the specified statuses.
# The diagnosis text explains WHY these findings are correlated (the
# value-add beyond what any single analyzer tells you).

@dataclass
class _Rule:
    name: str
    conditions: dict[str, set[str]]  # analyzer_key -> {status, ...}
    title: str
    diagnosis: str
    recommendation: str
    priority: str  # "high" | "medium" | "low" — floor, raised to "high"
                   # whenever a related analyzer is critical
    # analyzer_key -> statuses that BLOCK the rule. A key absent from the
    # results does not block: a missing analyzer is not evidence either
    # way. Use this when the diagnosis text asserts something about an
    # analyzer the conditions do not cover.
    excludes: dict[str, set[str]] = field(default_factory=dict)
    # analyzer_key -> evidence field that has to be over its threshold. Use
    # this when the diagnosis names one cause and the status can have others.
    measured: dict[str, str] = field(default_factory=dict)


def _build_cross_rules() -> list[_Rule]:
    return [
        _Rule(
            name="vibration_motor",
            conditions={
                "vibration": {"warning", "critical"},
                "motor": {"warning", "critical"},
            },
            title=t("ai.cross.vibration_motor.title"),
            diagnosis=t("ai.cross.vibration_motor.diagnosis"),
            recommendation=t("ai.cross.vibration_motor.recommendation"),
            priority="high",
        ),
        _Rule(
            name="gps_ekf",
            conditions={
                "gps": {"warning", "critical"},
                "ekf": {"warning", "critical"},
            },
            title=t("ai.cross.gps_ekf.title"),
            diagnosis=t("ai.cross.gps_ekf.diagnosis"),
            recommendation=t("ai.cross.gps_ekf.recommendation"),
            priority="medium",
            # gps_glitch_ekf is the same advice for a more specific
            # cause. Without this both print, near word for word.
            excludes={"gps_glitch": {"warning", "critical"}},
        ),
        _Rule(
            name="battery_motor_power",
            conditions={
                "battery": {"warning", "critical"},
                "motor": {"warning", "critical"},
            },
            title=t("ai.cross.battery_motor_power.title"),
            diagnosis=t("ai.cross.battery_motor_power.diagnosis"),
            recommendation=t("ai.cross.battery_motor_power.recommendation"),
            priority="high",
        ),
        _Rule(
            name="compass_motor_interference",
            # compass also warns on field variation and offsets. Of 292 logs
            # the old compass + motor pairing matched, 211 had no interference
            # measured, and motor's uneven output says nothing about the field.
            conditions={"compass": {"warning", "critical"}},
            measured={"compass": "motor_interference_pct"},
            title=t("ai.cross.compass_motor_interference.title"),
            diagnosis=t("ai.cross.compass_motor_interference.diagnosis"),
            recommendation=t("ai.cross.compass_motor_interference.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="imu_ekf_hardware",
            conditions={
                "imu_consistency": {"warning", "critical"},
                "ekf": {"warning", "critical"},
            },
            title=t("ai.cross.imu_ekf_hardware.title"),
            diagnosis=t("ai.cross.imu_ekf_hardware.diagnosis"),
            recommendation=t("ai.cross.imu_ekf_hardware.recommendation"),
            priority="high",
        ),
        _Rule(
            name="attitude_vibration_control",
            conditions={
                "attitude": {"warning", "critical"},
                "vibration": {"warning", "critical"},
            },
            title=t("ai.cross.attitude_vibration_control.title"),
            diagnosis=t("ai.cross.attitude_vibration_control.diagnosis"),
            recommendation=t("ai.cross.attitude_vibration_control.recommendation"),
            priority="high",
        ),
        _Rule(
            name="vtol_wind_transition",
            conditions={
                "vtol_transition": {"warning", "critical"},
                "wind": {"warning", "critical"},
            },
            title=t("ai.cross.vtol_wind_transition.title"),
            diagnosis=t("ai.cross.vtol_wind_transition.diagnosis"),
            recommendation=t("ai.cross.vtol_wind_transition.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="rc_failsafe_link",
            conditions={
                "rc_input": {"warning", "critical"},
                "failsafe": {"warning", "critical"},
            },
            title=t("ai.cross.rc_failsafe_link.title"),
            diagnosis=t("ai.cross.rc_failsafe_link.diagnosis"),
            recommendation=t("ai.cross.rc_failsafe_link.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="thermal_cpu_overload",
            conditions={
                "log_health": {"warning", "critical"},
                "temperature": {"warning", "critical"},
            },
            title=t("ai.cross.thermal_cpu_overload.title"),
            diagnosis=t("ai.cross.thermal_cpu_overload.diagnosis"),
            recommendation=t("ai.cross.thermal_cpu_overload.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="attitude_pid_tuning",
            conditions={
                "attitude": {"warning", "critical"},
                "pid_tuning": {"warning", "critical"},
            },
            title=t("ai.cross.attitude_pid_tuning.title"),
            diagnosis=t("ai.cross.attitude_pid_tuning.diagnosis"),
            recommendation=t("ai.cross.attitude_pid_tuning.recommendation"),
            priority="medium",
            # The diagnosis text asserts vibration is normal, and
            # attitude_vibration_control already carries the opposite
            # case. Without this the two fire together and contradict.
            excludes={"vibration": {"warning", "critical"}},
        ),
        _Rule(
            name="vibration_notch_missing",
            conditions={
                "vibration": {"warning", "critical"},
                "notch_tuning": {"warning", "critical"},
            },
            title=t("ai.cross.vibration_notch_missing.title"),
            diagnosis=t("ai.cross.vibration_notch_missing.diagnosis"),
            recommendation=t("ai.cross.vibration_notch_missing.recommendation"),
            priority="high",
        ),
        _Rule(
            name="gyro_sat_vibration",
            conditions={
                "gyro_saturation": {"warning", "critical"},
                "vibration": {"warning", "critical"},
            },
            title=t("ai.cross.gyro_sat_vibration.title"),
            diagnosis=t("ai.cross.gyro_sat_vibration.diagnosis"),
            recommendation=t("ai.cross.gyro_sat_vibration.recommendation"),
            priority="high",
        ),
        _Rule(
            name="compass_consistency_ekf",
            conditions={
                "compass_consistency": {"warning", "critical"},
                "ekf": {"warning", "critical"},
            },
            title=t("ai.cross.compass_consistency_ekf.title"),
            diagnosis=t("ai.cross.compass_consistency_ekf.diagnosis"),
            recommendation=t("ai.cross.compass_consistency_ekf.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="gps_glitch_ekf",
            conditions={
                "gps_glitch": {"warning", "critical"},
                "ekf": {"warning", "critical"},
            },
            title=t("ai.cross.gps_glitch_ekf.title"),
            diagnosis=t("ai.cross.gps_glitch_ekf.diagnosis"),
            recommendation=t("ai.cross.gps_glitch_ekf.recommendation"),
            priority="high",
        ),
        _Rule(
            name="tecs_vtol_transition",
            conditions={
                "tecs_tuning": {"warning", "critical"},
                "vtol_transition": {"warning", "critical"},
            },
            title=t("ai.cross.tecs_vtol_transition.title"),
            diagnosis=t("ai.cross.tecs_vtol_transition.diagnosis"),
            recommendation=t("ai.cross.tecs_vtol_transition.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="gps_glitch_vibration",
            conditions={
                "gps_glitch": {"warning", "critical"},
                "vibration": {"warning", "critical"},
            },
            title=t("ai.cross.gps_glitch_vibration.title"),
            diagnosis=t("ai.cross.gps_glitch_vibration.diagnosis"),
            recommendation=t("ai.cross.gps_glitch_vibration.recommendation"),
            priority="high",
        ),
        _Rule(
            name="battery_power",
            conditions={
                "battery": {"warning", "critical"},
                "power": {"warning", "critical"},
            },
            title=t("ai.cross.battery_power.title"),
            diagnosis=t("ai.cross.battery_power.diagnosis"),
            recommendation=t("ai.cross.battery_power.recommendation"),
            priority="high",
        ),
        _Rule(
            name="servo_dither_pid",
            conditions={
                "servo_dither": {"warning", "critical"},
                "pid_tuning": {"warning", "critical"},
            },
            title=t("ai.cross.servo_dither_pid.title"),
            diagnosis=t("ai.cross.servo_dither_pid.diagnosis"),
            recommendation=t("ai.cross.servo_dither_pid.recommendation"),
            priority="high",
        ),
        _Rule(
            name="clock_log_cpu",
            conditions={
                "clock_health": {"warning", "critical"},
                "log_health": {"warning", "critical"},
            },
            title=t("ai.cross.clock_log_cpu.title"),
            diagnosis=t("ai.cross.clock_log_cpu.diagnosis"),
            recommendation=t("ai.cross.clock_log_cpu.recommendation"),
            priority="medium",
        ),
        _Rule(
            name="compass_dual_fault",
            conditions={
                "compass": {"warning", "critical"},
                "compass_consistency": {"warning", "critical"},
            },
            title=t("ai.cross.compass_dual_fault.title"),
            diagnosis=t("ai.cross.compass_dual_fault.diagnosis"),
            recommendation=t("ai.cross.compass_dual_fault.recommendation"),
            priority="high",
        ),
        _Rule(
            name="gyro_sat_attitude",
            conditions={
                "gyro_saturation": {"warning", "critical"},
                "attitude": {"warning", "critical"},
            },
            title=t("ai.cross.gyro_sat_attitude.title"),
            diagnosis=t("ai.cross.gyro_sat_attitude.diagnosis"),
            recommendation=t("ai.cross.gyro_sat_attitude.recommendation"),
            priority="high",
        ),
        _Rule(
            name="airspeed_tecs",
            conditions={
                "airspeed": {"warning", "critical"},
                "tecs_tuning": {"warning", "critical"},
            },
            title=t("ai.cross.airspeed_tecs.title"),
            diagnosis=t("ai.cross.airspeed_tecs.diagnosis"),
            recommendation=t("ai.cross.airspeed_tecs.recommendation"),
            priority="high",
        ),
        _Rule(
            name="ekf_vel_div_vibration",
            conditions={
                "ekf_vel_divergence": {"warning", "critical"},
                "vibration": {"warning", "critical"},
            },
            title=t("ai.cross.ekf_vel_div_vibration.title"),
            diagnosis=t("ai.cross.ekf_vel_div_vibration.diagnosis"),
            recommendation=t("ai.cross.ekf_vel_div_vibration.recommendation"),
            priority="high",
        ),
        _Rule(
            name="ekf_vel_div_variance",
            conditions={
                "ekf_vel_divergence": {"warning", "critical"},
                "ekf": {"warning", "critical"},
            },
            title=t("ai.cross.ekf_vel_div_variance.title"),
            diagnosis=t("ai.cross.ekf_vel_div_variance.diagnosis"),
            recommendation=t("ai.cross.ekf_vel_div_variance.recommendation"),
            priority="high",
        ),
        _Rule(
            name="board_power_log",
            conditions={
                "board_power": {"warning", "critical"},
                "log_health": {"warning", "critical"},
            },
            title=t("ai.cross.board_power_log.title"),
            diagnosis=t("ai.cross.board_power_log.diagnosis"),
            recommendation=t("ai.cross.board_power_log.recommendation"),
            priority="high",
        ),
        _Rule(
            name="vibration_imu_consistency",
            conditions={
                "vibration": {"warning", "critical"},
                "imu_consistency": {"warning", "critical"},
            },
            title=t("ai.cross.vibration_imu_consistency.title"),
            diagnosis=t("ai.cross.vibration_imu_consistency.diagnosis"),
            recommendation=t(
                "ai.cross.vibration_imu_consistency.recommendation"),
            priority="high",
        ),
        _Rule(
            name="gps_mission",
            conditions={
                "gps": {"warning", "critical"},
                "mission": {"warning", "critical"},
            },
            title=t("ai.cross.gps_mission.title"),
            diagnosis=t("ai.cross.gps_mission.diagnosis"),
            recommendation=t("ai.cross.gps_mission.recommendation"),
            priority="medium",
        ),
    ]

# Status severity for ordering
_SEVERITY = {"critical": 3, "warning": 2, "info": 1, "good": 0}
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# =====================================================================
# Diagnosis engine
# =====================================================================

@dataclass
class _MatchedIssue:
    """A matched cross-correlation or individual issue."""
    title: str
    diagnosis: str
    recommendation: str
    priority: str
    related_keys: list[str]
    is_cross: bool = True  # cross-correlated vs individual


def _is_measured(result: AnalysisResult, evidence_field: str) -> bool:
    """Whether the result measured this field above its threshold."""
    return any(item.get("field") == evidence_field
               and item["value"] > item["threshold"]
               for item in result.evidence)


def _match_cross_rules(
    results_by_key: dict[str, AnalysisResult],
) -> list[_MatchedIssue]:
    """Find all cross-correlation rules whose conditions are met."""
    matched = []
    for rule in _build_cross_rules():
        blocked = any(
            (r := results_by_key.get(key)) is not None and r.status in statuses
            for key, statuses in rule.excludes.items()
        )
        if blocked:
            continue
        if not all(key in results_by_key
                   and _is_measured(results_by_key[key], evidence_field)
                   for key, evidence_field in rule.measured.items()):
            continue
        all_met = True
        related = []
        for key, allowed_statuses in rule.conditions.items():
            result = results_by_key.get(key)
            if result is None or result.status not in allowed_statuses:
                all_met = False
                break
            related.append(key)
        if all_met:
            # A matched rule hides its analyzers from the individual
            # list, so a static priority would let a pair of criticals
            # report as a medium issue while either one alone reports
            # high. The rule's priority is the floor, not the ceiling.
            worst = max(_SEVERITY.get(results_by_key[k].status, 0)
                        for k in related)
            priority = ("high" if worst >= _SEVERITY["critical"]
                        else rule.priority)
            matched.append(_MatchedIssue(
                title=rule.title,
                diagnosis=rule.diagnosis,
                recommendation=rule.recommendation,
                priority=priority,
                related_keys=related,
                is_cross=True,
            ))
    matched.sort(key=lambda m: _PRIORITY_ORDER.get(m.priority, 9))
    return matched


# Individual issue templates keyed by analyzer registry_key
@dataclass
class _IndividualTemplate:
    diagnosis: str
    recommendation: str


def _build_individual_templates() -> dict[str, dict[str, _IndividualTemplate]]:
    return {
        "vibration": {
            "critical": _IndividualTemplate(
                t("ai.individual.vibration.critical.diagnosis"),
                t("ai.individual.vibration.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.vibration.warning.diagnosis"),
                t("ai.individual.vibration.warning.recommendation")),
        },
        "motor": {
            "critical": _IndividualTemplate(
                t("ai.individual.motor.critical.diagnosis"),
                t("ai.individual.motor.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.motor.warning.diagnosis"),
                t("ai.individual.motor.warning.recommendation")),
        },
        "ekf": {
            "critical": _IndividualTemplate(
                t("ai.individual.ekf.critical.diagnosis"),
                t("ai.individual.ekf.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.ekf.warning.diagnosis"),
                t("ai.individual.ekf.warning.recommendation")),
        },
        "gps": {
            "critical": _IndividualTemplate(
                t("ai.individual.gps.critical.diagnosis"),
                t("ai.individual.gps.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.gps.warning.diagnosis"),
                t("ai.individual.gps.warning.recommendation")),
        },
        "battery": {
            "critical": _IndividualTemplate(
                t("ai.individual.battery.critical.diagnosis"),
                t("ai.individual.battery.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.battery.warning.diagnosis"),
                t("ai.individual.battery.warning.recommendation")),
        },
        "compass": {
            "critical": _IndividualTemplate(
                t("ai.individual.compass.critical.diagnosis"),
                t("ai.individual.compass.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.compass.warning.diagnosis"),
                t("ai.individual.compass.warning.recommendation")),
        },
        "attitude": {
            "critical": _IndividualTemplate(
                t("ai.individual.attitude.critical.diagnosis"),
                t("ai.individual.attitude.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.attitude.warning.diagnosis"),
                t("ai.individual.attitude.warning.recommendation")),
        },
        "imu_consistency": {
            "critical": _IndividualTemplate(
                t("ai.individual.imu_consistency.critical.diagnosis"),
                t("ai.individual.imu_consistency.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.imu_consistency.warning.diagnosis"),
                t("ai.individual.imu_consistency.warning.recommendation")),
        },
        "temperature": {
            "critical": _IndividualTemplate(
                t("ai.individual.temperature.critical.diagnosis"),
                t("ai.individual.temperature.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.temperature.warning.diagnosis"),
                t("ai.individual.temperature.warning.recommendation")),
        },
        "log_health": {
            "critical": _IndividualTemplate(
                t("ai.individual.log_health.critical.diagnosis"),
                t("ai.individual.log_health.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.log_health.warning.diagnosis"),
                t("ai.individual.log_health.warning.recommendation")),
        },
        "rc_input": {
            "critical": _IndividualTemplate(
                t("ai.individual.rc_input.critical.diagnosis"),
                t("ai.individual.rc_input.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.rc_input.warning.diagnosis"),
                t("ai.individual.rc_input.warning.recommendation")),
        },
        "failsafe": {
            "critical": _IndividualTemplate(
                t("ai.individual.failsafe.critical.diagnosis"),
                t("ai.individual.failsafe.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.failsafe.warning.diagnosis"),
                t("ai.individual.failsafe.warning.recommendation")),
        },
        "vtol_transition": {
            "critical": _IndividualTemplate(
                t("ai.individual.vtol_transition.critical.diagnosis"),
                t("ai.individual.vtol_transition.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.vtol_transition.warning.diagnosis"),
                t("ai.individual.vtol_transition.warning.recommendation")),
        },
        "airspeed": {
            "critical": _IndividualTemplate(
                t("ai.individual.airspeed.critical.diagnosis"),
                t("ai.individual.airspeed.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.airspeed.warning.diagnosis"),
                t("ai.individual.airspeed.warning.recommendation")),
        },
        "pid_tuning": {
            "critical": _IndividualTemplate(
                t("ai.individual.pid_tuning.critical.diagnosis"),
                t("ai.individual.pid_tuning.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.pid_tuning.warning.diagnosis"),
                t("ai.individual.pid_tuning.warning.recommendation")),
        },
        "notch_tuning": {
            "critical": _IndividualTemplate(
                t("ai.individual.notch_tuning.critical.diagnosis"),
                t("ai.individual.notch_tuning.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.notch_tuning.warning.diagnosis"),
                t("ai.individual.notch_tuning.warning.recommendation")),
        },
        "wind": {
            "critical": _IndividualTemplate(
                t("ai.individual.wind.critical.diagnosis"),
                t("ai.individual.wind.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.wind.warning.diagnosis"),
                t("ai.individual.wind.warning.recommendation")),
        },
        "rangefinder": {
            "critical": _IndividualTemplate(
                t("ai.individual.rangefinder.critical.diagnosis"),
                t("ai.individual.rangefinder.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.rangefinder.warning.diagnosis"),
                t("ai.individual.rangefinder.warning.recommendation")),
        },
        "mission": {
            "critical": _IndividualTemplate(
                t("ai.individual.mission.critical.diagnosis"),
                t("ai.individual.mission.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.mission.warning.diagnosis"),
                t("ai.individual.mission.warning.recommendation")),
        },
        "power": {
            "critical": _IndividualTemplate(
                t("ai.individual.power.critical.diagnosis"),
                t("ai.individual.power.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.power.warning.diagnosis"),
                t("ai.individual.power.warning.recommendation")),
        },
        "gyro_saturation": {
            "critical": _IndividualTemplate(
                t("ai.individual.gyro_saturation.critical.diagnosis"),
                t("ai.individual.gyro_saturation.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.gyro_saturation.warning.diagnosis"),
                t("ai.individual.gyro_saturation.warning.recommendation")),
        },
        "compass_consistency": {
            "critical": _IndividualTemplate(
                t("ai.individual.compass_consistency.critical.diagnosis"),
                t("ai.individual.compass_consistency.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.compass_consistency.warning.diagnosis"),
                t("ai.individual.compass_consistency.warning.recommendation")),
        },
        "gps_glitch": {
            "critical": _IndividualTemplate(
                t("ai.individual.gps_glitch.critical.diagnosis"),
                t("ai.individual.gps_glitch.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.gps_glitch.warning.diagnosis"),
                t("ai.individual.gps_glitch.warning.recommendation")),
        },
        "servo_dither": {
            "critical": _IndividualTemplate(
                t("ai.individual.servo_dither.critical.diagnosis"),
                t("ai.individual.servo_dither.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.servo_dither.warning.diagnosis"),
                t("ai.individual.servo_dither.warning.recommendation")),
        },
        "clock_health": {
            "critical": _IndividualTemplate(
                t("ai.individual.clock_health.critical.diagnosis"),
                t("ai.individual.clock_health.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.clock_health.warning.diagnosis"),
                t("ai.individual.clock_health.warning.recommendation")),
        },
        "tecs_tuning": {
            "critical": _IndividualTemplate(
                t("ai.individual.tecs_tuning.critical.diagnosis"),
                t("ai.individual.tecs_tuning.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.tecs_tuning.warning.diagnosis"),
                t("ai.individual.tecs_tuning.warning.recommendation")),
        },
        "board_power": {
            "critical": _IndividualTemplate(
                t("ai.individual.board_power.critical.diagnosis"),
                t("ai.individual.board_power.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.board_power.warning.diagnosis"),
                t("ai.individual.board_power.warning.recommendation")),
        },
        "ekf_vel_divergence": {
            "critical": _IndividualTemplate(
                t("ai.individual.ekf_vel_divergence.critical.diagnosis"),
                t("ai.individual.ekf_vel_divergence.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.ekf_vel_divergence.warning.diagnosis"),
                t("ai.individual.ekf_vel_divergence.warning.recommendation")),
        },
        "throttle_collapse": {
            "critical": _IndividualTemplate(
                t("ai.individual.throttle_collapse.critical.diagnosis"),
                t("ai.individual.throttle_collapse.critical.recommendation")),
            "warning": _IndividualTemplate(
                t("ai.individual.throttle_collapse.warning.diagnosis"),
                t("ai.individual.throttle_collapse.warning.recommendation")),
        },
        # SummaryAnalyzer tops out at "warning" (ERR event count), so
        # there is no critical entry to write.
        "summary": {
            "warning": _IndividualTemplate(
                t("ai.individual.summary.warning.diagnosis"),
                t("ai.individual.summary.warning.recommendation")),
        },
    }


def _collect_individual_issues(
    results_by_key: dict[str, AnalysisResult],
    cross_covered_keys: set[str],
) -> list[_MatchedIssue]:
    """Collect warning/critical findings not already covered by cross-rules."""
    individual_templates = _build_individual_templates()
    issues = []
    for key, result in results_by_key.items():
        if key in cross_covered_keys:
            continue
        if result.status not in ("warning", "critical"):
            continue
        templates = individual_templates.get(key, {})
        tpl = templates.get(result.status)
        diag = tpl.diagnosis if tpl else result.summary
        rec = tpl.recommendation if tpl else ""
        issues.append(_MatchedIssue(
            title=result.title,
            diagnosis=diag,
            recommendation=rec,
            priority="high" if result.status == "critical" else "medium",
            related_keys=[key],
            is_cross=False,
        ))
    issues.sort(key=lambda m: (
        _PRIORITY_ORDER.get(m.priority, 9),
        m.title,
    ))
    return issues


# =====================================================================
# Narrative generation
# =====================================================================

def _priority_labels() -> dict[str, str]:
    return {
        "high": t("ai.priority.high"),
        "medium": t("ai.priority.medium"),
        "low": t("ai.priority.low"),
    }


def _build_narrative(
    cross_issues: list[_MatchedIssue],
    individual_issues: list[_MatchedIssue],
    good_keys: list[str],
    results_by_key: dict[str, AnalysisResult],
    health_score: int,
    vehicle_info: VehicleInfo,
    flight_duration: float,
) -> tuple[str, str, list[str]]:
    """
    Build the diagnosis narrative.

    Returns:
        (summary, status, details_html_list)
    """
    all_issues = cross_issues + individual_issues
    n_high = sum(1 for i in all_issues if i.priority == "high")
    n_total = len(all_issues)
    failed = [r for r in results_by_key.values() if r.failed]
    n_analyzers = len(results_by_key) - len(failed)
    plabels = _priority_labels()

    # --- Overall summary ---
    if n_total == 0 and failed:
        summary = t("ai.summary.incomplete", score=health_score,
                    n_failed=len(failed), n_ok=n_analyzers)
        status = "info"
    elif n_total == 0:
        summary = t("ai.summary.good", score=health_score, n_analyzers=n_analyzers)
        status = "good"
    elif n_high > 0:
        summary = t("ai.summary.critical", score=health_score, n_total=n_total, n_high=n_high)
        status = "critical"
    else:
        summary = t("ai.summary.warning", score=health_score, n_total=n_total)
        status = "warning"

    # --- Details ---
    details: list[str] = []

    # Overview line
    dur_min = int(flight_duration // 60)
    dur_sec = int(flight_duration % 60)
    dur_str = f"{dur_min}m{dur_sec}s" if dur_min > 0 else f"{dur_sec}s"
    details.append(
        t("ai.detail.overview",
          vehicle_type=vehicle_info.vehicle_type,
          firmware_version=vehicle_info.firmware_version,
          duration=dur_str,
          n_analyzers=n_analyzers,
          n_issues=n_total)
    )

    if failed:
        details.append(t("ai.detail.failed_items",
                         items=", ".join(r.title for r in failed)))

    # Cross-correlated issues (the smart part)
    _e = _html.escape
    if cross_issues:
        details.append(t("ai.detail.section_cross"))
        for issue in cross_issues:
            label = _e(plabels.get(issue.priority, issue.priority))
            block = (
                f"<strong>{label}: {_e(issue.title)}</strong><br>"
                f"&nbsp;&nbsp;{t('ai.detail.label_diagnosis')}: {_e(issue.diagnosis)}<br>"
                f"&nbsp;&nbsp;{t('ai.detail.label_recommendation')}: {_e(issue.recommendation)}"
            )
            details.append(block)

    # Individual issues
    if individual_issues:
        details.append(t("ai.detail.section_individual"))
        for issue in individual_issues:
            label = _e(plabels.get(issue.priority, issue.priority))
            first_key = issue.related_keys[0] if issue.related_keys else None
            analyzer_result = (
                results_by_key.get(first_key) if first_key else None
            )
            extra = ""
            if analyzer_result and analyzer_result.summary:
                extra = f" — {_e(analyzer_result.summary)}"
            block = (
                f"<strong>{label}: {_e(issue.title)}</strong>{extra}<br>"
                f"&nbsp;&nbsp;{_e(issue.diagnosis)}"
            )
            if issue.recommendation:
                block += f"<br>&nbsp;&nbsp;{t('ai.detail.label_recommendation')}: {_e(issue.recommendation)}"
            details.append(block)

    # Recommendations summary
    recs = []
    for issue in all_issues:
        if issue.recommendation:
            tag = t("ai.detail.tag_urgent") if issue.priority == "high" else t("ai.detail.tag_suggestion")
            recs.append(f"{tag} {issue.recommendation}")

    if recs:
        details.append(t("ai.detail.section_actions"))
        for i, rec in enumerate(recs, 1):
            details.append(f"{i}. {_html.escape(rec)}")

    # Positive findings
    if good_keys:
        good_names = [
            results_by_key[k].title for k in good_keys
            if k in results_by_key
        ]
        if good_names:
            details.append(
                t("ai.detail.good_items", items=", ".join(good_names))
            )

    # Confidence note
    if n_analyzers >= 15:
        conf = t("ai.detail.confidence_high")
    elif n_analyzers >= 8:
        conf = t("ai.detail.confidence_medium")
    else:
        conf = t("ai.detail.confidence_low")
    details.append(
        t("ai.detail.confidence_note", confidence=conf, n_analyzers=n_analyzers)
    )

    return summary, status, details


# =====================================================================
# Public API
# =====================================================================

def _generate_rule_based(
    results_by_key: dict[str, AnalysisResult],
    vehicle_info: VehicleInfo,
    health_score: int,
    flight_duration: float,
) -> AnalysisResult:
    """Rule-based diagnosis engine (no external dependency)."""
    cross_issues = _match_cross_rules(results_by_key)

    cross_covered: set[str] = set()
    for issue in cross_issues:
        cross_covered.update(issue.related_keys)

    individual_issues = _collect_individual_issues(
        results_by_key, cross_covered
    )

    good_keys = [
        k for k, r in results_by_key.items()
        if r.status == "good"
    ]

    summary, status, details = _build_narrative(
        cross_issues, individual_issues, good_keys,
        results_by_key, health_score, vehicle_info, flight_duration,
    )

    return AnalysisResult(
        title=t("ai.report_title"),
        status=status,
        summary=summary,
        details=details,
        figures=[],
        sub_results=[],
        # This card asserts nothing on its own: it correlates findings
        # that each carry their own citation. Stated explicitly so a
        # warning/critical AI card is not a source-free verdict.
        evidence_source=(t("ai.evidence")
                         if status in ("warning", "critical") else ""),
    )


# =====================================================================
# LLM API diagnosis
# =====================================================================


def _build_llm_prompt(
    results_by_key: dict[str, AnalysisResult],
    vehicle_info: VehicleInfo,
    health_score: int,
    flight_duration: float,
) -> str:
    """Build the user prompt for LLM diagnosis."""
    dur_m = int(flight_duration // 60)
    dur_s = int(flight_duration % 60)

    def _md_cell(s: str) -> str:
        return str(s).replace("|", "\\|").replace("\n", " ")

    lines = [
        t("ai.llm.section_flight_info"),
        t("ai.llm.vehicle_type", vtype=vehicle_info.vehicle_type, version=vehicle_info.firmware_version),
        t("ai.llm.board", board=vehicle_info.board_name),
        t("ai.llm.motor_imu", motors=vehicle_info.motor_count, imus=vehicle_info.imu_count),
        t("ai.llm.flight_duration", m=dur_m, s=dur_s),
        t("ai.llm.health_score", score=health_score),
        "",
        t("ai.llm.section_results"),
        t("ai.llm.table_header"),
        "|--------|------|------|",
    ]
    for key, result in results_by_key.items():
        lines.append(f"| {_md_cell(result.title)} | {_md_cell(result.status)} | {_md_cell(result.summary)} |")

    lines.append("")
    lines.append(t("ai.llm.generate_request"))
    return "\n".join(lines)


# The Messages API wants a ceiling on the reply. A current Claude model
# decides for itself whether to think before it answers, and what it thinks
# counts against max_tokens: 2000 could be gone before the first word of
# the reply. The reply itself is a page at most.
_ANTHROPIC_MAX_TOKENS = 16000


def _anthropic_text(msg) -> str:
    """The text of a Messages reply, or an error that says what came instead.

    A reply is a list of blocks. When the model thought first, the first
    block is its thinking and has no text. A refusal is a normal reply
    with no text at all.
    """
    stop = getattr(msg, "stop_reason", None)
    if stop == "refusal":
        category = getattr(getattr(msg, "stop_details", None),
                           "category", None)
        raise ValueError("the model declined the request"
                         + (f" ({category})" if category else ""))
    if stop == "max_tokens":
        raise ValueError("the reply was cut off at max_tokens")
    text = "".join(block.text for block in msg.content
                   if getattr(block, "type", None) == "text").strip()
    if not text:
        raise ValueError(f"no text in the reply (stop_reason: {stop})")
    return text


def _call_anthropic(system: str, user: str, api_key: str, model: str,
                    base_url: str = "") -> str:
    """Ask a service that speaks Anthropic's Messages API, return its text."""
    import anthropic
    where = {"base_url": base_url} if base_url else {}
    client = anthropic.Anthropic(api_key=api_key, **where)
    msg = client.messages.create(
        model=model,
        max_tokens=_ANTHROPIC_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return _anthropic_text(msg)


def _openai_text(resp) -> str:
    """The text of a chat completion, or an error that says what came instead.

    A model that reasons first can spend the whole reply on it and leave
    the content empty, and a filtered reply has no content either.
    """
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise ValueError("no choices in the reply")
    stop = getattr(choices[0], "finish_reason", None)
    if stop == "content_filter":
        raise ValueError("the service filtered the reply")
    if stop == "length":
        raise ValueError("the reply was cut off at the length limit")
    text = (getattr(choices[0].message, "content", None) or "").strip()
    if not text:
        raise ValueError(f"no text in the reply (finish_reason: {stop})")
    return text


def _call_openai(system: str, user: str, api_key: str, model: str,
                 base_url: str = "") -> str:
    """Ask a service that speaks OpenAI's chat completions API.

    No ceiling on the reply is sent. The services disagree on its name
    (max_tokens, max_completion_tokens) and on whether reasoning counts
    against it, and every one of them accepts a request without it.
    """
    from openai import OpenAI
    where = {"base_url": base_url} if base_url else {}
    client = OpenAI(api_key=api_key, **where)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return _openai_text(resp)


def _worst_status(results_by_key: dict[str, AnalysisResult]) -> str:
    """The card's severity is the analyzers' verdict, never the LLM's."""
    statuses = {r.status for r in results_by_key.values()}
    if "critical" in statuses:
        return "critical"
    return "warning" if "warning" in statuses else "good"


def _llm_summary(lines: list[str]) -> str:
    """The first line of a reply that says something.

    The prompt asks for the assessment first, and a model may still open
    with a heading or a rule. Neither is an assessment.
    """
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        text = text.lstrip("-*>_ ").replace("**", "").strip()
        if text:
            return text
    return ""


def _parse_llm_response(
    text: str,
    health_score: int,
    n_analyzers: int,
    provider: str,
    status: str,
    model: str = "",
) -> tuple[str, list[str]]:
    """Parse LLM response into (summary, details)."""
    lines = text.strip().split("\n")
    summary = (_llm_summary(lines)
               or t("ai.llm.fallback_summary", score=health_score))

    # The model name comes from the command line and this line is markup.
    details = [t("ai.llm.generated_by", model=_html.escape(model),
                 provider=provider, n_analyzers=n_analyzers)]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            marks = len(line) - len(line.lstrip("#"))
            title = _html.escape(line[marks:].strip())
            details.append(f"<strong>━━ {title} ━━</strong>" if marks <= 2
                           else f"<strong>{title}</strong>")
        elif line.startswith("- "):
            details.append(_html.escape(line[2:]))
        else:
            details.append(_html.escape(line))

    return summary, details


def _generate_llm(
    results_by_key: dict[str, AnalysisResult],
    vehicle_info: VehicleInfo,
    health_score: int,
    flight_duration: float,
    provider: str = "anthropic",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> AnalysisResult:
    """Generate diagnosis via LLM API. Raises if the call fails."""
    user_prompt = _build_llm_prompt(
        results_by_key, vehicle_info, health_score, flight_duration,
    )

    system_prompt = t("ai.llm.system_prompt")
    call = _call_anthropic if provider == "anthropic" else _call_openai
    text = call(system_prompt, user_prompt, api_key, model, base_url)
    if not text:
        raise ValueError("empty response")

    status = _worst_status(results_by_key)
    summary, details = _parse_llm_response(
        text, health_score, len(results_by_key), provider, status, model,
    )

    return AnalysisResult(
        title=t("ai.report_title"),
        status=status,
        summary=summary,
        details=details,
        figures=[],
        sub_results=[],
        # An LLM narrative is not a traceable source. Say so rather than
        # letting it pass as one.
        evidence_source=(t("ai.llm.evidence", model=model, provider=provider)
                         if status in ("warning", "critical") else ""),
        provider=provider,
    )


# =====================================================================
# Public API
# =====================================================================

def generate_diagnosis(
    results_by_key: dict[str, AnalysisResult],
    vehicle_info: VehicleInfo,
    health_score: int,
    flight_duration: float,
    provider: str = "rule",
    api_key: str = "",
    model: str = "",
    base_url: str = "",
) -> AnalysisResult:
    """
    Generate an AI diagnosis AnalysisResult from all analyzer results.

    Args:
        results_by_key: dict of analyzer registry_key -> AnalysisResult
        vehicle_info: detected vehicle information
        health_score: 0-100 composite health score
        flight_duration: flight duration in seconds
        provider: "rule" (default), or the API an LLM is asked through:
                  "anthropic" (Messages) or "openai" (chat completions)
        api_key: API key for LLM providers (or set env var
                 ANTHROPIC_API_KEY / OPENAI_API_KEY)
        model: the model to ask for. No name is built in: a name in the
               code stops working the day its service retires it
        base_url: where the API is, for a service other than the
                  provider's own that speaks the same API

    Returns:
        AnalysisResult with title from t("ai.report_title"). Falls back to
        the rule engine if the LLM cannot be used, and says so on the card
        and on stderr: the user asked for a provider and must not take
        the rule engine's output for it.
    """
    import os as _os

    reason = ""
    if provider in ("anthropic", "openai"):
        env = ("ANTHROPIC_API_KEY" if provider == "anthropic"
               else "OPENAI_API_KEY")
        key = api_key or _os.environ.get(env, "")
        if not key:
            reason = t("ai.llm.no_key", env=env)
        elif not model:
            reason = t("ai.llm.no_model")
        else:
            try:
                return _generate_llm(
                    results_by_key, vehicle_info, health_score,
                    flight_duration, provider, key, model, base_url,
                )
            except ImportError:
                reason = t("ai.llm.no_package", package=provider)
            except Exception as e:  # noqa: BLE001 - SDK raises many types
                reason = t("ai.llm.call_failed",
                           error=f"{type(e).__name__}: {str(e)[:200]}")

    card = _generate_rule_based(
        results_by_key, vehicle_info, health_score, flight_duration,
    )
    card.provider = "rule"
    if reason:
        note = t("ai.llm.fallback", provider=provider, reason=reason)
        print(f"[warn] {note}", file=sys.stderr)
        card.details.insert(0, _html.escape(note))
    return card
