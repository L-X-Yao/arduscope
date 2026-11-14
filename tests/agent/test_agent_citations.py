"""Claims are checked against the tool calls they cite."""

from __future__ import annotations

import json

import pytest

from lib.agent.citations import ToolRecord, validate

pytestmark = pytest.mark.fast


def _record(seq=1, content=None, is_error=False):
    return ToolRecord(seq=seq, name="get_result",
                      arguments={"key": "vibration"},
                      content=json.dumps(content or {"value": 12.3,
                                                     "status": "warning"}),
                      is_error=is_error)


def test_a_matching_number_is_verified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is 12.3", "cites": [1],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 0
    assert submission.observations[0].cites == [1]


def test_a_number_not_in_the_citation_is_unverified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is 99.0", "cites": [1],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 1
    assert submission.observations[0].reason == "number_not_in_citation"


def test_an_unknown_citation_is_unverified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is high", "cites": [9],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 1
    assert submission.observations[0].reason == "unknown_citation"


def test_a_bare_number_is_a_citation_not_a_crash():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is 12.3", "cites": 1,
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 0
    assert submission.observations[0].cites == [1]


def test_an_integral_float_citation_is_accepted():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is 12.3", "cites": [1.0],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 0
    assert submission.observations[0].cites == [1]


def test_an_error_citation_is_unverified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is high", "cites": [1],
                          "kind": "observation"}],
    }, [_record(is_error=True)])
    assert submission.unverified_count == 1
    assert submission.observations[0].reason == "error_citation"


def test_no_citation_is_unverified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is high", "cites": [],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 1
    assert submission.observations[0].reason == "no_citation"


def test_a_qualitative_claim_needs_only_a_real_citation():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is high", "cites": [1],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 0


def test_rounded_numbers_match():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "vibration is 12.30", "cites": [1],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.unverified_count == 0


def test_a_difference_of_two_cited_numbers_is_verified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "the difference is 7.7", "cites": [1],
                          "kind": "observation"}],
    }, [_record(content={"low": 2.3, "high": 10.0})])
    assert submission.unverified_count == 0


def test_a_percentage_of_two_cited_numbers_is_verified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "the difference is 50 percent", "cites": [1],
                          "kind": "observation"}],
    }, [_record(content={"a": 10, "b": 20})])
    assert submission.unverified_count == 0


def test_zero_values_do_not_break_the_arithmetic_check():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "the difference is 5", "cites": [1],
                          "kind": "observation"}],
    }, [_record(content={"a": 0, "b": 5})])
    assert submission.unverified_count == 0


def test_a_data_gap_needs_no_citation():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "no FFT data is in the log", "cites": [],
                          "kind": "data_gap"}],
    }, [])
    assert submission.unverified_count == 0


def test_a_data_gap_may_cite_an_error():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "the spectrum call failed", "cites": [1],
                          "kind": "data_gap"}],
    }, [_record(is_error=True)])
    assert submission.unverified_count == 0


def test_a_ratio_as_a_percentage_is_verified():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "about 70 percent of the limit", "cites": [1],
                          "kind": "observation"}],
    }, [_record(content={"value": 20.9, "limit": 30.0})])
    assert submission.unverified_count == 0


def test_a_summary_number_outside_every_citation_is_flagged():
    submission = validate({
        "summary": "the peak was 99.0",
        "observations": [{"text": "vibration is high", "cites": [1],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.summary_unverified is True


def test_a_summary_number_inside_a_citation_is_verified():
    submission = validate({
        "summary": "the value was 12.3",
        "observations": [{"text": "vibration is high", "cites": [1],
                          "kind": "observation"}],
    }, [_record()])
    assert submission.summary_unverified is False


def test_a_data_gap_cannot_invent_a_number():
    submission = validate({
        "summary": "ok",
        "observations": [{"text": "the missing peak was 99.0", "cites": [],
                          "kind": "data_gap"}],
    }, [])
    assert submission.unverified_count == 1
