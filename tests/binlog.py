"""DataFlash .BIN writer for tests.

Real logs are not in the repository, so without this the parser, the
pipeline and every analyzer that needs a real log only run on a developer
machine. Formats below are copied from the FMT records of a V4.6.3 log.
"""

from __future__ import annotations

import math
import random
import struct

_STRUCT = {
    "a": "64s", "b": "b", "B": "B", "h": "h", "H": "H", "i": "i", "I": "I",
    "f": "f", "n": "4s", "N": "16s", "Z": "64s", "c": "h", "C": "H",
    "e": "i", "E": "I", "L": "i", "d": "d", "M": "b", "q": "q", "Q": "Q",
}
_SCALE = {"c": 100, "C": 100, "e": 100, "E": 100, "L": 1e7}
_HEAD = b"\xa3\x95"
_FMT_ID = 0x80

FORMATS = {
    "ARSP": ("QBffcffBBffB",
             "TimeUS,I,Airspeed,DiffPress,Temp,RawPress,Offset,U,H,Hp,TR,Pri"),
    "ATT": ("QffffffB", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,AEKF"),
    "BAT": ("QBfffffcfBBB",
            "TimeUS,Inst,Volt,VoltR,Curr,CurrTot,EnrgTot,Temp,Res,RemPct,H,SH"),
    "CTUN": ("Qffffffffffff",
             "TimeUS,ThI,ABst,ThO,ThH,DAlt,Alt,BAlt,DSAlt,SAlt,TAlt,DCRt,CRt"),
    "ERR": ("QBB", "TimeUS,Subsys,ECode"),
    "EV": ("QB", "TimeUS,Id"),
    "GPA": ("QBCCCCfBIHeHH",
            "TimeUS,I,VDop,HAcc,VAcc,SAcc,YAcc,VV,SMS,Delta,AEl,RTCMFU,RTCMFD"),
    "GPS": ("QBBIHBcLLeffffB",
            "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,GCrs,VZ,Yaw,U"),
    "IMU": ("QBffffffIIfBBHH",
            "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ,EG,EA,T,GH,AH,GHz,AHz"),
    "ISBD": ("QHHaaa", "TimeUS,N,seqno,x,y,z"),
    "ISBH": ("QHBBHHQf",
             "TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate"),
    "MAG": ("QBhhhhhhhhhBI",
            "TimeUS,I,MagX,MagY,MagZ,OfsX,OfsY,OfsZ,MOX,MOY,MOZ,Health,S"),
    "MCU": ("Qffff", "TimeUS,MTemp,MVolt,MVmin,MVmax"),
    "MODE": ("QMBB", "TimeUS,Mode,ModeNum,Rsn"),
    "MSG": ("QZ", "TimeUS,Message"),
    "PARM": ("QNff", "TimeUS,Name,Value,Default"),
    "PIDP": ("QffffffffffB", "TimeUS,Tar,Act,Err,P,I,D,FF,DFF,Dmod,SRate,Flags"),
    "PIDR": ("QffffffffffB", "TimeUS,Tar,Act,Err,P,I,D,FF,DFF,Dmod,SRate,Flags"),
    "PIDY": ("QffffffffffB", "TimeUS,Tar,Act,Err,P,I,D,FF,DFF,Dmod,SRate,Flags"),
    "PM": ("QHHHIIHHIIIIIIQ",
           "TimeUS,LR,NLon,NL,MaxT,Mem,Load,ErrL,InE,ErC,SPIC,I2CC,I2CI,Ex,R"),
    "POS": ("QLLfff", "TimeUS,Lat,Lng,Alt,RelHomeAlt,RelOriginAlt"),
    "POWR": ("QffHHB", "TimeUS,Vcc,VServo,Flags,AccFlags,Safety"),
    "QTUN": ("QffffffeccfBB",
             "TimeUS,ThI,ABst,ThO,ThH,DAlt,Alt,BAlt,DCRt,CRt,TMix,Trn,Ast"),
    "RATE": ("Qfffffffffffff",
             "TimeUS,RDes,R,ROut,PDes,P,POut,YDes,Y,YOut,ADes,A,AOut,AOutSlew"),
    "RCIN": ("QHHHHHHHHHHHHHH",
             "TimeUS,C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14"),
    "RCO2": ("QHHHH", "TimeUS,C15,C16,C17,C18"),
    "RCOU": ("QHHHHHHHHHHHHHH",
             "TimeUS,C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14"),
    "RFND": ("QBCBBb", "TimeUS,Instance,Dist,Stat,Orient,Quality"),
    "STAT": ("QBfBBBBBB",
             "TimeUS,isFlying,isFlyProb,Armed,Safety,Crash,Still,Stage,Hit"),
    "VIBE": ("QBfffI", "TimeUS,IMU,VibeX,VibeY,VibeZ,Clip"),
    "XKF1": ("QBccCfffffffccce",
             "TimeUS,C,Roll,Pitch,Yaw,VN,VE,VD,dPD,PN,PE,PD,GX,GY,GZ,OH"),
    "XKF2": ("QBccccchhhhhhfff",
             "TimeUS,C,AX,AY,AZ,VWN,VWE,MN,ME,MD,MX,MY,MZ,IDX,IDY,IS"),
    "XKF3": ("QBcccccchhhccff",
             "TimeUS,C,IVN,IVE,IVD,IPN,IPE,IPD,IMX,IMY,IMZ,IYAW,IVT,RErr,ErSc"),
    "XKF4": ("QBcccccfffHBIHb",
             "TimeUS,C,SV,SP,SH,SM,SVT,errRP,OFN,OFE,FS,TS,SS,GPS,PI"),
}


def _struct_of(fmt: str) -> str:
    return "<" + "".join(_STRUCT[c] for c in fmt)


class BinLog:
    """Append-only DataFlash writer: declare a format, then write messages."""

    def __init__(self) -> None:
        self.data = bytearray()
        self._defs: dict[str, tuple[int, str]] = {}
        self._next_id = 129
        self._write_fmt(_FMT_ID, "FMT", "BBnNZ",
                        "Type,Length,Name,Format,Columns")

    def _write_fmt(self, msg_id: int, name: str, fmt: str, cols: str) -> None:
        length = 3 + struct.calcsize(_struct_of(fmt))
        self.data += _HEAD + bytes([_FMT_ID]) + struct.pack(
            "<BB4s16s64s", msg_id, length, name.encode(), fmt.encode(),
            cols.encode())
        self._defs[name] = (msg_id, fmt)

    def declare(self, name: str, fmt: str | None = None,
                cols: str | None = None) -> None:
        """Declare a message; format defaults to the real one in FORMATS."""
        if fmt is None:
            fmt, cols = FORMATS[name]
        self._write_fmt(self._next_id, name, fmt, cols)
        self._next_id += 1

    def write(self, name: str, *values) -> None:
        if name not in self._defs:
            self.declare(name)
        msg_id, fmt = self._defs[name]
        packed = []
        for code, value in zip(fmt, values):
            if code == "a":
                samples = list(value)[:32]
                samples += [0] * (32 - len(samples))
                packed.append(struct.pack("<32h", *samples))
            elif _STRUCT[code].endswith("s"):
                raw = (value if isinstance(value, bytes)
                       else str(value).encode("utf-8"))
                size = int(_STRUCT[code][:-1])
                packed.append(raw[:size].ljust(size, b"\0"))
            elif code in _SCALE:
                packed.append(int(round(value * _SCALE[code])))
            else:
                packed.append(value)
        self.data += _HEAD + bytes([msg_id]) + struct.pack(
            _struct_of(fmt), *packed)

    def save(self, path) -> str:
        with open(path, "wb") as f:
            f.write(self.data)
        return str(path)


COPTER_PARAMS = {
    "FRAME_CLASS": 1, "FRAME_TYPE": 1,
    "SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34,
    "SERVO3_FUNCTION": 35, "SERVO4_FUNCTION": 36,
    "MOT_PWM_MIN": 1000, "MOT_PWM_MAX": 2000, "MOT_SPIN_ARM": 0.10,
    "MOT_SPIN_MIN": 0.15, "MOT_THST_HOVER": 0.35,
    "ATC_RAT_RLL_P": 0.135, "ATC_RAT_RLL_I": 0.135, "ATC_RAT_RLL_D": 0.0036,
    "ATC_RAT_PIT_P": 0.135, "ATC_RAT_PIT_I": 0.135, "ATC_RAT_PIT_D": 0.0036,
    "ATC_RAT_YAW_P": 0.18, "ATC_RAT_YAW_I": 0.018,
    "ATC_ANG_RLL_P": 6.0, "ATC_ANG_PIT_P": 6.0, "ATC_ANG_YAW_P": 6.0,
    "BATT_LOW_VOLT": 21.0, "BATT_CRT_VOLT": 20.0, "BATT_CAPACITY": 10000,
    "FS_EKF_THRESH": 0.8, "LOG_BITMASK": 176126, "LOG_DISARMED": 0,
    "INS_GYRO_FILTER": 20, "INS_ACCEL_FILTER": 20, "INS_HNTCH_ENABLE": 0,
    "COMPASS_USE": 1, "COMPASS_USE2": 1, "SCHED_LOOP_RATE": 400,
}

QUADPLANE_PARAMS = {
    "Q_ENABLE": 1, "Q_FRAME_CLASS": 1, "Q_FRAME_TYPE": 1,
    "SERVO1_FUNCTION": 33, "SERVO2_FUNCTION": 34,
    "SERVO3_FUNCTION": 35, "SERVO4_FUNCTION": 36, "SERVO5_FUNCTION": 70,
    "Q_M_PWM_MIN": 1000, "Q_M_PWM_MAX": 2000, "Q_M_SPIN_ARM": 0.10,
    "Q_M_SPIN_MIN": 0.15, "Q_M_THST_HOVER": 0.35,
    "Q_A_RAT_RLL_P": 0.135, "Q_A_RAT_RLL_I": 0.135, "Q_A_RAT_RLL_D": 0.0036,
    "Q_A_RAT_PIT_P": 0.135, "Q_A_RAT_PIT_I": 0.135, "Q_A_RAT_PIT_D": 0.0036,
    "Q_A_RAT_YAW_P": 0.18, "Q_A_RAT_YAW_I": 0.018,
    "RLL_RATE_P": 0.08, "RLL_RATE_I": 0.15, "PTCH_RATE_P": 0.08,
    "PTCH_RATE_I": 0.15, "BATT_LOW_VOLT": 42.0, "BATT_CRT_VOLT": 40.0,
    "BATT_CAPACITY": 22000, "LOG_BITMASK": 65535, "LOG_DISARMED": 0,
    "INS_GYRO_FILTER": 20, "INS_ACCEL_FILTER": 20, "INS_HNTCH_ENABLE": 0,
    "COMPASS_USE": 1, "COMPASS_USE2": 1, "SCHED_LOOP_RATE": 400,
}

_T0_US = 2_000_000
_TICK_US = 40_000          # 25 Hz: all regular messages
_IMU_DIV_HZ = 25           # IMU rides the same tick; ISB carries high rate


def _batch(log: BinLog, seq: int, time_us: int, tone_hz: float,
           samples: int = 512, rate_hz: float = 1000.0) -> None:
    """One gyro batch: ISBH header, then ISBD chunks of 32 samples."""
    log.write("ISBH", time_us, seq, 1, 0, 1, samples, time_us, rate_hz)
    wave = [int(2000 * math.sin(2 * math.pi * tone_hz * i / rate_hz))
            for i in range(samples)]
    for chunk, start in enumerate(range(0, samples, 32)):
        part = wave[start:start + 32]
        log.write("ISBD", time_us, seq, chunk, part, part, part)


def flight_log(vehicle: str = "copter", duration_s: float = 40.0,
               params: dict | None = None, batches: int = 6,
               tone_hz: float = 120.0, seed: int = 7) -> BinLog:
    """A healthy flight: boot, arm at 2 s, hover, disarm 2 s before the end.

    vehicle is "copter" or "quadplane". Pass params to change or add
    parameters; everything else stays at a plausible healthy value.
    """
    rnd = random.Random(seed)
    plane = vehicle == "quadplane"
    log = BinLog()
    for name in FORMATS:
        log.declare(name)

    t = _T0_US
    banner = "ArduPlane V4.6.3 (abc12345)" if plane else \
        "ArduCopter V4.6.3 (abc12345)"
    log.write("MSG", t, banner)
    log.write("MSG", t, "ChibiOS: 6a85082c")
    log.write("MSG", t, "CubeOrange 001A002B 3030510C 31323334")
    table = dict(QUADPLANE_PARAMS if plane else COPTER_PARAMS)
    table.update(params or {})
    for name, value in table.items():
        log.write("PARM", t, name, float(value), float(value))
    log.write("MODE", t, 17 if plane else 5, 17 if plane else 5, 1)

    n_ticks = int(duration_s * 1e6 / _TICK_US)
    arm_tick = int(2e6 / _TICK_US)
    disarm_tick = n_ticks - int(2e6 / _TICK_US)
    next_batch = arm_tick + 25
    batch_seq = 0
    for i in range(n_ticks):
        t = _T0_US + i * _TICK_US
        secs = i * _TICK_US / 1e6
        armed = arm_tick <= i < disarm_tick
        if i == arm_tick:
            log.write("EV", t, 10)
        if i == disarm_tick:
            log.write("EV", t, 11)

        roll = 2.0 * math.sin(secs)
        pitch = 1.5 * math.cos(secs)
        noise = rnd.uniform(-0.05, 0.05)
        log.write("ATT", t, roll, roll + noise, pitch, pitch - noise,
                  90.0, 90.0 + noise, 3)
        rate_r = 2.0 * math.cos(secs)
        log.write("RATE", t, rate_r, rate_r + noise, 0.02, 0.0, noise, 0.02,
                  0.0, noise, 0.01, 0.0, 0.0, 0.3, 0.3)
        pid = ("PIDR", "PIDP", "PIDY")
        for name in pid:
            rad = math.radians(rate_r)
            log.write(name, t, rad, rad + noise / 57.3, noise / 57.3,
                      0.01, 0.005, 0.001, 0.0, 0.0, 1.0, 1.0, 0)

        pwm = 1400 + int(10 * math.sin(secs)) if armed else 1000
        channels = [pwm] * 4 + ([1100] if plane else [1000]) + [1500] * 9
        log.write("RCOU", t, *channels)
        log.write("RCO2", t, 0, 0, 0, 0)
        log.write("RCIN", t, 1500, 1500, 1400, 1500, *([1000] * 10))

        for imu in (0, 1):
            log.write("IMU", t, imu, noise, noise, noise, 0.0, 0.0, -9.81,
                      0, 0, 45.0 + imu, 1, 1, 1000, 1000)
            log.write("VIBE", t, imu, 5.0, 5.0, 6.0, 0)
        if i % 5 == 0:
            alt = 10.0 if armed else 0.0
            lat = 34.1 + i * 1e-7
            log.write("GPS", t, 0, 3, 100000 + i * 40, 2300, 14, 0.8, lat,
                      108.9, 400.0 + alt, 1.0, 0.0, 0.0, 0.0, 1)
            log.write("GPA", t, 0, 1.0, 0.8, 1.2, 0.2, 0.0, 1, 200, 200,
                      0.0, 0, 0)
            log.write("POS", t, lat, 108.9, 400.0 + alt, alt, alt)
            log.write("BAT", t, 0, 50.0 if plane else 24.0,
                      50.0 if plane else 24.0, 20.0, i * 0.2, i * 0.01,
                      25.0, 0.01, 90, 1, 0)
            for core in (0, 1):
                log.write("XKF1", t, core, roll, pitch, 90.0, 1.0, 0.0, 0.0,
                          0.0, 0.0, 0.0, -alt, 0.0, 0.0, 0.0, 0.0)
                log.write("XKF2", t, core, 0.0, 0.0, 0.0, 3.0, 1.0, 200, 10,
                          400, 0, 0, 0, 0.0, 0.0, 0.0)
                log.write("XKF3", t, core, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0,
                          0, 0, 0.0, 0.0, 1.0, 0.0)
                log.write("XKF4", t, core, 0.1, 0.1, 0.1, 0.1, 0.0, 0.0,
                          0.0, 0.0, 0, 0, 0, 0, 0)
            for mag in (0, 1):
                log.write("MAG", t, mag, 200, 100, 400, 10, 10, 10, 0, 0, 0,
                          1, t)
            log.write("POWR", t, 5.1, 5.0, 3, 3, 4 if armed else 0)
            log.write("MCU", t, 45.0, 3.3, 3.29, 3.31)
            log.write("PM", t, 2500, 0, 2500, 3000, 100000, 240, 0, 0, 0, 0,
                      0, 0, 0, 0)
            log.write("RFND", t, 0, max(alt, 0.2), 4, 25, 100)
            log.write("STAT", t, 1 if armed else 0, 1.0 if armed else 0.0,
                      1 if armed else 0, 1, 0, 0, 2, 0)
        thr = 0.35 if armed else 0.0
        alt_now = 10.0 if armed else 0.0
        if plane:
            log.write("QTUN", t, thr, 0.0, thr, 0.35, alt_now, alt_now,
                      alt_now, 0.0, 0.0, 0.0, 0, 0)
        else:
            log.write("CTUN", t, thr, 0.0, thr, 0.35, alt_now, alt_now,
                      alt_now, 0.0, 0.0, alt_now, 0.0, 0.0)
        if armed and i == next_batch and batch_seq < batches:
            _batch(log, batch_seq, t, tone_hz)
            batch_seq += 1
            next_batch += 25
    return log
