# Changelog

All notable changes to ArduScope are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/).

0.9.7 is the first release in this repository. The versions before it were
made in a private repository: their entries are kept here, and their tags and
releases are not published.

## [Unreleased]

### Changed
- Releases after 0.9.10 are immutable once published: their files and their
  tag cannot change. A defect found in a published package goes out as a new
  version.
- The release workflow names `softprops/action-gh-release` by commit, the one
  v3.0.4 has, rather than by its `v3` tag, which its owner can move.

### Fixed
- `build_windows_package.py` no longer calls its `.pyc` mode source protection
  unfit for delivery, nor recommends `--nuitka` on Python 3.10. The release is
  built in the `.pyc` mode, and no test or release step runs `--nuitka`, which
  needs Python 3.14 as well. Without `--output` the zip is
  `dist/arduscope-win64.zip`, which was `arduscope-v5.zip`.

## [0.9.10] - 2026-11-11

### Changed
- The Windows package runs on Python 3.14.10, which was 3.12.10. Python 3.12
  has had six security releases since 3.12.10, and none of them has a Windows
  build. The libraries in the package are the versions 0.9.9 carried.
- The Windows package is built from pinned files: `win64-requirements.txt`
  names the version and SHA-256 of each library, so two builds of one tag ship
  the same files. The build stops when the Python download is not the file
  python.org lists, when a pin does not meet `pyproject.toml`, or when a
  library in the package needs one that is not in it.
- A release is created as a draft, and is published once its Windows package
  has passed `tools/win_package_check.py` on real Windows. The release used to
  be public before the check ran. The check also compares the package's Python
  and libraries with the pinned ones.
- The README says that ArduScope is not published on PyPI, and this file says
  where the versions before 0.9.7 were made.

## [0.9.9] - 2026-11-11

### Fixed
- The card took the first line of an LLM reply as its summary. A reply that
  opens with a heading gave the summary `## Overall assessment`. The summary
  is the first line that says something, without its list or emphasis marks,
  and a heading of any level is shown as a heading.
- An LLM card said that it is not a source only where it read warning or
  critical, and a healthy flight is where its words are most likely to be taken
  for the tool's. It says so at every status now. The prompt tells the model to
  use only the analyzers' figures, to leave parameters to the analyzer cards
  and not to call an aircraft airworthy: of the first replies from a real
  service, one gave a 10% limit of its own and another called the aircraft
  airworthy.
- Emphasis in an LLM reply is shown as emphasis and a rule line is dropped. The
  card showed `**` and `---` as they came.

### Changed
- The LLM diagnosis takes its model from `--ai-model` and no longer has one
  built in: `--ai-provider anthropic` or `openai` without it is refused. The
  names in the code were `claude-sonnet-5-5` and `gpt-4o-mini`, and a name in
  the code stops working the day its service retires it: OpenAI's deprecations
  page lists GPT-4o mini (`gpt-4o-mini-2024-07-18`) for shutdown on 2027-01-14.
- `--ai-base-url` gives the address of the API, so a service other than
  Anthropic and OpenAI that speaks one of the two APIs can be used. The key
  still comes from `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and goes to that
  address. Either option without an LLM provider is refused.
- The OpenAI request no longer sends `max_tokens`: services that speak the API
  disagree on its name and on whether reasoning counts against it. A reply
  that was cut off, filtered or empty is reported as what it is.
- The card names the model that wrote it and the API it was asked through. It
  named the provider only, which says little once the address can be changed.
- The LLM diagnosis was run against a real service for the first time:
  DeepSeek, through both APIs. `docs/known_limitations.md` says what that does
  and does not cover.

## [0.9.8] - 2026-11-11

### Fixed
- `--ai-provider anthropic` read the first block of the reply as its text.
  `claude-sonnet-5-5` may think before it answers, and then the first block is
  not text: the call failed and the report fell back to the rule engine. The
  reply is read by block type now, a refusal and a reply cut short are
  reported as what they are, and the request leaves room for the thinking.
- The feedback card in a report says where the feedback goes: an issue on
  GitHub. It said "tell us" and named no place, and the Chinese text of the
  multi-log report pointed at a support channel that a public tool does not
  have.

### Changed
- `POSITIONING.md` is in English, with the Chinese text in `POSITIONING_zh.md`.
  It says that feature work has ended and the maintainer fixes defects only,
  that a suggestion gives a value only where its rule has a source and a
  flight, and that a false alarm reported with its log is welcome. The roadmap
  is now a list of what is finished and of ideas with no schedule. The README
  has a Positioning section.
- The README says what METHODIC is and links it, shows the scenario templates,
  `--no-fleet-record` and the commands `pip install .` adds, and no longer says
  "no guesswork": the limits without a source are in the known limitations.
  `CONTRIBUTING.md` opens with the maintenance status and says how to install
  the commit hook.
- `docs/fleet_history_design.md` called itself a draft awaiting decisions. The
  feature shipped in v0.6.0 and the document says so. The three ledgers under
  `docs/` say that their commit hashes belong to the history from before the
  repository went public.
- The README says that reports and the web interface are in Chinese by
  default. `docs/known_limitations.md` has a section on the LLM diagnosis: it
  is not tested against the services, its model names are fixed, and OpenAI
  lists `gpt-4o-mini` for shutdown on 2027-01-14.

## [0.9.7] - 2026-11-11

### Fixed
- The Windows package carries ArduScope's licence, as `LICENSE.txt`. It held
  Python's and every library's, and not its own.

### Changed
- Flight logs are asked for by label. The real-log tests, the release gate and
  the Windows check took paths from the code that exist on one machine. They
  read them from `local_paths.json` now, which git ignores, and behave as
  before without it. `ARDUSCOPE_REF_LOG` is gone.
- The "Copter Factory Acceptance" template is described without a product name.
- The package metadata names the author without an email address.

### Added
- `SECURITY.md`: how to report a vulnerability, and what a web interface with no
  login means. The bug report form says that a flight log holds the GPS track
  and the board's serial number before it asks for one.
- The README says where the Windows package is and how to install from a clone.

## [0.9.6] - 2026-11-11

### Fixed
- A notch suggested for ESC telemetry now follows the motors. The set had no
  `INS_HNTCH_REF`, and at its default of 0 ArduPilot switches tracking off in
  every mode, so the notch stayed at `INS_HNTCH_FREQ`: all 178 such sets in the
  two corpora. The set now carries `INS_HNTCH_REF` 1, and `INS_HNTCH_FREQ` is
  the low end of the motor frequency the ESCs report in flight, where it was
  the spectrum's peak: under ESC tracking it is the lowest frequency the notch
  follows, and the wiki puts it below the hover frequency. Flown in SITL
  through climbs and descents, rate error went from 2.89 to 0.41 deg/s and gyro
  noise in the motor band from 1.75 to 0.39 deg/s. If you applied a notch set
  with `INS_HNTCH_MODE` 3 from an earlier report, take the values from a new
  one.
- Motor noise and hover throttle are measured in the air. Both took every armed
  row with the throttle over 5%, which let in the idle before takeoff, a
  QuadPlane's cruise with its lift motors stopped, and ground runs. 50 logs got
  a notch set without leaving the ground, and three ground runs read "hover
  throttle severely off", with half the real `Q_M_THST_HOVER` to set. A Copter
  log is now read by its land detector events and a QuadPlane log by its height
  above where it armed. The hover throttle is the median over level hover, the
  gate the firmware learns the parameter inside, and motor PWM no longer stands
  in for a missing throttle message. Hover throttle suggestions go from 17 logs
  to 7.
- Throttle tracking takes `INS_HNTCH_REF` from that hover throttle at any
  value. Under 0.2 it was written as 0.50, which no source supports: these
  aircraft hover at 0.05 to 0.2, and their throttle never reaches it. A log
  with no hover in it and no ESC RPM in flight gets no notch parameters and
  says why.
- ESC telemetry is judged on the flight. Over the whole log, time on the ground
  with the motors stopped counted against it: 40 logs with good telemetry in
  flight got throttle tracking.
- `notch_tuning` no longer grades a notch from one filtered spectrum. It looked
  for a dip in the notch band, and a notch on a peak leaves a smaller peak: a
  notch that took 97.7% off its peak read critical. A fixed notch still shows
  its dip, a peak left in its band is reported, and a notch that tracks is not
  measured.
- An enabled notch in a tracking mode with `INS_HNTCH_REF` 0 is reported as
  fixed at `INS_HNTCH_FREQ`, with the reference to set. An ESC notch whose
  `INS_HNTCH_FREQ` is above the low end of the flight gets it lowered, with
  `INS_HNTCH_BW` scaled to keep their ratio.
- A log with no gyro spectrum from a flight reads "not assessed". It read
  "configured correctly", which was said of an aircraft that shook on the
  ground and never took off.
- Batch samples logged with `INS_LOG_BAT_OPT` 3 alternate pre- and post-filter
  data, as 4 to 7 do. They were read as post-filter.
- A suggested value held as a numpy `float32` or `int64` went past the check
  against the parameter's documented range. No analyzer produces one today.
- A file that is not a DataFlash log is refused. `flight_report.py` on an HTML
  report or a `.param` file gave a report with a health score of 90, and
  `multi_report.py` compared such a file with the real logs: pymavlink reads
  anything but `.bin` and `.log` as a telemetry log, and makes packets of any
  bytes. The web upload takes `.BIN` names only and was not open to this.
- The web server no longer loses an upload slot to a file name with a NUL in
  it. There are 64 slots, none came back, and 64 such requests left it refusing
  every upload until restarted. The slot is released on any way out that does
  not hand the upload to a worker.
- An upload's name is cleaned before it goes into file names: characters
  Windows cannot hold and control characters become `_`, and a long name is cut
  to fit. A colon made Windows write the log into a stream of an empty file that
  stayed behind, and 80 Chinese characters made a report name too long to save.
- `/api/compare`, `/api/compare-history` and `/api/set-lang` answer 400 to a
  JSON body that is not an object, where they answered 500, and read no body
  over 64 KB.
- `/api/upload` refuses a template name it does not know. It ran the default
  thresholds without saying so.
- The template list on the web page said "all 20 analyzers". There are 32.
- A `.BIN` that is not a log is refused before it is parsed. pymavlink walks
  bytes it cannot read one at a time and prints a line for each: 5 MB of zeros
  took 30 s and 10 million lines to refuse, and 500 MB, the most the web server
  took, would have held a worker for most of an hour. The reader first counts
  the bytes that cannot be log data, which takes 2 s on an 813 MB log. A file
  that is mostly such bytes, or holds over 1 MB of them, is refused with how
  much and from where. None of the 560 corpus logs holds one such byte.
- A damaged log no longer floods the console. 64 KB of zeros inside a log
  printed 262,000 lines.
- A log whose format records cannot be read is refused in those words. The
  user was told "smp_cnt", "GMS" or "unpack requires a buffer of 87 bytes".
- A batch sampler row of a layout the reader does not know costs the spectrum
  and not the log.
- `log_health` could stop a report from being written. Its figure of ATT
  intervals left a repeated or backward timestamp out of the values and in the
  time axis, so later points were drawn early, and over 2000 points
  `flight_report.py` ended in a traceback. No corpus log has such a timestamp.
  The intervals over the threshold are now a trace of their own: their red
  colour was lost when the report thinned the trace. 49 corpus logs have the
  figure, and on 20 of them it has over 2000 points and was thinned.
- The report thins a trace whose x and y differ in length as far as the
  shorter one goes, where it failed.

The last six came from mutating five logs 510 ways and running the whole
pipeline on each. None ended the process or hung, and no analyzer raised.

- One analysis read a Copter log 7 times and a QuadPlane log 11 times.
  `flight_profile`, `pid_tuning` and `log_health` read ten message types they
  did not declare, and a type that is not declared is read from the file when
  asked for. They are declared now: a Copter log is read once and a Plane log
  twice, and 56 corpus logs take 17% less time with every analyzer saying the
  same as before. A 79.5 MB QuadPlane log went from 26.9 s to 19.0 s, and the
  largest corpus log, 813 MB, from 5 min 20 s and 7.9 GB of memory to 3 min
  32 s and 3.5 GB.
- The reader lets go of pymavlink's copy of a log's index when a read ends.
  pymavlink keeps its last connection in a module global, and for a log that
  is an offset for every message. It stayed until the next log had been indexed
  beside it: a server peaked at 5.2 GB on its third 813 MB log against 3.5 GB
  on its first. That is 4.1 GB against 3.0 GB now, and the command line takes
  2.9 GB for that log.
- `tools/check_msg_fields.py` did not know 14 messages, `BARO` among them. Its
  table is harvested from the ArduPilot source one string literal at a time,
  and these are written as literals side by side. It holds 310 messages now.

Over the 561 logs of the two corpora, `notch_tuning` moves on 341 and no other
analyzer moves. Notch sets go from 338 to 269: 72 are withdrawn, 50 of them
from logs that were never in the air, and 3 are new. 208 sets follow the ESCs,
all with `INS_HNTCH_REF` 1, and the spectrum's peak falls inside the range the
ESCs report in flight on 193 of them. With armed batches it did on 186 of 210.
`notch_tuning` critical goes from 106 to 98. One log reads worse for a reason
other than wording: `log_89` now reads critical, hovering at 0.32 with
`Q_M_THST_HOVER` at 0.20.

### Changed
- The web server takes logs up to 1 GB, where it took 500 MB. The limit stood
  there because the largest corpus log, 813 MB, peaked at 8.93 GB in the web
  worker. With the two fixes above it peaks at 3.0 GB in a new server and
  4.1 GB in one that has analysed it before. The estimate that paces analyses
  running together is refitted on nine logs, each analysed four times running
  in one server: 6 bytes of memory for each byte of log, where it was 11. The
  three corpus logs over the old limit, 622 to 813 MB, no longer need the
  command line. On Windows the packaged server took the 813 MB log, and a
  663 MB log three times in a row, at 3.5 and 3.2 GB.
- `hover_airborne_alt_m`, 0.5 m, is the height above where the aircraft armed
  from which a log with no land detector events counts as off the ground. It is
  the height Copter's ground effect detector takes a takeoff as done at. On 46
  QuadPlane logs the ground runs read 0.2 m at most and the lowest hover 1.7 m.
  The multi-log hover thrust uses the same gate: it used 3 m above the lowest
  point of the log, which shut out hover tests flown at 2 to 3 m and counted a
  ground run whose baro height sank as a hover.
- The SITL regression set has `copter_motor_noise_climbs`, and checks the
  suggested `INS_HNTCH_MODE`, `INS_HNTCH_REF` and `INS_HNTCH_FREQ`, where it
  checked that the names were present.
- `docs/known_limitations.md` lists what ArduScope does not do or does not get
  right, with what to do about each.
- `tools/win_package_check.py` also uploads under four names Windows cannot
  store: with a colon, a NUL, reserved characters, and 80 Chinese characters.
- Dependency floors are past the advisories known on 2026-11-10: `waitress`
  3.0.1, `weasyprint` 70.0 and `bottle` 0.13. A fresh install already resolved
  to clean versions. An environment holding an old copy did not.
- The hover throttle suggestion has a flight behind it, which leaves 12 of the
  20 rules in `docs/suggestion_evidence.md` flown and 6 not. A parameter of
  0.20 on an aircraft that hovers at 0.36 got 0.36, and flew the same within
  6%.

## [0.9.5] - 2026-11-06

### Fixed
- The notch spectrum now comes from the flight. Batch times counted from boot and
  armed intervals from the start of the log, so the filter for armed batches
  never worked: a log that opened after boot kept the wrong batches or none, and
  then fell back to all of them, ground included. This goes back to the first
  version of the notch advisor. On 269 logs with ESC RPM, median `INS_HNTCH_FREQ`
  error goes from 4.3% to 3.5%, with suggestions such as 283 Hz on a 48 Hz motor
  moving to 49 Hz. If you applied notch values from an earlier report, check them
  against a new one.
- `notch_tuning` no longer assesses a log that never armed. 179 of the 561 logs
  in the two corpora got advice with their motors stopped, and 59 read critical.
- A peak has to stand 10 dB above the noise floor, which is `notch_atten_crit`: a
  smaller one leaves a notch nothing to remove. At idle the highest point was
  often just the floor rising to the end of the band. Suggestions at 440 Hz or
  above go from 31 logs to 1. Of the 276 logs with ESC RPM, 7 lost theirs, none
  within 10% of the motor frequency.

## [0.9.4] - 2026-11-06

### Added
- `docs/suggestion_evidence.md` lists every parameter ArduScope can suggest:
  the rule, its source, and whether it has been flown. 11 of 20 rules have a
  SITL flight behind them, 7 do not, and 2 are log settings.
  `tools/check_suggestions.py` holds the table to the code in CI, so a new
  suggestion needs a row, and a flight or a waiver.
- `tools/release_gate.py` runs what CI cannot before a tag: real-log snapshots,
  SITL flights, and both corpora against the accepted baseline. The result is
  written against the commit, and a corpus change needs someone to accept it.
- `tools/win_package_check.py` checks a Windows package on real Windows from
  WSL: every entry point, the four CLI tools, six uploads, and nothing left
  behind. It replaces scripts that were kept out of the repository.

### Changed
- `COMPASS_MOTCT` is no longer suggested. ArduPilot says not to change it by
  hand, so the report points at CompassMot, which sets it.
- `FS_EKF_THRESH` is no longer suggested. It loosened a failsafe after a critical
  variance. The report now says when a stricter value set the limit.
- Five rules with no source for their value now name the parameter and leave the
  number out: the waypoint radius x1.5, `Q_TRANSITION_MS` x1.5, `Q_TRAN_PIT_MAX`
  lowered by 2, `MOT_SPIN_MIN` raised by 0.02 in `servo_dither`, and
  `ATC_RAT_*_FLTD` x0.7 in `pid_tuning`. If a `.param` file from an earlier
  report changed one of these, `COMPASS_MOTCT` or `FS_EKF_THRESH`, go back to
  your previous value.
- `tools/release.py` refuses a commit without a pass from the gate. It releases
  the notes under `[Unreleased]` rather than generating them from commit
  subjects, and `--skip-ci` is gone.
- `CONTRIBUTING.md` says which gates each kind of change has to pass, and what a
  suggestion needs before it ships.

### Fixed
- `mission` no longer suggests centimetres for `WP_RADIUS_M`. A radius of 2 m got
  500, where the range is 0.05 to 10 m. The report text also names the parameter
  the log uses, where it always said `WPNAV_RADIUS`.

## [0.9.3] - 2026-11-06

### Changed
- Notch suggestions follow the ArduPilot wiki, and were flown in SITL on
  injected motor noise. `INS_HNTCH_BW` is half of `INS_HNTCH_FREQ`: sized from
  the peak's width it was often 10 Hz, which left 4 times the residual peak.
  `INS_HNTCH_HMNCS` is 3, the firmware default: it cost nothing without a 2nd
  harmonic and cut the residual 7 times with one. A wide peak gets a triple
  notch, `INS_HNTCH_OPTS` 16, since the wiki no longer recommends the double
  notch. ArduScope's own suggestions now take a noise peak down 96 to 98% and
  leave nothing further to suggest.
- `pid_tuning` lowers a D gain only on an axis whose `RATE` output oscillates.
  It went by the D term's share of the output, which stays high at any gain:
  applied again and again in SITL it took D down 73%, and 278 of the 296 axes
  it flagged in the two corpora were under METHODIC's limit. The wiki lowers D
  to clear an oscillation. If a `.param` file from an earlier report lowered a
  `_D` gain, go back to your previous value.
- `notch_tuning` no longer suggests raising `ATC_RAT_*_FLTD`. Raising it doubled
  D-term noise in SITL with a notch or without. Lowering it to
  `INS_GYRO_FILTER`/2 stays: D-term noise fell 45% with tracking unchanged, and
  the source is the wiki, not a forum post as the report said.
- `notch_tuning` no longer suggests `ATC_THR_MIX_MAX` 0.7, which 153 logs had.
  It fired on a critical attitude error, while the wiki raises it only after a
  good tune and warns against it on an oscillating aircraft. It changed nothing
  in SITL.
- `pid_tuning` no longer lowers an I gain when the I term keeps growing. A
  growing I term is chasing an offset, and lowering the gain chases it worse:
  in SITL, `ATC_RAT_YAW_I` at 0.7 of its value raised yaw error 22% in 3 of 3
  flights. Reports now point at CG, trim and propulsion. None of the 560
  readable logs in the two corpora had this suggestion.

### Added
- `vtol_transition` reports VTOL assist in fixed-wing flight from `QTUN.Ast`:
  how long, how many times and why, by low speed, low altitude, attitude error
  or forced. It does not affect the grade. Older firmware does not log it.

### Fixed
- A log that never armed is no longer graded as a flight. `get_armed_mask` gave
  it every row, so bench logs drew flight verdicts: of the 180 logs this
  changes, 179 show motors under 1100 us throughout and the other no altitude
  change, and 24 of their warnings and criticals were GPS settling and logging
  pauses on the ground. Those analyzers now say the log never armed.
- `tecs_tuning` no longer suggests `TECS_SPDWEIGHT` when no airspeed is in use.
  TECS holds the speed weight at 0 then, so the parameter does nothing.
- The notch spectrum uses every gyro batch again. 0.9.0 grouped batches by
  their measured rate, and no two rates match, so a flight's spectrum came from
  one to three batches. A batch from before the motors spun read as no vibration
  at all. On the 274 logs with ESC RPM to check against, the median error of the
  suggested `INS_HNTCH_FREQ` falls from 24% to 4.4%. Go back over any notch set
  from a 0.9.0 to 0.9.2 report.
- The `compass_motor_interference` diagnosis now needs the interference itself
  to be measured over 30%, the CompassMot limit. It used to fire whenever
  `compass` and `motor` were both flagged, but `compass` also warns on field
  variation and offsets, and uneven motor output says nothing about the field:
  of the 292 logs it matched, 211 had no such measurement, and it missed 21
  that did. Those `compass` and `motor` findings now report on their own.
- `gps_glitch` warns on a single jump past `EK3_GLITCH_RAD`, 25 m by default,
  while armed. It graded the share of samples only, so one jump in a long
  flight passed as good. A jump the speed explains does not count, nor does
  one on the ground: all 6 such jumps in the two corpora were a fix settling
  before arming, so no log there changes.
- `tools/gen_param_table.py` missed prefixes registered with `GOBJECTN`, such
  as `EK3_`, so `tools/check_params.py` rejected real names.

## [0.9.2] - 2026-11-05

### Changed
- `pid_tuning` no longer lowers an I gain when the I term makes up most of the
  output. That share means the aircraft is holding a steady offset, which is
  the I term's job. Lowering it made tracking worse: in SITL with a weak motor,
  roll attitude error rose 31% and yaw 20%. 172 of 561 logs had this suggestion.
  Reports now point at CG, trim and propulsion, and leave AutoTune for poor
  tracking. If a `.param` file from an earlier report lowered an `_I` gain, go
  back to your previous value.
- `tecs_tuning` no longer suggests `TECS_PITCH_MAX` or `TECS_LAND_ARSPD` for a
  height error. TECS holds its height demand back when pitch clips, so this
  error cannot show a pitch limit, and raising the limit made it 11 to 24% worse
  in SITL.

### Fixed
- `tecs_tuning` grades cruise only. It counted every TECS row, so a hovering
  QuadPlane scored as 20 m/s of speed error and a plane rolling out after
  touchdown as 13 m/s. All 15 logs it flagged in the two corpora came from
  those rows: 10 cruise at 0.8 to 2.6 m and 0.5 to 1.3 m/s, inside the
  unchanged thresholds, and 5 hold under 10 s of cruise, which now gets no
  grade. Cruise means `STAT.Stage` NORMAL, `STAT.isFlying`, and outside the
  VTOL windows, and the figures show those rows alone. If a `.param` file from
  an earlier report changed `TECS_SPDWEIGHT` or `TECS_PITCH_MAX` on a
  QuadPlane, go back to your previous value.

## [0.9.1] - 2026-11-05

### Fixed
- `power` adds up every pack that measures current, as `cruise_efficiency`
  does, and a pack on a later instance is no longer reported as "no current
  data". No log in the two corpora has a second current reading, so nothing
  changes on them.
- `power` and `cruise_efficiency` leave out a `BATTn_MONITOR=10` Sum monitor.
  `cruise_efficiency` counted its current twice in 0.9.0. Reports list the
  instances they added up, since a log cannot show that one monitor measures
  a branch of another.
- On Windows, an upload that failed while its format records were read
  stayed in the report folder: pymavlink had the file open and left no
  reader to close. Found by running the package on real Windows, which
  S1-07 had not been. A file that cannot be deleted is now reported on
  stderr.
- `flight_report` and `fleet_report` start in the Windows package. Both
  imported `lib` before setting up `sys.path`, which the bundled Python needs,
  so `fleet_report.py` shipped in 0.9.0 but could not run there.
- The batch example in the README exited with an error.

## [0.9.0] - 2026-11-05

A full rescan on 2026-11-05 found 129 problems, including 49 of the 102
findings of the 2026-07-15 audit that had been recorded as closed.
`docs/rescan-2026-11-05.md` lists them all; each commit names the items
it closes.

### Changed: behaviour you can notice
- Python 3.10 is now the minimum. 3.9 never worked: importing `lib`
  failed. 3.10 to 3.15 are tested.
- A file with no flight data is refused instead of scoring 90.
- Any critical finding caps the health score at 49, the red band, and
  batch runs count it as critical.
- `--ai-key` is gone, since a key on the command line shows up in `ps`.
  Use `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. The SDKs are in the new
  `ai` extra.
- `--thresholds` and `--only`/`--skip` now stop on a bad file or an
  unknown key instead of ignoring it.
- When analyzers disagree on a value, the parameter is left out of the
  `.param` and listed as a comment. A person has to choose.
- `web_app.py` keeps reports in `~/.arduscope/reports` by default instead
  of a temporary folder that was lost on restart. `--keep-reports N`
  deletes the oldest; by default nothing is deleted.
- Behind a reverse proxy, name the public host with `--allow-host`.
- `--format pdf` exits with an error when charts cannot be rendered,
  instead of writing a PDF without them. kaleido 1.x needs Chrome.

### Fixed: wrong parameter suggestions
- `pid_tuning` no longer changes gains from the tracking ratio, which has
  no direction: 11 of 92 parameter values were told to move both ways.
  Fixed-wing errors are no longer inflated 57 times.
- Motors are found through `SERVOn_FUNCTION`, and outputs above 14 are
  read. 22 of 43 logs were reading the wrong channels.
- A short gyro batch no longer truncates the notch spectrum (a peak moved
  from 479 to 68 Hz). Attenuation is graded only where it means something.
- Every suggestion stays inside the parameter's documented range.
- Removed suggestions that cannot work: `INS_ACCEL_FILTER` for clipping,
  `INS_GYRO_FILTER` for saturation, `INS_ACC_BODYFIX`, and a hardcoded
  `LOG_BITMASK` for every vehicle. `COMPASS_USE2` no longer disables a
  healthy compass. Gyro saturation knows the +-4000 dps parts, which
  corrects the +-2000 dps statement in 0.8.4.

### Fixed: wrong diagnoses
- Armed windows now use `POWR.Safety` when events are missing, and most
  flight checks grade the armed window only. 118 `rc_input` criticals were
  ground handling.
- `ekf`, `failsafe` and `ekf_vel_divergence` follow the primary lane from
  `XKF4.PI`. `throttle_collapse` compares in the thrust domain.
  `log_health` no longer reports a power cut on a log that never armed.
- An analyzer that fails is shown as failed, instead of as an info card
  that added 80 points.

### Fixed: multi-log and fleet
- Trends sort by flight time, so improvement no longer reads as regression
  when logs arrive newest first. `max_clip` read a field that does not
  exist and was always 0. Hover throttle compared two different scales.
- Fleet records use the armed battery minimum, full runs only, and keep
  undated flights in order. `history.json` is written atomically.

### Fixed: robustness and security
- A corrupt log can no longer kill the process: pymavlink's compiled
  indexer called C `exit()`, and 5 of 15 damaged logs ended the server.
- Log-derived text can no longer run script in a report. The web server
  checks Host and Origin, serves only reports, and sets a CSP sandbox.
- Peak memory is about half: 11x the log size instead of 22x. The upload
  limit stays at 500 MB, since the largest log still peaks at 8.9 GB.

### Added
- `arduscope-fleet`, and `fleet_report.py` in the wheel and Windows package.
- `tests/binlog.py` writes real-format logs, so CI reaches the parser and
  the pipeline: coverage went from 66% to 89%.
- CI runs ruff, `lint_project.py`, Python 3.10 to 3.15, and a clean install
  of the wheel. Every threshold is now in `docs/convergence_criteria.md`.

## [0.8.6] - 2026-07-24

### Fixed
- `COMPASS_MOT_COMP` is not an ArduPilot parameter. The compensation
  *type* is `COMPASS_MOTCT` (0=off, 1=throttle, 2=current);
  `COMPASS_MOT_X/Y/Z` hold the compensation vector. `compass.py` both
  read the wrong name (so the "already enabled?" guard was always false)
  and **suggested** it, putting a line in the user's `.param` file that
  does nothing. Present since the analyzer shipped.
- Two parameters ArduPilot moved to SI units were only read under their
  old names, so on current firmware both reads silently returned their
  defaults:
  - `mission.py` read Copter's `WPNAV_RADIUS` (cm). It is now
    `WP_RADIUS_M` (m). Both are read, newest first, and the report names
    whichever one the log actually carries.
  - `rangefinder.py` read `RNGFND1_MAX_CM` (cm). It is now `RNGFND1_MAX`
    (m). Same fallback, and the max-range line and figure threshold are
    now in metres.

### Added
- `tools/gen_param_table.py` + `tools/check_params.py`: every ArduPilot
  parameter this project names — `get_param()` calls, `param_suggestions`
  entries, and locale text — is validated against a table harvested from
  the ArduPilot source tree, in CI and in the fast test suite. Wrong
  parameter names were the most common defect in both AI-diagnosis audit
  passes, and all three ways of naming one fail silently: a bad
  `get_param` returns its default forever, a bad suggestion ships to the
  user, and bad prose sends them looking for a parameter that does not
  exist. The three fixes above are its first catch.

## [0.8.5] - 2026-07-24

### Fixed
- AI diagnosis, second pass over the text the first review did not reach
  (the 118 individual-template strings, plus the rule pairings):
  - `imu_consistency` and `imu_ekf_hardware` told the user to disable
    "the faulty IMU" via `INS_USE`, which only disables instance 1. Now
    instance-addressed (`INS_USE` / `INS_USE2` / `INS_USE3`), and the
    critical text now says to rule out vibration before condemning an
    IMU.
  - `clock_health` critical advice led with "check whether the board is
    overheating". Same non-existent thermal-throttle mechanism removed
    from `thermal_cpu_overload` in 0.8.4; the advice is now CPU load,
    `LOG_BITMASK` and card write speed.
  - `notch_tuning` said "correct hover throttle" without naming the
    parameter; now `MOT_THST_HOVER` / `Q_M_THST_HOVER` plus
    `INS_HNTCH_FREQ`/`BW`.
  - Six recommendations were advice-shaped but contained no action
    (`ekf`, `rangefinder` ×2, `pid_tuning`, `mission`, `failsafe` at
    warning level, e.g. "check sensor mounting"). Rewritten with the
    parameter or comparison to actually make.
- AI diagnosis: `gps_ekf` and `gps_glitch_ekf` printed one after the
  other with near-identical advice. The generic rule now stands down
  when the specific one applies.

### Added
- Two cross-rules whose absence made the report blame the wrong thing:
  `vibration` + `imu_consistency` (differing damping paths pull the
  instances apart, so the disagreement is a vibration symptom, not a
  dead IMU) and `gps` + `mission` (waypoint deviation is measured
  against a track that GPS itself produced, so navigation gains cannot
  be judged until GPS is sound).
- `tests/test_ai_diagnosis.py` guards every ArduPilot parameter named in
  AI diagnosis text against a reviewed list, in both locales. Wrong
  parameter names were the most common defect in both review passes and
  nothing caught them.

## [0.8.4] - 2026-07-24

### Fixed
- AI diagnosis: a matched cross-rule reports the priority of its own
  worst finding instead of a hardcoded one. Matching a rule hides its
  analyzers from the individual list, so with a static priority a pair
  of criticals (`gps` + `ekf`, a "medium" rule) reported the card as a
  warning while either critical alone reported it as critical.
- AI diagnosis: `attitude_pid_tuning` fired next to
  `attitude_vibration_control` while asserting vibration was normal, so
  one block read "this is not a PID tuning problem" and the next read
  "vibration is normal". Rules gained an `excludes` field; that rule now
  stands down while vibration is flagged. A missing vibration analyzer
  does not block it, since absence is not evidence.
- AI diagnosis: a warning or critical card carried no `evidence_source`.
  It now states what it is — a cross-correlation over findings that each
  cite their own source. The LLM path declares itself non-authoritative
  rather than passing as a cited verdict.
- AI diagnosis, wrong technical basis in the rule texts:
  - `thermal_cpu_overload` blamed MCU thermal throttling. Neither the
    STM32 nor ArduPilot throttles on temperature, and the temperature
    analyzer reads `IMU.T`, not MCU temperature. Reworded as a shared
    cause, and retitled from a causal claim to a co-occurrence.
  - `gyro_sat_attitude` recommended raising the IMU range. Gyro full
    scale is fixed at +/-2000 dps by the driver, with no parameter.
  - `vtol_wind_transition` said airspeed builds slowly *into* wind. A
    headwind builds airspeed sooner; the downwind transition is the
    dangerous one.
  - `vibration_notch_missing` named `THST_HOVER`, which is neither the
    multirotor (`MOT_THST_HOVER`) nor the QuadPlane (`Q_M_THST_HOVER`)
    parameter.
  - `compass_motor_interference` named `COMPASS_MOT`; the parameter is
    `COMPASS_MOT_COMP`.
  - `compass_consistency_ekf` offered only `COMPASS_USE2`, which cannot
    disable a different instance.
  - `battery_power` quoted an unsourced "< 15 mΩ per cell" and "replace
    past 200 cycles"; both are gone (POSITIONING: no unsourced numbers).

### Added
- AI diagnosis coverage for analyzers the engine ignored: individual
  templates for `board_power`, `ekf_vel_divergence`, `throttle_collapse`
  and `summary` (previously rendered as a bare summary line with no
  recommendation), plus three cross-rules — `ekf_vel_divergence` with
  `vibration` and with `ekf`, and `board_power` with `log_health`.
- `tests/test_ai_diagnosis.py`: 17 tests over the engine, including
  mechanical guards that every rule and template references a real
  analyzer `registry_key`.

## [0.8.3] - 2026-07-24

### Fixed
- `ekf_vel_divergence` compared samples across GPS gaps. `np.interp`
  clamps past its range and draws a straight line over a dropout, so an
  EKF sample inside one was graded against a GPS value from seconds
  away. An EKF sample now needs a GPS sample within 1 s to be graded.
- `ekf_vel_divergence` could merge two brief windows into one long one.
  Divergent runs were built from adjacent array indices, which stop
  being adjacent in *time* once the coverage filter leaves holes; runs
  now break when the sample step exceeds 1 s.
- `ekf_vel_divergence` graded samples the comparison does not apply to.
  Three gates added, each dropping samples rather than moving a
  threshold, and each inert when the log lacks its field:
  - GPS must hold a 3D fix (`GPS.Status >= 3`, AP `GPS_OK_FIX_3D`) with
    at least `gps_nsats_crit` satellites. Without a fix the receiver
    reports `Spd` 0 while the EKF dead reckons; the satellite count is
    the corroboration, since a driver can hold `Status` 4 through a
    stretch where `NSats` has already fallen to 0
  - the firmware must not report the vehicle on the ground
    (`STAT.isFlying`, the gate `pid_tuning` already uses)
  - `get_armed_mask` is all-True on logs with no ARM event, which is how
    bench runs reached the analyzer in the first place

  Measured on the 199-log corpus (2026-07-24): the finding set is
  unchanged at 9 logs, one window drops off `log_43`, and 27 bench logs
  move from "tracks GPS" to "not graded". A relative-altitude airborne
  proxy was tried first and rejected — the only sourced floor available
  (`log_abrupt_airborne_alt_m` = 3 m) is a "clearly flying" test, not a
  "definitely on the ground" test, and it disqualified 84 logs including
  genuine low hovers.

## [0.8.2] - 2026-07-20

### Added
- New analyzer `ekf_vel_divergence` (EKF 速度发散): per-core EKF 3D
  velocity vs GPS doppler divergence windows (≥1 s, ≥15%/2 m/s apart,
  faster source ≥5 m/s). The sprint "GPS 眩晕" signature — EKF velocity
  states running away while the receiver stays healthy — is now a
  first-class finding (warning; critical at ≥40% peak or ≥5 s total)
  instead of something only visible by manual cross-plotting.
  2026-07-20 16-40-11 flags 5 windows / 24.4 s / peak 88% off GPS.

### Changed
- Flight profile climb/descent extremes are now GPS/EKF cross-checked
  with the same gate as the 3D-speed max (previously EKF-only, which
  read 24.6 m/s climb on 16-40-11 while GPS + altitude slope showed
  ~31). A real GPS peak falling inside a divergence window (not
  double-confirmable) is excluded from the stat but reported on its
  own line. Single-source logs keep the raw extreme.
- 3D-speed rejection line reworded as a divergence diagnostic
  ("检测到 EKF 速度发散…"), source-generic.

## [0.8.1] - 2026-07-20

### Fixed
- Flight profile: max-3D-speed headline could report a diverged EKF
  velocity state as the flight's top speed. During an EKF variance
  event the EKF
  velocity runs away while GPS doppler stays sound — real case
  2026-07-20 16-40-11: EKF 88.2 m/s vs GPS 43.3 m/s at the same
  instant (v0.8.0 headlined 88.2 with a 43.6% source-delta footnote).
  The max is now taken only over samples where GPS and EKF agree
  within 15% (or 1 m/s absolute), headlining the GPS-side value; an
  out-of-tolerance rejected peak is reported on its own line instead
  of being dropped silently. Single-source logs are unchanged.

## [0.8.0] - 2026-07-18

### Added
- Flight profile: max 3D speed (ground speed + vertical combined,
  `√(Spd²+VZ²)`), dual-sourced from GPS and EKF (`√(VN²+VE²+VD²)`,
  primary core) with cross-source delta and climb/descent component
  breakdown; 3D speed trace added to the speed-profile figure.
  Motivated by vertical-boost flights where ground
  speed alone understates the true speed by 20%+

## [0.7.0] - 2026-07-17

### Added
- `fleet_report.py --backfill <paths...>`: seed the fleet archive from
  historical logs (analyze + archive only, no HTML). Already-archived
  logs are skipped by sha1 *before* analysis, so re-runs cost seconds;
  per-file failures don't abort the run (exit code 1 flags them)
- Web fleet detail page: alias edit form — set or clear an airframe's
  human-friendly name directly from `/fleet/<key>` (previously CLI-only)

## [0.6.0] - 2026-07-16

### Added
- Fleet history archive (`lib/fleet_history.py`,
  docs/fleet_history_design.md): every analyzed flight is archived
  into `fleet.db` next to the report directory (sqlite, stdlib-only,
  sha1-deduplicated), keyed by the STM32 hardware serial ArduPilot
  prints in its boot banner — airframes self-identify from the log.
  Reports gain an informational "airframe history" card (health score
  + vibration trend across the last 20 flights) once an airframe has
  two archived flights; recording is on by default
  (`--no-fleet-record` to disable), and archive failures never block
  the report
- `fleet_report.py` CLI: `--list` airframes, per-airframe trend view
  (text or standalone HTML page), `--set-alias` for human-friendly
  names; metrics per flight: health score, per-analyzer statuses,
  vibe peaks, battery start/min voltage, board Vcc sag, RATE output
  RMS per axis, duration. Trends are display-only — no thresholds, no
  verdicts (POSITIONING)
- Web fleet pages (Phase 2 of the fleet history design): `/fleet`
  lists every archived airframe (serial, alias, board, flight count,
  last flight, latest health score) and links into `/fleet/<key>` —
  a per-airframe page with the trend figure and a flight table.
  `<key>` accepts a serial, a unique serial prefix, or an alias
  (UTF-8 aliases work in the URL). The main page header links to the
  fleet view; `fleet_history.list_airframes()` now returns the latest
  health score and serial/prefix/alias resolution moved into
  `lib/fleet_history.resolve_serial()` shared by CLI and web

## [0.5.1] - 2026-07-16

### Added
- Message field matrix guardrail: `tools/gen_msg_field_table.py`
  harvests every logged message's field names from the ArduPilot
  source tree into a vendored table (281 messages);
  `tools/check_msg_fields.py` validates every literal message/field
  reference in analyzers/ and lib/ against it, in CI — the
  "field renamed upstream → silent all-NaN dead check" class is now a
  build failure

### Fixed
- Three dead field reads caught by the new matrix checker on its first
  run: `PM.NLoop` is logged as `NL` on the entire fleet (the long-loop
  CPU-pressure check had been silently dead since it shipped),
  `ARSP.Primary` is `Pri`, and `MOTB.BatRes` no longer exists upstream
- Audit H-group closed (26 items, `docs/audit-2026-07-15.md` §六):
  toolchain gates now real (ci_local runs the i18n/contract checks and
  marker-based quick selection; release.py checks staged changes and
  existing tags, pushes only its own tag; baseline_diff compares the
  full result signature and exits non-zero on change), the
  evidence_source contract and param_suggestions dict shape are
  enforced mechanically at runtime, and 18 vacuous/if-gated test
  assertion sites were rebuilt as behavioral tests with preconditions

## [0.5.0] - 2026-07-16

### Added
- New `board_power` analyzer (31st): board 5V rail sag / brownout
  forensics from POWR (wiki-sourced 0.15 V variation limit and
  4.1-5.7 V operating range), MAV_POWER_STATUS flag events (peripheral
  overcurrent, primary-supply loss while armed), and MCU
  temperature/rail envelope with a plausibility guard for the
  H7 Rev V AN5312 temperature mis-scaling
- Logger dropout detection in `log_health` (DSF.Dp): warning at ≥100
  dropped messages (fleet-calibrated — routine boot/arming transients
  of 0-13 stay info), with min-free-buffer forensics; plus LOG_REPLAY
  bloat detection with a LOG_REPLAY=0 suggestion when the EKF replay
  debug stream is left enabled
- QuadPlane VTOL-phase PID coverage in `pid_tuning`: PIQR/PIQP/PIQY
  analyzed against Q_A_RAT_* with the authoritative RATE gate
  restricted to VTOL windows; PSC position-loop velocity tracking
  reported as info metrics (no verdict — no authoritative threshold)
- Message coverage map (`docs/msg_coverage.md`) and scanner
  (`tools/scan_msg_coverage.py`): 116 message types inventoried across
  34 logs, consumption traced, candidate dimensions tiered

### Fixed
- QuadPlane phase mis-attribution in `pid_tuning`: PIDR/PIDP/PIDY (the
  fixed-wing controllers) were mapped to Q_A_RAT_* parameters and
  judged by the hover controller's RATE output. Now mapped to
  RLL_RATE_*/PTCH_RATE_*/YAW_RATE_* and evaluated only on airborne
  fixed-wing windows: VTOL windows cut via the PIQ/QTUN streams
  unioned with Q-mode intervals, MANUAL/TRAINING passthrough excluded
  (stale PID rows), STAT.isFlying as the airborne gate, 10 s data
  floor. Hover-only flights now report "no fixed-wing data" instead of
  >100%-tracking false criticals, and the diluted whole-flight RATE
  gate no longer hides VTOL saturation (a tail-fault fixed-wing flight
  and a hover yaw saturation now correctly surface as critical)
- `pid_tuning` aggregate no longer lets a no-data (info) axis downgrade
  an all-good result

## [0.4.3] - 2026-07-15

### Fixed
- Windows launcher (`start.bat`) reported a complete package as
  "incomplete or corrupted". It built paths from the `%~dps0` 8.3 short
  path, which on some volumes resolved `python\` (a short-safe name) but
  not `log_analysis\` (a 12-char long name), so the entry-point check
  failed even though the file was present. The launcher now runs from
  `pushd "%~dp0"` with relative paths, which resolve regardless of 8.3
  short-name state and handle spaces / parens / dots in the path.

## [0.4.2] - 2026-07-15

### Changed
- Windows packaging is now part of the release workflow: every tagged
  release builds a standalone Windows zip (bundled Python 3.12 +
  dependencies, `.pyc` mode) and attaches it alongside the wheel and sdist
- `build_windows_package.py` fails loudly when no interpreter matches the
  bundled embeddable's minor version, instead of silently compiling
  version-mismatched `.pyc` that dies with "bad magic number" on Windows

## [0.4.1] - 2026-07-15

### Fixed
Runtime bug hunt (`docs/bug-hunt-2026-07-15.md`): 28 diagnostic-correctness
bugs — false positives (crying wolf) and false negatives (silently dead
checks) — found via multi-agent review + dynamic execution on 22 real logs
+ ArduPilot-source cross-check. No crashes; each fixed with a
precondition-guarded regression test.

- Field-name assumptions vs firmware version (silent all-NaN → dead
  check): RFND instance is `Instance` not `I`; VIBE clip is `Clip` not
  Clip0/1/2; BAT remaining is `RemPct` not `Rem`; IMU count reads the
  instance column (was always 1 on modern single-message logs)
- Instance/core interleaving reduced without filtering (mixed sensors) in
  ekf, gyro_saturation, cruise_efficiency, mission, log_health,
  flight_profile and vibration — now filtered to the primary instance/core
- Empty armed-window fallback drove false criticals from pre-arm / boot /
  power-down artifacts: the failsafe battery/GPS/EKF margins only escalate
  inside a known armed window; pid_tuning no longer mislabels an airborne
  flight a ground test
- `gps_glitch`: filter no-fix (0,0) samples and use the primary GPS
  instance (was hardcoded to instance 0)
- `pid_tuning`: tracking gate compared rad/s against deg/s floors (dead
  gate); a constant I-term read as windup; no-op (suggested==current)
  suggestions dropped
- `motor`: MOT_PWM_MIN/MAX=0 ("auto") collapsed the saturation floor →
  false critical and disabled floor detection; ground-idle no longer
  flagged as floor saturation
- `notch_tuning`: a warning/critical with no auto-adjustable parameter no
  longer reports PASS
- `compass`: all-NaN offsets no longer leak `nan` or disable the
  field-variation warning
- `throttle_collapse`: no-MODE logs are no longer silently disabled
- `log_health`: a clean land+disarm+unplug is no longer misdiagnosed as a
  power-path interruption
- `vtol_transition`: rebuilt on the QTUN.Trn transition state (TMix is the
  throttle mix, not transition progress) — reports real ~5-6s transitions
  instead of a whole-flight artifact
- `web_app`: the UI locale is propagated to the worker and request threads
  (reports were always Chinese under a threaded server)

### Changed
- `BaseAnalyzer.read_primary_instance()` helper for instance/core
  filtering
- `release.py`: emit a `chore:` commit prefix (the bare `release:` was
  rejected by the commit-msg hook) and splice new CHANGELOG sections after
  `[Unreleased]`

## [0.4.0] - 2026-07-15

### Added
- New `flight_profile` analyzer (30th): a descriptive, info-level flight
  envelope — max relative altitude, ground/air speed, climb & descent
  rate, distance travelled, range from home, and roll/pitch envelope,
  all over the armed window, with altitude and speed profile plots
- New `throttle_collapse` analyzer (29th): collective output pinned at
  the MOT_SPIN_MIN floor while airborne, baro false-climb trigger
  detection, and the post-collapse attitude limit cycle — with an
  aliasing guard that refuses to name an oscillation frequency when the
  log rate is under 4x
- Crash-forensics pattern: abrupt log end while armed (power-path
  interruption signature) in `log_health`, with battery-trend
  discrimination between path faults and cell exhaustion
- Throttle deadzone forensics in `rc_input` (info-only): windows where
  the throttle was pushed up but never crossed the THR_DZ band top
- Charge-backfill calibration hint in `battery` (info-only, analog
  monitors): consumed mAh with a pre-filled formula so the charger's
  refill reading verifies BATT_AMP_PERVLT end-to-end

### Fixed
- ArduPilot-semantics batch (audit §2.2), all verified against
  `AP_Logger/AP_Logger.h`:
  - `summary` EVENT_NAMES / ERROR_SUBSYS_NAMES enum tables were
    mis-mapped — routine landings read as flips, and THRUST_LOSS_CHECK
    (a crash signal) displayed as "Failsafe Leak". Rebuilt from the
    authoritative LogEvent / LogErrorSubsystem enums
  - `failsafe` counted ECode 0 (ERROR_RESOLVED / recovery) as a
    trigger, turning recovery-only logs into false criticals; now
    counts occurrences only
  - `failsafe` subsystem scope corrected: dropped detection subsystems
    (EKFCHECK, BARO) that are not failsafes, added the missing real
    failsafes (ADSB / TERRAIN / SENSORS / LEAK / VIBE / DEADRECKON)
  - `failsafe` EKF variance now evaluated on the primary lane only
    (XKF4/NKF4 C column) — a backup-lane spike no longer reads as an
    FS_EKF_THRESH breach
  - `failsafe` removed the BATT_LOW_VOLT auto-suggestion (0.92 × peak
    voltage — unsourced, and high enough to itself provoke a false
    low-battery failsafe)
  - `airspeed` health field renamed Health → H in 4.1+; a LogReader
    alias restores the check, which had silently read all-NaN and never
    fired on a failing pitot
- `gyro_saturation`: IMU gyro is logged in rad/s while the saturation
  threshold is in deg/s — the missing conversion silenced every real
  saturation event
- `notch_tuning`: ISBH batch-sampler gyro type selection, plane
  hover-throttle N/A handling, and primary-IMU FFT source
- `vibration` FFT filtered to the primary IMU instance (multi-IMU logs
  interleave instances at identical timestamps)
- Vehicle-type namespace unified: canonical type propagated to
  `Thresholds`, and Q_ENABLE ≥ 1 now detects VTOL-AUTO QuadPlanes;
  vendor-renamed firmware banners fall back to the parameter namespace
- PM.Load per-mille values in the 100-200 range were treated as percent
  and produced impossible "180% CPU" criticals
- Deep-audit hardening: ~75 correctness fixes across 38 files (all-NaN
  crashes, timestamp-ordering false positives, HTML-escaping gaps)

### Changed
- `action_level` ↔ `param_suggestions` invariant is now mechanically
  enforced in `AnalysisResult.__post_init__`
- Rule-based diagnosis relabelled from "AI" to "Integrated"
- Engineering infra: BaseAnalyzer helpers (`get_armed_mask`,
  `read_primary_imu`, `safe_nanmax`), behavioral test suite, and CI
  hardening (i18n missing-key scan, threshold-authority check)

## [0.3.1] - 2026-05-25

### Fixed
- TECS field name mismatch on V4.6.x firmware (LogReader alias layer)
- Airspeed noise detection mixed multi-sensor instances
- Windows build path double-nesting
- Windows build Python version fallback
- Baseline snapshot test path corrections

### Changed
- Engineering improvements: thread-safe HISTORY, pipeline dedup
- Project structure cleanup: removed 97 redundant files
- Merged ruff.toml into pyproject.toml
- Local CI runner (tools/ci_local.py) with 5 checks

## [0.3.0] - 2026-05-24

### Added
- Baseline snapshot regression testing (364 snapshots, 14 real logs)
- Waitress production WSGI server (--server waitress --workers N)
- Batch upload API endpoint (/api/upload-batch)
- 7 cross-diagnosis rules (22 total)
- SITL integration test framework (6 scenarios)
- Dual airspeed sensor cross-comparison

### Changed
- Unified analysis pipeline (lib/pipeline.py)
- batch_report uses pipeline entry point

### Fixed
- Thread-safe i18n locale via threading.local()
- Deduplicated health score computation

## [0.2.0] - 2026-05-23

### Added
- Dark mode toggle with persistent preference
- Toast notifications for user feedback
- Compare-history feature in web UI
- Trend overlay in multi-log reports
- PDF export via weasyprint (optional)
- Web language switcher (zh/en)
- Conflict detection for cross-analyzer parameter suggestions
- QuadPlane-specific analyzers (TECS tuning)

### Changed
- Full i18n migration (1147 keys, zh + en)

## [0.1.0] - 2026-05-22

### Added
- Initial release: 26 analyzers with auto-discovery
- HTML report generation with Plotly charts
- CLI entry points: flight_report, multi_report, batch_report
- Web interface (Bottle)
- Structured parameter suggestions (.param export)
- Health score and executive summary
- GPS track extraction and visualization
- MIT license, pip packaging
