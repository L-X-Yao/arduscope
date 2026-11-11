"""
Thresholds: analysis threshold configuration for flight log evaluation.

All thresholds have sensible community-standard defaults. The `from_params()`
classmethod auto-populates from log parameters when available, adapting to
the specific vehicle configuration. External config files (JSON) can override
any threshold via `load_overrides()`.
"""

import json
import math
from dataclasses import dataclass, fields

from .i18n import t


@dataclass
class Thresholds:
    """
    All analysis thresholds with defaults.

    Every threshold MUST be backed by an entry in
    `docs/convergence_criteria.md`. Cite the source ID in the comment.
    No "I think" or "looks reasonable" — only authoritative sources.

    Naming convention: <category>_<metric>_<level>
    Levels: warn = yellow, crit = red
    """

    # -- Vibration (m/s^2) --
    # Source A: METHODIC-VIBE + ARDU-WIKI-VIBE
    # METHODIC: <10 good, 10-15 grey, 15-20 concerning, 20-30 very bad
    # ARDU-WIKI-VIBE: <30 acceptable, >60 nearly always problematic
    # We adopt Methodic's conservative values — modern high-precision IMUs
    # (ICM-42688 etc.) degrade EKF quality above 15 m/s².
    vibe_warn: float = 15.0          # A: METHODIC-VIBE "concerning" boundary
    vibe_crit: float = 30.0          # A: METHODIC-VIBE "very bad" boundary
    vibe_clip_warn: int = 100        # A: ARDU-WIKI-VIBE "<100 acceptable"

    # -- EKF variance --
    # Source A: ARDU-SRC-EKF (ArduCopter/config.h:106)
    # FS_EKF_THRESH: 0.6=Strict, 0.8=Default, 1.0=Relaxed
    # Variance scale: 0=very trustworthy, 1.0=very untrustworthy
    # Warn = Strict preset: if variance exceeds the most conservative
    # failsafe threshold ArduPilot itself defines, flag it — even if
    # the user is running Default (0.8) or Relaxed (1.0). Keeps warn
    # anchored to an authoritative value rather than an arbitrary ratio.
    ekf_variance_warn: float = 0.6   # A: FS_EKF_THRESH Strict preset
    ekf_variance_crit: float = 0.8   # A: = FS_EKF_THRESH default, overridden

    # -- GPS --
    # Source A: ARDU-WIKI-PREARM (HDOP pre-arm=2.0), ARDU-SRC-GPS
    # GPS_HDOP_GOOD varies by board (140~200 = HDOP 1.4~2.0)
    gps_hdop_warn: float = 1.5       # D: quality target, stricter than pre-arm 2.0
    gps_hdop_crit: float = 2.5       # D: above pre-arm limit, GPS unreliable
    gps_nsats_warn: int = 10          # D: pre-arm min 6 + 1.67× buffer
    gps_nsats_crit: int = 6           # A: GPS-STD minimum for 3D fix
    gps_hacc_warn: float = 2.0       # D: crit / 2.5, early warning
    gps_hacc_crit: float = 5.0       # A: EKF3 hAccFail = hAcc > 5m (AP_NavEKF3_VehicleStatus.cpp:124)
    gps_no_fix_frac_crit: float = 0.01     # E: armed time without a 3D fix
    gps_high_hdop_frac_warn: float = 0.10  # E: armed time above gps_hdop_crit
    gps_low_nsats_frac_warn: float = 0.10  # E: armed time below gps_nsats_crit

    # -- Motor output --
    # Source A: ARDU-SRC-MOTOR (AP_MotorsMulticopter.h:14,16)
    # Saturation/imbalance have no wiki or Methodic thresholds.
    # Motor PWM saturation: fraction of active-flight samples at SPIN_MIN.
    # Physical reasoning: any non-zero saturation means the controller
    # wanted to drive a motor below SPIN_MIN (imbalance or over-throttle).
    # Below 10% is routinely observed on correctly balanced craft during
    # normal flight due to transient maneuvers — not diagnostically
    # meaningful. ≥10% starts to indicate persistent issue, ≥15% is a
    # real problem. Baseline 2026-04-16 (18 real logs): the 5%
    # threshold fired false positives on 4 logs with mild C3 asymmetry
    # (6-9% saturation, <7% imbalance) that flew fine.
    motor_saturation_warn: float = 0.10   # D: physical + 18-log baseline
    motor_saturation_crit: float = 0.15   # D: warn × 1.5, frequent limit
    motor_imbalance_warn: float = 0.10    # D: 10% asymmetry = real issue
    motor_imbalance_crit: float = 0.20    # D: imbalance_warn × 2
    mot_spin_min: float = 0.15            # A: ARDU-SRC-MOTOR, overridden by param
    mot_spin_arm: float = 0.10            # A: ARDU-SRC-MOTOR, overridden by param
    mot_spin_gap_min: float = 0.03        # A: METHODIC: SPIN_MIN >= SPIN_ARM + 0.03
    # Forward-thrust saturation: no METHODIC or wiki authority for pushers.
    fwd_thrust_sat_level: float = 0.95      # D: fraction of PWM_MAX, physical reasoning
    fwd_thrust_sat_frac_warn: float = 0.05  # D: flying but struggling
    fwd_thrust_sat_frac_crit: float = 0.20  # D: sustained thrust demand unmet

    # -- Battery --
    # Source A: ARDU-SRC-BATT (AP_BattMonitor_Params.cpp)
    # All read from flight params; 0 = not configured.
    batt_low_volt: float = 0.0       # A: overridden by BATT_LOW_VOLT
    batt_crt_volt: float = 0.0       # A: overridden by BATT_CRT_VOLT
    batt_low_mah: float = 0.0        # A: overridden by BATT_LOW_MAH
    batt_crt_mah: float = 0.0        # A: overridden by BATT_CRT_MAH
    # Charge-backfill calibration hint (analog monitors only): below
    # this consumption the charger's ~10 mAh refill resolution and
    # balance-charge overhead dominate the refill/consumed ratio.
    backfill_min_consumed_mah: float = 300.0  # D: keeps refill-reading error contribution under ~3%

    # -- Attitude tracking (degrees) --
    # Source A(soft): ARDU-SRC-AUTOTUNE AUTOTUNE_LEVEL_ANGLE_CD
    # (libraries/AC_AutoTune/AC_AutoTune.cpp:15,18)
    #   Copter: 250 cd = 2.5° — "angle which qualifies as level"
    #   Plane : 500 cd = 5.0° — relaxed variant
    # Relaxation ceiling: threshold_mul ∈ [0, 2] at 2s timeout (line 332),
    # so ArduPilot accepts up to 2× the base value as still-level.
    # This is the procedural gate AutoTune itself uses to decide whether
    # the craft is stable enough to run the next test — the closest
    # ArduPilot gets to quantifying "attitude tracking is OK". NOT a
    # tuning convergence standard, but a hard number from ArduPilot code.
    #
    # Not a hard authority for "tuning done" (neither wiki nor METHODIC
    # specifies that in degrees — they use RATE output RMS, covered by
    # pid_rate_out_warn=0.10), but the strongest anchor available for
    # attitude-level error and better than pure engineering guess.
    #
    # Defaults are for Copter; Plane overrides in from_params().
    att_rms_warn: float = 2.5        # A(soft): = AUTOTUNE_LEVEL_ANGLE_CD Copter
    att_rms_crit: float = 5.0        # A(soft): = Copter relaxation ceiling 2×

    # -- Compass --
    # Source A: ARDU-WIKI-COMPASS (offset >600 = "likely interference")
    # Source A: ARDU-WIKI-COMPASSMOT (<30% good, 30-60% grey, >60% too high)
    # COMPASS_OFS_MAX default=1800 (ARDU-SRC-COMPASS), pre-arm magnitude limit=500
    compass_ofs_warn: float = 600.0   # A: ARDU-WIKI-COMPASS interference indicator
    compass_ofs_crit: float = 1000.0  # D: well above pre-arm 500, still within OFS_MAX
    compass_interference_warn: float = 30.0   # A: ARDU-WIKI-COMPASSMOT "acceptable" limit
    compass_interference_crit: float = 60.0   # A: ARDU-WIKI-COMPASSMOT "too high" limit

    # -- IMU consistency --
    # Source A: ARDU-SRC-ARMING (AP_Arming.cpp:82,446)
    # Pre-arm: accel 0.75 m/s² (ARMING_ACCTHRESH), gyro 5 deg/s (hardcoded)
    # Our gradient: warn < pre-arm < crit (in-flight allows more than ground)
    imu_accel_diff_warn: float = 0.5   # D: < pre-arm 0.75, early warning
    imu_accel_diff_crit: float = 1.0   # D: > pre-arm 0.75, in-flight allowance
    imu_gyro_diff_warn: float = 0.05   # D: 2.9°/s, < pre-arm 5°/s
    imu_gyro_diff_crit: float = 0.1    # D: 5.7°/s, ≈ pre-arm 5°/s

    # -- Log health --
    # Source A: ARDU-SRC-PERF (PerfInfo.cpp:229, 1.2x = "slow loop")
    # Our warn is intentionally more relaxed than AP's 1.2x instant check
    # because we evaluate statistical averages, not individual loop spikes.
    loop_time_warn: float = 1.5    # D: statistical avg, > AP's 1.2x instant
    loop_time_crit: float = 2.0    # D: 2x loop = severe RTOS deadline miss
    cpu_load_warn: float = 80.0    # D: 20% headroom reserve for peaks
    cpu_load_crit: float = 95.0    # D: <5% headroom breaks real-time guarantee

    # -- RC input --
    rc_failsafe_pwm: int = 975     # A: typical RC failsafe PWM, overridden by FS_THR_VALUE

    # -- Temperature (°C) --
    # Source D: derived from component datasheets
    # STM32H743 ambient max 85°C (ST-DS-H743), ICM-42688-P max 85°C (TDK-DS-42688)
    # ArduPilot has NO built-in temperature warning mechanism.
    temp_warn: float = 60.0        # D: 70% of 85°C component limit
    temp_crit: float = 75.0        # D: 10°C margin before 85°C limit

    # -- Airspeed vs GPS ground-speed disagreement --
    # Source D: physical reasoning — in still wind the two should match
    # exactly; under typical wind (≤5 m/s component along the path) the
    # difference stays <5 m/s. Sustained >10 m/s disagreement is almost
    # always a pitot fault (blockage = over-read, leak = under-read).
    # No hard METHODIC/wiki threshold exists for this specific
    # cross-check; ArduPilot's EK3_ARSP_THR does use a cross-check but
    # for different purpose (fault switchover, not pilot-facing alert).
    airspeed_gps_diff_warn: float = 5.0    # D: normal wind envelope
    airspeed_gps_diff_crit: float = 10.0   # D: warn × 2, sensor-fault range
    airspeed_gps_frac_warn: float = 0.50   # D: majority-time threshold
    airspeed_gps_frac_crit: float = 0.30   # D: minority but large-gap samples
    airspeed_noise_frac_warn: float = 0.10  # D: >10% adjacent samples jump >5m/s
    airspeed_noise_frac_crit: float = 0.30  # D: >30% adjacent samples jump >5m/s
    airspeed_dual_rms_warn: float = 3.0    # D: dual sensor RMS disagreement (m/s)
    airspeed_dual_rms_crit: float = 5.0    # D: dual sensor RMS disagreement (m/s)

    # -- PID tuning --
    # Source A: METHODIC-PID (RATE.*out RMS is the authoritative metric)
    #   < 0.10 = acceptable (well-tuned)
    #   0.10-0.15 = borderline
    #   > 0.15 = oscillation problems
    pid_rate_out_warn: float = 0.10        # A: METHODIC-PID "acceptable" boundary
    pid_rate_out_crit: float = 0.15        # A: METHODIC-PID "oscillation" boundary
    # Ground-vibration fallback (armed but never lifted off):
    # Peak RATE.*out > 0.5 means the rate controller saturated at >50%
    # of max command — on a passive ground test this almost never
    # happens (typical <0.3). Paired with no-takeoff, it is a clear
    # "PID fighting oscillation but can't stabilize" signal (pilot's
    # "震荡未能起飞" failure mode). Normalised output range is defined
    # by AC_AttitudeControl (outputs clamped to [-1, +1]).
    pid_ground_vib_peak: float = 0.5       # D: PID output 50% saturation on ground
    pid_ground_vib_peak_crit: float = 0.8  # D: near-full saturation (>80%) on ground

    # Legacy/secondary metrics — no authoritative source, kept as supporting signals.
    pid_tracking_warn: float = 0.30        # E: RMS(Tar-Act)/RMS(Tar) ratio
    pid_tracking_crit: float = 0.50        # E: warn × 1.67
    pid_tracking_abs_warn: float = 5.0     # E: absolute floor (deg/s)
    pid_tracking_abs_crit: float = 10.0    # E: warn × 2
    pid_dterm_noise_warn: float = 0.3      # E: D-term stdev / total output stdev
    pid_dterm_noise_crit: float = 0.5      # E: warn × 1.67
    pid_iterm_ratio_warn: float = 0.6      # E: |I| / (|P|+|I|+|D|+|FF|) ratio
    pid_iterm_monotonic_warn: float = 0.92 # E: See round 6 fix rationale
    pid_osc_crossings_warn: float = 5.0    # E: zero-crossings/sec

    # -- Autotune result quality --
    # Source A: METHODIC-PID
    autotune_atc_p_min_acceptable: float = 4.5  # A: METHODIC "poor" boundary
    autotune_atc_p_excellent: float = 36.0      # A: METHODIC autotune ceiling

    # -- Convergence criteria ("drone is fully tuned") --
    # All MUST hold simultaneously to declare ✅ "调参完美" status.
    # Full rationale: docs/convergence_criteria.md
    converge_vibe_max: float = 15.0              # A: METHODIC-VIBE
    converge_clip_max: int = 100                 # A: ARDU-WIKI-VIBE
    converge_rate_out_max: float = 0.10          # A: METHODIC-PID
    converge_hover_thr_err: float = 0.05         # D: 5%, from MOT_THST_HOVER docs
    converge_ekf_variance_factor: float = 0.5    # D: max variance < 0.5 × FS_EKF_THRESH
    converge_ekf_var_max: float = 0.4            # D: 0.5 × 0.8 default

    # -- Hover --
    # Height above where the aircraft armed from which it counts as off the
    # ground, for hover thrust and motor noise on a log with no land
    # detector events (Plane logs none). A ground run has the throttle up
    # and is no flight. Copter's ground effect detector takes the takeoff
    # as done once the vehicle is this far above its takeoff altitude
    # (baro_ground_effect.cpp). On 46 QuadPlane logs the ground runs read
    # 0.2 m at most and the lowest hover 1.7 m.
    hover_airborne_alt_m: float = 0.5     # D: Copter ground effect detector's takeoff height

    # -- Notch tuning --
    # Depth of the dip a fixed notch leaves in the filtered gyro spectrum,
    # inside its band against beside it. Values informed by METHODIC-NOTCH
    # target -50dB; one field log (FREQ=13 BW=10 MODE=0) reads 32.3 dB. They
    # word the report line and grade nothing: a notch on a peak leaves a
    # smaller peak and no dip, so a missing dip is not a failed notch, and
    # what a notch removes takes the spectrum from before the filter too.
    # notch_atten_crit is also how far a peak has to stand above the floor
    # to be worth a notch.
    # Source A: METHODIC-NOTCH + HarmonicNotchFilter.cpp default 40dB.
    notch_atten_warn: float = 20.0        # A: dip that shows a fixed notch cutting
    notch_atten_crit: float = 10.0        # A: least attenuation worth having
    notch_atten_converge: float = 40.0    # A: METHODIC "well-tuned"

    # Secondary time-domain proxies: no source gives a limit for them.
    # They trigger independent checks (freq match, REF match, BW coverage).
    notch_hover_error_warn: float = 0.06  # E: |actual - configured| throttle
    notch_hover_error_crit: float = 0.12  # E: warn × 2
    notch_freq_drift_hz: float = 20.0     # E: |configured - FFT peak| tolerance
    notch_ref_error: float = 0.10         # E: |REF - actual hover throttle|
    notch_rate_max_crit: float = 100.0    # E: max angular rate (deg/s)
    notch_att_err_warn: float = 2.5       # E: mean |Roll-DesRoll| degrees
    notch_att_err_crit: float = 5.0       # E: warn × 2

    # -- Log health --
    # MissionPlanner LogAnalyzer TestPerformance defines slow-loop
    # WARN at 6% and FAIL at 10% (single-loop spike basis). Our
    # thresholds are for statistical long-loop rate (persistent lag),
    # so strict proportions are appropriate.
    log_long_loop_warn: float = 0.005    # D: MP 6% instant / ~12 = 0.5% sustained
    log_long_loop_crit: float = 0.05     # D: ≈ MP FAIL 10% / 2
    log_min_free_mem: int = 10240        # D: 1% of STM32H743 1MB RAM floor
    # DSF.Dp counts messages the logger dropped because its buffer was
    # full (AP_Logger_Backend.cpp `_dropped`) — unrecoverable data loss.
    # Fleet calibration (2026-07-15, 34 logs): healthy flights show
    # 0-13 drops (boot/arming transients); the one pathological log
    # dropped 116k with the buffer starved to 487 B. Any non-zero count
    # is reported as a detail; the warning fires two orders of
    # magnitude above the routine transient tail.
    log_dropped_msgs_warn: int = 100     # D: >>fleet transient tail (0-13) = sustained starvation

    # -- Board power (POWR) --
    # ArduPilot wiki "Diagnosing problems using logs" (POWR section):
    # "Variations of 0.10 to 0.15 volts are normal. Beyond that could
    # be a sign that other devices sharing the power source with the
    # autopilot are causing ripples in the power supply that could
    # lead to a brown-out."
    # ArduPilot wiki "Powering the Pixhawk": normal operation range for
    # power module / servo rail / USB input is 4.1 V to 5.7 V.
    board_vcc_sag_warn: float = 0.15   # A: wiki diagnosing-logs — >0.15 V Vcc variation abnormal
    board_vcc_low_crit: float = 4.1    # A: wiki powering — normal-operation lower bound
    board_vcc_high_crit: float = 5.7   # A: wiki powering — normal-operation upper bound
    # MCU.MTemp plausibility band. Outside it the sensor/scaling is
    # broken (e.g. STM32H7 Rev V ÷2 ADC divider, ST AN5312 — reads
    # hundreds of °C), so the envelope is reported as untrustworthy
    # rather than as a temperature.
    board_mcu_temp_valid_min: float = -40.0  # A: STM32 datasheet junction operating range
    board_mcu_temp_valid_max: float = 130.0  # A: STM32 Tj(max) 125 °C + sensor margin

    # -- Abrupt log end (power interruption forensics) --
    # ArduPilot keeps logging until disarm and always writes the
    # DISARMED event (AP_Logger stops via Write_Event on disarm), so a
    # log that stops while still armed means logging was killed
    # externally: power-path interruption, SD failure, or crash.
    # Gap = one PM reporting cycle (1 s) so "armed interval reaches log
    # end" tolerates the tail message jitter.
    log_abrupt_end_gap_s: float = 1.0    # D: 1× PM report cycle tail tolerance
    log_abrupt_airborne_alt_m: float = 3.0  # D: above WPNAV_RADIUS-class ground band; clearly flying

    # -- Throttle collapse / floor limit cycle (copter) --
    # In altitude-controlled modes the controller holds ~hover throttle;
    # a sustained collective output at the MOT_SPIN_MIN floor while
    # airborne means the altitude loop demanded "full down" (classic
    # trigger: baro ground-effect false climb). The margin sits just
    # above the mixer floor so normal descents don't fire.
    collapse_tho_margin: float = 0.03    # D: just above thrust 0, the mixer floor in ThO terms
    collapse_min_dur_s: float = 0.7      # D: > Z-controller transient; filters stick flicks
    collapse_climb_spike_ms: float = 1.5  # D: baro ground-effect spikes are m-scale in <2 s
    collapse_att_amp_deg: float = 20.0   # D: 2/3 of ANGLE_MAX default 30° — far beyond hover attitude
    collapse_osc_min_crossings: int = 4  # D: ≥2 full periods = sustained oscillation, not a bump

    # -- Throttle deadzone forensics (copter, info-only) --
    # get_pilot_desired_climb_rate() applies THR_DZ around mid-stick
    # (500 on the 0-1000 control_in scale): inside the band the climb
    # command stays at or below zero until the stick crosses the band
    # top. "Pushing up" = net rise between the window's first and last
    # quarter above RC noise.
    rc_dz_stick_move_pwm: float = 25.0   # D: ≥2× quality-RX noise floor (≤10 PWM)
    rc_dz_stuck_min_s: float = 3.0       # D: long enough to be a pilot intent, not a pass-through

    # -- RC input --
    # RC normalised range 1000-2000 PWM. 10 PWM = 1% of span chosen
    # as saturation-detect margin (noise floor is typically ≤5 PWM
    # on quality receivers, so 10 PWM separates real saturation from
    # signal noise).
    rc_pwm_min_sat: int = 1010           # D: 1% margin above 1000 PWM floor
    rc_pwm_max_sat: int = 1990           # D: 1% margin below 2000 PWM ceiling
    rc_sat_frac_warn: float = 0.05       # D: 5% time at endpoint = persistent

    # -- Compass --
    # Compass corrected-field variation threshold.
    # Source: ArduPilot compass_learn.cpp: variance > 35% triggers
    # "Compass X variance" GCS warning. Earth field at one location
    # is constant to within ~5%; variations above 35% on the
    # offset-corrected magnitude indicate real magnetic interference
    # (motors, cables, soft iron not captured by calibration).
    compass_field_var_warn: float = 35.0  # A: ArduPilot compass_learn.cpp

    # -- Gyro saturation --
    # Source D: physical reasoning — gyro sensor limit is ±2000 deg/s.
    # Fraction of armed samples where |GyrX/Y/Z| >= 1950 deg/s
    # (50 deg/s margin for noise). Any persistent saturation means the
    # flight controller is flying blind on rotation rate.
    gyro_sat_warn: float = 0.001   # D: 0.1% samples at gyro limit
    gyro_sat_crit: float = 0.01    # D: 1% samples at gyro limit

    # -- GPS glitch --
    # Source D: derived thresholds for GPS position jump detection.
    # A glitch is a position jump unexplainable by vehicle speed.
    # 1% samples showing jumps is a minor GPS environment issue;
    # 5% indicates persistent GPS problems (multipath, antenna).
    gps_glitch_warn: float = 0.01        # D: 1% samples show position jumps
    gps_glitch_crit: float = 0.05        # D: 5% samples show position jumps
    gps_glitch_min_dist: float = 5.0     # D: minimum 5m to count as glitch

    # -- Clock health --
    # Source D: timestamp quality metrics derived from log structure.
    # Jitter = std(dt) / median(dt); a ratio > 3.0 indicates the
    # scheduler is failing to maintain consistent message timing.
    # Gap count > 5 indicates frequent large timing holes.
    clock_jitter_warn: float = 3.0       # D: std/median ratio
    clock_gap_count_warn: int = 5        # D: number of large gaps

    # -- Compass consistency --
    # Source D: derived from physical reasoning. mGauss RMS difference
    # between compass field magnitudes. Earth field ~500 mGauss;
    # 100 mGauss RMS diff = 20% disagreement (moderate interference);
    # 200 mGauss = 40% disagreement (severe, compass data unreliable).
    compass_consist_warn: float = 100.0  # D: mGauss RMS field magnitude diff
    compass_consist_crit: float = 200.0  # D: mGauss, severe disagreement

    # -- Servo dither --
    # Source D: derived from signal processing reasoning.
    # Zero-crossing rate of first-difference of PWM output.
    # Normal servo movement has low ZC rate (<10 Hz). >20 Hz indicates
    # the controller is rapidly oscillating the output; >50 Hz is severe
    # dither that stresses servos and indicates control loop instability.
    servo_dither_warn: float = 20.0      # D: zero-crossing rate Hz
    servo_dither_crit: float = 50.0      # D: severe oscillation Hz

    # -- TECS tuning (fixed-wing / QuadPlane) --
    # Source D: derived from survey flight performance expectations.
    # Height: 5m RMS is acceptable for autonomous survey ops (altitude
    # hold at typical 50-100m AGL); 10m RMS indicates the controller
    # cannot track altitude commands adequately. Speed: 3 m/s RMS is
    # within normal wind gust compensation range; 5 m/s RMS means
    # TECS cannot maintain airspeed, risking stall or overspeed.
    tecs_height_err_warn: float = 5.0    # D: 5m RMS acceptable for survey
    tecs_height_err_crit: float = 10.0   # D: 10m RMS = poor height control
    tecs_speed_err_warn: float = 3.0     # D: 3 m/s RMS within wind envelope
    tecs_speed_err_crit: float = 5.0     # D: 5 m/s RMS = stall/overspeed risk

    # -- Temperature --
    # Sustained 30°C drift during a single flight is well outside
    # the ±5°C typical stability of mature IMU thermal designs and
    # signals either a failing thermal path or ongoing heat soak.
    temp_drift_warn: float = 30.0        # D: 30°C sustained drift = abnormal
    temp_inter_imu_warn: float = 10.0    # D: >10°C between IMUs = thermal path issue

    # -- VTOL transition --
    # D: alt loss during transition — 5m warn, 10m crit
    vtol_alt_loss_warn: float = 5.0      # D: noticeable sag during transition
    vtol_alt_loss_crit: float = 10.0     # D: dangerous altitude loss

    # -- Airspeed sensor health --
    airspeed_unhealthy_pct_warn: float = 10.0  # E: percent of samples unhealthy
    airspeed_unhealthy_pct_crit: float = 50.0  # E: percent of samples unhealthy
    airspeed_stuck_pct_warn: float = 5.0       # E: percent of samples stuck

    # -- Rangefinder --
    rangefinder_quality_pct_warn: float = 50.0  # E: percent of good readings

    # -- Mission --
    mission_completion_warn: float = 50.0   # E: percent of waypoints reached
    mission_accuracy_warn: float = 20.0     # E: mean waypoint miss distance, m

    # -- Error events --
    summary_error_count_warn: int = 5       # E: ERR events in one log

    # -- Wind (m/s) --
    wind_speed_warn: float = 10.0        # D: Beaufort 5/6 boundary, 10.8 m/s
    wind_speed_crit: float = 17.0        # D: Beaufort 8 starts at 17.2 m/s
    wind_gust_ratio_warn: float = 0.5    # E: std over mean wind speed
    wind_gust_min_mean: float = 2.0      # E: below this the ratio is noise

    def load_overrides(self, filepath: str):
        """
        Load threshold overrides from a JSON config file.

        The JSON file should be a flat dict of threshold names to values,
        e.g. {"vibe_warn": 20.0, "ekf_variance_crit": 1.0}

        Raises ValueError, naming the problem, on a file that cannot be
        used as written. Ignoring part of it would run the analysis on
        limits the user believes they replaced.
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                overrides = json.load(f)
        except OSError as e:
            raise ValueError(t("thresholds.err_unreadable",
                               path=filepath, detail=e)) from e
        except ValueError as e:
            raise ValueError(t("thresholds.err_bad_json",
                               path=filepath, detail=e)) from e
        if not isinstance(overrides, dict):
            raise ValueError(t("thresholds.err_not_object", path=filepath))
        unknown = set(overrides) - {fld.name for fld in fields(self)}
        if unknown:
            raise ValueError(t("thresholds.err_unknown_keys", path=filepath,
                               keys=", ".join(sorted(unknown))))
        for key, value in overrides.items():
            current = getattr(self, key)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or (isinstance(current, int) and value != int(value))):
                raise ValueError(t("thresholds.err_bad_value", path=filepath,
                                   key=key, value=value))
            setattr(self, key, type(current)(value))

    def to_dict(self) -> dict[str, float | int]:
        """Export all thresholds as a dict."""
        return {fld.name: getattr(self, fld.name) for fld in fields(self)}

    @classmethod
    def from_params(cls, params: dict[str, float],
                    vehicle_type: str = "Unknown") -> "Thresholds":
        """
        Create a Thresholds instance populated from log parameters.

        Args:
            params: dict of parameter name -> value from the log.
            vehicle_type: "ArduCopter", "ArduPlane", etc.

        Returns:
            Thresholds with values adjusted per the log's configuration.
        """
        t = cls()

        # EKF threshold from failsafe param
        fs_ekf = params.get('FS_EKF_THRESH', None)
        if fs_ekf is not None and fs_ekf > 0:
            t.ekf_variance_crit = fs_ekf
            # Anchor warn to Strict preset (0.6) — unless the user
            # configured FS_EKF_THRESH at or below Strict, in which case
            # warn cannot exceed crit.
            t.ekf_variance_warn = min(0.6, fs_ekf)
            # Convergence ceiling derives from FS_EKF_THRESH
            t.converge_ekf_var_max = fs_ekf * t.converge_ekf_variance_factor

        # Motor thresholds
        spin_min = params.get('MOT_SPIN_MIN', None)
        if spin_min is not None:
            t.mot_spin_min = spin_min
        spin_arm = params.get('MOT_SPIN_ARM', None)
        if spin_arm is not None:
            t.mot_spin_arm = spin_arm

        # Battery thresholds
        for attr, pname in [
            ('batt_low_volt', 'BATT_LOW_VOLT'),
            ('batt_crt_volt', 'BATT_CRT_VOLT'),
            ('batt_low_mah', 'BATT_LOW_MAH'),
            ('batt_crt_mah', 'BATT_CRT_MAH'),
        ]:
            val = params.get(pname, None)
            if val is not None and val > 0:
                setattr(t, attr, val)

        # RC failsafe PWM
        rc_fs = params.get('FS_THR_VALUE', None)
        if rc_fs is not None and rc_fs > 0:
            t.rc_failsafe_pwm = int(rc_fs)

        # Vehicle-type adjustments
        if 'Copter' in vehicle_type:
            # Copters are more sensitive to vibrations
            pass  # defaults are already tuned for copters
        elif 'Plane' in vehicle_type:
            # Plane: AUTOTUNE_LEVEL_ANGLE_CD = 500 cd (5°), relaxation ×2 → 10°
            # libraries/AC_AutoTune/AC_AutoTune.cpp:15
            t.att_rms_warn = 5.0
            t.att_rms_crit = 10.0
            # QuadPlane VTOL motor params
            q_spin_min = params.get('Q_M_SPIN_MIN', None)
            if q_spin_min is not None:
                t.mot_spin_min = q_spin_min

        return t
