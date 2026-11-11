"""
Shared fixtures for log analyzer tests.

Provides pre-loaded LogReader + VehicleInfo + Thresholds for each test log,
cached per session so the expensive log parsing happens only once.
"""

import os
import sys
from dataclasses import dataclass

import pytest

# Ensure the log_analysis package is importable
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

_TOOLS_DIR = os.path.join(_SCRIPT_DIR, "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import local_paths
from lib.log_reader import LogReader
from lib.vehicle_detect import detect_vehicle
from lib.thresholds import Thresholds

# ---- Real flight logs ----
# Not in the repository. A machine that has them names them in
# local_paths.json, see tools/local_paths.py. Without it each constant is a
# name that no file has, and the tests that need the log skip.
REAL_LOG_LABELS = (
    "quadplane_flight", "quadplane_ground", "quadplane_flight_2",
    "octa_notch_off", "octa_autotune", "octa_notch_bw10",
)
# The folder they are under, "" without one. The smoke test walks all of it.
REAL_LOG_ROOT = local_paths.log_root()

# A QuadPlane with 4 lift motors: two VTOL flights and a ground run
LOG_QUADPLANE_FLIGHT = local_paths.log_for("quadplane_flight")
LOG_QUADPLANE_GROUND = local_paths.log_for("quadplane_ground")
LOG_QUADPLANE_FLIGHT_2 = local_paths.log_for("quadplane_flight_2")

# An OctaQuad with dual GPS and an airframe resonance: before the notch,
# during autotune, and with the notch at 10 Hz bandwidth
LOG_OCTA_NOTCH_OFF = local_paths.log_for("octa_notch_off")
LOG_OCTA_AUTOTUNE = local_paths.log_for("octa_autotune")
LOG_OCTA_NOTCH_BW10 = local_paths.log_for("octa_notch_bw10")


@dataclass
class LogFixture:
    """Pre-loaded log analysis context."""
    reader: LogReader
    vehicle: object
    thresholds: Thresholds
    path: str


_cache: dict[str, LogFixture] = {}


def _load(path: str) -> LogFixture:
    """Load a log file. Cached for metadata (vehicle/thresholds) only."""
    if path not in _cache:
        if not os.path.isfile(path):
            pytest.skip(f"Log file not found: {path}")
        reader = LogReader(path, disk_cache=True)
        vehicle = detect_vehicle(reader)
        thresholds = Thresholds.from_params(reader.params, reader.vehicle_type)
        _cache[path] = LogFixture(reader, vehicle, thresholds, path)
    return _cache[path]


def run_analyzer(analyzer_cls, log_path: str):
    """Run a single analyzer on a FRESH LogReader to avoid cache conflicts.

    Each analyzer may request different fields from the same message type.
    LogReader caches the field set from the first request, so sharing one
    reader across analyzers causes later reads to return incomplete data.
    """
    if not os.path.isfile(log_path):
        pytest.skip(f"Log file not found: {log_path}")
    # Fresh reader per analyzer — disk_cache makes this fast on repeat runs
    reader = LogReader(log_path, disk_cache=True)
    meta = _load(log_path)
    analyzer = analyzer_cls(reader, meta.thresholds, meta.vehicle)
    if not analyzer.can_run():
        pytest.skip(f"{analyzer_cls.registry_key} cannot run on {log_path}")
    return analyzer.analyze()
