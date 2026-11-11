"""tools/local_paths.py: the logs a machine has, named outside the repository.

The paths were written in the tests and the tools, so the repository named a
folder and files only one machine has. They are looked up by label now, and a
checkout without the file has to behave like one without the logs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import local_paths  # noqa: E402

pytestmark = pytest.mark.fast


@pytest.fixture
def listed(tmp_path, monkeypatch):
    def write(content: dict) -> None:
        path = tmp_path / "local_paths.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        monkeypatch.setattr(local_paths, "FILE", str(path))
    return write


class TestWithoutTheFile:
    @pytest.fixture(autouse=True)
    def no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_paths, "FILE", str(tmp_path / "none.json"))

    def test_nothing_is_listed(self):
        assert local_paths.load() == {}
        assert local_paths.corpora() == {}
        assert local_paths.log_root() == ""

    def test_a_default_stands(self):
        assert local_paths.get("sitl_lock", "/tmp/x.lock") == "/tmp/x.lock"

    def test_a_label_is_a_name_no_file_has(self):
        path = local_paths.log_for("quadplane_flight")

        assert "quadplane_flight" in path
        assert not os.path.exists(path)


class TestWithTheFile:
    def test_relative_paths_start_at_the_repository(self, listed):
        listed({"test_logs": {"root": "logs",
                              "files": {"quadplane_flight": "day/1.BIN"}}})

        assert local_paths.log_root() == os.path.join(
            local_paths.REPO_ROOT, "logs")
        assert local_paths.log_for("quadplane_flight") == os.path.join(
            local_paths.REPO_ROOT, "logs", "day/1.BIN")

    def test_an_absolute_root_is_kept(self, listed, tmp_path):
        listed({"test_logs": {"root": str(tmp_path),
                              "files": {"quadplane_flight": "1.BIN"}}})

        assert local_paths.log_for("quadplane_flight") == str(
            tmp_path / "1.BIN")

    def test_a_label_left_out_is_still_no_file(self, listed):
        listed({"test_logs": {"root": "logs",
                              "files": {"quadplane_flight": "1.BIN"}}})

        assert not os.path.exists(local_paths.log_for("link_loss"))

    def test_corpora_are_given_as_written(self, listed):
        listed({"corpora": {"field": "logs", "bench": "../more-logs"}})

        assert local_paths.corpora() == {"field": "logs",
                                         "bench": "../more-logs"}

    def test_a_listed_value_replaces_the_default(self, listed):
        listed({"sitl_lock": "/tmp/shared.lock"})

        assert local_paths.get("sitl_lock", "/tmp/x.lock") == "/tmp/shared.lock"

    def test_a_file_that_is_not_json_is_an_error(self, tmp_path, monkeypatch):
        path = tmp_path / "local_paths.json"
        path.write_text("{", encoding="utf-8")
        monkeypatch.setattr(local_paths, "FILE", str(path))

        with pytest.raises(json.JSONDecodeError):
            local_paths.load()


def test_the_repository_does_not_hold_the_file():
    """It names one machine's folders. A tracked copy would put them back in
    the repository."""
    ignored = Path(local_paths.REPO_ROOT, ".gitignore").read_text("utf-8")

    assert "local_paths.json" in ignored.split()
