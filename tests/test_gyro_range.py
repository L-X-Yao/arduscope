"""Saturation is relative to the range the driver gave the gyro.

AP_InertialSensor_Invensensev3 runs ICM45686 and IIM42653 at +/-4000 deg/s
and the other parts at +/-2000. The device type sits in INS_GYRn_ID.
"""

from __future__ import annotations

import math

import pytest

import binlog
from analyzers.gyro_saturation import GyroSaturationAnalyzer
from lib.i18n import t as _t
from lib.log_reader import LogReader
from lib.thresholds import Thresholds
from lib.vehicle_detect import detect_vehicle

pytestmark = pytest.mark.fast

_ICM45686 = 0x3B
_IIM42653 = 0x3D
_ICM42688 = 0x34


def _gyro_id(devtype: int) -> float:
    """devid layout (AP_HAL::Device): type in bits 16-23, bus below it."""
    return float((devtype << 16) | 0x0B)


def _log(tmp_path, peak_dps, params) -> LogReader:
    """40 s armed; 2% of the IMU samples read peak_dps, far past crit."""
    log = binlog.BinLog()
    log.write("MSG", 1_000_000, "ArduCopter V4.6.3 (abc12345)")
    for name, value in params.items():
        log.write("PARM", 1_000_000, name, value, value)
    log.write("EV", 1_000_000, 10)
    for i in range(1000):
        rate = math.radians(peak_dps if i % 50 == 0 else 10.0)
        log.write("IMU", 1_000_000 + i * 40_000, 0, rate, 0.0, 0.0, 0.0, 0.0,
                  -9.81, 0, 0, 45.0, 1, 1, 1000, 1000)
    return LogReader(log.save(tmp_path / "gyro.bin"))


def _run(reader):
    thresholds = Thresholds.from_params(reader.params,
                                        reader.canonical_vehicle_type)
    return GyroSaturationAnalyzer(reader, thresholds,
                                  detect_vehicle(reader)).analyze()


class TestRangeFollowsTheSensor:
    @pytest.mark.parametrize("devtype", [_ICM45686, _IIM42653])
    def test_4000_dps_part_is_not_saturated_at_2500(self, tmp_path, devtype):
        reader = _log(tmp_path, 2500.0, {"INS_GYR1_ID": _gyro_id(devtype)})
        result = _run(reader)
        assert result.status == "good", result.summary

    def test_4000_dps_part_saturates_at_its_own_limit(self, tmp_path):
        reader = _log(tmp_path, 3990.0, {"INS_GYR1_ID": _gyro_id(_ICM45686)})
        result = _run(reader)
        assert result.status == "critical"
        assert result.evidence_source == _t("gyro_sat.evidence", limit=4000,
                                            thresh=3950)

    def test_2000_dps_part_keeps_its_limit(self, tmp_path):
        reader = _log(tmp_path, 2500.0, {"INS_GYR1_ID": _gyro_id(_ICM42688)})
        result = _run(reader)
        assert result.status == "critical"
        assert result.evidence_source == _t("gyro_sat.evidence", limit=2000,
                                            thresh=1950)

    def test_old_parameter_name(self, tmp_path):
        reader = _log(tmp_path, 2500.0, {"INS_GYR_ID": _gyro_id(_ICM45686)})
        assert _run(reader).status == "good"

    def test_unknown_sensor_keeps_the_common_limit(self, tmp_path):
        assert _run(_log(tmp_path, 2500.0, {})).status == "critical"
