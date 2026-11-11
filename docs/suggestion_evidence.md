# Suggestion evidence

Every parameter an analyzer can suggest, how the value is chosen, where that
rule comes from, and whether it has been flown. `tools/check_suggestions.py`
holds this table and the code together, in CI and in `tools/ci_local.py`.

A suggestion ends up in a `.param` file that someone may load on an aircraft,
so a new one needs a source for the value and a flight. See
[CONTRIBUTING.md](../CONTRIBUTING.md). SITL can rule a suggestion out, it cannot
prove one on an aircraft: `flown` means the value survived a flight in SITL.

Columns:

- **Parameter**: names or globs. A row is one rule, so a parameter suggested by
  two analyzers with two rules has two rows.
- **Rule**: how the value is chosen.
- **Source**: `A:` states this rule. `D:` documents the parameter, and the rule
  is derived from that. `none`: nothing authoritative for the value.
- **Flight**: runs from the table below, `-` for none.
- **Status**: `flown`; `not_flown`, which needs a waiver in
  `tools/check_suggestions.py`; `log_setting`, which changes what is logged, so
  a flight cannot grade it.

## Registry

| Parameter | Analyzer | Rule | Source | Flight | Status |
|---|---|---|---|---|---|
| `INS_HNTCH_ENABLE` | notch_tuning | 1 when a peak shows, the notch is off and the log can set the rest | A: wiki common-imu-notch-filtering | N3, Z1, Z5 | flown |
| `INS_HNTCH_FREQ` | notch_tuning | ESC tracking: 5th percentile of the motor frequency the ESCs report in the air, above INS_GYRO_FILTER. Throttle tracking: measured peak, batches from the air only, 10 dB above the floor | A: wiki common-esc-telem-based-notch, common-throttle-based-notch | E3, Z5, N3, Z2 | flown |
| `INS_HNTCH_BW` | notch_tuning | FREQ/2. Scaled with FREQ where a tracking notch has its FREQ lowered | A: wiki common-throttle-based-notch, common-esc-telem-based-notch | N2, N3, E3 | flown |
| `INS_HNTCH_MODE` | notch_tuning | 3 where every ESC reports RPM in the air, else 1 where the log has a level hover | A: wiki common-imu-notch-filtering | E3, Z5, Z2 | flown |
| `INS_HNTCH_REF` | notch_tuning | 1 for ESC or RPM tracking. For throttle tracking, the median throttle in level hover | A: wiki common-esc-telem-based-notch, common-throttle-based-notch | E2, E3, Z5, N7, Z2, T2 | flown |
| `INS_HNTCH_HMNCS` | notch_tuning | 3 | A: HarmonicNotchFilter.cpp default | N8, N9, N12 | flown |
| `INS_HNTCH_OPTS` | notch_tuning | 16 for a wide peak, else 0 | A: wiki common-imu-notch-filtering | N13 | flown |
| `INS_HNTC2_*` | notch_tuning | Same rules, for a second band | A: wiki common-imu-notch-filtering | - | not_flown |
| `ATC_RAT_RLL_FLTD` `ATC_RAT_PIT_FLTD` `Q_A_RAT_RLL_FLTD` `Q_A_RAT_PIT_FLTD` | notch_tuning | INS_GYRO_FILTER/2, when above it | A: wiki setting-up-for-tuning | F2, Z4 | flown |
| `MOT_THST_HOVER` `Q_M_THST_HOVER` | notch_tuning | Median throttle in level hover, where the parameter is over 0.12 from it | D: AP_MotorsMulticopter.cpp THST_HOVER, measured inside the gate of update_throttle_hover | H1 | flown |
| `ATC_RAT_*_D` `Q_A_RAT_*_D` | pid_tuning | x0.9, x0.8 when noise is critical, on an oscillating axis | A: wiki ac_rollpitchtuning | D1 | flown |
| `RLL_RATE_D` `PTCH_RATE_D` `YAW_RATE_D` | pid_tuning | Same rule, on the Plane rate loop | D: Copter rule applied to Plane | - | not_flown |
| `MOT_SPIN_MIN` `Q_M_SPIN_MIN` | motor | MOT_SPIN_ARM + 0.03 | A: wiki set-motor-range | S1 | flown |
| `TECS_SPDWEIGHT` | tecs_tuning | + 0.5, up to 2.0, with airspeed in use | D: AP_TECS.cpp SPDWEIGHT | P1 | flown |
| `ARSPD_USE` | airspeed | 0 on critical disagreement with GPS | D: AP_Airspeed_Params.cpp USE | - | not_flown |
| `AUTOTUNE_AXES` `Q_AUTOTUNE_AXES` | attitude | 7 | A: AC_AutoTune_Multi.cpp default | - | not_flown |
| `GPS_AUTO_SWITCH` | gps | 1 with two GPS and a GPS finding | A: AP_GPS.cpp default | - | not_flown |
| `COMPASS_USE` `COMPASS_USE2` `COMPASS_USE3` | compass_consistency | 0 for the outlier | D: AP_Compass.cpp USE | - | not_flown |
| `LOG_REPLAY` | log_health | 0 | D: AP_Logger.cpp REPLAY | - | log_setting |
| `LOG_BITMASK` | log_health | Vehicle default | D: vehicle default bitmask | - | log_setting |

Flown rows cover the rule in the row. These notch variants have not been flown:
the `INS_HNTCH_FREQ` floor in FFT mode, `INS_HNTCH_BW` widened to cover a peak
that lands near the edge of the band, and `INS_HNTCH_REF` 1 on an RPM sensor
notch, which takes the same path through the firmware as the ESC notch that
was flown.

Throttle tracking was flown through climbs and descents with REF at the hover
throttle and with a REF the throttle never reaches (T2, T1). The two came out
the same: the notch does not go below FREQ either way, and that is where the
motors were. The wiki's answer, a lower FREQ with REF scaled to match, is not
derived here.

## Flights

`tools/closed_loop.py`, three flights each way, figures from the logs, on
`copter_motor_noise` unless noted. Percentages are the change after the
suggestion. Run in November 2026, on ArduPilot `b96f0a5797`. The E and T runs
are on `copter_motor_noise_climbs`, against the same flight with no notch:
climbs at 8 m/s and descents at 5 m/s take the motors from 105 to 187 Hz, where
a hover holds them near 137 Hz.

| Run | Change | Result |
|---|---|---|
| N2 | Notch, BW 10 | Gyro peak -87%, motor jitter -70%, rate error -70% |
| N3 | Notch, BW 69, FREQ/2 | Gyro peak -97%, motor jitter -92%, rate error -92% |
| N7 | Throttle mode, BW 68 | Gyro peak -98%, motor jitter -94%, rate error -93% |
| N8 | 2nd harmonic present, HMNCS 1 | Gyro peak -82%, motor jitter -78%, rate error -80% |
| N9 | 2nd harmonic present, HMNCS 3 | Gyro peak -98%, motor jitter -93%, rate error -92% |
| N12 | HMNCS 3 with no 2nd harmonic | Gyro peak -97%, motor jitter -92%, rate error -92% |
| N13 | Triple notch, OPTS 16 | Gyro peak -98%, motor jitter -92%, rate error -92% |
| D1 | D x0.9 | Motor jitter -17%, rate error -2% |
| F2 | FLTD 20 to 10, gyro filter 20 | D noise -45%, tracking unchanged |
| S1 | MOT_SPIN_MIN 0.11 to 0.13, on `copter_normal` | Within 2% |
| P1 | TECS_SPDWEIGHT to 0.6, on `plane_tecs_tuning` | Speed error -21%, height error -20% |
| Z1 | ArduScope's own suggestions to v0.9.5, ESC mode with REF left at 0 | Gyro peak -98%, motor jitter -93%, rate error -92% |
| Z2 | ArduScope's own suggestions, throttle | Gyro peak -97%, motor jitter -93%, rate error -93% |
| Z4 | ArduScope's own suggestions, gyro filter 20 | Gyro peak -96%, motor jitter -38%, rate error -47% |
| Z5 | ArduScope's own suggestions, ESC mode with REF 1 and FREQ 119 | Gyro peak -99%, motor jitter -95%, rate error -95% |
| E1 | ESC mode as suggested to v0.9.5: REF 0, FREQ 119 at the peak | Gyro peak -67%, motor jitter -56%, rate error -57% |
| E2 | E1 with REF 1 | Gyro peak -94%, motor jitter -80%, rate error -86% |
| E3 | ArduScope's own suggestions: REF 1, FREQ 105, BW 52 | Gyro peak -97%, motor jitter -88%, rate error -94% |
| T1 | Throttle mode, no ESC telemetry, FREQ 138, REF 1.0 | Gyro peak -58%, motor jitter -65%, rate error -64% |
| T2 | ArduScope's own suggestions with no ESC telemetry: FREQ 138, REF 0.36 | Gyro peak -54%, motor jitter -63%, rate error -64% |
| H1 | MOT_THST_HOVER 0.20 to 0.36 with learning off, on `copter_normal`, FLTD 10 applied with it | Within 6%. 0.36 is where that aircraft hovers |

## Removed

What flights and the wiki took out. Do not add these back without new evidence.

| Suggestion | Removed | Why |
|---|---|---|
| `INS_HNTCH_FREQ` at the spectrum's peak, under ESC tracking | v0.9.6 | There FREQ is the lowest frequency the notch follows, and the wiki puts it below the hover frequency. At the peak the notch could not follow the motors down: rate error 0.93 deg/s against 0.41 (E2, E3) |
| `INS_HNTCH_REF` 0.50 for a hover throttle under 0.2 | v0.9.6 | No source. The wiki takes the hover throttle itself. These aircraft hover at 0.05 to 0.2, and their throttle never reaches 0.50 |
| A notch set from a log with no level hover and no ESC RPM in the air | v0.9.6 | The wiki sets a notch from a hover. 72 logs lost theirs: 50 were never in the air, 19 flew with no level hover and no ESC RPM, and 3 show no peak once the ground is left out |
| `MOT_THST_HOVER` from a log with no level hover | v0.9.6 | The median over every armed row above 5% throttle called a ground run a hover, and gave a crash at full throttle 0.69. 11 of 17 such suggestions are gone |
| `COMPASS_MOTCT` 1 | v0.9.4 | `AP_Compass.cpp` says not to change it by hand: CompassMot sets it, and the report now points there |
| `FS_EKF_THRESH` 0.8 | v0.9.4 | It loosened a failsafe after a critical variance. The report now says when a stricter value set the limit |
| `WP_RADIUS`, `WP_RADIUS_M`, `WPNAV_RADIUS` x1.5 | v0.9.4 | No source. For `WP_RADIUS_M` it also came out in centimetres, 100 times too large |
| `ATC_RAT_*_FLTD` x0.7, in pid_tuning | v0.9.4 | No source, and it could disagree with the wiki's INS_GYRO_FILTER/2, which notch_tuning applies |
| `MOT_SPIN_MIN` + 0.02, in servo_dither | v0.9.4 | No source |
| `Q_TRANSITION_MS` x1.5, `Q_TRAN_PIT_MAX` - 2 | v0.9.4 | No source |
| `ATC_THR_MIX_MAX` 0.7 | v0.9.3 | No effect in flight. The wiki raises it only after a good tune |
| `ATC_RAT_*_FLTD` raised | v0.9.3 | D-term noise doubled in three scenarios |
| `ATC_RAT_*_D` on the D term's share | v0.9.3 | A share stays high at any gain: 278 of 296 axes were under METHODIC's limit, and D fell 73% over nine rounds |
| `ATC_RAT_*_I` lowered | v0.9.2, v0.9.3 | The share means a steady offset, and lowering it raised error 20 to 31% |
| `TECS_PITCH_MAX`, `TECS_LAND_ARSPD` | v0.9.2 | Height error cannot show a pitch limit, as TECS holds its demand back when pitch clips. Raising it was 11 to 24% worse |
