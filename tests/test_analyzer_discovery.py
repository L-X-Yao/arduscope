"""discover_analyzers finds every analyzer and nothing else."""

from __future__ import annotations

import os
import py_compile
import shutil
import subprocess
import sys
import warnings

import pytest

import analyzers

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.fast
def test_stale_bytecode_is_not_an_analyzer(monkeypatch):
    """A leftover __pycache__ entry names no importable module: CPython
    never imports from there without the source. Treating one as an
    analyzer only produced a load warning after any analyzer was renamed."""
    real_listdir, real_isdir = os.listdir, os.path.isdir
    ghost = "removed_analyzer.cpython-312.pyc"

    def listdir(path="."):
        names = real_listdir(path) if real_isdir(path) else []
        return names + [ghost] if str(path).endswith("__pycache__") else names

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os.path, "isdir", lambda p: (
        str(p).endswith("__pycache__") or real_isdir(p)))
    assert ghost in os.listdir(
        os.path.join(os.path.dirname(analyzers.__file__), "__pycache__"))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        found = analyzers.discover_analyzers()

    assert found


def test_sourceless_package_is_discovered(tmp_path):
    """The Windows package ships same-directory .pyc files and no .py,
    which is the one compiled layout that has to keep working."""
    for name in ("lib", "locales"):
        shutil.copytree(os.path.join(_REPO, name), tmp_path / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    package = tmp_path / "analyzers"
    shutil.copytree(os.path.join(_REPO, "analyzers"), package,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for source in package.glob("*.py"):
        py_compile.compile(str(source), cfile=str(source) + "c", doraise=True)
        source.unlink()
    assert not list(package.glob("*.py"))

    out = subprocess.run(
        [sys.executable, "-B", "-c",
         "import analyzers; print(len(analyzers.discover_analyzers()))"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60)

    assert out.returncode == 0, out.stderr
    assert int(out.stdout) == len(analyzers.discover_analyzers())
