# Log message coverage map

Which ArduPilot `.BIN` message types exist in real logs, and which of
them ArduScope's analyzers actually consume. Every type that appears in
logs but is referenced nowhere in `analyzers/` or `lib/` is a diagnostic
dimension the tool is blind to.

- **Snapshot date:** 2026-07-15
- **Corpus:** 20 QuadPlane field logs +
  14 SITL logs (`../ardupilot/logs`)
- **Result:** 116 message types (logger bookkeeping excluded),
  33 consumed, 83 unreferenced.

Regenerate the raw table with:

```
python3 tools/scan_msg_coverage.py --group customer <dir> --group sitl <dir>
```

Consumption detection is a heuristic (any quoted ALL-CAPS token in
`analyzers/*.py` / `lib/*.py` counts as a reference) — verify a row
before acting on it.

## Tier 1 — implemented (2026-07 iteration)

| Type(s) | Coverage (customer) | Consumer | Dimension |
|---------|--------------------|----------|-----------|
| POWR, MCU | 20/20 | `board_power` analyzer | Board 5V rail sag / brownout forensics, MCU temp & rail envelope, MAV_POWER_STATUS flag events (overcurrent, brick loss) |
| DSF | 20/20 | `log_health` | Logger dropout count (`Dp`) — meta-dimension: when the log itself lost messages, other conclusions degrade |
| RFRH (presence only) | 2/20 | `log_health` | LOG_REPLAY enabled → replay stream bloats the log; info + suggestion |
| PIQR/PIQP/PIQY/PIQA | 15/20 | `pid_tuning` | QuadPlane VTOL-phase rate PID tracking/windup (plane-phase PIDR/PIDP alone misses the hover phase entirely) |
| PSCN/PSCE/PSCD | 15/20 | `pid_tuning` | VTOL position-controller desired-vs-actual velocity tracking (info metrics) |

## Tier 2 — candidates (waiting for a real diagnostic question)

Promote when a customer case or tuning workflow actually needs the
dimension; every promoted check still needs an authoritative threshold
source per POSITIONING.md.

**System / hardware health**

| Type | Coverage | What it would enable |
|------|----------|----------------------|
| STAK | 20/20 | Per-thread stack headroom — overflow precursor |
| UART | 20/20 | Per-UART throughput/error counters — serial link quality |
| IOMC | 17/20 | IOMCU status — in-flight IOMCU reset detection |
| HEAT | 17/20 | IMU heater target vs actual — hardware QA on shipped boards |
| TSYN | 20/20 | Time-sync round trips — companion/GCS clock skew |
| MAVC | 20/20 | Executed MAVLink commands — who commanded what, when |
| MAV | 20/20 | Per-channel MAVLink txp/rxp/rxdp — telemetry link dropouts (customer "link loss" complaints) |

**GPS / navigation sources**

| Type | Coverage | What it would enable |
|------|----------|----------------------|
| UBX2 | 20/20 | uBlox RF samples (ofsI/magI) — RF front-end health |
| UBX1 | 3/20 | uBlox noise floor + jamming indicator |
| GPYW | 17/20 | GPS-for-yaw (moving baseline) health on dual-GPS builds |
| CANS / CAND | 17/20 | DroneCAN bus error counters / node inventory — wiring & termination QA |

**EKF depth**

| Type | Coverage | What it would enable |
|------|----------|----------------------|
| XKF5 | 20/20 | Optical-flow / AGL innovations |
| XKV1/XKV2 | 20/20 | Full per-lane variance vector |
| XKT | 20/20 | EKF core timing (buffer delays) |
| XKFM | 17/20 | On-ground-and-not-moving check diagnostics |
| XKY0/XKY1 | 15/20 | GSF yaw-estimator diagnostics |
| DCM / EAHR / AHR2 | 20/20 | Fallback estimator vs EKF divergence cross-check |
| XKFS / XKQ | 20/20 | EKF sensor selection / quaternions |

**Plane / QuadPlane control**

| Type | Coverage | What it would enable |
|------|----------|----------------------|
| CTRL | 18/20 | Attitude-control oscillation monitor — firmware's own oscillation verdict |
| ANG | 18/20 | Attitude target vs actual at loop rate |
| NTUN | 17/20 | Plane nav tuning: xtrack error, WP tracking |
| AETR | 17/20 | Pre-mixer control surface outputs — surface saturation |
| AOA | 17/20 | Angle of attack / sideslip envelope |
| STAT | 17/20 | is_flying / stage flags — phase segmentation without mode inference |
| PIDN/PIDE | 17/20 | Plane nav PID internals |
| TEC2/TEC3 | 14/20 | TECS internals beyond TECS msg |
| ATSC | 14/20 | Attitude controller scale factors |
| QPOS | 15/20 | QuadPlane position controller state |
| LAND | 16/20 | Plane landing stage/flare forensics |
| ESCX | 17/20 | Extended ESC telemetry (error rates) beyond ESC RPM |
| RCI2/RCO2 | 20/20 | RC channels 15+ — invisible to rc_input/motor today |

**Feature usage (promote only if customers use the feature)**

| Type | Coverage | What it would enable |
|------|----------|----------------------|
| PL | 17/20 | Precision landing full-cycle analysis (316k rows currently unread) |
| SURF | 3/20 | Surface tracking state |
| TERR | 3/20 | Terrain-following data |
| MISE | 9/20 | Mission item execution trace |
| PPLD | 7/20 | Payload-place state machine |
| QWIK | 1/20 | Quicktune session trace |
| AUXF | 16/20 | Aux switch actions — timeline reconstruction ("pilot flipped RTL") |
| ARM | 15/20 | Arm/disarm method + forced flag |
| ORGN | 17/20 | EKF/AHRS origin sets |
| DU32 | 3/20 | Copter subsystem bitmask storage |

## No action

| Type(s) | Reason |
|---------|--------|
| RISH/RISI, RFRH/RFRF/RFRN, RBRH/RBRI, RGPH/RGPI/RGPJ, RMGH/RMGI, RASH/RASI, RRNH/RRNI, REV3 | LOG_REPLAY input streams for offline EKF replay — not analyzer material; log_health flags the bloat (Tier 1) |
| SIM/SIM2, XKTV, SRTL, D32 | SITL-only or storage internals; absent from customer logs |

## Raw inventory (generated 2026-07-15)

| Type | customer logs (of 20) | customer rows | sitl logs (of 14) | sitl rows | Referenced in |
|------|------|------|------|------|------|
| IMU | 20 | 8555999 | 14 | 646118 | analyzers/__init__.py, analyzers/gyro_saturation.py, analyzers/imu_consistency.py, analyzers/notch_tuning.py, analyzers/temperature.py, analyzers/vibration.py, lib/vehicle_detect.py |
| VIBE | 20 | 1104086 | 14 | 55172 | analyzers/notch_tuning.py, analyzers/vibration.py |
| XKF4 | 20 | 752602 | 14 | 23043 | analyzers/ekf.py, analyzers/failsafe.py |
| XKF1 | 20 | 752601 | 14 | 23038 | analyzers/flight_profile.py |
| XKF2 | 20 | 752601 | 14 | 23038 | analyzers/wind.py |
| XKF3 | 20 | 752599 | 14 | 23037 | analyzers/ekf.py |
| XKFS | 20 | 752598 | 14 | 23032 | — |
| XKQ | 20 | 752598 | 14 | 23031 | — |
| CTUN | 20 | 376636 | 14 | 27591 | analyzers/log_health.py, analyzers/notch_tuning.py, analyzers/rangefinder.py, analyzers/throttle_collapse.py |
| ATT | 20 | 376583 | 14 | 11529 | analyzers/attitude.py, analyzers/clock_health.py, analyzers/flight_profile.py, analyzers/log_health.py, analyzers/notch_tuning.py, analyzers/throttle_collapse.py, analyzers/vtol_transition.py, lib/log_reader.py, lib/pipeline.py |
| RCIN | 20 | 376545 | 14 | 27590 | analyzers/rc_input.py |
| RCI2 | 20 | 376542 | 14 | 27589 | — |
| PIDR | 20 | 376476 | 14 | 11525 | analyzers/attitude.py, analyzers/pid_tuning.py |
| PIDP | 20 | 376475 | 14 | 11525 | analyzers/attitude.py, analyzers/pid_tuning.py |
| RCOU | 20 | 376465 | 14 | 27589 | analyzers/compass.py, analyzers/motor.py, analyzers/notch_tuning.py, analyzers/servo_dither.py, analyzers/throttle_collapse.py, lib/pipeline.py |
| RCO2 | 20 | 376463 | 14 | 27588 | — |
| XKF5 | 20 | 376301 | 14 | 11520 | — |
| POS | 20 | 373970 | 14 | 11511 | analyzers/flight_profile.py, analyzers/mission.py, analyzers/pid_tuning.py, lib/pipeline.py |
| BAT | 20 | 306717 | 5 | 833 | analyzers/battery.py, analyzers/cruise_efficiency.py, analyzers/failsafe.py, analyzers/log_health.py, analyzers/power.py, analyzers/temperature.py, lib/log_reader.py, lib/pipeline.py |
| BARO | 20 | 306591 | 14 | 23058 | analyzers/flight_profile.py |
| POWR | 20 | 166386 | 0 | 0 | — |
| MCU | 20 | 166327 | 0 | 0 | — |
| MAG | 20 | 166192 | 14 | 34584 | analyzers/compass.py, analyzers/compass_consistency.py |
| DCM | 20 | 165704 | 14 | 11493 | — |
| EAHR | 20 | 165522 | 14 | 11494 | — |
| STAK | 20 | 163158 | 0 | 0 | — |
| GPS | 20 | 152935 | 14 | 5770 | analyzers/airspeed.py, analyzers/cruise_efficiency.py, analyzers/failsafe.py, analyzers/flight_profile.py, analyzers/gps.py, analyzers/gps_glitch.py, analyzers/mission.py, analyzers/power.py, analyzers/summary.py, lib/log_reader.py, lib/pipeline.py |
| GPA | 20 | 152820 | 14 | 5770 | analyzers/gps.py |
| UART | 20 | 116402 | 14 | 2292 | — |
| MAV | 20 | 58444 | 14 | 3272 | — |
| XKV1 | 20 | 32069 | 14 | 2304 | — |
| XKV2 | 20 | 32069 | 14 | 2304 | — |
| DSF | 20 | 16336 | 14 | 1130 | — |
| XKT | 20 | 6572 | 14 | 446 | — |
| UBX2 | 20 | 3322 | 0 | 0 | — |
| PM | 20 | 3087 | 14 | 224 | analyzers/log_health.py |
| MAVC | 20 | 2042 | 14 | 23 | — |
| TSYN | 20 | 1646 | 0 | 0 | — |
| MODE | 20 | 123 | 14 | 34 | analyzers/throttle_collapse.py, lib/log_reader.py |
| RFND | 19 | 734221 | 0 | 0 | analyzers/rangefinder.py, lib/log_reader.py |
| RATE | 18 | 450018 | 14 | 308374 | analyzers/attitude.py, analyzers/notch_tuning.py, analyzers/pid_tuning.py, lib/pipeline.py |
| ANG | 18 | 420515 | 14 | 308383 | — |
| MOTB | 18 | 45580 | 14 | 10762 | analyzers/motor.py |
| CTRL | 18 | 45575 | 14 | 10762 | — |
| NTUN | 17 | 351166 | 9 | 26758 | — |
| AETR | 17 | 351165 | 9 | 26754 | — |
| AHR2 | 17 | 349198 | 14 | 11512 | — |
| PL | 17 | 316109 | 0 | 0 | — |
| PIDE | 17 | 167656 | 9 | 10248 | — |
| PIDN | 17 | 167656 | 9 | 10249 | — |
| AOA | 17 | 140465 | 9 | 10675 | — |
| ARSP | 17 | 140465 | 9 | 10696 | analyzers/airspeed.py, analyzers/cruise_efficiency.py, analyzers/flight_profile.py, lib/log_reader.py |
| XKFM | 17 | 95215 | 9 | 452 | — |
| STAT | 17 | 70311 | 9 | 5373 | — |
| ESC | 17 | 69358 | 14 | 568092 | analyzers/notch_tuning.py |
| ESCX | 17 | 69358 | 14 | 568042 | — |
| GPYW | 17 | 64271 | 0 | 0 | — |
| CANS | 17 | 14031 | 0 | 0 | — |
| IOMC | 17 | 13454 | 0 | 0 | — |
| HEAT | 17 | 13421 | 0 | 0 | — |
| ORGN | 17 | 3343 | 14 | 50 | — |
| EV | 17 | 114 | 13 | 17 | lib/log_reader.py |
| CAND | 17 | 66 | 0 | 0 | — |
| LAND | 16 | 320 | 9 | 144 | — |
| AUXF | 16 | 83 | 0 | 0 | — |
| XKY0 | 15 | 204017 | 11 | 21257 | — |
| XKY1 | 15 | 204017 | 11 | 21255 | — |
| PIQA | 15 | 53151 | 9 | 10258 | — |
| PIQP | 15 | 53151 | 9 | 10259 | — |
| PIQR | 15 | 53151 | 9 | 10260 | — |
| PIQY | 15 | 53151 | 9 | 10258 | — |
| QTUN | 15 | 49657 | 9 | 23679 | analyzers/notch_tuning.py, analyzers/vtol_transition.py |
| PSCD | 15 | 40761 | 9 | 23677 | — |
| PSCE | 15 | 40023 | 9 | 23678 | — |
| PSCN | 15 | 40023 | 9 | 23678 | — |
| QPOS | 15 | 38553 | 9 | 20237 | — |
| ARM | 15 | 30 | 11 | 11 | — |
| ATSC | 14 | 45846 | 0 | 0 | — |
| TEC2 | 14 | 26234 | 9 | 1837 | — |
| TECS | 14 | 26234 | 9 | 1837 | analyzers/tecs_tuning.py, lib/log_reader.py |
| CMD | 11 | 160 | 9 | 18 | analyzers/mission.py |
| TEC3 | 9 | 24873 | 9 | 1622 | — |
| MISE | 9 | 44 | 0 | 0 | — |
| ERR | 9 | 12 | 0 | 0 | lib/log_reader.py |
| PPLD | 7 | 4293 | 0 | 0 | — |
| SURF | 3 | 102585 | 5 | 3330 | — |
| PIDA | 3 | 25304 | 5 | 833 | — |
| PIDY | 3 | 25304 | 5 | 833 | analyzers/attitude.py, analyzers/pid_tuning.py |
| DU32 | 3 | 2822 | 5 | 81 | — |
| TERR | 3 | 2493 | 14 | 1148 | — |
| UBX1 | 3 | 519 | 0 | 0 | — |
| RISI | 2 | 2647242 | 0 | 0 | — |
| RFRF | 2 | 882907 | 0 | 0 | — |
| RFRH | 2 | 882414 | 0 | 0 | — |
| RBRI | 2 | 87827 | 0 | 0 | — |
| ISBD | 2 | 79728 | 0 | 0 | analyzers/notch_tuning.py, lib/log_reader.py |
| RGPJ | 2 | 64981 | 0 | 0 | — |
| RFRN | 2 | 44168 | 0 | 0 | — |
| RASI | 2 | 44129 | 0 | 0 | — |
| RMGI | 2 | 44125 | 0 | 0 | — |
| RRNI | 2 | 26729 | 0 | 0 | — |
| RGPI | 2 | 5668 | 0 | 0 | — |
| ISBH | 2 | 2493 | 0 | 0 | analyzers/notch_tuning.py, lib/log_reader.py |
| REV3 | 2 | 500 | 0 | 0 | — |
| RGPH | 2 | 10 | 0 | 0 | — |
| RMGH | 2 | 5 | 0 | 0 | — |
| RASH | 2 | 4 | 0 | 0 | — |
| RBRH | 2 | 4 | 0 | 0 | — |
| RISH | 2 | 4 | 0 | 0 | — |
| RRNH | 2 | 4 | 0 | 0 | — |
| QWIK | 1 | 84 | 0 | 0 | — |
| D32 | 0 | 0 | 2 | 2 | — |
| SIM | 0 | 0 | 14 | 11510 | — |
| SIM2 | 0 | 0 | 14 | 354191 | — |
| SRTL | 0 | 0 | 2 | 2 | — |
| XKTV | 0 | 0 | 14 | 4543 | — |
