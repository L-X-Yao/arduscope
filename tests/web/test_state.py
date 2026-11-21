"""The web state objects are independent of the HTTP module."""

from __future__ import annotations

from arduscope.web.state import JobStore, HistoryStore, WebState


def test_each_web_state_owns_its_own_stores():
    first = WebState()
    second = WebState()

    first.jobs.jobs["x"] = {"id": "x"}
    first.history.entries.append({"id": "x"})

    assert second.jobs.jobs == {}
    assert second.history.entries == []
    assert first.jobs.lock is not second.jobs.lock
    assert first.history.lock is not second.history.lock
    assert first.jobs.budget is not second.jobs.budget


def test_store_limits_are_visible():
    jobs = JobStore(max_jobs=3, max_pending=2)
    history = HistoryStore("/tmp/history.json")
    assert jobs.max_jobs == 3
    assert history.path == "/tmp/history.json"
