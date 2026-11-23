"""The typed suggestion model preserves limits, merging and conflicts."""

from __future__ import annotations

import pytest

from arduscope.lib.param_conflict import detect_param_conflicts, merge_suggestions
from arduscope.lib.param_limits import limit_suggestions
from arduscope.lib.results import AnalysisResult
from arduscope.lib.suggestions import ParamSuggestion

pytestmark = pytest.mark.fast


def _suggestion(param="X_P", suggested=2):
    return ParamSuggestion(param=param, current=1, suggested=suggested,
                           reason="test reason")


def _result(title, suggested=2):
    return AnalysisResult(title=title, status="warning", summary="test",
                          param_suggestions=[_suggestion(suggested=suggested)])


def test_to_dict_has_stable_key_order():
    assert list(_suggestion().to_dict()) == [
        "param", "current", "suggested", "reason"]
    assert _suggestion().to_dict() == {
        "param": "X_P", "current": 1, "suggested": 2, "reason": "test reason"}


@pytest.mark.parametrize("param", ["", None, 123])
def test_param_must_be_a_non_empty_string(param):
    with pytest.raises(ValueError, match="non-empty string"):
        ParamSuggestion(param=param, current=1, suggested=2, reason="bad")


def test_analysis_result_rejects_dict_suggestions():
    with pytest.raises(TypeError, match="ParamSuggestion"):
        AnalysisResult(
            title="bad", status="warning", summary="test",
            param_suggestions=[{
                "param": "X_P", "current": 1, "suggested": 2, "reason": "bad"}])


def test_limit_suggestions_returns_new_typed_objects_and_clamps():
    original = ParamSuggestion(
        param="INS_HNTCH_BW", current=40, suggested=290, reason="wide peak")
    limited = limit_suggestions([original])
    assert len(limited) == 1
    assert isinstance(limited[0], ParamSuggestion)
    assert limited[0] is not original
    assert limited[0].suggested == 250.0
    assert limited[0].param == original.param
    assert limited[0].current == original.current
    assert limited[0].reason == original.reason
    assert original.suggested == 290


def test_merge_and_conflict_keep_their_dict_boundaries():
    first = _result("first", 2)
    merged, conflicts = merge_suggestions([first])
    assert conflicts == []
    assert merged == [{
        "param": "X_P", "current": 1, "suggested": 2,
        "reason": "test reason", "source": "first"}]

    second = _result("second", 3)
    conflicts = detect_param_conflicts([first, second])
    assert len(conflicts) == 1
    assert conflicts[0]["param"] == "X_P"
    assert {row["source"] for row in conflicts[0]["suggestions"]} == {
        "first", "second"}
