"""The parse cache must not live inside the package tree."""

import os
from pathlib import Path

import pytest

from arduscope.lib import log_reader


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.skipif(os.environ.get("ARDUSCOPE_LOG_CACHE"),
                    reason="an explicit cache location wins")
def test_module_cache_dir_is_at_the_repository_root():
    assert Path(log_reader._CACHE_DIR) == _repo_root() / ".log_cache"


def test_default_cache_dir_for_a_checkout():
    module_file = Path(log_reader.__file__).resolve()
    assert (Path(log_reader._default_cache_dir(str(module_file)))
            == _repo_root() / ".log_cache")


def test_default_cache_dir_for_an_install(tmp_path, monkeypatch):
    module_file = (tmp_path / "venv/lib/python3.12/site-packages"
                   / "arduscope/lib/log_reader.py")
    module_file.parent.mkdir(parents=True)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert (Path(log_reader._default_cache_dir(str(module_file)))
            == home / ".cache/arduscope/log_cache")
