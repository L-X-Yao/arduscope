# Known limitations

What ArduScope does not do, or does not get right, as of 2026-11-12, and what
to do about each. What was found and fixed is in [CHANGELOG.md](../CHANGELOG.md).
Figures are from the two log corpora the release gate scans: 561 logs, 560 of
them readable.

## Verdicts

- **A critical is common, and its share of false alarms is not known.** 414 of
  560 logs carry at least one: `attitude` on 223, `battery` on 109,
  `notch_tuning` on 98, `vibration` on 93, `failsafe` on 88. No one has gone
  through them flight by flight. Read the figures on a card, not its badge
  alone.
- **`attitude` reads critical on one sample.** A single row with over 15 deg
  of roll or pitch error is critical. That 15, and the 20 and 4.0 beside it in
  `analyzers/attitude.py`, are literals with no source, outside
  `lib/thresholds.py` and its checks.
- **`notch_tuning` grades attitude by proxies.** A mean attitude error over
  2.5 deg with a peak rate over 100 deg/s reads critical: 81 of its 98
  criticals. A mean error over 5 deg does too: 14 more. `lib/thresholds.py`
  marks these limits E, with no source, and a fast flight passes 100 deg/s on
  purpose.
- **A hover throttle parameter over 0.12 from the measured hover reads
  critical.** In SITL a parameter of 0.20 on an aircraft that hovers at 0.36
  flew the same as the right one, within 6%. The limits are marked E.
- **`compass` misses interference fixed to the airframe.** It compares the
  field at high and low throttle, which averages a body-fixed offset away: an
  offset injected in SITL read 14.3%, under the 30% limit. A fit per axis would
  catch it and is not implemented.

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
- **Map tiles need a network.** Everything else in a report is offline.

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

- **Top-level module names are generic.** `lib`, `analyzers`, `locales`, the
  five entry scripts and an `__init__.py` install at the root of site-packages.
  Give ArduScope a virtual environment of its own, or use the Windows package.
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
