"""Tests for lib.param_conflict — parameter suggestion conflict detection."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.fast

from analyzers import AnalysisResult
from lib.param_conflict import detect_param_conflicts
from lib.html_report import HtmlReportGenerator
from lib.vehicle_detect import VehicleInfo


def _result(title: str, suggestions: list[dict],
            sub_results: list | None = None) -> AnalysisResult:
    return AnalysisResult(
        title=title,
        status="warning",
        summary="test",
        param_suggestions=suggestions,
        sub_results=sub_results or [],
    )


def _vehicle() -> VehicleInfo:
    return VehicleInfo(
        vehicle_type="ArduCopter",
        firmware_version="4.6.3",
        board_name="synthetic",
        frame_class=1,
        motor_count=4,
        imu_count=2,
        is_quadplane=False,
    )


class TestDetectParamConflicts:
    """Core detection logic."""

    def test_no_conflicts_different_params(self):
        results = [
            _result("vibration", [
                {"param": "INS_ACCEL_FILTER", "suggested": 10, "current": 0, "reason": "t"},
            ]),
            _result("notch_tuning", [
                {"param": "INS_HNTCH_FREQ", "suggested": 150, "current": 0, "reason": "t"},
            ]),
        ]
        assert detect_param_conflicts(results) == []

    def test_same_param_same_value_no_conflict(self):
        results = [
            _result("vibration", [
                {"param": "INS_ACCEL_FILTER", "suggested": 10, "current": 0, "reason": "t"},
            ]),
            _result("notch_tuning", [
                {"param": "INS_ACCEL_FILTER", "suggested": 10, "current": 0, "reason": "t"},
            ]),
        ]
        assert detect_param_conflicts(results) == []

    def test_same_param_different_values_conflict(self):
        results = [
            _result("vibration", [
                {"param": "INS_ACCEL_FILTER", "suggested": 10, "current": 0, "reason": "t"},
            ]),
            _result("notch_tuning", [
                {"param": "INS_ACCEL_FILTER", "suggested": 15, "current": 0, "reason": "t"},
            ]),
        ]
        conflicts = detect_param_conflicts(results)
        assert len(conflicts) == 1
        assert conflicts[0]["param"] == "INS_ACCEL_FILTER"
        assert len(conflicts[0]["suggestions"]) == 2
        sources = {s["source"] for s in conflicts[0]["suggestions"]}
        assert sources == {"vibration", "notch_tuning"}

    def test_sub_results_included(self):
        """Conflicts in sub_results are detected too."""
        sub = _result("imu0", [
            {"param": "INS_GYR_CAL", "suggested": 1, "current": 0, "reason": "t"},
        ])
        results = [
            _result("imu_consistency", [], sub_results=[sub]),
            _result("vibration", [
                {"param": "INS_GYR_CAL", "suggested": 0, "current": 0, "reason": "t"},
            ]),
        ]
        conflicts = detect_param_conflicts(results)
        assert len(conflicts) == 1
        assert conflicts[0]["param"] == "INS_GYR_CAL"

    def test_empty_results(self):
        assert detect_param_conflicts([]) == []

    def test_no_suggestions(self):
        results = [
            _result("vibration", []),
            _result("notch_tuning", []),
        ]
        assert detect_param_conflicts(results) == []


class TestConflictRenderedInHtml:
    """Verify the HTML report renders the conflict warning box."""

    def test_conflict_warning_in_output(self):
        from lib.i18n import set_locale, get_locale
        prev = get_locale()
        set_locale("en")
        try:
            results = [
                _result("vibration", [
                    {"param": "INS_ACCEL_FILTER", "suggested": 10,
                     "current": 20, "reason": "lower filter"},
                ]),
                _result("notch_tuning", [
                    {"param": "INS_ACCEL_FILTER", "suggested": 15,
                     "current": 20, "reason": "match notch"},
                ]),
            ]
            conflicts = detect_param_conflicts(results)
            assert len(conflicts) == 1

            gen = HtmlReportGenerator(
                vehicle_info=_vehicle(),
                results=results,
            )
            html_output = gen.generate()
            assert "Parameter Suggestion Conflicts" in html_output
            assert "INS_ACCEL_FILTER" in html_output
        finally:
            set_locale(prev)

    def test_no_conflict_box_when_none(self):
        from lib.i18n import set_locale, get_locale
        prev = get_locale()
        set_locale("en")
        try:
            results = [
                _result("vibration", [
                    {"param": "INS_ACCEL_FILTER", "suggested": 10,
                     "current": 20, "reason": "lower filter"},
                ]),
            ]
            gen = HtmlReportGenerator(
                vehicle_info=_vehicle(),
                results=results,
            )
            html_output = gen.generate()
            assert "Parameter Suggestion Conflicts" not in html_output
        finally:
            set_locale(prev)
