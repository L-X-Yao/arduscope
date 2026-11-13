"""What the repository calls an entry point must reach every package."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).parent.parent
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
WINDOWS = (ROOT / "build_windows_package.py").read_text(encoding="utf-8")


def _entry_points() -> list[str]:
    """Top-level scripts that define a CLI main()."""
    found = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        has_main = any(isinstance(n, ast.FunctionDef) and n.name == "main"
                       for n in tree.body)
        if has_main and path.stem != "build_windows_package":
            found.append(path.stem)
    return found


def test_the_scan_finds_the_known_entry_points():
    assert {"flight_report", "multi_report", "batch_report", "fleet_report",
            "web_app"} <= set(_entry_points())


def _imports_our_packages(node: ast.stmt) -> bool:
    if isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
    elif isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    else:
        return False
    return any(n.split(".")[0] in ("lib", "analyzers", "locales") for n in names)


def _extends_sys_path(node: ast.stmt) -> bool:
    return isinstance(node, (ast.If, ast.Expr)) and any(
        isinstance(call, ast.Call) and ast.unparse(call.func) == "sys.path.insert"
        for call in ast.walk(node))


@pytest.mark.parametrize("module", _entry_points())
def test_sets_up_sys_path_before_importing_our_packages(module):
    """The embedded Python in the Windows package leaves the script directory
    off sys.path. Running the script there is the only thing that shows it:
    CI starts entry points from a wheel, where the packages are installed."""
    tree = ast.parse((ROOT / f"{module}.py").read_text(encoding="utf-8"))
    imports = [i for i, n in enumerate(tree.body) if _imports_our_packages(n)]
    path_setup = [i for i, n in enumerate(tree.body) if _extends_sys_path(n)]

    assert imports
    assert path_setup
    assert path_setup[0] < imports[0]


@pytest.mark.parametrize("module", _entry_points())
def test_ships_in_the_wheel(module):
    py_modules = re.search(r"py-modules = \[(.*?)\]", PYPROJECT).group(1)
    assert f'"{module}"' in py_modules


@pytest.mark.parametrize("module", _entry_points())
def test_has_a_console_script(module):
    assert f'"{module}:main"' in PYPROJECT


@pytest.mark.parametrize("module", _entry_points())
def test_ships_in_the_windows_package(module):
    files = re.search(r"INCLUDE_FILES = \[(.*?)\]", WINDOWS, re.S).group(1)
    assert f'"{module}.py"' in files
    entry = re.search(r"NUITKA_ENTRY_POINTS = \[(.*?)\]", WINDOWS, re.S).group(1)
    assert f'"{module}.py"' in entry


def test_report_assets_reach_every_package():
    assert "lib/vendor" in re.search(
        r"INCLUDE_DATA_DIRS = \[(.*?)\]", WINDOWS).group(1)
    assert "lib/vendor" in PYPROJECT or "vendor/" in PYPROJECT


def test_declared_python_matches_the_ci_matrix():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r'python-version: \[(.*?)\]', workflow).group(1)
    oldest = re.search(r'"(\d+\.\d+)"', matrix).group(1)
    assert f'requires-python = ">={oldest}"' in PYPROJECT


def test_the_version_lives_in_the_library_package():
    """The root __init__ was an importable module named __init__ in
    site-packages; the version now lives with the package it describes."""
    assert 'version = { attr = "lib.__version__" }' in PYPROJECT
    py_modules = re.search(r"py-modules = \[(.*?)\]", PYPROJECT).group(1)
    assert '"__init__"' not in py_modules
    from lib import __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_the_package_check_sees_subpackages():
    """v0.10.0 shipped without lib/agent because both scans were top-level."""
    import sys
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    import win_package_check

    modules = win_package_check.source_modules(str(ROOT))
    assert "lib/agent/__init__" in modules
    assert "lib/agent/tools" in modules


def test_the_windows_build_copies_subpackages(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    import build_windows_package as builder

    (tmp_path / "lib" / "agent").mkdir(parents=True)
    (tmp_path / "lib" / "agent" / "__init__.py").write_text("")
    (tmp_path / "lib" / "agent" / "tools.py").write_text("")
    (tmp_path / "analyzers").mkdir()
    (tmp_path / "analyzers" / "one.py").write_text("")

    out = tmp_path / "out"
    copied = builder.copy_module_files(str(tmp_path), str(out))

    assert copied == 3
    assert (out / "lib" / "agent" / "__init__.py").is_file()
    assert (out / "lib" / "agent" / "tools.py").is_file()
    assert (out / "analyzers" / "one.py").is_file()
