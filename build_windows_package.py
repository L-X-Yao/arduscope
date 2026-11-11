#!/usr/bin/env python3
"""
Build a standalone Windows distribution package.

Automatically:
1. Downloads the Python embeddable (~12MB, Windows runtime) and checks it
   against the SHA-256 python.org lists, see EMBED_SHA256
2. Installs pip and the libraries pinned in win64-requirements.txt
3. Compiles/protects source code (two modes)
4. Packages as zip (extract and run, no Python install needed)

Source protection modes:

  --nuitka (recommended, requires Windows):
    Python -> C -> native .pyd (machine code), cannot decompile back to Python.
    Requires: Windows + Python 3.10 + Nuitka + MSVC/MinGW.

  Default (.pyc, best compatibility):
    .py -> .pyc, lightweight protection. Can be reversed by decompyle3.
    Suitable for quick testing, not for production delivery.

Usage:
    python build_windows_package.py --nuitka --output arduscope-v12.zip
    python build_windows_package.py                    # fallback: pyc mode
    python build_windows_package.py --python-version 3.14.10

Requirements:
    --nuitka mode: Windows + pip install nuitka ordered-set + MSVC
    Default mode: Linux/Windows, needs internet to download Python embeddable
"""

import argparse
import glob
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.request
import zipfile

# Ensure UTF-8 output on Windows CI (default cp1252 cannot print Chinese)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PACKAGE_NAME = "arduscope"
# Top-level files to include
INCLUDE_FILES = [
    "web_app.py",
    "flight_report.py",
    "multi_report.py",
    "batch_report.py",
    "fleet_report.py",
    "__init__.py",
]
# Files needed during build but NOT shipped in the package
_BUILD_ONLY_FILES: list[str] = []
# Subdirectories to include (all .py files are copied automatically)
INCLUDE_DIRS = ["lib", "analyzers"]
# Data directories to copy verbatim (all files, preserving structure)
INCLUDE_DATA_DIRS = ["locales", "lib/vendor"]

# The Python the package carries. 3.12 stopped at 3.12.10: its later releases
# are source only, with no embeddable. The runner that builds the release has
# to be the same minor version, see release.yml.
DEFAULT_PYTHON = "3.14.10"
# The SHA-256 python.org lists for python-<version>-embed-amd64.zip, from
# https://www.python.org/api/v2/downloads/release_file/ (sha256_sum). A version
# that is not here is refused.
EMBED_SHA256 = {
    "3.14.10": "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159",
}
REQUIREMENTS = "win64-requirements.txt"
# Needed by a library in the package, by its metadata, and left out on purpose.
# 0.9.9 shipped without both and passed its check on Windows.
_NOT_BUNDLED = {
    "fastcrc": "pymavlink falls back to its own CRC code without it",
    "lxml": "pymavlink imports it only in DFReader.metadata_tree and mavgen, "
            "which ArduScope does not call",
}

# Nuitka compilation: entry points get compiled as --standalone executables,
# library packages get compiled as --module .pyd extensions.
NUITKA_ENTRY_POINTS = ["web_app.py", "flight_report.py", "batch_report.py",
                       "multi_report.py", "fleet_report.py"]
NUITKA_MODULES = ["lib", "analyzers"]

# Launcher bat: no first-run compilation needed (all .pyc pre-built)
_LAUNCHER_BAT = r"""@echo off
chcp 65001 >nul 2>&1
title ArduScope

:: Run from the batch's own directory using relative paths. The old
:: short-path (%~dps0) approach failed to resolve long-named subdirs like
:: log_analysis on volumes with 8.3 quirks: python\ has an 8.3-safe name
:: and was found, but log_analysis\ (12 chars) was not. pushd handles
:: spaces / parens / dots in the extracted path.
pushd "%~dp0"

set "LOG=%CD%\startup.log"
echo ArduScope - %date% %time% > "%LOG%"
echo Working dir: %CD% >> "%LOG%"
echo. >> "%LOG%"

:: Check Python runtime
set "PYTHON=python\python.exe"
echo [CHECK] Python: %PYTHON% >> "%LOG%"
if not exist "%PYTHON%" (
    echo [FAIL] python.exe not found >> "%LOG%"
    echo.
    echo [ERROR] Python runtime not found.
    echo         Please reinstall the package.
    echo.
    echo Log: %LOG%
    goto :end
)
echo [OK] Python found >> "%LOG%"

:: Check compiled entry point
set "ENTRY=log_analysis\web_app.pyc"
if not exist "%ENTRY%" (
    echo [FAIL] web_app.pyc not found >> "%LOG%"
    echo.
    echo [ERROR] Package is incomplete or corrupted.
    echo         Please reinstall the package.
    echo.
    echo Log: %LOG%
    goto :end
)
echo [LAUNCH] Entry: %ENTRY% >> "%LOG%"

echo.
echo ============================================
echo   ArduScope
echo   Close this window to stop the service
echo ============================================
echo.
echo   Port: 8080  (use: start.bat --port 9090 to change)
echo.
"%PYTHON%" "%ENTRY%" --report-dir "reports" %* 2>>"%LOG%"
if errorlevel 1 goto :failed
goto :end

:failed
echo [FAIL] Python exited with error >> "%LOG%"
echo.
echo ============================================
echo   Startup Failed!
echo ============================================
echo.
echo   Startup log: %LOG%
echo.
echo --- Error details ---
type "%LOG%"
echo.

:end
popd
pause
"""


def download(url: str, desc: str) -> bytes:
    """Download a URL with progress."""
    print(f"  Downloading {desc}...")
    print(f"    {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "arduscope-builder/1.0"})
    resp = urllib.request.urlopen(req)
    data = resp.read()
    print(f"    Done ({len(data) / 1024 / 1024:.1f} MB)")
    return data


def check_embed(data: bytes, py_ver: str) -> None:
    """Stop unless data is the embeddable python.org lists for py_ver."""
    expected = EMBED_SHA256.get(py_ver)
    if expected is None:
        raise RuntimeError(
            f"No SHA-256 on record for Python {py_ver}: add the one python.org "
            f"lists for python-{py_ver}-embed-amd64.zip to EMBED_SHA256")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"python-{py_ver}-embed-amd64.zip has SHA-256 {actual}, "
            f"python.org lists {expected}")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_requirements(path: str) -> dict[str, str]:
    """{name: version} of the `name==version` pins in a requirements file."""
    pins = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", line)
            if m:
                pins[_canonical(m.group(1))] = m.group(2)
    return pins


def _load_pyproject(script_dir: str) -> dict:
    try:
        import tomllib
    except ImportError:
        raise RuntimeError("Reading pyproject.toml needs Python 3.11 or later")
    with open(os.path.join(script_dir, "pyproject.toml"), "rb") as f:
        return tomllib.load(f)


def check_requirements(pins: dict[str, str], pyproject: dict) -> list[str]:
    """What in pyproject's dependencies and web extra the pins do not meet."""
    from packaging.requirements import Requirement

    project = pyproject["project"]
    wanted = (list(project["dependencies"])
              + project.get("optional-dependencies", {}).get("web", []))
    problems = []
    for text in wanted:
        req = Requirement(text)
        name = _canonical(req.name)
        if name not in pins:
            problems.append(f"{name} is not pinned")
        elif not req.specifier.contains(pins[name], prereleases=True):
            problems.append(f"{name}=={pins[name]} does not satisfy {text}")
    return problems


def _windows_markers(py_ver: str) -> dict[str, str]:
    """Marker values of the bundled Python on 64-bit Windows."""
    return {
        "os_name": "nt", "sys_platform": "win32", "platform_system": "Windows",
        "platform_machine": "AMD64", "platform_python_implementation": "CPython",
        "implementation_name": "cpython", "implementation_version": py_ver,
        "python_version": ".".join(py_ver.split(".")[:2]),
        "python_full_version": py_ver, "extra": "",
    }


def check_closure(site_packages: str, py_ver: str) -> list[str]:
    """Requirements of the installed libraries that are not installed.

    --no-deps installs only what is pinned, so a library that gains a
    dependency would ship without it. _NOT_BUNDLED names the ones left out
    on purpose.
    """
    from packaging.requirements import Requirement

    metadata = glob.glob(os.path.join(site_packages, "*.dist-info", "METADATA"))
    installed = {}
    for path in metadata:
        folder = os.path.basename(os.path.dirname(path))
        installed[_canonical(folder[:-len(".dist-info")].rsplit("-", 1)[0])] = path
    env = _windows_markers(py_ver)
    problems = []
    for name, path in sorted(installed.items()):
        with open(path, encoding="utf-8") as f:
            lines = [line[len("Requires-Dist:"):].strip() for line in f
                     if line.startswith("Requires-Dist:")]
        for text in lines:
            req = Requirement(text)
            if req.marker is not None and not req.marker.evaluate(env):
                continue
            needed = _canonical(req.name)
            if needed not in installed and needed not in _NOT_BUNDLED:
                problems.append(f"{name} needs {needed}, which is not pinned")
    return problems


def _get_compiler(py_ver: str, build_dir: str) -> str:
    """
    Return path to a Python executable matching py_ver (e.g. "3.10.12").

    Search order:
      1. System python3.X matching the minor version
      2. Raise error with install instructions

    Only called on Linux/macOS. On Windows the embedded python.exe is used.
    """
    major, rest = py_ver.split(".", 1)
    minor = rest.split(".")[0]          # "10" from "10.12"

    # Check system Python
    for cmd in [f"python{major}.{minor}", f"python{major}", "python3"]:
        path = shutil.which(cmd)
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, "-c",
                 f"import sys; exit(0 if sys.version_info[:2] == ({major},{minor}) else 1)"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                print(f"  Using system Python {major}.{minor}: {path}")
                return path
        except Exception:
            continue

    # No system Python matches the target minor version. Compiling .pyc
    # with a different minor version produces a package that cannot run on
    # the bundled interpreter (it fails with "bad magic number" on launch),
    # so fail loudly instead of silently shipping a broken build. (The old
    # code warned and used the mismatched interpreter anyway.)
    current = shutil.which("python3") or shutil.which("python")
    cur_ver = "?"
    if current:
        result = subprocess.run(
            [current, "-c",
             "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=5,
        )
        cur_ver = result.stdout.strip() or "?"

    raise RuntimeError(
        f"No Python {major}.{minor} interpreter found to compile .pyc for "
        f"the bundled Python {major}.{minor} embeddable. Compiling with a "
        f"different minor version yields a package that fails with 'bad "
        f"magic number' on Windows.\n"
        f"Fix one of:\n"
        f"  - install the matching interpreter:\n"
        f"      sudo add-apt-repository ppa:deadsnakes/ppa\n"
        f"      sudo apt install python{major}.{minor}\n"
        f"  - or build for your host Python (bundles a matching embeddable\n"
        f"    so the .pyc runs):\n"
        f"      python build_windows_package.py --python-version {cur_ver}.<patch>"
    )


def _precompile(la_dir: str, py_exe: str) -> int:
    """
    Compile all .py files in la_dir to same-dir .pyc using py_exe.

    Every .py → .pyc in the same directory (e.g. lib/log_reader.py →
    lib/log_reader.pyc).  Python's SourcelessFileLoader finds these directly
    without needing any .py stub.  __init__.py → __init__.pyc keeps the
    directory recognised as a regular package.

    All .py source files are deleted after compilation.
    No __pycache__/ directories are created or kept.

    Returns the number of .py files deleted.
    """
    compile_code = textwrap.dedent(f"""\
        import os, py_compile
        la_dir = {repr(la_dir)}
        mode = py_compile.PycInvalidationMode.UNCHECKED_HASH

        for root, _dirs, files in os.walk(la_dir):
            for f in sorted(files):
                if f.endswith(".py"):
                    src = os.path.join(root, f)
                    py_compile.compile(src, cfile=src + "c", doraise=True,
                                       invalidation_mode=mode)

        print("compilation_ok")
    """)

    result = subprocess.run(
        [py_exe, "-c", compile_code],
        capture_output=True, text=True,
    )
    if "compilation_ok" not in result.stdout:
        raise RuntimeError(
            f"Compilation failed!\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    # Delete all .py source files — .pyc in same dir are directly importable
    deleted = 0
    for root, _dirs, files in os.walk(la_dir):
        for f in files:
            if f.endswith(".py"):
                os.remove(os.path.join(root, f))
                deleted += 1

    return deleted


def _nuitka_compile_module(module_dir: str, output_dir: str) -> str:
    """
    Compile a package directory to a single .pyd using Nuitka --module.

    Returns the path to the generated .pyd file.
    """
    module_name = os.path.basename(module_dir)
    print(f"    Compiling module {module_name}/ -> {module_name}.pyd ...")

    cmd = [
        sys.executable, "-m", "nuitka",
        "--module",
        "--include-package=" + module_name,
        "--output-dir=" + output_dir,
        "--remove-output",             # clean .build dir after
        "--no-pyi-file",               # no type stub
        module_dir,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(module_dir))
    if result.returncode != 0:
        print(f"    [ERROR] Nuitka compilation of {module_name} failed:")
        print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
        print(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
        raise RuntimeError(f"Nuitka module compilation failed for {module_name}")

    # Find the compiled extension (.pyd on Windows, .so on Linux)
    for f in os.listdir(output_dir):
        if f.startswith(module_name) and (f.endswith(".pyd") or f.endswith(".so")):
            return os.path.join(output_dir, f)

    raise RuntimeError(f"Nuitka produced no .pyd/.so for {module_name}")


def _nuitka_compile_entry(entry_py: str, output_dir: str, extra_paths: list) -> str:
    """
    Compile a single .py entry point to a standalone .exe using Nuitka.

    Returns the path to the generated .exe file.
    """
    entry_name = os.path.splitext(os.path.basename(entry_py))[0]
    print(f"    Compiling entry {entry_name}.py -> {entry_name}.exe ...")

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--onefile",
        "--output-dir=" + output_dir,
        "--remove-output",
        f"--output-filename={entry_name}.exe",
        "--assume-yes-for-downloads",   # auto-download dependency walker if needed
    ]
    # Add module search paths
    for p in extra_paths:
        cmd.append(f"--include-data-dir={p}={os.path.basename(p)}")

    cmd.append(entry_py)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [ERROR] Nuitka compilation of {entry_name} failed:")
        print(result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr)
        raise RuntimeError(f"Nuitka standalone compilation failed for {entry_name}")

    exe_path = os.path.join(output_dir, f"{entry_name}.exe")
    if not os.path.exists(exe_path):
        # search subdirs
        for root, _dirs, files in os.walk(output_dir):
            for f in files:
                if f == f"{entry_name}.exe":
                    return os.path.join(root, f)
        raise RuntimeError(f"Nuitka produced no .exe for {entry_name}")
    return exe_path


def _build_nuitka(script_dir: str, pkg_dir: str, py_ver: str):
    """
    Nuitka protected build — compile lib/ and analyzers/ to .pyd,
    entry points remain as thin .pyc wrappers that import the .pyd modules.

    Strategy:
      - lib/ → lib.cpython-310-x86_64.pyd (single compiled extension)
      - analyzers/ → analyzers.cpython-310-x86_64.pyd
      - Entry points (web_app.py etc.) → .pyc (tiny, just "import lib; main()")
        The actual logic is in the .pyd which is native machine code.
    """
    la_dir = os.path.join(pkg_dir, "log_analysis")
    nuitka_out = os.path.join(pkg_dir, "_nuitka_build")
    os.makedirs(nuitka_out, exist_ok=True)

    # Compile each package to .pyd
    compiled_pyds = []
    for mod in NUITKA_MODULES:
        mod_src = os.path.join(la_dir, mod)
        if os.path.isdir(mod_src):
            pyd_path = _nuitka_compile_module(mod_src, nuitka_out)
            compiled_pyds.append(pyd_path)
            # Remove the source directory
            shutil.rmtree(mod_src)

    # Move .pyd files into la_dir
    for pyd in compiled_pyds:
        shutil.move(pyd, la_dir)

    # Entry points: compile to .pyc (they're thin wrappers, logic is in .pyd)
    # This is fine — entry points just do `from lib.xxx import main; main()`
    # which routes into the native .pyd immediately.
    py_exe = sys.executable
    for entry in NUITKA_ENTRY_POINTS:
        entry_path = os.path.join(la_dir, entry)
        if os.path.exists(entry_path):
            subprocess.run(
                [py_exe, "-c",
                 f"import py_compile; py_compile.compile({repr(entry_path)}, "
                 f"cfile={repr(entry_path + 'c')}, doraise=True)"],
                check=True, capture_output=True,
            )
            os.remove(entry_path)

    # Clean up nuitka build dir
    shutil.rmtree(nuitka_out, ignore_errors=True)

    # Count results
    pyd_count = len([f for f in os.listdir(la_dir) if f.endswith(".pyd")])
    pyc_count = len([f for f in os.listdir(la_dir) if f.endswith(".pyc")])
    print(f"  Nuitka done: {pyd_count} .pyd modules + {pyc_count} .pyc entries")


def main():
    parser = argparse.ArgumentParser(description="Build standalone Windows package")
    parser.add_argument(
        "--python-version", default=DEFAULT_PYTHON,
        help="Python embeddable version, one of EMBED_SHA256 "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output zip filename (default: arduscope-vX.zip)",
    )
    parser.add_argument(
        "--nuitka", action="store_true",
        help="Compile core modules to native .pyd with Nuitka (requires Windows + MSVC)",
    )
    parser.add_argument(
        "--embed-zip",
        help="Path to a pre-downloaded Python embeddable zip (skips download)",
    )
    args = parser.parse_args()
    if args.python_version not in EMBED_SHA256:
        parser.error(f"no SHA-256 on record for Python {args.python_version}, "
                     f"see EMBED_SHA256")

    if args.nuitka and platform.system() != "Windows":
        print("Error: --nuitka requires Windows (Nuitka does not support cross-compilation)")
        print("Hint: use GitHub Actions workflow 'ArduScope Package' for automated builds")
        sys.exit(1)

    if args.nuitka:
        # Verify nuitka is installed
        try:
            subprocess.run(
                [sys.executable, "-m", "nuitka", "--version"],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Error: Nuitka not installed. Run: pip install nuitka ordered-set")
            sys.exit(1)

    py_ver = args.python_version
    py_ver_short = py_ver.rsplit(".", 1)[0].replace(".", "")  # "312"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Build in local /tmp to avoid Samba/NFS directory-cache issues where
    # files created by a subprocess are not visible to the parent's os.walk.
    import tempfile
    build_dir = tempfile.mkdtemp(prefix=f"_{PACKAGE_NAME}_build_")
    pkg_dir = os.path.join(build_dir, PACKAGE_NAME)
    os.makedirs(pkg_dir)

    # ---- Step 1: Download Python embeddable (Windows runtime) ----
    if args.embed_zip and os.path.isfile(args.embed_zip):
        print(f"\n[1/6] Using local Python embeddable: {args.embed_zip}")
        with open(args.embed_zip, "rb") as f:
            py_zip_data = f.read()
    else:
        print(f"\n[1/6] Downloading Python {py_ver} embeddable (Windows runtime)...")
        py_url = (
            f"https://www.python.org/ftp/python/{py_ver}/"
            f"python-{py_ver}-embed-amd64.zip"
        )
        py_zip_data = download(py_url, "Python embeddable")
    check_embed(py_zip_data, py_ver)
    print(f"  SHA-256 matches python.org's")

    python_dir = os.path.join(pkg_dir, "python")
    os.makedirs(python_dir)
    with zipfile.ZipFile(io.BytesIO(py_zip_data)) as zf:
        zf.extractall(python_dir)
    print(f"  Extracting to {python_dir}")

    # ---- Step 2: Enable pip (modify ._pth file) ----
    print(f"\n[2/6] Enabling pip...")
    pth_file = os.path.join(python_dir, f"python{py_ver_short}._pth")
    if os.path.exists(pth_file):
        with open(pth_file, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("#import site", "import site")
        with open(pth_file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  Modified {os.path.basename(pth_file)}: enabled import site")
    else:
        print(f"  Warning: {pth_file} not found")

    # ---- Step 3: Install pip ----
    print(f"\n[3/6] Installing pip...")
    python_exe = os.path.join(python_dir, "python.exe")

    if platform.system() == "Windows":
        getpip_url = "https://bootstrap.pypa.io/get-pip.py"
        getpip_data = download(getpip_url, "get-pip.py")
        getpip_path = os.path.join(build_dir, "get-pip.py")
        with open(getpip_path, "wb") as f:
            f.write(getpip_data)
        subprocess.run(
            [python_exe, getpip_path, "--no-warn-script-location", "-q"],
            check=True,
        )
        print("  pip installed")
    else:
        print("  Skipped (using system pip on Linux)")

    # ---- Step 4: Install the pinned libraries ----
    print(f"\n[4/6] Installing the libraries in {REQUIREMENTS}...")
    target_dir = os.path.join(python_dir, "Lib", "site-packages")
    requirements = os.path.join(script_dir, REQUIREMENTS)
    pins = read_requirements(requirements)
    problems = check_requirements(pins, _load_pyproject(script_dir))
    if problems:
        raise RuntimeError(f"{REQUIREMENTS} does not meet pyproject.toml: "
                           + "; ".join(problems))

    # Exactly the pinned files: every pin carries its wheel's SHA-256.
    pip_install = ["install", "--require-hashes", "--no-deps",
                   "-r", requirements, "--no-warn-script-location", "-q"]
    if platform.system() == "Windows":
        subprocess.run([python_exe, "-m", "pip", *pip_install], check=True)
    else:
        subprocess.run(
            [sys.executable, "-m", "pip", *pip_install,
             "--platform", "win_amd64",
             "--target", target_dir,
             "--python-version", py_ver_short,
             "--only-binary", ":all:"],
            check=True,
        )
    problems = check_closure(target_dir, py_ver)
    if problems:
        raise RuntimeError("; ".join(problems))
    print(f"  {len(pins)} libraries installed, nothing they need is missing")

    # ---- Step 5: Copy tool code ----
    print(f"\n[5/6] Copying tool code...")
    la_dir = os.path.join(pkg_dir, "log_analysis")
    os.makedirs(la_dir, exist_ok=True)

    # Copy top-level files
    for fname in INCLUDE_FILES:
        src = os.path.join(script_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, la_dir)
        else:
            print(f"  Warning: {fname} not found")

    # Copy subdirectories (all .py files, auto-discovers new analyzers)
    file_count = 0
    for subdir in INCLUDE_DIRS:
        src_dir = os.path.join(script_dir, subdir)
        dst_dir = os.path.join(la_dir, subdir)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in sorted(os.listdir(src_dir)):
            if fname.endswith(".py"):
                shutil.copy2(os.path.join(src_dir, fname), dst_dir)
                file_count += 1

    # Copy data directories (locales, etc.) preserving structure
    data_count = 0
    for subdir in INCLUDE_DATA_DIRS:
        src_dir = os.path.join(script_dir, subdir)
        dst_dir = os.path.join(la_dir, subdir)
        if os.path.isdir(src_dir):
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__init__.py"))
            for root, _dirs, files in os.walk(dst_dir):
                data_count += len(files)
    print(f"  Copied {len(INCLUDE_FILES)} entry files + {file_count} module files + {data_count} data files")

    # ---- Step 6: Compile and protect source ----
    if args.nuitka:
        print(f"\n[6/6] Nuitka compiling core modules to native .pyd...")
        _build_nuitka(script_dir, pkg_dir, py_ver)
    else:
        print(f"\n[6/6] Compiling source (.py -> .pyc)...")

        if platform.system() == "Windows":
            # On Windows: use the downloaded embedded Python to compile
            compile_code = textwrap.dedent(f"""\
                import os, py_compile
                la_dir = {repr(la_dir)}
                mode = py_compile.PycInvalidationMode.UNCHECKED_HASH
                for root, _dirs, files in os.walk(la_dir):
                    for f in sorted(files):
                        if f.endswith(".py"):
                            src = os.path.join(root, f)
                            py_compile.compile(src, cfile=src + "c", doraise=True,
                                               invalidation_mode=mode)
                print("compilation_ok")
            """)
            result = subprocess.run(
                [python_exe, "-c", compile_code],
                capture_output=True, text=True,
            )
            if "compilation_ok" not in result.stdout:
                raise RuntimeError(f"Compilation failed: {result.stderr}")
            deleted = 0
            for root, _dirs, files in os.walk(la_dir):
                for f in files:
                    if f.endswith(".py"):
                        os.remove(os.path.join(root, f))
                        deleted += 1
        else:
            # On Linux/macOS: find or download Python matching the embedded version
            compiler = _get_compiler(py_ver, build_dir)
            deleted = _precompile(la_dir, compiler)

        print(f"  Compiled, {deleted} .py files deleted (compiled to .pyc)")

    # Create launcher
    bat_path = os.path.join(pkg_dir, "start.bat")
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(_LAUNCHER_BAT)

    # Create reports directory
    os.makedirs(os.path.join(pkg_dir, "reports"), exist_ok=True)

    # Create README
    readme_path = os.path.join(pkg_dir, "README.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(
            "ArduScope - ArduPilot Flight Log Analysis Tool\n"
            "=======================================\n\n"
            "Usage:\n"
            "  1. Double-click start.bat\n"
            "  2. Browser opens analysis interface\n"
            "  3. Drag .BIN log files onto the page\n"
            "  4. Wait for analysis, click View Report\n\n"
            "Reports saved in reports/ directory\n\n"
            "Licence: MIT, see LICENSE.txt. Python's is python/LICENSE.txt\n"
            "and each library's is in its .dist-info folder under\n"
            "python/Lib/site-packages.\n"
            "Source: https://github.com/L-X-Yao/arduscope\n"
        )
    shutil.copy2(os.path.join(script_dir, "LICENSE"),
                 os.path.join(pkg_dir, "LICENSE.txt"))

    # ---- Zip ----
    raw_output = args.output or f"{PACKAGE_NAME}-v5.zip"
    if os.path.dirname(raw_output):
        output_path = os.path.abspath(raw_output)
    else:
        output_path = os.path.join(script_dir, "dist", raw_output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    output_name = os.path.basename(output_path)

    # Diagnostic: verify .pyc files in analyzers/ before zipping
    _ana_dir = os.path.join(la_dir, "analyzers")
    _pyc_count = len([f for f in os.listdir(_ana_dir) if f.endswith(".pyc")]) if os.path.isdir(_ana_dir) else 0
    print(f"  [diag] analyzers/*.pyc: {_pyc_count} files")

    print(f"\nPackaging as {output_name}...")
    shutil.make_archive(
        os.path.splitext(output_path)[0],
        "zip",
        build_dir,
        PACKAGE_NAME,
    )

    # Size
    zip_size = os.path.getsize(output_path)
    print(f"\nDone! {output_path}")
    print(f"Size: {zip_size / 1024 / 1024:.1f} MB")
    print(f"\nDistribute: send {output_name}, extract and run start.bat")
    if args.nuitka:
        print("Protection: Nuitka native compilation (.pyd machine code)")
    else:
        print("Protection: .pyc bytecode (basic, reversible with decompyle3)")
        print("Tip: use --nuitka on Windows for strongest protection")

    # Cleanup
    shutil.rmtree(build_dir)


if __name__ == "__main__":
    main()
