"""gate_preflight.py under a real cgroup namespace.

The unit tests lay out a fake /proc and a fake mount table. This file runs the
reader inside `unshare --user --map-root-user --cgroup`, so the kernel writes
the namespaced `/proc/self/cgroup` (`0::/`) and the inherited cgroup2 mount
(`/.. /sys/fs/cgroup`). It is the case `_cgroup_directory` exists for: the
directory at the mount point is an ancestor's, and the process's cgroup is
found by its `cgroup.procs`.

A user namespace is needed because a plain `unshare --cgroup` is refused
without CAP_SYS_ADMIN. Both tests skip where that is not available, as on
Windows and on runners that block unprivileged user namespaces.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest

from tests._repo import REPO_ROOT

PROBE = """
import json, sys
sys.path.insert(0, "tools")
import gate_preflight
print("RESULT:" + json.dumps(gate_preflight.cgroup_memory_limits()))
"""


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=60)


def _namespace_argv() -> list[str]:
    return ["unshare", "--user", "--map-root-user", "--cgroup", "--fork",
            sys.executable, "-c", PROBE]


def _in_namespace(argv: list[str]) -> dict | None:
    """The probe's JSON from inside a cgroup namespace, or None when the
    platform does not allow one."""
    if shutil.which("unshare") is None:
        return None
    probe = _run(["unshare", "--user", "--map-root-user", "--cgroup",
                  "--fork", sys.executable, "-c", "pass"])
    if probe.returncode != 0:
        return None
    result = _run(argv)
    assert result.returncode == 0, result.stderr
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    pytest.fail(f"probe printed no RESULT line: {result.stdout!r} "
                f"{result.stderr!r}")


def test_a_real_namespace_resolves_the_inherited_mount():
    caps = _in_namespace(_namespace_argv())
    if caps is None:
        pytest.skip("user and cgroup namespaces are unavailable")
    assert caps["cgroup"] == "/"
    if caps["memory_max"] is None:
        # No cap: the directory was still found, and the reason names it.
        assert "memory.max is max" in caps["error"]
        assert "under none of the mounted" not in caps["error"]
    else:
        assert caps["memory_max"] > 0


def test_a_real_namespace_reads_a_finite_cap():
    if shutil.which("systemd-run") is None:
        pytest.skip("systemd-run is unavailable")
    probe = _run(["systemd-run", "--user", "--scope",
                  "-p", "MemoryMax=1G", "-p", "MemorySwapMax=0",
                  "--", sys.executable, "-c", "pass"])
    if probe.returncode != 0:
        pytest.skip("a user systemd scope is unavailable: "
                    + probe.stderr.strip())
    caps = _in_namespace(["systemd-run", "--user", "--scope",
                          "-p", "MemoryMax=1G", "-p", "MemorySwapMax=0",
                          "--", *_namespace_argv()])
    if caps is None:
        pytest.skip("user and cgroup namespaces are unavailable")
    assert caps["cgroup"] == "/"
    assert caps["memory_max"] == 1024 ** 3
    assert caps["memory_swap_max"] == 0
