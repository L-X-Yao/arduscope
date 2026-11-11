#!/usr/bin/env python3
"""
Check a Windows package on real Windows, from WSL.

Linux will delete a file a program still has open, so a leaked handle cannot
show in CI, and neither can Windows paths or the embedded Python's import
rules. Running v0.8.6 this way crashed its server twice and left five uploads
behind; v0.9.0 had one left. Both had a green CI.

This unzips the package under the Windows temp directory and drives it with
its own python.exe: every entry point starts, the four CLI tools run on a real
log, the web server takes eleven uploads, three of them damaged, one of them
2 MB of zeros, and four under names Windows cannot store, and nothing is left
behind.

Usage:
    python3 tools/win_package_check.py --release v0.9.3
    python3 tools/win_package_check.py --zip dist/x.zip --version 0.9.3

The result goes to reports/release-check/<version>.json, or to
<version>-local.json for --zip, so a local build cannot stand in for the
published package. The whole run takes about 15 minutes, so start it in the
background.

Exit 0 = every check passed; 1 = at least one did not.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import local_paths  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_DIR = os.path.join(REPO_ROOT, "reports", "release-check")
DRIVER = os.path.join(REPO_ROOT, "tools", "win_package_driver.py")
DEFAULT_PLANE_LOG = "tests/sitl/logs/plane_tecs_tuning.BIN"

ENTRY_POINTS = ("web_app", "flight_report", "multi_report", "batch_report",
                "fleet_report")

# What the web server must do with each upload. The damaged logs can go either
# way, as it depends on which bytes were hit: they must not take it down.
UPLOADS = {
    "normal.BIN": "done",
    "中文 名称.BIN": "done",
    "random.BIN": "error",
    # pymavlink printed a line to the console for each of these bytes.
    "zeros.BIN": "error",
    "truncated.BIN": None,
    "flipped.BIN": None,
    "head7.BIN": None,
    # Names Windows cannot store, sent by the driver with the first log's
    # bytes. See ODD_NAMES in win_package_driver.py.
    "odd name, colon": "done",
    "odd name, NUL": "done",
    "odd name, reserved characters": "done",
    "odd name, 80 Chinese characters": "done",
}


def package_modules(names: list[str]) -> set[str]:
    """Modules in a package zip, as `lib/results`, from its .pyc files."""
    prefix = "arduscope/log_analysis/"
    return {name[len(prefix):-len(".pyc")] for name in names
            if name.startswith(prefix) and name.endswith(".pyc")}


def source_modules(root: str = REPO_ROOT) -> set[str]:
    """Modules the package has to ship, as `lib/results`."""
    found = set()
    for folder in ("lib", "analyzers"):
        for path in glob.glob(os.path.join(root, folder, "*.py")):
            found.add(f"{folder}/{os.path.basename(path)[:-3]}")
    return found


def judge_uploads(lines: list[dict]) -> list[dict]:
    """One check per upload, from the driver's report."""
    by_case = {line["case"]: line for line in lines if "case" in line}
    checks = []
    for name, expected in UPLOADS.items():
        line = by_case.get(name)
        if line is None:
            checks.append(_check(f"upload {name}", False, "no result"))
            continue
        problems = []
        status = line.get("status")
        if status not in ("done", "error"):
            problems.append(f"status {status}")
        elif expected and status != expected:
            problems.append(f"status {status}, expected {expected}")
        if line.get("uploads_left"):
            problems.append(f"left {line['uploads_left']}")
        if not line.get("server_alive"):
            problems.append("server died")
        checks.append(_check(f"upload {name}", not problems,
                             "; ".join(problems) or status))
    return checks


def _check(name: str, ok: bool, detail: object = "") -> dict:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return {"check": name, "ok": ok, "detail": detail}


def _flip(data: bytes, rng: random.Random, count: int) -> bytes:
    damaged = bytearray(data)
    for _ in range(count):
        damaged[rng.randrange(len(damaged))] ^= 1 << rng.randrange(8)
    return bytes(damaged)


def write_inputs(log_path: str, inputs: str, batch: str) -> None:
    with open(log_path, "rb") as f:
        data = f.read()
    rng = random.Random(1107)
    cases = {
        "normal.BIN": data,
        "中文 名称.BIN": data,
        "truncated.BIN": data[:len(data) // 2],
        "flipped.BIN": _flip(data, rng, 20000),
        "head7.BIN": data[7:],
        "random.BIN": rng.randbytes(200_000),
        "zeros.BIN": bytes(2_000_000),
    }
    for name, content in cases.items():
        with open(os.path.join(inputs, name), "wb") as f:
            f.write(content)
    for name in ("normal.BIN", "truncated.BIN", "flipped.BIN"):
        shutil.copy(os.path.join(inputs, name), os.path.join(batch, name))


def windows_path(path: str) -> str:
    return subprocess.run(["wslpath", "-w", path], check=True,
                          capture_output=True, text=True).stdout.strip()


def windows_temp() -> str:
    """The Windows temp directory, as a WSL path.

    %TEMP% can come back in 8.3 form, ADMINI~1, and a user's package never
    runs from one of those: the launcher has failed on 8.3 paths before.
    """
    out = subprocess.run(["cmd.exe", "/c", "echo %LOCALAPPDATA%"], cwd="/mnt/c",
                         check=True, capture_output=True, text=True)
    local = subprocess.run(["wslpath", "-u", out.stdout.strip()], check=True,
                           capture_output=True, text=True).stdout.strip()
    return os.path.join(local, "Temp")


def fetch_release(tag: str, into: str) -> str:
    name = f"arduscope-{tag}-win64.zip"
    subprocess.run(["gh", "release", "download", tag, "-p", name, "--clobber",
                    "--dir", into], cwd=REPO_ROOT, check=True)
    return os.path.join(into, name)


def check_zip(path: str, version: str) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        init = z.read("arduscope/log_analysis/__init__.pyc")
    missing = sorted(source_modules() - package_modules(names))
    return [
        _check("version in package", version.encode() in init, version),
        _check("every module packaged", not missing, missing[:5]),
        _check("licence in package", "arduscope/LICENSE.txt" in names),
    ]


def _python(package: str, args: list[str], timeout: int) -> int:
    try:
        return subprocess.run(
            ["./python/python.exe", *args], cwd=package, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except subprocess.TimeoutExpired:
        return -1


def check_entry_points(package: str) -> list[dict]:
    checks = []
    for name in ENTRY_POINTS:
        code = _python(package, [f"log_analysis/{name}.pyc", "--help"], 60)
        checks.append(_check(f"start {name}", code == 0, f"exit {code}"))
    return checks


def check_cli(package: str, work: str, plane_log: str) -> list[dict]:
    cli = os.path.join(work, "cli")
    os.makedirs(cli)
    win = windows_path
    normal = win(os.path.join(work, "inputs", "normal.BIN"))
    truncated = win(os.path.join(work, "inputs", "truncated.BIN"))
    plane = shutil.copy(plane_log, cli)
    runs = {
        "flight_report": (["--no-open", "-o", win(cli) + "\\report.html",
                           normal], "report.html"),
        "multi_report": (["--no-open", "-o", win(cli) + "\\multi.html",
                          normal, truncated], "multi.html"),
        "batch_report": ([win(os.path.join(work, "batch")), "-o",
                          win(os.path.join(work, "batch_out")), "--no-open"],
                         None),
        "fleet_report": (["--db", win(cli) + "\\fleet.db", "--list"], None),
    }
    checks = []
    for name, (args, output) in runs.items():
        code = _python(package, [f"log_analysis/{name}.pyc", *args], 240)
        wrote = output is None or os.path.exists(os.path.join(cli, output))
        checks.append(_check(f"run {name}", code == 0 and wrote,
                             f"exit {code}"))
    code = _python(package, ["log_analysis/flight_report.pyc", "--no-open",
                             "-o", win(cli) + "\\plane.html", win(plane)], 240)
    checks.append(_check("run flight_report on a Plane log", code == 0,
                         f"exit {code}"))
    return checks


def check_web(package: str, work: str, port: int) -> list[dict]:
    win = windows_path
    driver = shutil.copy(DRIVER, work)
    reports = os.path.join(work, "reports")
    os.makedirs(reports)
    results = os.path.join(work, "uploads.jsonl")
    subprocess.run(
        [os.path.join(package, "python", "python.exe"), win(driver),
         win(package), win(reports), win(os.path.join(work, "inputs")),
         str(port), win(results)],
        cwd=work, timeout=900, stdout=subprocess.DEVNULL)
    with open(results, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    busy = any(line.get("event") == "port_busy" for line in lines)
    return ([_check(f"port {port} free", not busy)]
            + judge_uploads(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--release", metavar="TAG", help="download this release")
    source.add_argument("--zip", help="a package built locally")
    parser.add_argument("--version", help="required with --zip")
    parser.add_argument("--log", default=local_paths.get("windows_check_log"),
                        help="a real flight log, may be a glob. Default: "
                             "windows_check_log in local_paths.json")
    parser.add_argument("--plane-log", default=DEFAULT_PLANE_LOG)
    parser.add_argument("--port", type=int, default=18770)
    args = parser.parse_args(argv)
    if args.zip and not args.version:
        parser.error("--zip needs --version")
    version = args.version or args.release.lstrip("v")

    if not args.log:
        parser.error("name a flight log with --log, or as windows_check_log "
                     "in local_paths.json")
    logs = sorted(glob.glob(os.path.join(REPO_ROOT, args.log)))
    plane_log = os.path.join(REPO_ROOT, args.plane_log)
    if not logs or not os.path.exists(plane_log):
        parser.error("flight logs not found, see --log and --plane-log")

    work = tempfile.mkdtemp(prefix="arduscope-check-", dir=windows_temp())
    checks: list[dict] = []
    with tempfile.TemporaryDirectory() as download:
        path = args.zip or fetch_release(args.release, download)
        checks += check_zip(path, version)
        if all(c["ok"] for c in checks):
            for folder in ("inputs", "batch", "package"):
                os.makedirs(os.path.join(work, folder))
            write_inputs(logs[0], os.path.join(work, "inputs"),
                         os.path.join(work, "batch"))
            with zipfile.ZipFile(path) as z:
                z.extractall(os.path.join(work, "package"))
            package = os.path.join(work, "package", "arduscope")
            checks += check_entry_points(package)
            checks += check_cli(package, work, plane_log)
            checks += check_web(package, work, args.port)

    shutil.rmtree(work, ignore_errors=True)
    checks.append(_check("nothing left behind", not os.path.exists(work), work))

    failed = [c["check"] for c in checks if not c["ok"]]
    os.makedirs(RESULT_DIR, exist_ok=True)
    name = f"{version}-local" if args.zip else version
    with open(os.path.join(RESULT_DIR, f"{name}.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": version, "passed": not failed, "checks": checks},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\n{version}: {len(checks) - len(failed)} of {len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
