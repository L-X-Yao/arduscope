"""ArduPilot's EKF failsafe test, evaluated on the logged variances.

ArduCopter and ArduPlane use the same scoring (``ekf_check.cpp``): a
magnetometer variance at or above ``FS_EKF_THRESH`` scores one; a velocity
variance at or above it scores one, and two at twice the threshold;
position fires only with at least one other score, so the test needs two
indicators (or a very large velocity variance). Height is not part of the
decision. Beyond the score, ``ekf_failsafe_replay`` replicates the rest of
the firmware state machine (all citations are the local ArduPilot fork,
which matches upstream 4.5.x line for line in these functions):

- The check runs at 10 Hz (``SCHED_TASK(ekf_check, 10, ...)``) and counts
  up one per bad check, down one per good check, and fires at ten
  (``EKF_CHECK_ITERATIONS_MAX``) -- a leaky counter, not a continuous
  second. Recovery likewise takes ten good checks. Plane resets the
  counter outside armed VTOL position control; Copter keeps counting
  disarmed as well, but holds off until the checks have ever passed
  (``has_ever_passed``) and until the EKF origin exists (``get_origin``).
- Copter low-passes the position and velocity variances at
  ``FS_EKF_FILT`` Hz (default 5) before scoring
  (``ekf_check.cpp:129-130``); the filter is the first-order
  ``DigitalLPF`` of ``AP_Filter/LowPassFilter.cpp``, not a biquad. Plane
  scores the raw variances.
- Copter additionally requires a position estimate
  (``checks_passed = !over_threshold && has_position``): armed flight
  needs ``HORIZ_POS_ABS`` without ``CONST_POS_MODE``; disarmed accepts the
  predicted flags; a relative position needs optflow or visual odometry
  enabled or active dead reckoning (``ArduCopter/system.cpp:249-313``).
  All of it is visible in the primary lane's ``XKF4.SS``
  (``AP_NavEKF/AP_Nav_Common.h``). Plane has no such gate.

Known approximations, all documented in ``docs/known_limitations.md``:
one logged XKF4 row stands for one 10 Hz check; ``optflow.healthy()`` is
not logged, so velocity at twice the threshold always scores two (the
strict reading of ``ekf_check.cpp:146-158``); origin readiness is
approximated by the first origin-proving SS bit (``HORIZ_POS_ABS`` or
``USING_GPS`` -- a fused absolute source, which requires ``validOrigin``
-- never the PRED flags, which only say GPS navigation is possible);
and ``has_ever_passed`` latches only on an observed pass, so a log that
opens inside a bad interval holds the check closed and leaves the
verdict to the ERR events.

QLOITER, QLAND, QRTL and QAUTOTUNE are direct evidence of Plane's
``in_vtol_posvel_mode()``. AUTO needs phase evidence too:
``Plane::update_flight_stage`` logs ``STAT.Stage=VTOL`` both for
``in_vtol_auto()`` and for fixed-wing Q assist, so Stage alone is not
enough. ``QTUN.Ast`` records ``assisted_flight``; a nearby row with its
active bit clear separates the two. AUTO stays closed without both
signals or while assist is active. If assist and ``in_vtol_auto()``
overlap, as during a takeoff transition, this conservative gate can miss
a real AUTO VTOL trigger.

GUIDED remains excluded. Its gate also needs ``auto_state.vtol_loiter``
and ``poscontrol.get_state() > QPOS_APPROACH``. Neither state is logged
directly; even with Ast, QTUN does not expose ``guided_takeoff`` or the
poscontrol state, so it cannot prove the conjunction.

Rover and Sub have EKF failsafes of different shapes (Rover folds position
into the score and scores the raw magnetic norm; Sub wants one indicator
held for two seconds) and are not replayed: their logs keep the older
continuous-second model below.

A single variance over the threshold, the compass being the common case,
is therefore a health signal, not an ArduPilot failsafe.
"""

from __future__ import annotations

import math

import numpy as np

# Ten failed checks at 10 Hz before Plane/Copter declare the failsafe.
# Used by the legacy continuous-second model for Rover/Sub/unknown logs.
TRIGGER_SECONDS = 1.0

# The firmware counter: EKF_CHECK_ITERATIONS_MAX, identical in
# ArduCopter/ekf_check.cpp:11, ArduPlane/ekf_check.cpp:11 and
# Rover/ekf_check.cpp:10, driven at 10 Hz (Copter.cpp:241, Plane.cpp:83).
EKF_CHECK_ITERATIONS_MAX = 10
EKF_CHECK_RATE_HZ = 10.0

# ArduCopter/config.h:114 (FS_EKF_FILT_DEFAULT) and
# ArduCopter/Parameters.cpp:908 (FS_DR_TIMEOUT, seconds).
FS_EKF_FILT_DEFAULT = 5.0
FS_DR_TIMEOUT_DEFAULT = 30.0

# Primary-lane XKF4.SS bits, AP_NavEKF/AP_Nav_Common.h:26-46.
SS_HORIZ_POS_REL = 8
SS_HORIZ_POS_ABS = 16
SS_CONST_POS_MODE = 128
SS_PRED_HORIZ_POS_REL = 256
SS_PRED_HORIZ_POS_ABS = 512
SS_USING_GPS = 8192
SS_DEAD_RECKONING = 262144

# Row codes for failsafe_windows().
_RUN_GOOD, _RUN_BAD, _HOLD, _RESET = 0, 1, 2, 3

# Direct-mode cases in QuadPlane::in_vtol_posvel_mode(): QLOITER, QLAND,
# QRTL and QAUTOTUNE. AUTO and GUIDED need state beyond the mode number.
PLANE_VTOL_POSVEL_MODES = (19, 20, 21, 22)
_PLANE_AUTO_MODE = 10                 # ArduPlane/mode.h
_FLIGHT_STAGE_VTOL = 2               # AP_FixedWing::FlightStage::VTOL
_QTUN_MATCH_SECONDS = 0.25           # QTUN is 25 Hz; its tail lasts 250 ms
# Plane-4.5.1 writes assisted_flight as a boolean in QTUN.Ast; Plane 4.6+
# writes log_assistance_flags. Bit 0 is the active indication in both.
_QTUN_ASSIST_ACTIVE = 1


def over_threshold(sv, sp, sm, thresh: float) -> np.ndarray:
    """Per-sample boolean: ArduPilot's EKF test says "bad" at this row."""
    sv = np.asarray(sv, dtype=float)
    sp = np.asarray(sp, dtype=float)
    sm = np.asarray(sm, dtype=float)
    count = (sm >= thresh).astype(np.int8)
    count += np.where(sv >= 2.0 * thresh, 2,
                      np.where(sv >= thresh, 1, 0)).astype(np.int8)
    return ((sp >= thresh) & (count >= 1)) | (count >= 2)


def sustained(mask, timestamps, seconds: float = TRIGGER_SECONDS) -> np.ndarray:
    """Per-sample boolean: part of a run that held at least ``seconds``."""
    mask = np.asarray(mask, dtype=bool)
    ts = np.asarray(timestamps, dtype=float)
    held = np.zeros(len(mask), dtype=bool)
    i, n = 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        if ts[j] - ts[i] >= seconds:
            held[i:j + 1] = True
        i = j + 1
    return held


def in_modes(mode_list, timestamps, modes) -> np.ndarray | None:
    """Rows whose last mode change names one of ``modes``.

    None when the log records no mode change: without that evidence the
    caller cannot gate on a mode.
    """
    rows = [m for m in mode_list if getattr(m, "timestamp", None) is not None]
    if not rows:
        return None
    changes = np.array([m.timestamp for m in rows], dtype=float)
    numbers = np.array([m.mode_number for m in rows], dtype=int)
    idx = np.searchsorted(changes, np.asarray(timestamps, dtype=float),
                          side="right") - 1
    mask = np.zeros(len(timestamps), dtype=bool)
    valid = idx >= 0
    mask[valid] = np.isin(numbers[idx[valid]], modes)
    return mask


def plane_posvel_mask(reader, timestamps) -> np.ndarray | None:
    """Plane rows with positive evidence for ``in_vtol_posvel_mode()``.

    Direct Q modes come from MODE. AUTO additionally requires STAT's VTOL
    stage and a nearby QTUN row whose assist-active bit is clear. This is
    the distinction in Plane-4.5.1 ``Plane::update_flight_stage`` and
    ``QuadPlane::Log_Write_QControl_Tuning``. Unknown evidence fails closed.

    None means the log has no usable MODE changes, as in ``in_modes``.
    """
    ts = np.asarray(timestamps, dtype=float)
    direct = in_modes(reader.mode_list, ts, PLANE_VTOL_POSVEL_MODES)
    if direct is None:
        return None
    auto = in_modes(reader.mode_list, ts, (_PLANE_AUTO_MODE,))
    if not auto.any():
        return direct
    if not reader.has_message("STAT") or not reader.has_message("QTUN"):
        return direct

    stat = reader.get_messages_as_arrays("STAT", ["Stage"])
    qtun = reader.get_messages_as_arrays("QTUN", ["Ast"])
    stat_ts = stat["_timestamp"]
    qtun_ts = qtun["_timestamp"]
    if len(stat_ts) == 0 or len(qtun_ts) == 0:
        return direct

    # STAT is written on a stage change. Hold the last known stage, but do
    # not back-fill the first one into an earlier, unknown interval.
    stat_idx = np.searchsorted(stat_ts, ts, side="right") - 1
    stage_known = stat_idx >= 0
    stage_vtol = np.zeros(len(ts), dtype=bool)
    stage_vtol[stage_known] = (
        stat["Stage"][stat_idx[stage_known]] == _FLIGHT_STAGE_VTOL)

    # Match each EKF row to the nearest QTUN sample. A missing/old Ast field
    # is unknown, not evidence that Q assist was inactive. At assist start,
    # the previous Ast can persist for at most about one 25 Hz QTUN interval
    # (40 ms), far below the 1 s hold, so this cannot create a false critical.
    right = np.searchsorted(qtun_ts, ts)
    left = np.clip(right - 1, 0, len(qtun_ts) - 1)
    right = np.clip(right, 0, len(qtun_ts) - 1)
    use_left = np.abs(ts - qtun_ts[left]) <= np.abs(qtun_ts[right] - ts)
    nearest = np.where(use_left, left, right)
    distance = np.minimum(np.abs(ts - qtun_ts[left]),
                          np.abs(qtun_ts[right] - ts))
    ast = np.asarray(qtun["Ast"], dtype=float)
    finite_ast = np.isfinite(ast)
    ast_bits = np.zeros(len(ast), dtype=np.int64)
    ast_bits[finite_ast] = ast[finite_ast].astype(np.int64)
    no_assist = ((distance <= _QTUN_MATCH_SECONDS) & finite_ast[nearest]
                 & ((ast_bits[nearest] & _QTUN_ASSIST_ACTIVE) == 0))

    return direct | (auto & stage_vtol & no_assist)


# ----------------------------------------------------------------------
# Firmware state-machine replication (Copter and QuadPlane)
# ----------------------------------------------------------------------

class _DigitalLPF:
    """``DigitalLPF<float>`` from AP_Filter/LowPassFilter.cpp:24-31.

    ``ekf_check.cpp`` low-passes the position and velocity variances
    through this filter before scoring them, so the offline model has to
    reproduce its arithmetic exactly, including the first-sample
    pass-through of the ``initialised`` flag.
    """

    def __init__(self) -> None:
        self.output = 0.0
        self.initialised = False

    def apply(self, sample: float, alpha: float) -> float:
        self.output += (sample - self.output) * alpha
        if not self.initialised:
            self.initialised = True
            self.output = sample
        return self.output


def _lowpass_alpha(dt: float, cutoff_hz: float) -> float:
    """``calc_lowpass_alpha_dt``, AP_Math/AP_Math.cpp:418-432."""
    if dt < 0 or cutoff_hz < 0:
        return 1.0
    if cutoff_hz == 0:
        return 1.0
    if dt == 0:
        return 0.0
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    return dt / (dt + rc)


def lowpass_filtered(values, timestamps, cutoff_hz: float) -> np.ndarray:
    """Variance as ``FS_EKF_FILT`` leaves it, row by row.

    One call per XKF4 row with the elapsed dt, like the firmware's
    ``apply(value, dt)`` at ekf_check.cpp:129-130. A NaN row (a decode
    gap, not a flight state) holds the previous output, does not touch
    the filter state, but still advances the clock: the next finite
    sample's dt spans the whole gap, so the filter is fed the same
    elapsed time the firmware would have measured.
    """
    vals = np.asarray(values, dtype=float)
    ts = np.asarray(timestamps, dtype=float)
    out = np.full(len(vals), np.nan)
    filt = _DigitalLPF()
    last_t = None
    for i, (v, t) in enumerate(zip(vals, ts)):
        if not np.isfinite(v):
            if filt.initialised:
                out[i] = filt.output
            last_t = t
            continue
        dt = 0.0 if last_t is None else float(t - last_t)
        out[i] = filt.apply(float(v), _lowpass_alpha(dt, cutoff_hz))
        last_t = t
    return out


def copter_has_position(ss, timestamps, armed_mask, reader) -> np.ndarray | None:
    """Per-row ``ekf_has_relative_position() || ekf_has_absolute_position()``.

    ArduCopter/system.cpp:249-313. Absolute: armed rows need
    ``HORIZ_POS_ABS`` without ``CONST_POS_MODE``, disarmed rows also
    accept ``PRED_HORIZ_POS_ABS``. Relative: additionally requires
    optflow (``FLOW_TYPE``), visual odometry (``VISO_TYPE``) or live dead
    reckoning (SS bit, timed out by ``FS_DR_TIMEOUT``, events.cpp:360-380),
    then the same armed/disarmed split on the REL bits.

    Returns None when the SS column carries no data at all (synthetic
    logs), so the caller keeps variance-only semantics. A readable but
    NaN row fails closed.
    """
    ss = np.asarray(ss, dtype=float)
    ts = np.asarray(timestamps, dtype=float)
    if not np.isfinite(ss).any():
        return None
    armed = np.asarray(armed_mask, dtype=bool)

    bits = np.zeros(len(ss), dtype=np.int64)
    finite = np.isfinite(ss)
    bits[finite] = ss[finite].astype(np.int64)

    rel_enabled_static = (
        reader.get_param("FLOW_TYPE", 0.0) > 0
        or reader.get_param("VISO_TYPE", 0.0) > 0)

    # Dead reckoning: events.cpp mirrors the EKF status bit into
    # dead_reckoning.active and times it out after FS_DR_TIMEOUT seconds.
    dr_timeout = reader.get_param("FS_DR_TIMEOUT", FS_DR_TIMEOUT_DEFAULT)
    dr_ok = np.zeros(len(ss), dtype=bool)
    dr_active = (bits & SS_DEAD_RECKONING) != 0
    run_start = None
    for i, active in enumerate(dr_active):
        if active:
            if run_start is None:
                run_start = ts[i]
            if ts[i] - run_start <= dr_timeout:
                dr_ok[i] = True
        else:
            run_start = None
    rel_enabled = rel_enabled_static | dr_ok

    const_mode = (bits & SS_CONST_POS_MODE) != 0
    abs_ok = np.where(
        armed,
        ~const_mode & ((bits & SS_HORIZ_POS_ABS) != 0),
        ((bits & SS_HORIZ_POS_ABS) != 0)
        | ((bits & SS_PRED_HORIZ_POS_ABS) != 0),
    )
    rel_ok = rel_enabled & np.where(
        armed,
        ~const_mode & ((bits & SS_HORIZ_POS_REL) != 0),
        (bits & SS_PRED_HORIZ_POS_REL) != 0,
    )
    return (abs_ok | rel_ok) & finite


def _origin_ready_mask(ss) -> np.ndarray | None:
    """Rows at and after the first origin-proving SS bit.

    ``ekf_check`` returns before touching the counter while
    ``ahrs.get_origin`` fails (ekf_check.cpp:35-39, both vehicles). The
    origin cannot be read from the log. Of the status bits, only
    ``HORIZ_POS_ABS`` (set by ``doingNormalGpsNav``) and ``USING_GPS``
    (``PV_AidingMode == AID_ABSOLUTE``) prove a fused absolute source,
    and GPS fusion requires ``validOrigin``
    (AP_NavEKF2_Control.cpp:389-391, AP_NavEKF3_Control.cpp:594).
    ``PRED_HORIZ_POS_ABS`` is only ``gpsNavPossible && filterHealthy``
    (AP_NavEKF2_Control.cpp:513) and does not. Holding the check closed
    until an origin-proving bit can only delay, never invent, a
    trigger. None when SS is unreadable.
    """
    ss = np.asarray(ss, dtype=float)
    if not np.isfinite(ss).any():
        return None
    bits = np.zeros(len(ss), dtype=np.int64)
    finite = np.isfinite(ss)
    bits[finite] = ss[finite].astype(np.int64)
    ready = (((bits & SS_HORIZ_POS_ABS) != 0)
             | ((bits & SS_USING_GPS) != 0))
    seen = np.cumsum(ready) > 0
    return seen


def failsafe_windows(states, timestamps) -> list[tuple[float, float | None]]:
    """Trigger/clear windows of the ``EKF_CHECK_ITERATIONS_MAX`` counter.

    Walks ``states`` (per-row ``_RUN_BAD``/``_RUN_GOOD``/``_HOLD``/
    ``_RESET``) exactly as ArduCopter/ArduPlane ekf_check.cpp:62-105
    does: a bad check increments while the failsafe is off, a good check
    decrements, ten bad checks fire (window opens), and the failsafe
    clears only when the counter has walked back to zero. A ``_RESET``
    row is Plane's disabled branch (ekf_check.cpp:48-54): counter zero,
    failsafe cleared. ``_HOLD`` rows (origin not ready, or Copter's
    ``has_ever_passed`` not yet latched) change nothing. A window still
    open at the end of the log ends at None.
    """
    ts = np.asarray(timestamps, dtype=float)
    count = 0
    bad_variance = False
    windows: list[tuple[float, float | None]] = []
    open_start: float | None = None
    for state, t in zip(states, ts):
        if state == _HOLD:
            continue
        if state == _RESET:
            count = 0
            if bad_variance:
                bad_variance = False
                windows.append((open_start, float(t)))
                open_start = None
            continue
        if state == _RUN_BAD:
            if not bad_variance:
                count += 1
                if count >= EKF_CHECK_ITERATIONS_MAX:
                    count = EKF_CHECK_ITERATIONS_MAX
                    bad_variance = True
                    open_start = float(t)
        else:
            if count > 0:
                count -= 1
                if bad_variance and count == 0:
                    bad_variance = False
                    windows.append((open_start, float(t)))
                    open_start = None
    if open_start is not None:
        windows.append((open_start, None))
    return windows


def vehicle_kind(reader, vehicle=None) -> str:
    """"copter", "quadplane", "plane" or "other" (Rover/Sub/unknown).

    ``vehicle`` (a detected VehicleInfo, when the caller has one) wins
    over the reader's own flags: the reader cannot see Q_ENABLE in logs
    whose parameters the detector already digested.
    """
    if vehicle is not None:
        if getattr(vehicle, "is_copter", False):
            return "copter"
        if getattr(vehicle, "is_plane", False):
            return "quadplane" if getattr(vehicle, "is_quadplane", False) \
                else "plane"
        return "other"
    if getattr(reader, "is_copter", False):
        return "copter"
    if getattr(reader, "is_plane", False):
        return "quadplane" if getattr(reader, "is_quadplane", False) \
            else "plane"
    return "other"


def _armed_row_mask(timestamps, armed_intervals) -> np.ndarray:
    ts = np.asarray(timestamps, dtype=float)
    mask = np.zeros(len(ts), dtype=bool)
    for s, e in armed_intervals:
        mask |= (ts >= s) & (ts <= e)
    return mask


def ekf_failsafe_replay(reader, kf4, timestamps, kind: str, thresh: float,
                        gate: np.ndarray | None = None):
    """Replay the firmware EKF failsafe on primary-lane XKF4 rows.

    ``kf4`` holds the primary-lane SV/SP/SM (and SS when present) arrays
    aligned with ``timestamps``. ``kind`` is a ``vehicle_kind`` string;
    "copter" adds the variance filter and the position gate, "quadplane"
    adds the mode-gate reset (``gate``, from :func:`plane_posvel_mask`;
    None leaves only the arming reset). Returns ``(windows, pos_loss,
    over)``: the ``(start, end|None)`` times of each failsafe window,
    which of them never saw a variance breach on a bad row (the Copter
    position-loss path), and the per-row over-threshold mask the replay
    scored (post-filter on Copter).
    """
    ts = np.asarray(timestamps, dtype=float)
    sv = np.asarray(kf4["SV"], dtype=float)
    sp = np.asarray(kf4["SP"], dtype=float)
    sm = np.asarray(kf4["SM"], dtype=float)
    ss = kf4.get("SS")

    armed = _armed_row_mask(ts, reader.get_armed_intervals())
    origin = _origin_ready_mask(ss) if ss is not None else None

    if kind == "copter":
        filt_hz = reader.get_param("FS_EKF_FILT", FS_EKF_FILT_DEFAULT)
        sv_cmp = lowpass_filtered(sv, ts, filt_hz)
        sp_cmp = lowpass_filtered(sp, ts, filt_hz)
    else:
        sv_cmp, sp_cmp = sv, sp
    # optflow.healthy() is not logged; treating it as false keeps the
    # velocity double-score, the strict reading (ekf_check.cpp:146-158).
    over = over_threshold(sv_cmp, sp_cmp, sm, thresh)

    pos = None
    if kind == "copter" and ss is not None:
        pos = copter_has_position(ss, ts, armed, reader)
    passed = ~over if pos is None else (~over & pos)

    states = np.where(passed, _RUN_GOOD, _RUN_BAD).astype(np.int8)
    if origin is not None:
        states[~origin] = _HOLD
    if kind == "copter":
        # has_ever_passed (ekf_check.cpp:56-62): nothing counts until
        # the checks have passed once. The firmware sets the latch only
        # from an observed pass, so the replay starts unlatched too; a
        # log that opens inside a bad interval stays held closed and the
        # ERR events decide what really happened -- the model does not
        # invent a trigger it never saw a pass for.
        latched = False
        for i in range(len(states)):
            if not latched:
                if states[i] == _RUN_GOOD:
                    latched = True
                else:
                    states[i] = _HOLD
    elif kind == "quadplane":
        reset = ~armed
        if gate is not None:
            reset = reset | ~np.asarray(gate, dtype=bool)
        states[reset] = _RESET

    windows = failsafe_windows(states, ts)

    # A window driven purely by the position gate: no variance breach on
    # any bad row inside it.
    pos_loss = []
    for start, end in windows:
        lo = int(np.searchsorted(ts, start, side="left"))
        hi = int(np.searchsorted(ts, end, side="right")) \
            if end is not None else len(ts)
        seg_bad = states[lo:hi] == _RUN_BAD
        seg_over = over[lo:hi]
        pos_loss.append(bool(seg_bad.any() and not seg_over[seg_bad].any()))

    return windows, pos_loss, over


def window_touches_armed(window, timestamps, armed_intervals) -> bool:
    """Whether a failsafe window overlaps any armed row."""
    start, end = window
    ts = np.asarray(timestamps, dtype=float)
    armed = _armed_row_mask(ts, armed_intervals)
    inside = (ts >= start) & (armed if end is None
                              else armed & (ts <= end))
    return bool(inside.any())
