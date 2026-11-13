"""
Smoke test: every analyzer × every available log = zero crashes.

This is the most important test — it catches import errors, field name
mismatches, division by zero, NaN handling failures, and anything else
that would make an analyzer throw an exception on real data.
"""

import os
import pytest
from conftest import REAL_LOG_ROOT, _load
from analyzers import discover_analyzers

ANALYZERS = discover_analyzers()
ANALYZER_IDS = [key for key, _ in ANALYZERS]

# Every .bin under the folder local_paths.json names, none without one
def _find_logs():
    logs = []
    for root, _dirs, files in os.walk(REAL_LOG_ROOT):
        for f in sorted(files):
            if f.lower().endswith(".bin"):
                logs.append(os.path.join(root, f))
    return logs

ALL_LOGS = _find_logs()
LOG_IDS = [os.path.basename(p)[:40] for p in ALL_LOGS]


# log_path is the bottom decorator, so it is the outer loop: all analyzers
# for one log run before the next log is loaded. The conftest cache keeps
# only the most recent logs, so the decoded arrays of the whole corpus are
# never alive at once.
@pytest.mark.parametrize("analyzer_key,analyzer_cls", ANALYZERS, ids=ANALYZER_IDS)
@pytest.mark.parametrize("log_path", ALL_LOGS, ids=LOG_IDS)
def test_no_crash(analyzer_key, analyzer_cls, log_path):
    """Every analyzer must not crash on any real log file."""
    if not os.path.isfile(log_path):
        pytest.skip("file missing")
    fix = _load(log_path)
    analyzer = analyzer_cls(fix.reader, fix.thresholds, fix.vehicle)
    if not analyzer.can_run():
        pytest.skip(f"{analyzer_key} cannot run (missing messages)")
    result = analyzer.analyze()
    # Basic sanity
    assert result.status in ("good", "warning", "critical", "info")
    assert isinstance(result.summary, str)
    assert isinstance(result.details, list)
