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
