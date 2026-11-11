#!/usr/bin/env python3
"""
Generate SITL log files for ArduScope regression testing.

Each scenario in scenarios.json flies on the SITL binary, driven over MAVLink,
and leaves a .BIN log for tests/test_sitl_regression.py to check. Every step
waits on what the vehicle reports, on its own clock, so a step that does not
happen fails the scenario instead of producing a log with no flight in it.

Usage:
    python3 tests/sitl/generate_logs.py --sitl-dir ~/ardupilot    # all scenarios
    python3 tests/sitl/generate_logs.py --sitl-dir ~/ardupilot copter_normal
    python3 tests/sitl/generate_logs.py --list

Prerequisite, in the ArduPilot tree:
    ./waf configure --board sitl && ./waf copter plane
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

from pymavlink import mavutil

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCENARIOS_FILE = os.path.join(_SCRIPT_DIR, "scenarios.json")
_LOGS_DIR = os.path.join(_SCRIPT_DIR, "logs")

# Canberra Model Aircraft Club, the default SITL field.
_HOME = (-35.363261, 149.165230, 584.0, 353.0)
_PORT = 5760
_RC_PORT = 5501
_DEFAULT_SPEEDUP = 10
# Roll, pitch, throttle, yaw and the rest, in PWM.
_NEUTRAL_STICKS = (1500, 1500, 1000, 1500, 1500, 1500, 1500, 1500)

# vehicle: binary, --model, default parameters from Tools/autotest/
_VEHICLES = {
    "ArduCopter": ("arducopter", "+", "default_params/copter.parm"),
    "ArduPlane": ("arduplane", "plane", "models/plane.parm"),
}


class ScenarioFailed(Exception):
    """The vehicle did not do what the scenario asked."""


def load_scenarios() -> list[dict]:
    with open(_SCENARIOS_FILE, encoding="utf-8") as f:
        return json.load(f)["scenarios"]


class _Sticks(threading.Thread):
    """Neutral sticks on the SITL RC port, as MAVProxy would send.

    With no RC at all the firmware reads the yaw stick as full left, and
    disarms a few seconds after arming.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.throttle = _NEUTRAL_STICKS[2]
        self._stop_sending = threading.Event()

    def run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not self._stop_sending.wait(0.02):
                sticks = list(_NEUTRAL_STICKS)
                sticks[2] = self.throttle
                packet = struct.pack(f"<{len(sticks)}H", *sticks)
                sock.sendto(packet, ("127.0.0.1", _RC_PORT))

    def stop(self) -> None:
        self._stop_sending.set()
        self.join()


class Flight:
    """One SITL vehicle, driven over MAVLink."""

    def __init__(self, conn, is_plane: bool, sticks: _Sticks):
        self.conn = conn
        self.is_plane = is_plane
        self.sticks = sticks
        self.now = 0.0  # seconds on the vehicle clock
        self.alt = 0.0  # metres above home
        self.reached = -1  # last mission item reached
        self.position_ok = False  # the EKF has an absolute position
        self.texts: list[str] = []

    def pump(self) -> None:
        msg = self.conn.recv_match(blocking=True, timeout=1)
        if msg is None:
            return
        kind = msg.get_type()
        if kind == "GLOBAL_POSITION_INT":
            self.now = msg.time_boot_ms / 1000.0
            self.alt = msg.relative_alt / 1000.0
        elif kind == "EKF_STATUS_REPORT":
            self.position_ok = bool(
                msg.flags & mavutil.mavlink.EKF_POS_HORIZ_ABS)
        elif kind == "MISSION_ITEM_REACHED":
            self.reached = msg.seq
        elif kind == "STATUSTEXT":
            self.texts.append(msg.text)

    def wait(self, done, seconds: float, what: str) -> None:
        deadline = self.now + seconds
        wall_deadline = time.monotonic() + seconds + 60
        while not done():
            self.pump()
            if self.now > deadline or time.monotonic() > wall_deadline:
                raise ScenarioFailed(
                    f"gave up on {what} after {seconds:.0f} s "
                    f"at {self.alt:.1f} m. Last messages: {self.texts[-4:]}")

    def start(self) -> None:
        self.conn.wait_heartbeat(timeout=30)
        self.conn.mav.request_data_stream_send(
            self.conn.target_system, self.conn.target_component,
            mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1)
        self.wait(lambda: self.now > 0, 30, "the first position report")
        self.wait(lambda: self.position_ok, 120, "an EKF position")

    def read_param(self, name: str) -> float | None:
        for _ in range(3):
            self.conn.param_fetch_one(name)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                msg = self.conn.recv_match(type="PARAM_VALUE", blocking=True,
                                           timeout=1)
                if msg is not None and msg.param_id == name:
                    return msg.param_value
        return None

    def check_params(self, params: dict) -> None:
        """--defaults skips a name it does not know, so a renamed SIM_
        parameter would quietly leave the fault out of the scenario."""
        for name, value in params.items():
            actual = self.read_param(name)
            if actual is None:
                raise ScenarioFailed(f"this firmware has no parameter {name}")
            if not math.isclose(actual, value, rel_tol=1e-3, abs_tol=1e-6):
                raise ScenarioFailed(f"{name} is {actual}, not {value}")

    def set_mode(self, name: str) -> None:
        self.conn.set_mode(self.conn.mode_mapping()[name])
        self.wait(lambda: self.conn.flightmode == name, 15, f"mode {name}")

    def arm(self) -> None:
        """Retry until the pre-arm checks pass: the EKF needs time to settle."""
        next_try = 0.0
        deadline = self.now + 120
        while not self.conn.motors_armed():
            if self.now >= next_try:
                self.conn.arducopter_arm()
                next_try = self.now + 2
            self.pump()
            if self.now > deadline:
                raise ScenarioFailed(f"did not arm: {self.texts[-4:]}")

    def _position(self, north_m: float, east_m: float) -> tuple[int, int]:
        lat = _HOME[0] + north_m / 111320.0
        lng = _HOME[1] + east_m / (111320.0 * math.cos(math.radians(_HOME[0])))
        return int(lat * 1e7), int(lng * 1e7)

    def _item(self, seq: int, command: int, north_m: float, east_m: float,
              alt_m: float, param1: float = 0.0):
        lat, lng = self._position(north_m, east_m)
        return self.conn.mav.mission_item_int_encode(
            self.conn.target_system, self.conn.target_component, seq,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT, command,
            0, 1, param1, 0, 0, 0, lat, lng, alt_m)

    def _upload(self, items: list) -> None:
        self.conn.mav.mission_count_send(
            self.conn.target_system, self.conn.target_component, len(items))
        while True:
            msg = self.conn.recv_match(
                type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
                blocking=True, timeout=5)
            if msg is None:
                raise ScenarioFailed("mission upload stalled")
            if msg.get_type() == "MISSION_ACK":
                if msg.type != mavutil.mavlink.MAV_MISSION_ACCEPTED:
                    raise ScenarioFailed(f"mission refused: {msg.type}")
                return
            self.conn.mav.send(items[msg.seq])

    # ---- steps, named after their key in scenarios.json ----

    def step_takeoff(self, alt_m: float) -> None:
        self.set_mode("GUIDED")
        self.arm()
        self.conn.mav.command_long_send(
            self.conn.target_system, self.conn.target_component,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0,
            0, 0, 0, 0, 0, 0, alt_m)
        self.wait(lambda: self.alt >= alt_m * 0.95, 60, f"climbing to {alt_m} m")

    def step_hold(self, seconds: float) -> None:
        end = self.now + seconds
        self.wait(lambda: self.now >= end, seconds + 10, f"holding {seconds} s")

    def step_mode(self, name: str) -> None:
        self.set_mode(name)

    def step_throttle(self, pwm: int) -> None:
        """Move the stick. CIRCLE descends with it at the bottom, 1000."""
        self.sticks.throttle = pwm

    def step_set(self, params: dict) -> None:
        """Change parameters in flight, to start or stop a fault."""
        for name, value in params.items():
            self.conn.param_set_send(name, value)
        self.step_hold(1)
        self.check_params(params)

    def step_mission(self, waypoints: list) -> None:
        """Fly [north_m, east_m, alt_m] points in AUTO.

        A plane takes off from the mission and lands at its end. A copter
        must be in the air already, and stays there.
        """
        cmd = mavutil.mavlink
        items = [self._item(0, cmd.MAV_CMD_NAV_WAYPOINT, 0, 0, 0)]
        if self.is_plane:
            items.append(self._item(1, cmd.MAV_CMD_NAV_TAKEOFF, 0, 0,
                                    waypoints[0][2], param1=15))
        for north, east, alt in waypoints:
            items.append(self._item(len(items), cmd.MAV_CMD_NAV_WAYPOINT,
                                    north, east, alt))
        last_waypoint = len(items) - 1
        if self.is_plane:
            items.append(self._item(len(items), cmd.MAV_CMD_NAV_LAND, 0, 0, 0))
        self._upload(items)
        self.set_mode("AUTO")
        if self.is_plane:
            self.arm()
        self.wait(lambda: self.reached >= last_waypoint, 600,
                  f"mission item {last_waypoint}")

    def step_return(self, seconds: float) -> None:
        """Copter: come home and land. Disarming is what ends the flight."""
        self.set_mode("RTL")
        self.step_wait_disarm(seconds)

    def step_wait_disarm(self, seconds: float) -> None:
        self.wait(lambda: not self.conn.motors_armed(), seconds, "disarm")

    def run(self, step: dict) -> None:
        (name, argument), = step.items()
        print(f"    {name} {argument} (t={self.now:.0f} s)", flush=True)
        getattr(self, f"step_{name}")(argument)


def _launch(scenario: dict, ardupilot: str, work_dir: str):
    binary, model, defaults = _VEHICLES[scenario["vehicle"]]
    path = os.path.join(ardupilot, "build", "sitl", "bin", binary)
    if not os.path.isfile(path):
        raise ScenarioFailed(f"{path} not found. Build it with ./waf copter plane")

    extra = os.path.join(work_dir, "scenario.parm")
    with open(extra, "w", encoding="utf-8") as f:
        for name, value in scenario.get("sitl_params", {}).items():
            f.write(f"{name} {value}\n")

    autotest = os.path.join(ardupilot, "Tools", "autotest")
    cmd = [path, "--wipe", "--synthetic-clock", "--model", model,
           "--speedup", str(scenario.get("speedup", _DEFAULT_SPEEDUP)),
           "--home", ",".join(str(v) for v in _HOME),
           "--defaults", f"{os.path.join(autotest, defaults)},{extra}"]
    out = open(os.path.join(work_dir, "sitl.out"), "w", encoding="utf-8")
    return subprocess.Popen(cmd, cwd=work_dir, stdin=subprocess.DEVNULL,
                            stdout=out, stderr=subprocess.STDOUT)


def run_scenario(scenario: dict, ardupilot: str) -> str:
    """Fly one scenario and return the path of its .BIN log."""
    name = scenario["name"]
    work_dir = os.path.join(_LOGS_DIR, f".{name}_work")
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(work_dir)

    print(f"[{name}] {scenario['description']}", flush=True)
    proc = _launch(scenario, ardupilot, work_dir)
    sticks = _Sticks()
    sticks.start()
    conn = None
    try:
        time.sleep(1)
        conn = mavutil.mavlink_connection(f"tcp:127.0.0.1:{_PORT}", retries=30)
        flight = Flight(conn, scenario["vehicle"] == "ArduPlane", sticks)
        flight.start()
        flight.check_params(scenario.get("sitl_params", {}))
        for step in scenario["steps"]:
            flight.run(step)
        # The logger closes its file a moment after disarming.
        time.sleep(2)
    finally:
        sticks.stop()
        if conn is not None:
            conn.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    logs = glob.glob(os.path.join(work_dir, "logs", "*.BIN"))
    if not logs:
        raise ScenarioFailed("the flight left no .BIN log")
    dst = os.path.join(_LOGS_DIR, f"{name}.BIN")
    shutil.copy2(max(logs, key=os.path.getmtime), dst)
    print(f"[{name}] saved {os.path.getsize(dst) / 1024:.0f} KB", flush=True)
    return dst


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenarios", nargs="*", help="Scenario names to run (default: all)")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    parser.add_argument("--sitl-dir", help="Path to the ArduPilot source directory")
    args = parser.parse_args()

    all_scenarios = load_scenarios()

    if args.list:
        print("Available SITL scenarios:")
        for s in all_scenarios:
            print(f"  {s['name']:30s} {s['description']}")
        return

    if not args.sitl_dir:
        parser.error("--sitl-dir is required")

    selected = all_scenarios
    if args.scenarios:
        names = set(args.scenarios)
        selected = [s for s in all_scenarios if s["name"] in names]
        unknown = names - {s["name"] for s in selected}
        if unknown:
            parser.error(f"unknown scenarios: {', '.join(sorted(unknown))}")

    os.makedirs(_LOGS_DIR, exist_ok=True)
    failed = {}
    for scenario in selected:
        try:
            run_scenario(scenario, os.path.abspath(args.sitl_dir))
        except ScenarioFailed as e:
            failed[scenario["name"]] = str(e)
            print(f"[{scenario['name']}] FAILED: {e}", file=sys.stderr, flush=True)

    print(f"\nGenerated {len(selected) - len(failed)} / {len(selected)} logs")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
