# Convergence Criteria

Threshold traceability table for all fields in `lib/thresholds.py`.

METHODIC is the [ArduPilot Methodic Configurator's tuning guide](https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter.html).

Source key:
- **A** -- Authoritative: METHODIC documentation, ArduPilot wiki, or ArduPilot source code.
- **A(soft)** -- Authoritative but indirect: ArduPilot code value used as proxy (not its original purpose).
- **D** -- Derived: calculated from an authoritative source with stated reasoning.
- **E** -- Engineering estimate: legacy/secondary metric, no authoritative source.

| Field | Value | Source | Citation |
|-------|-------|--------|----------|
| `vibe_warn` | 15.0 | A | METHODIC-VIBE "concerning" boundary |
| `vibe_crit` | 30.0 | A | METHODIC-VIBE "very bad" boundary |
| `vibe_clip_warn` | 100 | A | ARDU-WIKI-VIBE "<100 acceptable" |
| `ekf_variance_warn` | 0.6 | A | FS_EKF_THRESH Strict preset (ArduCopter/config.h:106) |
| `ekf_variance_crit` | 0.8 | A | FS_EKF_THRESH default, overridden by log param |
| `gps_hdop_warn` | 1.5 | D | quality target, stricter than pre-arm 2.0 |
| `gps_hdop_crit` | 2.5 | D | above pre-arm limit, GPS unreliable |
| `gps_nsats_warn` | 10 | D | pre-arm min 6 + 1.67x buffer |
| `gps_nsats_crit` | 6 | A | GPS-STD minimum for 3D fix |
| `gps_hacc_warn` | 2.0 | D | crit / 2.5, early warning |
| `gps_hacc_crit` | 5.0 | A | EKF3 hAccFail = hAcc > 5m (AP_NavEKF3_VehicleStatus.cpp:124) |
| `motor_saturation_warn` | 0.10 | D | physical reasoning + 18-log baseline |
| `motor_saturation_crit` | 0.15 | D | warn x 1.5, frequent limit |
| `motor_imbalance_warn` | 0.10 | D | 10% asymmetry = real issue |
| `motor_imbalance_crit` | 0.20 | D | imbalance_warn x 2 |
| `mot_spin_min` | 0.15 | A | ARDU-SRC-MOTOR (AP_MotorsMulticopter.h:14,16), overridden by param |
| `mot_spin_arm` | 0.10 | A | ARDU-SRC-MOTOR (AP_MotorsMulticopter.h:14,16), overridden by param |
| `batt_low_volt` | 0.0 | A | overridden by BATT_LOW_VOLT (AP_BattMonitor_Params.cpp) |
| `batt_crt_volt` | 0.0 | A | overridden by BATT_CRT_VOLT (AP_BattMonitor_Params.cpp) |
| `batt_low_mah` | 0.0 | A | overridden by BATT_LOW_MAH (AP_BattMonitor_Params.cpp) |
| `batt_crt_mah` | 0.0 | A | overridden by BATT_CRT_MAH (AP_BattMonitor_Params.cpp) |
| `att_rms_warn` | 2.5 | A(soft) | AUTOTUNE_LEVEL_ANGLE_CD Copter (AC_AutoTune.cpp:15,18) |
| `att_rms_crit` | 5.0 | A(soft) | Copter relaxation ceiling 2x (AC_AutoTune.cpp:332) |
| `compass_ofs_warn` | 600.0 | A | ARDU-WIKI-COMPASS "likely interference" indicator |
| `compass_ofs_crit` | 1000.0 | D | well above pre-arm 500, still within OFS_MAX 1800 |
| `compass_interference_warn` | 30.0 | A | ARDU-WIKI-COMPASSMOT "acceptable" limit |
| `compass_interference_crit` | 60.0 | A | ARDU-WIKI-COMPASSMOT "too high" limit |
| `imu_accel_diff_warn` | 0.5 | D | < pre-arm 0.75 (ARMING_ACCTHRESH), early warning |
| `imu_accel_diff_crit` | 1.0 | D | > pre-arm 0.75, in-flight allowance |
| `imu_gyro_diff_warn` | 0.05 | D | 2.9 deg/s, < pre-arm 5 deg/s (AP_Arming.cpp:82,446) |
| `imu_gyro_diff_crit` | 0.1 | D | 5.7 deg/s, approx pre-arm 5 deg/s |
| `loop_time_warn` | 1.5 | D | statistical avg, > AP's 1.2x instant (PerfInfo.cpp:229) |
| `loop_time_crit` | 2.0 | D | 2x loop = severe RTOS deadline miss |
| `cpu_load_warn` | 80.0 | D | 20% headroom reserve for peaks |
| `cpu_load_crit` | 95.0 | D | <5% headroom breaks real-time guarantee |
| `rc_failsafe_pwm` | 975 | A | typical RC failsafe PWM, overridden by FS_THR_VALUE |
| `temp_warn` | 60.0 | D | 70% of 85 deg C component limit (STM32H743/ICM-42688-P datasheets) |
| `temp_crit` | 75.0 | D | 10 deg C margin before 85 deg C limit |
| `airspeed_gps_diff_warn` | 5.0 | D | normal wind envelope |
| `airspeed_gps_diff_crit` | 10.0 | D | warn x 2, sensor-fault range |
| `airspeed_gps_frac_warn` | 0.50 | D | majority-time threshold |
| `airspeed_gps_frac_crit` | 0.30 | D | minority but large-gap samples |
| `pid_rate_out_warn` | 0.10 | A | METHODIC-PID "acceptable" boundary |
| `pid_rate_out_crit` | 0.15 | A | METHODIC-PID "oscillation" boundary |
| `pid_ground_vib_peak` | 0.5 | D | PID output 50% saturation on ground |
| `pid_ground_vib_peak_crit` | 0.8 | D | near-full saturation (>80%) on ground |
| `pid_tracking_warn` | 0.30 | E | RMS(Tar-Act)/RMS(Tar) ratio |
| `pid_tracking_crit` | 0.50 | E | warn x 1.67 |
| `pid_tracking_abs_warn` | 5.0 | E | absolute floor (deg/s) |
| `pid_tracking_abs_crit` | 10.0 | E | warn x 2 |
| `pid_dterm_noise_warn` | 0.3 | E | D-term stdev / total output stdev |
| `pid_dterm_noise_crit` | 0.5 | E | warn x 1.67 |
| `pid_iterm_ratio_warn` | 0.6 | E | abs(I) / (abs(P)+abs(I)+abs(D)+abs(FF)) ratio |
| `pid_iterm_monotonic_warn` | 0.92 | E | See round 6 fix rationale |
| `pid_osc_crossings_warn` | 5.0 | E | zero-crossings/sec |
| `autotune_atc_p_min_acceptable` | 4.5 | A | METHODIC-PID "poor" boundary |
| `autotune_atc_p_excellent` | 36.0 | A | METHODIC autotune ceiling |
| `converge_vibe_max` | 15.0 | A | METHODIC-VIBE |
| `converge_clip_max` | 100 | A | ARDU-WIKI-VIBE |
| `converge_rate_out_max` | 0.10 | A | METHODIC-PID |
| `converge_hover_thr_err` | 0.05 | D | 5%, from MOT_THST_HOVER docs |
| `converge_ekf_variance_factor` | 0.5 | D | max variance < 0.5 x FS_EKF_THRESH |
| `converge_ekf_var_max` | 0.4 | D | 0.5 x 0.8 default |
| `hover_airborne_alt_m` | 0.5 | D | off the ground, for hover thrust and motor noise where the log has no land detector events: Copter's ground effect detector takes the takeoff as done 0.5 m above the takeoff altitude (ArduCopter/baro_ground_effect.cpp) |
| `notch_atten_warn` | 20.0 | A | dip that shows a fixed notch cutting; reported, not graded (METHODIC-NOTCH) |
| `notch_atten_crit` | 10.0 | A | least attenuation worth having: a peak must stand this far above the floor (METHODIC-NOTCH) |
| `notch_atten_converge` | 40.0 | A | METHODIC "well-tuned" + HarmonicNotchFilter.cpp default 40dB |
| `notch_hover_error_warn` | 0.06 | E | abs(actual - configured) throttle |
| `notch_hover_error_crit` | 0.12 | E | warn x 2 |
| `notch_freq_drift_hz` | 20.0 | E | abs(configured - FFT peak) tolerance |
| `notch_ref_error` | 0.10 | E | abs(REF - actual hover throttle) |
| `notch_rate_max_crit` | 100.0 | E | max angular rate (deg/s) |
| `notch_att_err_warn` | 2.5 | E | mean abs(Roll-DesRoll) degrees |
| `notch_att_err_crit` | 5.0 | E | warn x 2 |
| `log_long_loop_warn` | 0.005 | D | MP 6% instant / ~12 = 0.5% sustained |
| `log_long_loop_crit` | 0.05 | D | approx MP FAIL 10% / 2 |
| `log_min_free_mem` | 10240 | D | 1% of STM32H743 1MB RAM floor |
| `rc_pwm_min_sat` | 1010 | D | 1% margin above 1000 PWM floor |
| `rc_pwm_max_sat` | 1990 | D | 1% margin below 2000 PWM ceiling |
| `rc_sat_frac_warn` | 0.05 | D | 5% time at endpoint = persistent |
| `compass_field_var_warn` | 35.0 | A | ArduPilot compass_learn.cpp variance > 35% triggers warning |
| `temp_drift_warn` | 30.0 | D | 30 deg C sustained drift = abnormal |
| `log_abrupt_end_gap_s` | 1.0 | D | 1x PM report cycle: armed-interval end reaching log end within one cycle = no disarm was written (AP_Logger writes DISARMED event on disarm) |
| `log_abrupt_airborne_alt_m` | 3.0 | D | above landing-detector ground band; height above home > 3 m at cutoff = unambiguously flying |
| `collapse_tho_margin` | 0.03 | D | just above the mixer floor: CTUN.ThO is a thrust, and thrust 0 puts the motors at MOT_SPIN_MIN (AP_MotorsMatrix.cpp, thrust_to_actuator), so ThO <= 0.03 means output at the floor whatever MOT_SPIN_MIN is |
| `collapse_min_dur_s` | 0.7 | D | longer than Z-controller transients and stick flicks; sustained floor demand only |
| `collapse_climb_spike_ms` | 1.5 | D | baro ground-effect (prop wash) spikes are metre-scale within ~2 s (ARDU-WIKI ground effect); 1.5 m/s separates them from normal climb noise |
| `collapse_att_amp_deg` | 20.0 | D | 2/3 of ANGLE_MAX default 30 deg (ArduCopter Parameters.cpp) — far beyond hover attitude, unambiguous loss of tracking |
| `collapse_osc_min_crossings` | 4 | D | >= 2 full periods of the demeaned signal = sustained oscillation, not a single upset |
| `rc_dz_stick_move_pwm` | 25.0 | D | >= 2x quality-receiver noise floor (<= 10 PWM, see rc_pwm_min_sat note) — net upward rise across the window, a genuine push |
| `rc_dz_stuck_min_s` | 3.0 | D | long enough to represent pilot intent rather than a transient pass-through of the band |
| `backfill_min_consumed_mah` | 300 | D | charger refill resolution (~10 mAh) plus balance-charge overhead; above 300 mAh their contribution to the refill/consumed ratio stays under ~3% |
| `gps_no_fix_frac_crit` | 0.01 | E | armed time without a 3D fix |
| `gps_high_hdop_frac_warn` | 0.1 | E | armed time above gps_hdop_crit |
| `gps_low_nsats_frac_warn` | 0.1 | E | armed time below gps_nsats_crit |
| `mot_spin_gap_min` | 0.03 | A | METHODIC: SPIN_MIN >= SPIN_ARM + 0.03 |
| `fwd_thrust_sat_level` | 0.95 | D | fraction of PWM_MAX, physical reasoning |
| `fwd_thrust_sat_frac_warn` | 0.05 | D | flying but struggling |
| `fwd_thrust_sat_frac_crit` | 0.2 | D | sustained thrust demand unmet |
| `airspeed_noise_frac_warn` | 0.1 | D | >10% adjacent samples jump >5m/s |
| `airspeed_noise_frac_crit` | 0.3 | D | >30% adjacent samples jump >5m/s |
| `airspeed_dual_rms_warn` | 3.0 | D | dual sensor RMS disagreement (m/s) |
| `airspeed_dual_rms_crit` | 5.0 | D | dual sensor RMS disagreement (m/s) |
| `log_dropped_msgs_warn` | 100 | D | >>fleet transient tail (0-13) = sustained starvation |
| `board_vcc_sag_warn` | 0.15 | A | wiki diagnosing-logs — >0.15 V Vcc variation abnormal |
| `board_vcc_low_crit` | 4.1 | A | wiki powering — normal-operation lower bound |
| `board_vcc_high_crit` | 5.7 | A | wiki powering — normal-operation upper bound |
| `board_mcu_temp_valid_min` | -40.0 | A | STM32 datasheet junction operating range |
| `board_mcu_temp_valid_max` | 130.0 | A | STM32 Tj(max) 125 °C + sensor margin |
| `gyro_sat_warn` | 0.001 | D | 0.1% samples at gyro limit |
| `gyro_sat_crit` | 0.01 | D | 1% samples at gyro limit |
| `gps_glitch_warn` | 0.01 | D | 1% samples show position jumps |
| `gps_glitch_crit` | 0.05 | D | 5% samples show position jumps |
| `gps_glitch_min_dist` | 5.0 | D | minimum 5m to count as glitch |
| `clock_jitter_warn` | 3.0 | D | std/median ratio |
| `clock_gap_count_warn` | 5 | D | number of large gaps |
| `compass_consist_warn` | 100.0 | D | mGauss RMS field magnitude diff |
| `compass_consist_crit` | 200.0 | D | mGauss, severe disagreement |
| `servo_dither_warn` | 20.0 | D | zero-crossing rate Hz |
| `servo_dither_crit` | 50.0 | D | severe oscillation Hz |
| `tecs_height_err_warn` | 5.0 | D | 5m RMS acceptable for survey |
| `tecs_height_err_crit` | 10.0 | D | 10m RMS = poor height control |
| `tecs_speed_err_warn` | 3.0 | D | 3 m/s RMS within wind envelope |
| `tecs_speed_err_crit` | 5.0 | D | 5 m/s RMS = stall/overspeed risk |
| `temp_inter_imu_warn` | 10.0 | D | >10°C between IMUs = thermal path issue |
| `vtol_alt_loss_warn` | 5.0 | D | noticeable sag during transition |
| `vtol_alt_loss_crit` | 10.0 | D | dangerous altitude loss |
| `airspeed_unhealthy_pct_warn` | 10.0 | E | percent of samples unhealthy |
| `airspeed_unhealthy_pct_crit` | 50.0 | E | percent of samples unhealthy |
| `airspeed_stuck_pct_warn` | 5.0 | E | percent of samples stuck |
| `rangefinder_quality_pct_warn` | 50.0 | E | percent of good readings |
| `mission_completion_warn` | 50.0 | E | percent of waypoints reached |
| `mission_accuracy_warn` | 20.0 | E | mean waypoint miss distance, m |
| `summary_error_count_warn` | 5 | E | ERR events in one log |
| `wind_speed_warn` | 10.0 | D | Beaufort 5/6 boundary, 10.8 m/s |
| `wind_speed_crit` | 17.0 | D | Beaufort 8 starts at 17.2 m/s |
| `wind_gust_ratio_warn` | 0.5 | E | std over mean wind speed |
| `wind_gust_min_mean` | 2.0 | E | below this the ratio is noise |
