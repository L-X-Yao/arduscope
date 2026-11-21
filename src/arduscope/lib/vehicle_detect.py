"""
Vehicle detection from ArduPilot .BIN log metadata.

Parses firmware MSG lines and key parameters to determine vehicle type,
board name, firmware version, frame class, motor count, and IMU count.
"""

from dataclasses import dataclass

import numpy as np

from .fleet_history import extract_identity
from .log_reader import LogReader


@dataclass
class VehicleInfo:
    """Detected vehicle information."""
    vehicle_type: str       # "ArduCopter", "ArduPlane", "ArduRover", "Unknown"
    board_name: str         # e.g. "CubeOrange", "Pixhawk6C"
    firmware_version: str   # e.g. "V4.6.3"
    frame_class: int        # FRAME_CLASS or Q_FRAME_CLASS param value
    motor_count: int        # number of motors (inferred from frame class)
    imu_count: int          # number of IMUs detected
    is_quadplane: bool      # ArduPlane + Q_ENABLE=1

    @property
    def is_copter(self) -> bool:
        return 'Copter' in self.vehicle_type

    @property
    def is_plane(self) -> bool:
        return 'Plane' in self.vehicle_type


# Frame class -> motor count mapping (ArduPilot FRAME_CLASS param)
_FRAME_CLASS_MOTORS = {
    0: 0,    # Undefined
    1: 4,    # Quad
    2: 6,    # Hexa
    3: 8,    # Octa
    4: 8,    # OctaQuad
    5: 6,    # Y6 (3 arms x 2)
    6: 0,    # Heli (variable)
    7: 3,    # Tri
    8: 0,    # SingleCopter
    9: 0,    # CoaxCopter
    10: 2,   # BiCopter
    11: 0,   # Heli_Dual
    12: 12,  # DodecaHexa
    13: 0,   # HeliQuad
    14: 10,  # Deca
    15: 0,   # Scripting
    16: 6,   # 6DoF scripting
    17: 0,   # Dynamic scripting
}


def _count_imus(reader: LogReader) -> int:
    """Count IMUs from the log.

    Modern ArduPilot logs a single IMU message type with an instance
    column ``I`` (IMU0/1/2 interleaved), so the legacy per-type scan
    (IMU/IMU2/IMU3) always found exactly one and undercounted. Prefer the
    instance column; fall back to the legacy message types, then to the
    INS_ACC*_ID params.
    """
    if reader.has_message('IMU'):
        try:
            inst = reader.get_messages_as_arrays('IMU', ['I']).get('I')
            if inst is not None:
                valid = inst[~np.isnan(inst)]
                if len(valid) > 0:
                    return int(np.max(valid)) + 1
        except Exception:
            pass

    count = 0
    for suffix in ['', '2', '3']:
        if reader.has_message(f'IMU{suffix}'):
            count += 1
    # Also check INS_ACC2_ID etc. params
    if count == 0:
        for i in range(1, 4):
            key = f'INS_ACC{i}_ID' if i > 1 else 'INS_ACC_ID'
            if reader.get_param(key, 0) != 0:
                count += 1
    return max(count, 1)  # At least 1


def _detect_board_name(reader: LogReader) -> str:
    """Board name from the boot banner, else BRD_TYPE."""
    # The "ChibiOS: <hash>" line is the kernel's git revision, not a board.
    _serial, board = extract_identity(reader)
    if board:
        return board
    # Fallback: check BRD_TYPE
    brd_type = reader.get_param('BRD_TYPE', 0)
    if brd_type != 0:
        return f"BRD_TYPE={int(brd_type)}"
    return "Unknown"


def detect_vehicle(reader: LogReader) -> VehicleInfo:
    """
    Detect vehicle information from a LogReader instance.

    Args:
        reader: A LogReader that has already completed its pre-scan.

    Returns:
        VehicleInfo dataclass with detected properties.
    """
    # Vendor forks rename the firmware banner; the reader's canonical
    # property carries the ArduPilot family in the string so every
    # 'Copter'/'Plane' substring check downstream (VehicleInfo.is_copter,
    # Thresholds.from_params) still classifies correctly.
    vehicle_type = reader.canonical_vehicle_type
    firmware_version = reader.firmware_version
    board_name = _detect_board_name(reader)
    is_quadplane = reader.is_quadplane

    # Determine frame class
    if reader.is_copter:
        frame_class = int(reader.get_param('FRAME_CLASS', 0))
    elif is_quadplane:
        frame_class = int(reader.get_param('Q_FRAME_CLASS', 0))
    else:
        frame_class = 0

    # Motor count from frame class
    motor_count = _FRAME_CLASS_MOTORS.get(frame_class, 0)

    # IMU count
    imu_count = _count_imus(reader)

    return VehicleInfo(
        vehicle_type=vehicle_type,
        board_name=board_name,
        firmware_version=firmware_version,
        frame_class=frame_class,
        motor_count=motor_count,
        imu_count=imu_count,
        is_quadplane=is_quadplane,
    )
