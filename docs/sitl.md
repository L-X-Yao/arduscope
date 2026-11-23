# SITL scenarios

What the SITL suite covers, what it does not, and how to add a scenario or an
expectation. The tests are `tests/sitl/test_sitl_regression.py`; the flights
are described in `tests/sitl/scenarios.json`; the generated logs stay in
`tests/sitl/logs/` and are not committed.

## Running it

ArduPilot must be built for the `sitl` board with both vehicles:

```
cd /path/to/ardupilot
./waf configure --board sitl
./waf copter plane
```

Then:

```
python3 tests/sitl/generate_logs.py --sitl-dir /path/to/ardupilot --list
python3 tests/sitl/generate_logs.py --sitl-dir /path/to/ardupilot
python3 -m pytest tests/sitl/test_sitl_regression.py -q
```

`tools/release_gate.py` runs both commands as its `sitl` step. The flights hold
a shared `flock` on the SITL binaries, so a rebuild waits for them; the lock
path can be named in `local_paths.json`. A scenario that does not fly fails
instead of producing a log with no flight in it.

## Scenarios

| Scenario | Vehicle | Injected fault | Test expectation |
|---|---|---|---|
| `copter_normal` | Copter | None | `vibration`, `motor`, `gps`, `ekf`, `battery` good; `attitude`, `compass`, `imu_consistency` not critical; health >= 70 |
| `copter_high_vibration` | Copter | Motor 1 loses 30% thrust, high vibration | `vibration` warning or critical; cross rules `vibration_motor`, `attitude_vibration_control`, `vibration_imu_consistency` |
| `copter_gps_glitch` | Copter | Satellites drop to 5, position jumps | `gps` and `gps_glitch` warning or critical; cross rule `gps_glitch_ekf` |
| `copter_compass_interference` | Copter | Motor compass interference | `compass` warning or critical |
| `plane_tecs_tuning` | Plane | Deliberately poor TECS | `tecs_tuning` warning or critical; `gps` good |
| `copter_battery_aging` | Copter | Voltage sags below `BATT_CRT_VOLT` | `battery` warning or critical |
| `copter_motor_noise` | Copter | Gyro motor noise, no notch | Notch suggestions: `INS_HNTCH_ENABLE`, `MODE=3`, `REF=1`, `FREQ` 110-128 Hz, `BW` |
| `copter_motor_noise_climbs` | Copter | Motor noise through climbs and descents | Notch suggestions: the same set with `FREQ` 95-115 Hz |
| `copter_autotune_axes` | Copter | High vibration with `AUTOTUNE_AXES=1` | `attitude` warning or critical; `AUTOTUNE_AXES=7` suggested |
| `copter_gps_glitch_dual` | Copter | `copter_gps_glitch` with a second GPS and `GPS_AUTO_SWITCH=0` | `gps`, `gps_glitch` warning or critical; cross rule `gps_glitch_ekf`; `GPS_AUTO_SWITCH=1` suggested |
| `plane_airspeed_offset` | Plane | Pitot reporting 36% of true airspeed (`SIM_ARSPD_RATIO=15`) | `airspeed` critical; `ARSPD_USE=0` suggested |
| `copter_compass_outlier` | Copter | 600 mGauss X offset injected on compass 2 in flight | `compass_consistency` warning or critical; `COMPASS_USE2=0` suggested |
| `copter_second_notch_band` | Copter | Motor noise near 105 Hz, static first notch at 40 Hz | `INS_HNTC2` set suggested: `ENABLE=1`, `MODE=3`, `REF=1`, `FREQ` 95-115 Hz, `BW` |
| `copter_static_dual_notch` | Copter | Both notches enabled and static off the peak | `notch_tuning` warning or critical (the different-band branch ran); no `INS_HNTC2_*` suggestion (`suggestions_exclude`) |

The complete expected blocks are in `scenarios.json`; the table is a summary,
not a second source. A scenario can also set `sitl_params` and a speedup.

## What the suite checks

Each scenario runs the full pipeline, then checks the parts it has an
expectation for:

- pipeline completion, vehicle detection, analyzer count and required fields;
- health score floor;
- named analyzers good, warning or critical, exactly critical, or not
  critical;
- cross-correlation rules that should fire;
- suggested parameters, their exact values and their ranges;
- parameters a scenario asserts are NOT suggested (`suggestions_exclude`);
- `.param` export, including the no-suggestion case;
- every analyzer result has its required fields.

A full run has 196 tests. The skips are optional expectation blocks: a
scenario without `analyzers_good`, `cross_rules_should_fire`,
`suggestions_include` or a related key skips that test. They are not missing
logs.

## Coverage

Twelve analyzers are named directly in an expectation:

`attitude`, `airspeed`, `battery`, `compass`, `compass_consistency`, `ekf`,
`gps`, `gps_glitch`, `imu_consistency`, `motor`, `tecs_tuning`, `vibration`.

`notch_tuning` is exercised through the four motor-noise scenarios'
suggestions — including the second-band set and the enabled-HNTC2 exclusion —
but no status is asserted. `summary` and `flight_profile` run in the pipeline
but have no expectation. The other analyzers have no SITL expectation:
`board_power`, `clock_health`, `cruise_efficiency`, `ekf_vel_divergence`,
`failsafe`, `gyro_saturation`, `log_health`, `mission`, `pid_tuning`, `power`,
`rangefinder`, `rc_input`, `servo_dither`, `temperature`,
`throttle_collapse`, `vtol_transition`, `wind`.

That does not mean they never run on these logs; it means a failure there
would not fail the SITL suite. Real-log snapshots and the unit suite cover
some of them, and `docs/known_limitations.md` records the ones without a real
flight.

## Gaps worth closing

Priority order is by what a wrong result would cost:

1. **Failsafe and link**: an in-flight RC or GCS failsafe scenario, with the
   armed window and the clear line.
2. **Power and thermal**: `power`, `board_power`, `temperature`; the battery
   scenario covers only the pack voltage.
3. **Plane and VTOL**: `vtol_transition`, `mission`, `throttle_collapse`,
   `wind`, `cruise_efficiency`.
4. **Control and sensors**: `pid_tuning`, `gyro_saturation`, `servo_dither`,
   `rangefinder`, `ekf_vel_divergence`, `log_health`, `clock_health`,
   `rc_input`.
5. **`RLL_RATE_D` on the plane rate loop**: the D-reduction rule's
   oscillation gate reads the `RATE` message, which the fixed-wing controller
   does not log (`pid_tuning.py` skips the rate-output block for the plane
   phase), so no flight — SITL or real — can fire it as the analyzer stands.
   Exercising it needs an analyzer change that grades plane oscillation from
   `PIDR`, which is out of scope here; the row stays `not_flown`.

SITL can rule a suggestion out; it cannot prove one on an aircraft. A
suggestion still needs a real flight or a waiver in
`docs/suggestion_evidence.md`.

## Adding an expectation

1. Run the scenario and read the report. Do not copy the current status
   blindly: decide what the fault should produce from the ArduPilot source or
   the wiki.
2. Add the key to that scenario's `expected` block in `scenarios.json`:
   `analyzers_good`, `analyzers_warning_or_critical`, `analyzers_critical`,
   `analyzers_not_critical`, `cross_rules_should_fire`, `health_score_min`,
   `suggestions_include`, `suggestions_equal`, `suggestions_between`,
   `suggestions_exclude`.
3. Run the SITL regression. A new expectation that fails is a finding, not a
   test to loosen.
4. If the expectation encodes a known limitation, say so in
   `docs/known_limitations.md`; do not hide it in a skip.

See [CONTRIBUTING.md](../CONTRIBUTING.md#testing) for the test tiers and
[docs/workflow.md](workflow.md) for the gate a change has to pass.
