"""
The parameter and log-field tables behind check_params.py and
check_msg_fields.py are published with the repository. A tree off a
release, such as a fork or a working branch, would put names into them
that upstream ArduPilot does not have, and the checks would then pass
those names. So the generators take only a tree checked out at an
ArduPilot release tag, and each table names the release it came from.
"""

from __future__ import annotations

from tests._repo import REPO_ROOT

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = REPO_ROOT
RELEASE = re.compile(r"(Copter|Plane|Rover|Sub|Tracker|Blimp)-\d+\.\d+\.\d+")
GENERATORS = ["gen_param_table", "gen_msg_field_table"]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path, tag: str | None = None) -> str:
    """A git tree that looks enough like ArduPilot to get past the path
    checks, with HEAD at `tag` when one is given."""
    tree = tmp_path / "ardupilot"
    (tree / "libraries").mkdir(parents=True)
    (tree / "libraries" / "x.cpp").write_text("// empty\n")
    git = ["git", "-C", str(tree), "-c", "user.name=test",
           "-c", "user.email=test@example.invalid"]
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
    if tag:
        subprocess.run(git + ["tag", tag], check=True)
    return str(tree)


@pytest.mark.parametrize("table", ["param_table.json", "msg_field_table.json"])
def test_each_table_names_the_release_it_came_from(table):
    tags = json.loads((REPO / "tools" / table).read_text())["_source"].get("tags")
    assert tags, f"{table} names no release"
    assert all(RELEASE.fullmatch(t) for t in tags), tags


@pytest.mark.parametrize("module", GENERATORS)
@pytest.mark.parametrize("tag, accepted", [
    (None, False),
    ("fork/v1.0", False),
    ("ArduCopter-stable", False),
    ("Copter-4.7.2", True),
    ("Plane-4.7.2", True),
])
def test_only_a_release_tag_is_accepted(tmp_path, module, tag, accepted):
    assert bool(_load(module)._release_tags(_tree(tmp_path, tag))) is accepted


@pytest.mark.parametrize("module", GENERATORS)
def test_a_tree_off_a_release_is_refused_before_writing(tmp_path, monkeypatch,
                                                        module):
    mod = _load(module)
    table = tmp_path / "table.json"
    monkeypatch.setattr(mod, "TABLE_PATH", str(table))
    monkeypatch.setattr("sys.argv", [module, "--ap-path", _tree(tmp_path)])
    try:
        code = mod.main()
    except SystemExit as stop:
        code = stop.code
    assert code not in (0, None)
    assert not table.exists()
