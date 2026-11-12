# ArduScope positioning

[中文](POSITIONING_zh.md)

**Last updated**: 2026-11-11
**Maintainer**: L-X-Yao
**What this document is for**: what the tool does and does not do is settled against it. To change the positioning, change this file.

---

## 1. In one sentence

**A diagnostic tool for ArduPilot tuning engineers that turns "it flies bad" into "change this parameter".**

---

## 2. Users

**Target user: the ArduPilot tuning engineer**, who has changed parameters before and can read the terms of METHODIC, the [ArduPilot Methodic Configurator's tuning guide](https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter.html).

- Design decisions are the maintainer's (L-X-Yao).
- **Log data** from users is accepted as a source for calibrating thresholds.
- A false alarm is a defect: a report of one, with the log, is welcome.
- A request to lower the diagnostic standard is not accepted. Strictness is a feature, not a bug.
- There is no simplified mode, and nothing is designed to lower the bar.

---

## 3. The problem it solves

**One pain point: diagnosis, from "it flies bad" to "change this parameter".**

Where things stand:
- A pilot can feel that something is wrong and **does not know which parameter to change**.
- Most tools show raw data and give no conclusion.
- ArduPilot's METHODIC documents take knowledge to use and time to search.

What this tool does:
- It reads the log, detects the problems, and gives its evidence and its parameter suggestions.
- It encodes what METHODIC says into the rules of 32 analyzers.
- Every warning and critical cites what it rests on.
- A suggestion names its parameter. A value needs a source and a flight, both recorded in `docs/suggestion_evidence.md`. A rule that has not been flown either points the way without a value, or carries a waiver with its reason in `tools/check_suggestions.py` and is listed in `docs/known_limitations.md`. A setting that only changes what is logged is not graded by a flight.

**What it does not do**, at this stage:
- No certification and no scoring: proving an aircraft good is not the aim. The health score in a report is for sorting and triage, not a certificate. With a critical it is capped at 49, in the red band (confirmed 2026-11-05).
- No teaching: that is the job of documentation, not of a tool.
- No reassurance: the tool does not soften what it finds.

---

## 4. Where the tool stands

### 4.1 Strictness
**Thresholds follow METHODIC strictly**, and are not loosened so that a user feels the aircraft flies well.

- "It flies fine" to the user does not mean the tool should be all green.
- Where the tool finds a gap, it says so.
- A user who disagrees can ignore a suggestion, and **cannot ask the tool to change its judgement**.

### 4.2 Where thresholds come from, in order
1. **The METHODIC documents**: the highest authority, a baseline that is not crossed.
2. **Hard limits in the ArduPilot source**: failsafe and pre-arm conditions, the basis for a critical.
3. **Calibration on real logs**: the distribution over real flights, used to place the warning and optimize boundaries.
4. ❌ **Not used**: a value an engineer picked from experience.

Where the code does not meet this yet is listed in [docs/known_limitations.md](docs/known_limitations.md). The fields marked `E` in `lib/thresholds.py` have no authoritative source and stand as waivers: `tests/test_threshold_authority.py` holds the list, a new one needs a written reason, and those that still move a verdict are in the limitations.

### 4.3 Showing the authority
Every suggestion in a report **cites what it rests on**:
- "Source: METHODIC IMU_Temperature.md §3.2"
- "Source: ArduPilot wiki, EKF Failsafe, default FS_EKF_THRESH=1.0"
- "Source: 75th percentile over 21 flight logs"

A user trusts the tool once the source is on the page.

---

## 5. Form

Today:

- CLI: `flight_report.py log.BIN` gives an HTML report and `.param` suggestions
- Web: `web_app.py`, drag and drop in the browser
- Several logs: `multi_report.py`, `batch_report.py`
- Fleet history: `fleet_report.py`, and `/fleet` in the web interface
- HTML reports that work offline, map tiles aside

**How deep and how right the report is** comes first, its form second.

---

## 6. Maintenance

**Feature work ended on 2026-11-10. On 2026-11-12 one item was reopened: the online analysis agent (see [docs/online_agent_plan.md](docs/online_agent_plan.md)). Outside it, the maintainer fixes defects and adds no features.**

Pull requests go through the gates in [CONTRIBUTING.md](CONTRIBUTING.md). One that changes analyzer logic or a threshold waits for the maintainer to run the snapshot and corpus steps, on logs that are not public.

**Not done**:
- ❌ Polish so that a report looks professional
- ❌ Design ahead of need, to cover more cases
- ❌ A change of policy for one piece of feedback: the standard is not lowered for one user

**Done**:
- ✅ Better accuracy: fewer false alarms, fewer misses
- ✅ Making the authority visible, so that a suggestion can be trusted
- ✅ Calibrating thresholds on real log data

---

## 7. Where it stands

### Finished
- The offline diagnostic engine: 32 analyzers over the main dimensions, threshold sources in `docs/convergence_criteria.md`, structured `param_suggestions` with `.param` export, audits and tests.
- Open source: public since 2026-11-11. `pip install .` works (the package is `ardupilot-scope`, released on GitHub only, never on PyPI), the README is in English, and reports come in Chinese and English.

### Ideas with no schedule
Listed to show where the project was heading. None is a promise:
- Trends over many flights, as a tuning convergence assistant
- An anonymous community log dataset, for thresholds calibrated on statistics
- Mission Planner and MAVProxy integration
- Two tiers of threshold, warning and optimize

---

## 8. Changing this document

- The maintainer changes it, and keeps the Chinese and English copies in step.
- **Log data** from the community can calibrate a threshold. **A request for a lower standard** is no ground for a change.
- Before a significant change, read the one-sentence positioning and check that the change points the same way.

---

**End of document.**
