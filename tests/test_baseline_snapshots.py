"""
Snapshot regression tests for analyzer outputs across known logs.

Two defensive datasets:
  - baseline good logs:  must NOT trigger safety_stop
  - case studies:        must trigger specific keywords/levels

In practice we don't have a guaranteed "clean" log yet (every log we
own has at least one real engineering issue the analyzer correctly
catches). So this file implements a more general approach: SNAPSHOT
TESTING.

For each (log, analyzer) pair, the expected (status, action_level)
output is recorded in snapshot_baseline.json. On every test run we
re-run the analyzers and assert the output matches. Any change —
intentional or not — fails the test loudly. To bless an intentional
change, run:

    python tests/test_baseline_snapshots.py --update

This catches both regression directions:
  - false positive regression: a "good" log suddenly becomes critical
  - false negative regression: a known-bad log suddenly becomes good

Both are exactly what we care about for "防回退".

CI-friendly: the logs are not in the repository, and without them each
test skips via pytest.skip.
"""

from __future__ import annotations

import json
import os
import sys

# Allow `python tests/test_baseline_snapshots.py --update` invocation
# by adding the package directory to sys.path before package imports.
# pytest already does this via conftest.py so this is a no-op there.
_TESTS_DIR_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_PKG_DIR_BOOTSTRAP = os.path.dirname(_TESTS_DIR_BOOTSTRAP)
if _PKG_DIR_BOOTSTRAP not in sys.path:
    sys.path.insert(0, _PKG_DIR_BOOTSTRAP)

_TOOLS_DIR_BOOTSTRAP = os.path.join(_PKG_DIR_BOOTSTRAP, "tools")
if _TOOLS_DIR_BOOTSTRAP not in sys.path:
    sys.path.insert(0, _TOOLS_DIR_BOOTSTRAP)

import pytest

import local_paths
from conftest import REAL_LOG_LABELS
from analyzers import discover_analyzers
from lib.log_reader import LogReader
from lib.vehicle_detect import detect_vehicle
from lib.thresholds import Thresholds


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SNAPSHOT_FILE = os.path.join(_TESTS_DIR, "snapshot_baseline.json")


# The logs behind the snapshots, by label. The files are a maintainer's and
# are named in local_paths.json, see tools/local_paths.py.
_LABELS = (
    # A QuadPlane with 4 lift motors: two VTOL flights and a ground run
    "quadplane_flight",
    "quadplane_ground",
    "quadplane_flight_2",
    # An OctaQuad with an airframe resonance, before the notch and with it
    "octa_notch_off",
    "octa_notch_bw10",
    # "VIBE looks normal but the pilot felt strong vibration": cases where
    # notch_tuning catches the problem in the spectrum. The snapshots hold
    # that behaviour in place.
    "resonance_hover",
    "resonance_dampers_removed",
    "resonance_endurance",
    "resonance_flight",
    "resonance_endurance_heavy",
    # The pusher motor failed in fixed-wing flight
    "pusher_fault",
    # An airspeed sensor that read wrong
    "airspeed_fault",
    # Oscillated on the ground and never took off
    "ground_oscillation",
    # A full mission that lost its link
    "link_loss",
)


def _resolve(label: str) -> str:
    return local_paths.log_for(label)


def _load_snapshots() -> dict:
    """Load the snapshot file, returning {} if missing."""
    if not os.path.isfile(_SNAPSHOT_FILE):
        return {}
    with open(_SNAPSHOT_FILE, encoding="utf-8") as f:
        return json.load(f)


_SNAPSHOTS = _load_snapshots()


# Parametrize over every (log, analyzer) pair recorded in the snapshot.
# Building the list at module-load time means missing logs still produce
# parametrized test items (each will pytest.skip in the test body).
def _build_param_list():
    items = []
    for label, by_analyzer in _SNAPSHOTS.items():
        for analyzer_key, expected in by_analyzer.items():
            items.append((label, analyzer_key, expected))
    return items


_PARAMS = _build_param_list()


@pytest.fixture(scope="module")
def _analyzer_registry():
    """{key: cls} for fast lookup."""
    return dict(discover_analyzers())


@pytest.mark.parametrize(
    "label,analyzer_key,expected",
    _PARAMS,
    ids=[f"{label}::{key}" for label, key, _ in _PARAMS],
)
def test_snapshot_match(label, analyzer_key, expected, _analyzer_registry):
    """Each recorded (log, analyzer) snapshot must still match."""
    log_path = _resolve(label)
    if not os.path.isfile(log_path):
        pytest.skip(f"log not present: {os.path.basename(log_path)}")

    cls = _analyzer_registry.get(analyzer_key)
    if cls is None:
        pytest.fail(
            f"analyzer '{analyzer_key}' is in the snapshot but not in "
            f"the registry — was it removed? Update the snapshot."
        )

    # Fresh reader per analyzer (each analyzer may request different
    # field sets from the same message type).
    reader = LogReader(log_path, disk_cache=True)
    vehicle = detect_vehicle(reader)
    thresholds = Thresholds.from_params(
        reader.params, reader.canonical_vehicle_type)

    analyzer = cls(reader, thresholds, vehicle)
    if not analyzer.can_run():
        # An analyzer that previously ran on this log but no longer
        # can_run is itself a regression worth flagging.
        pytest.fail(
            f"{analyzer_key} previously ran on {label} but now reports "
            f"can_run=False. Investigate or update snapshot."
        )

    result = analyzer.analyze()
    actual = {
        "status": result.status,
        "action_level": result.action_level,
        "param_count": len(result.param_suggestions),
    }

    # Backward compat: old snapshots without param_count default to 0
    expected_full = {
        "status": expected["status"],
        "action_level": expected["action_level"],
        "param_count": expected.get("param_count", 0),
    }

    if actual != expected_full:
        pytest.fail(
            f"\nSnapshot mismatch for {label} / {analyzer_key}:\n"
            f"  expected: {expected_full}\n"
            f"  actual:   {actual}\n"
            f"  summary:  {result.summary[:120]}\n"
            f"\nIf this change is intentional, regenerate snapshots:\n"
            f"  python tests/test_baseline_snapshots.py --update"
        )


def test_snapshot_file_exists():
    """The snapshot file must be present in the test tree."""
    assert os.path.isfile(_SNAPSHOT_FILE), \
        f"snapshot_baseline.json missing at {_SNAPSHOT_FILE}"


def test_every_snapshot_has_a_label():
    """A snapshot under a label nobody lists is never regenerated."""
    assert set(_SNAPSHOTS) == set(_LABELS)


def test_a_machine_with_logs_has_every_one():
    """A label local_paths.json leaves out skips its tests without a word,
    on the one machine that could have run them."""
    if not local_paths.load().get("test_logs"):
        pytest.skip("this machine lists no logs")
    labels = set(_LABELS) | set(REAL_LOG_LABELS)
    missing = sorted(label for label in labels
                     if not os.path.isfile(local_paths.log_for(label)))
    assert not missing, f"not on this machine: {missing}"


def test_snapshot_file_well_formed():
    """The snapshot file must be valid JSON with the expected shape."""
    if not _SNAPSHOTS:
        pytest.skip("snapshot file empty")
    for label, by_analyzer in _SNAPSHOTS.items():
        assert isinstance(by_analyzer, dict), \
            f"{label} must be a dict of analyzer→outputs"
        for key, expected in by_analyzer.items():
            assert "status" in expected, \
                f"{label}/{key} missing 'status'"
            assert "action_level" in expected, \
                f"{label}/{key} missing 'action_level'"
            assert "param_count" in expected, \
                f"{label}/{key} missing 'param_count'"
            assert expected["status"] in (
                "good", "warning", "critical", "info",
            ), f"{label}/{key} bad status {expected['status']}"
            from analyzers import VALID_ACTION_LEVELS
            assert expected["action_level"] in VALID_ACTION_LEVELS, \
                f"{label}/{key} bad action_level {expected['action_level']}"
            assert isinstance(expected["param_count"], int) \
                and expected["param_count"] >= 0, \
                f"{label}/{key} bad param_count {expected['param_count']}"


# ====================================================================
# CLI helper: regenerate snapshots
# ====================================================================

def _regenerate_all_snapshots() -> int:
    """
    Re-run every analyzer on every registered log and overwrite the
    snapshot file. Returns the number of (log, analyzer) entries.
    """
    snapshots = {}
    crashed: list[str] = []
    registry = dict(discover_analyzers())
    for label in _LABELS:
        path = _resolve(label)
        if not os.path.isfile(path):
            print(f"  SKIP {label}: file not found")
            continue
        reader = LogReader(path, disk_cache=True)
        vehicle = detect_vehicle(reader)
        thresholds = Thresholds.from_params(reader.params, reader.vehicle_type)
        snapshots[label] = {}
        for key, cls in registry.items():
            a = cls(reader, thresholds, vehicle)
            if not a.can_run():
                continue
            try:
                r = a.analyze()
                snapshots[label][key] = {
                    "status": r.status,
                    "action_level": r.action_level,
                    "param_count": len(r.param_suggestions),
                }
            except Exception as e:
                # A crash must never be blessed OUT of the baseline
                # (the old code dropped the entry, permanently removing
                # the crashed analyzer from regression coverage).
                # Record it so the snapshot diff shows it, and fail the
                # update run below.
                print(f"  CRASH {label}/{key}: {type(e).__name__}: {e}")
                crashed.append(f"{label}/{key}")
                snapshots[label][key] = {
                    "status": f"CRASH:{type(e).__name__}",
                    "action_level": "n/a",
                    "param_count": 0,
                }
        print(f"  {label}: {len(snapshots[label])} analyzers")

    with open(_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshots, f, indent=2, ensure_ascii=False)

    total = sum(len(v) for v in snapshots.values())
    print(f"Wrote {total} snapshots across {len(snapshots)} logs to "
          f"{_SNAPSHOT_FILE}")
    if crashed:
        print(f"REFUSING a clean bless: {len(crashed)} analyzer "
              f"crash(es): {', '.join(crashed)}")
        sys.exit(1)
    return total


if __name__ == "__main__":
    if "--update" in sys.argv:
        _regenerate_all_snapshots()
    else:
        print(__doc__)
        print("Usage: python test_baseline_snapshots.py --update")
