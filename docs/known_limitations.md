# Known limitations

What ArduScope does not do, or does not get right, as of 2026-11-17, and what
to do about each. What was found and fixed is in [CHANGELOG.md](../CHANGELOG.md).
Figures are from the two log corpora the release gate scans: 561 logs, 560 of
them readable.

## Verdicts

- **A critical is common, and its share of false alarms is not known.** 414 of
  560 logs carry at least one: `attitude` on 223, `battery` on 109,
  `notch_tuning` on 98, `vibration` on 93, `failsafe` on 88. No one has gone
  through them flight by flight. Read the figures on a card, not its badge
  alone.
- **Plane's EKF failsafe gate is conservative in AUTO and GUIDED.** QLOITER,
  QLAND, QRTL and QAUTOTUNE are recognised directly. AUTO also needs
  `STAT.Stage=VTOL` and a nearby `QTUN.Ast` whose assist-active bit is clear:
  `Plane::update_flight_stage` uses the same STAT stage for `in_vtol_auto()`
  and fixed-wing Q assist. Missing STAT/QTUN, an unknown Ast field, or active
  assist leaves AUTO closed. If assist overlaps `in_vtol_auto()`, as during a
  takeoff transition, the gate stays closed and can miss a genuine trigger.
  GUIDED needs `auto_state.vtol_loiter` and
  `poscontrol.get_state() > QPOS_APPROACH`; neither is logged directly and QTUN
  does not expose `guided_takeoff` or the poscontrol state, so GUIDED stays
  closed. Sources: Plane-4.5.1 `ArduPlane/quadplane.cpp`,
  `ArduPlane/ArduPlane.cpp` and `ArduPlane/ekf_check.cpp`.
  Audited against real logs on 2026-11-17 over the 2026-11-11 backup of the
  gate corpora (the backup holds both gate corpora complete: 43 eagle and
  518 test logs, 561 .bin files, every one scanned): 49 QuadPlane logs
  matched, 17 flew AUTO, 16 opened AUTO VTOL
  windows of 6-29 s at their VTOL climbs while the assist flag kept
  Q-assisted fixed-wing phases closed, and no log had a variance breach
  inside a gated window or an ERR EKF event — the trigger path still has no
  real-log exercise.
- **The failsafe and EKF cards intentionally answer different questions.** For
  the EKF check, the failsafe card asks whether ArduPilot would trigger its EKF
  failsafe, so it applies the vehicle's mode gate. The card also aggregates
  triggered ERR events, battery voltage, GPS satellite count and the EKF margin
  (`_FS_SUBSYS_KEYS`, `_battery_margin`, `_gps_margin` and `_ekf_margin` in
  `analyzers/failsafe.py`). A non-EKF failsafe or a battery breach can therefore
  make it critical; low GPS satellite count is a warning. The EKF card asks
  whether the variance combination itself reaches the failsafe condition, and
  is not downgraded by mode. Its warning only means a variance reached
  `ekf_variance_warn`, not that the sustained failsafe combination was met
  (`analyzers/ekf.py`). ArduPilot does not run that check in a fixed-wing
  segment, but this does not make the navigation risk lower.
  The two cards therefore replay at different fidelities on purpose: the
  failsafe card runs the full state machine including Plane's mode gate,
  while the EKF card replays Copter's state machine (filter, position gate,
  ten-check counter) as the firmware does but, for QuadPlane and Plane,
  counts the ten checks across all armed flight without the VTOL mode gate
  and says so in its trigger line — the card answers the EKF-health
  question, not the mode question.
  A 2026-11-17 census used the 557 logs accepted by `run_analysis`; three of
  the 560 readable logs had no flight data and were excluded. Its records sit
  beside the local gate reports in untracked `reports/`, not in the repository.
  The census found 433 logs with the same card status and 124 (22.3%) with
  different statuses. None had a good/warning failsafe card with a critical EKF
  card; all six critical EKF cards were paired with a critical failsafe card.
  Fifty-nine had a critical failsafe card with a good/warning EKF card, 55
  Copter and four QuadPlane (all four battery-critical), and none reproduced
  the sustained combination. Fifty-four had no EKF ERR event, so their critical
  came from another failsafe or the battery check. The other five — Copter
  logs with a vendor v4.1.x banner that are actually ArduCopter 4.7.0 — had
  EKFCHECK/FAILSAFE_EKFINAV events but did not reproduce the
  sustained combination: three fired outside armed flight; in L_log_20 the
  primary lane carried no horizontal position for about nine seconds across
  its event (XKF4.SS HORIZ_POS_REL and HORIZ_POS_ABS clear) while the
  variances stayed below the threshold, so only the position gate can have
  fired it; and in log_22 the 5 Hz-filtered SV held at or above twice
  FS_EKF_THRESH for the ten consecutive checks the counter needs (raw SV
  peak 2.32).
  The EKF card now shows the firmware's own EKF failsafe events directly:
  ERR `EKFCHECK=16` / `FAILSAFE_EKFINAV=17` trigger and clear times, armed
  or not, and a trigger inside armed flight makes the card critical on its
  own. Since 2026-11-18 the variance recomputation is no longer a
  continuous-second approximation either: it replays the firmware's state
  machine (`lib/ekf_failsafe.py`) — the `FS_EKF_FILT` low-pass (a
  first-order `DigitalLPF`, Copter only), the `has_position` gate from the
  primary lane's XKF4.SS (armed flight wants `HORIZ_POS_ABS` without
  `CONST_POS_MODE`; disarmed accepts the PRED flags; a relative position
  needs `FLOW_TYPE`/`VISO_TYPE` or dead reckoning under `FS_DR_TIMEOUT`),
  and the ten-check up/down counter (`EKF_CHECK_ITERATIONS_MAX`), with
  Copter's `has_ever_passed` and origin holds and Plane's mode-gate reset.
  Replayed on the census's five previously unreproduced Copter logs, all
  five ERR windows now reproduce to within 0.3 s, two of them exactly: the
  two armed ones (L_log_20 by the position gate at 301.9 s, log_22 by the
  filtered variance at 91.0 s) go critical, and the three that fired after
  disarm reproduce as unarmed windows and stay detail lines.
  Four approximations remain: one XKF4 row stands for one 10 Hz check, so
  another logging rate stretches the counter in time; `optflow.healthy()`
  is not in the log, so a velocity variance at twice the threshold always
  scores two, the strict reading of `ekf_check.cpp:146-158`; origin
  readiness is the first origin-proving SS bit — `HORIZ_POS_ABS` or
  `USING_GPS`, both of which require a fused absolute source and
  therefore `validOrigin` (`AP_NavEKF2_Control.cpp:389-391,513`,
  `AP_NavEKF3_Control.cpp:594`), never the PRED flags, which only say GPS
  navigation is possible — so an indoor no-GPS log's check stays closed;
  and `has_ever_passed` latches only on an observed pass, so a log that
  opens inside a bad interval holds the check closed and leaves the
  verdict to the ERR events instead of inventing a trigger. Rover and Sub
  run EKF failsafes of different
  shapes (Rover scores the magnetic norm and folds position into the
  score; Sub wants one indicator held for two continuous seconds) and
  keep the older continuous-second model.
  Different badges are therefore a deliberate distinction, not by themselves
  evidence of a contradiction or of the Plane mode gate changing a verdict.
- **`attitude` still grades one-sample peaks.** A single roll or pitch error
  above 30 deg is critical because ArduPilot's
  `AC_ATTITUDE_THRUST_ERROR_ANGLE=30°` is the controller's thrust-allocation
  failure hard limit; a peak above 15 deg is now only a warning. The 15, and
  the 20 and 4.0 beside it in `analyzers/attitude.py`, remain engineering
  values with no source, outside `lib/thresholds.py` and its checks. The
  existing RMS criteria can still make sustained tracking error critical.
- **`notch_tuning` grades attitude by proxies.** A mean attitude error over
  2.5 deg with a peak rate over 100 deg/s reads critical: 81 of its 98
  criticals. A mean error over 5 deg does too: 14 more. `lib/thresholds.py`
  marks these limits E, with no source, and a fast flight passes 100 deg/s on
  purpose.
- **A hover throttle parameter over 0.12 from the measured hover reads
  critical.** In SITL a parameter of 0.20 on an aircraft that hovers at 0.36
  flew the same as the right one, within 6%. The limits are marked E.
- **`compass` vector interference is an offline robust fit, not a controlled
  CompassMot calibration.** The scalar field-strength contrast remains. A second
  metric fits each corrected MAG axis against the driver plus nine ATT
  rotation-matrix terms, downweights driver outliers outside a 1.5×IQR fence to
  25% weight, and applies Huber IRLS to three-axis field residuals. The
  coefficient is evaluated at the observed 95th-percentile driver, never
  extrapolated to full throttle. A fit is rejected when its observed impact
  exceeds the entire 530 mGauss reference field or its median residual is at
  least half the impact; no conclusion is better than a false attribution.
  With usable primary BAT current (maximum at least 3 A), current replaces
  throttle without any full-throttle scaling. Firmware updates compensation only
  from samples at or above 3 A (`compassmot.cpp:197-203`), while this fit uses
  all valid current rows to stabilize the slope. Firmware reads throttle from
  the RC stick (`compassmot.cpp:160-165`), so normalized motor PWM remains a
  proxy rather than a reproduction of the calibration input.
  The worst compass instance is shown as informational detail only; the vector
  metric does not affect severity, CompassMot guidance or cross-rule evidence.
  Even after robust fitting, a corpus evaluation escalated 56 of 518 test logs
  and two of 43 eagle logs. Non-controlled flight cannot separate motor impact
  from a position-related external magnetic field, so the scalar contrast alone
  drives severity. The existing SITL scenario with `SIM_MAG_MOT_X/Y/Z=7.0` read
  14.1% by the scalar method and 66.7% by the observed-current vector method
  (64.9% with RCOU only), demonstrating detection without grading it. A normal
  control produced no vector conclusion and stayed good. Missing ATT,
  insufficient low/high support, attitude-confounded drivers, and the validity
  guards leave the vector metric unavailable.

## Notch advice

- **How much a notch removes is not measured.** That takes the spectrum from
  before the filter beside the one after it. Batches that hold both
  (`INS_LOG_BAT_OPT` 3 to 7) are told apart by an offset on the instance, which
  is not read: they are averaged into one spectrum. The report says so only
  beside an enabled notch that stays put, not where it looks for a peak.
  Compare a log from before the notch with one after.
- **A throttle-tracking set is a hover set.** `INS_HNTCH_FREQ` is the peak and
  `INS_HNTCH_REF` the hover throttle, and the notch does not go below FREQ.
  Through climbs and descents in SITL it did no better than a notch that stays
  put. The wiki's answer, a lower FREQ with REF scaled to match, is not
  derived. With ESC telemetry the set follows the motors and this does not
  apply.
- **Without ESC RPM, the strongest peak is taken as the base frequency.** On
  15 of the 208 logs that have ESC RPM to check against, it is not where the
  motors flew: 2 are the third harmonic, 412 Hz on motors at 140 Hz, and 13 sit
  below, 66 to 103 Hz with the motors at 129 Hz or more. `INS_HNTCH_HMNCS` is
  always 3, and a third harmonic is pointed out in the text, not set.
- **A flight with motor speed all over the range may show no peak.** A peak
  has to stand 10 dB above the floor of the mean spectrum, and a wide spread of
  RPM smears it. No peak means no advice, not no motor noise.
- **It needs a flight.** A set needs ESC RPM in the air, or 100 rows of level
  hover. A QuadPlane counts as in the air 0.5 m above the height it armed at,
  by `QTUN.Alt`. A log that opens in flight counts from its first row, so what
  it flew at or below that height is not seen.
- **Six suggestion rules have no flight behind them**:
  `ARSPD_USE`, `AUTOTUNE_AXES`, `COMPASS_USE`, `GPS_AUTO_SWITCH`,
  `INS_HNTC2_*` and `RLL_RATE_D`. See
  [suggestion_evidence.md](suggestion_evidence.md). The flown ones flew in
  SITL, which can rule a value out and cannot prove it on an aircraft.

## Logs

- **DataFlash only.** `.BIN`, and `.log` in the text format. A telemetry
  `.tlog` is refused.
- **A damaged log is read as far as it goes, and the report does not say it is
  damaged.** Up to 1 MB of a `.BIN` may be bytes that are not log data, such as
  zeros from a failed copy or an incomplete download. Over that it is refused.
  A message cut by such a hole can hold wrong values, and a long hole shows in
  `clock_health` as a gap. Nothing can see a wrong value inside a whole
  message: the format has no checksum. A log cut short is not damage, and
  `log_health` says when one ends armed.
- **The web upload stops at 1 GB.** Every corpus log is under it: the largest
  is 813 MB and takes 3.0 to 4.1 GB of memory in the web server. Larger logs
  go through `flight_report.py`.
- **Old history entries keep old metrics.** `history.json` and `fleet.db` hold
  what the version of the day computed, and are not recomputed.

## Web server

- **No login.** It binds to loopback. `--host 0.0.0.0` opens uploads, reports
  and parameters to anyone who can reach the address.
- **The map has no basemap.** Since v0.12.0 the flight track is drawn on a
  blank projection (`lib/html_report.py` carries no tile layer); a report is
  fully offline.

## LLM diagnosis

- **It has been run against three services, by hand.** On 2026-11-11 DeepSeek
  answered through both APIs, `deepseek-flash` through the OpenAI one and
  `deepseek-v4-pro` through the Anthropic one. On 2026-11-13 Kimi for Coding
  (`kimi-for-coding`, `https://api.kimi.com/coding`) and GLM-5.3
  (`https://open.bigmodel.cn/api/anthropic`) answered through their
  Anthropic-compatible endpoints. The SDKs were `openai` 3.49.0 and
  `anthropic` 2.8.0. Anthropic's and OpenAI's own services have not been
  called. The tests put a stand-in where the request goes, and no release
  check sends one. A Kimi `sk-kimi-` key belongs to Kimi for Coding: the
  Moonshot endpoints refuse it, and `https://api.kimi.com/coding` is the one
  that answers.
- **What the model writes is not checked.** The prompt tells it to use only
  the analyzers' figures, to leave parameters to the analyzer cards and not to
  call an aircraft airworthy. A model can still do otherwise: before those
  lines were in the prompt, one reply gave a 10% limit of its own and another
  called the aircraft airworthy. The card says at every status that it is not a
  source. The verdict and the `.param` file come from the analyzers alone.
- **Each service answers in its own way.** `--ai-provider` names the API that
  is spoken, `--ai-model` the model and `--ai-base-url` a service other than
  the provider's own. Which models a service has, what it accepts and how it
  reports a refusal are its own. The OpenAI request sends no ceiling on the
  reply, because the services disagree on what it is called. A call that fails
  for any reason gives the rule engine's diagnosis, and the card says why.
- **The online agent has been tested against three real services.** On
  2026-11-12 DeepSeek's OpenAI-compatible endpoint ran the eight SITL
  scenarios three times each: every injected fault was named, the normal runs
  named none, and no observation or summary number went unverified. On
  2026-11-13 Kimi for Coding passed 18 of 24 runs: five observations cited a
  result that does not carry their number, and one run named the injected
  gyro noise as IMU divergence rather than motor noise. GLM-5.3 passed 23 of
  24 runs at a 600-second budget with no unverified number; one run passed ten
  minutes and was stopped. At the 300-second default GLM times out on the
  heavier scenarios. Anthropic's and OpenAI's own services are uncalled, and
  other compatible services need a run with their own key. The tool loop's
  fallbacks also run in the test suite against a fake provider.
- **A citation check cannot judge meaning.** The agent marks an observation
  unverified when a number is absent from the tool result it cites, or when
  the citation does not exist. A qualitative claim can still be wrong, and a
  number copied from an unrelated part of the same result passes. The card
  shows the unverified count; it is not a guarantee.
- **The agent is web and CLI only.** Batch, multi-log and fleet reports use
  the rule engine. There is no follow-up conversation: the web server deletes
  the upload when the analysis ends, so a second question would need the log
  kept.
- **The settings page is loopback only.** On a LAN listener `/settings`
  returns 404. The key then has to be placed in `~/.arduscope/agent.json` or
  an environment variable by someone at the machine.
- **What leaves the machine is a fixed list.** GPS coordinates, the board
  serial and STATUSTEXT are stripped unless opted in, and the raw `.BIN` is
  never sent. The categories the run sends are on the card. A service still
  receives the analyzer results, the parameters the agent requests and the
  downsampled message series.

## Install

- **Top-level module names are generic.** `lib`, `analyzers`, `locales` and
  the five entry scripts install at the root of site-packages. Give ArduScope
  a virtual environment of its own, or use the Windows package.
- **The Windows package carries its own Python**, 3.14.10 since 0.9.10, and
  pinned libraries. A fix in either reaches it only with a new ArduScope
  release. Once a Python version takes security fixes only, its releases are
  source only and the package has to move to a newer one: 3.12 has had six
  security releases since 3.12.10, the package's Python until 0.9.9, and none
  of them has a Windows build.
- **PDF export is tested with stand-ins.** A real export needs `weasyprint`
  and Chrome, and no test runs one.

## Process

- **CI runs about 40% of the tests.** The rest need the log corpora and SITL
  and run in `tools/release_gate.py`, on a machine that has both.
- **No pull request has had a review.** `main` refuses force pushes and
  deletion, and nothing requires a review or a passing check before a merge.
