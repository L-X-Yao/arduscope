"""What this machine has that the repository does not.

Flight logs are not in the repository: they belong to whoever flew them. A
maintainer names theirs in local_paths.json at the repository root, which git
ignores, and the tests and tools that need a real log look it up here by
label. Without the file the real-log tests skip, and the release gate and the
Windows check take their logs from the command line.

    {
      "test_logs": {
        "root": "logs",
        "files": {"quadplane_flight": "2026-04/flight1.BIN"}
      },
      "corpora": {"field": "logs", "bench": "../more-logs"},
      "windows_check_log": "logs/2026-04/flight1.BIN",
      "sitl_lock": "/tmp/shared-sitl.lock"
    }

Relative paths start at the repository root. The labels the tests ask for are
in tests/conftest.py and tests/snapshot_baseline.json.
"""

from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE = os.path.join(REPO_ROOT, "local_paths.json")


def load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def corpora() -> dict[str, str]:
    """Corpus name to its folder, as written: the gate runs from the root."""
    return dict(load().get("corpora", {}))


def log_root() -> str:
    """The folder the labelled logs are under, or "" when none is named."""
    root = load().get("test_logs", {}).get("root")
    return os.path.join(REPO_ROOT, root) if root else ""


def log_for(label: str) -> str:
    """Where the log with this label is, or a name that no file has."""
    name = load().get("test_logs", {}).get("files", {}).get(label)
    if name is None:
        return f"{label} (not in local_paths.json)"
    return os.path.join(log_root() or REPO_ROOT, name)
