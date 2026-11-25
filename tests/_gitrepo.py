"""Throwaway git repositories, for tests of the release tooling.

release.py and check_release_tag.py ask git about branches, tags and
remotes. A mock only repeats what the test already believes about git's
answers, so these make real ones: a bare `origin` and a clone on `main`,
under tmp_path.

hermetic() keeps the machine running the tests out of them. No global or
system config is read, so none of its identities, hooks or signatures takes
part, and no repository named by the environment is inherited when pytest is
started inside one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

VERSION_FILE = Path("src") / "arduscope" / "__init__.py"

_SET = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "ArduScope tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "ArduScope tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}
_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
         "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT")


def hermetic(monkeypatch) -> None:
    for name, value in _SET.items():
        monkeypatch.setenv(name, value)
    for name in _DROP:
        monkeypatch.delenv(name, raising=False)


def git(cwd, *args: str) -> str:
    """git in cwd; the test fails when git does. Its standard output."""
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def write_version(work, version: str) -> None:
    path = work / VERSION_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def commit(work, message: str) -> None:
    git(work, "commit", "-q", "--allow-empty", "-m", message)


def repository(tmp_path, version: str = "0.0.1") -> tuple[Path, Path]:
    """(work, origin): a clone on main, one commit pushed to a bare origin."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    write_version(work, version)
    git(work, "add", str(VERSION_FILE))
    git(work, "commit", "-q", "-m", "first")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "-u", "origin", "main")
    return work, origin
