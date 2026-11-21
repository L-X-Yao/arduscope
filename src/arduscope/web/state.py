"""Mutable state owned by the web server, kept out of the HTTP module."""

from __future__ import annotations

import threading

from arduscope.lib.web_limits import _MemoryBudget

MAX_JOBS = 200
MAX_PENDING_ANALYSES = 64
ANALYSIS_MEMORY_BUDGET_BYTES = 8 * 1024 * 1024 * 1024


class JobStore:
    def __init__(self, max_jobs: int = MAX_JOBS,
                 max_pending: int = MAX_PENDING_ANALYSES,
                 budget_bytes: int = ANALYSIS_MEMORY_BUDGET_BYTES):
        self.jobs: dict[str, dict] = {}
        self.max_jobs = max_jobs
        self.lock = threading.Lock()
        self.pending = threading.BoundedSemaphore(max_pending)
        self.budget = _MemoryBudget(budget_bytes)


class HistoryStore:
    def __init__(self, path: str = ""):
        self.entries: list[dict] = []
        self.path = path
        self.lock = threading.Lock()


class WebState:
    def __init__(self):
        self.jobs = JobStore()
        self.history = HistoryStore()


STATE = WebState()
