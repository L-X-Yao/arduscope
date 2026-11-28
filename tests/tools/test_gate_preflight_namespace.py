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
import os
import shutil
import signal
import subprocess
import sys
import uuid

import pytest

from tests._repo import REPO_ROOT

PROBE = """
import json, sys
# marker: {marker}
sys.path.insert(0, "tools")
import gate_preflight
print("RESULT:" + json.dumps(gate_preflight.cgroup_memory_limits()))
"""


def _kill_marked(marker: str) -> None:
    """Kill every process whose command line names this run.

    unshare forks helpers and may put the probe in a process group of its
    own, so killing the direct child leaves the probe behind. The marker is
    in the probe's `-c` argument and in unshare's copy of it.
    """
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                command = f.read()
        except OSError:
            continue
        if marker.encode() in command:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def _run(argv: list[str], marker: str | None = None,
         timeout: float = 60) -> subprocess.CompletedProcess:
    """Run a command, and on timeout kill the marked process tree too."""
    proc = subprocess.Popen(argv, cwd=REPO_ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            start_new_session=(os.name == "posix"))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if marker is not None:
            _kill_marked(marker)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _namespace_argv(marker: str) -> list[str]:
    return ["unshare", "--user", "--map-root-user", "--cgroup",
            sys.executable, "-c", PROBE.replace("{marker}", marker)]


def _in_namespace(marker: str, prefix: list[str] | None = None) -> dict | None:
    """The probe's JSON from inside a cgroup namespace, or None when the
    platform does not allow one."""
    if shutil.which("unshare") is None:
        return None
    probe = _run(["unshare", "--user", "--map-root-user", "--cgroup",
                  sys.executable, "-c", "pass"])
    if probe.returncode != 0:
        return None
    result = _run([*(prefix or []), *_namespace_argv(marker)], marker=marker)
    assert result.returncode == 0, result.stderr
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("RESULT:"):
            return json.loads(line[len("RESULT:"):])
    pytest.fail(f"probe printed no RESULT line: {result.stdout!r} "
                f"{result.stderr!r}")


def test_a_real_namespace_resolves_the_inherited_mount():
    caps = _in_namespace(uuid.uuid4().hex)
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
    caps = _in_namespace(uuid.uuid4().hex, prefix=[
        "systemd-run", "--user", "--scope",
        "-p", "MemoryMax=1G", "-p", "MemorySwapMax=0", "--"])
    if caps is None:
        pytest.skip("user and cgroup namespaces are unavailable")
    assert caps["cgroup"] == "/"
    assert caps["memory_max"] == 1024 ** 3
    assert caps["memory_swap_max"] == 0
