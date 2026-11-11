# ArduScope

[![CI](https://github.com/L-X-Yao/arduscope/actions/workflows/ci.yml/badge.svg)](https://github.com/L-X-Yao/arduscope/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)

ArduPilot .BIN flight log diagnostic tool. Turns "it flies bad" into "change this parameter".

[中文说明](README_zh.md)

## What it does

ArduScope reads ArduPilot `.BIN` logs and runs 32 analyzers to produce actionable parameter recommendations, plus a descriptive flight-envelope summary. Every warning cites its source: METHODIC, which is the [ArduPilot Methodic Configurator's tuning guide](https://ardupilot.github.io/MethodicConfigurator/TUNING_GUIDE_ArduCopter.html), the ArduPilot wiki, or the ArduPilot source code.

**Key features:**
- **32 auto-discovered analyzers** — drop a new analyzer into `analyzers/` and it works
- **Structured parameter suggestions** — concrete param names + values, exportable as `.param` files for Mission Planner
- **METHODIC-backed thresholds** — every warning and critical cites its source. The limits that have none yet are listed in the [known limitations](docs/known_limitations.md)
- **Cross-correlation diagnosis** — 27 rules detect multi-system fault patterns
- **Interactive HTML reports** — Plotly charts and map code embedded, so reports work offline. Map tiles need a network; the map says so when it has none
- **Multi-log comparison** — track tuning convergence across flights
- **Fleet history** — every flight auto-archived per airframe (hardware-serial keyed); trend card in reports, `fleet_report.py` CLI (incl. `--backfill` for historical logs) + web fleet page (`/fleet`, with alias editing)
- **Web + CLI** — browser drag-and-drop or command-line batch processing
- **Bilingual** — Chinese by default, English with `--lang en`

## Quick start

On Windows without Python: download `arduscope-vX.Y.Z-win64.zip` from
[Releases](https://github.com/L-X-Yao/arduscope/releases), unzip it and
double-click `start.bat`. The package carries its own Python and opens the web
interface in the browser.

From source, with Python 3.10 or later:

```bash
git clone https://github.com/L-X-Yao/arduscope.git
cd arduscope
pip install .

# Analyze a single log
python3 flight_report.py 00000285.BIN

# Compare multiple logs
python3 multi_report.py flight1.BIN flight2.BIN flight3.BIN

# Batch processing
python3 batch_report.py logs/ -o reports/

# Web interface
pip install .[web]
python3 web_app.py
```

`pip install .` also installs the commands `arduscope`, `arduscope-multi`, `arduscope-batch`, `arduscope-fleet` and `arduscope-web`, one for each script above and for `fleet_report.py`.

Reports and the web interface are in Chinese unless told otherwise: add `--lang en` to any command, or use the switch at the top of the web page.

## Usage

```bash
# Output to specific path
python3 flight_report.py -o report.html log.BIN

# Export as JSON / CSV / PDF
python3 flight_report.py --format json log.BIN
python3 flight_report.py --format csv log.BIN
python3 flight_report.py --format pdf log.BIN   # requires: pip install .[pdf], and Chrome for the charts

# Language selection
python3 flight_report.py --lang en log.BIN

# Custom threshold overrides
python3 flight_report.py --thresholds custom.json log.BIN

# Run / skip specific analyzers
python3 flight_report.py --only summary,attitude,motor log.BIN
python3 flight_report.py --skip vibration,ekf log.BIN

# Scenario templates: a subset of the analyzers, some with stricter thresholds
python3 flight_report.py --list-templates
python3 flight_report.py --template crash-diagnosis log.BIN

# Leave a flight out of the fleet history (fleet.db, next to the report)
python3 flight_report.py --no-fleet-record log.BIN

# Multi-log comparison
python3 multi_report.py -o trend.html *.BIN

# Web interface (development)
python3 web_app.py --port 9090 --report-dir ./reports   # default: ~/.arduscope/reports
python3 web_app.py --keep-reports 1000                  # delete the oldest past N (default: keep all)
python3 web_app.py --host 0.0.0.0 --allow-host my.host  # no login: only on a network you trust

# Web interface (production)
pip install .[production]
python3 web_app.py --server waitress --workers 8

# AI diagnosis through an LLM (default: the offline rule engine)
pip install .[ai]
ANTHROPIC_API_KEY=... python3 flight_report.py --ai-provider anthropic log.BIN
```

## Analyzers

| Analyzer | Key | Messages | Description |
|----------|-----|----------|-------------|
| Flight Summary | `summary` | prescan | Firmware, mode timeline, error events |
| Flight Profile | `flight_profile` | GPS, POS, XKF1, ARSP, ATT | Altitude / speed / climb / range / attitude envelope (descriptive) |
| Attitude | `attitude` | ATT, RATE, PIDx | Roll/Pitch/Yaw tracking error, PID analysis |
| Motor Output | `motor` | RCOU, MOTB | Saturation, balance, hover margin |
| Vibration | `vibration` | VIBE, IMU | Vibration levels, FFT spectrum |
| Gyro Saturation | `gyro_saturation` | IMU | Gyroscope sensor saturation detection |
| Throttle Collapse | `throttle_collapse` | CTUN, RCOU | Throttle pinned at the mixer floor, limit-cycle forensics |
| Notch Tuning | `notch_tuning` | RCOU, ISBH/ISBD, IMU | FFT peak detection + notch parameter recommendations |
| PID Tuning | `pid_tuning` | ATT, RATE, PIDx | PID quality assessment + tuning suggestions |
| Servo Dither | `servo_dither` | RCOU | Zero-crossing rate oscillation detection |
| EKF Health | `ekf` | XKF4/NKF4, XKF3/NKF3 | Variance monitoring, innovation |
| Battery | `battery` | BAT | Voltage, current, remaining capacity |
| Power | `power` | BAT, GPS | Power draw, energy (Wh), efficiency (Wh/km) |
| Board Power | `board_power` | POWR, MCU | Board voltage sag and brownout forensics |
| GPS Quality | `gps` | GPS, GPA | Satellite count, HDop, track plot |
| GPS Glitch | `gps_glitch` | GPS | Position jump detection via haversine distance |
| EKF Velocity Divergence | `ekf_vel_divergence` | GPS, XKF1 | EKF velocity against GPS, per lane |
| Compass | `compass` | MAG, RCOU | Field consistency, offsets, interference |
| Compass Consistency | `compass_consistency` | MAG | Multi-compass field magnitude RMS comparison |
| Failsafe Margin | `failsafe` | ERR/EV, BAT, GPS, XKF4 | Margin to failsafe trigger thresholds |
| Clock Health | `clock_health` | ATT | Timestamp monotonicity, jitter, gap detection |
| IMU Consistency | `imu_consistency` | IMU | Multi-sensor accel/gyro comparison |
| Temperature | `temperature` | IMU | IMU temp drift, high-temp warnings |
| RC Input | `rc_input` | RCIN | Channel monitoring, signal loss detection |
| Rangefinder | `rangefinder` | RFND, CTUN | Distance quality, baro comparison |
| Log Health | `log_health` | PM, ATT | Loop time, CPU load, message timing |
| VTOL Transition | `vtol_transition` | QTUN | Transition time, altitude loss (QuadPlane only) |
| Airspeed | `airspeed` | ARSP, GPS | Dual-airspeed consistency, pitot blockage |
| TECS Tuning | `tecs_tuning` | TECS | Height/speed tracking RMS in cruise (ArduPlane only) |
| Cruise Efficiency | `cruise_efficiency` | BAT, GPS | Energy per km, cruise power estimate |
| Mission | `mission` | CMD, GPS/POS | Waypoint accuracy, completion rate |
| Wind | `wind` | XKF2/NKF2 | Wind speed/direction, gust analysis |

## Output formats

**HTML report** (primary) — executive summary, health dashboard, 32 analyzer cards with interactive charts, unified `.param` suggestion table.

**JSON** — structured data with `status` / `action_level` / `param_suggestions` per analyzer.

**CSV** — tabular format, opens directly in Excel.

**PDF** — print-ready report (requires `pip install .[pdf]`, and Chrome for kaleido to render charts).

## Threshold sources (priority order)

1. **METHODIC official docs** — baseline, non-negotiable
2. **ArduPilot source code limits** — failsafe/pre-arm trigger values
3. **Real flight log calibration** — statistical distributions from real flights

Thresholds are automatically read from the log's own parameters (`FS_EKF_THRESH`, `BATT_LOW_VOLT`, etc.) when available.

Custom overrides via `--thresholds custom.json`:
```json
{
    "vibe_warn": 20.0,
    "vibe_crit": 40.0,
    "temp_warn": 55.0
}
```

## Adding a new analyzer

Auto-discovery — just create a `.py` file in `analyzers/`:

```python
from . import AnalysisResult, BaseAnalyzer

class MyAnalyzer(BaseAnalyzer):
    registry_key = "my_analyzer"
    sort_order = 200
    required_messages = ["MSG_TYPE"]

    def analyze(self) -> AnalysisResult:
        return self.make_result(
            title="My Analysis",
            status="good",
            summary="All clear",
        )
```

No entry-point changes needed. Automatically registered in CLI, Web, and multi-log comparison.

## Positioning

ArduScope is for **ArduPilot tuning engineers**. Strictness is a feature, not a bug: finding a threshold strict is no ground for loosening it. Log data is welcome for calibrating thresholds, and so is a false alarm reported with its log. A request to lower the standard is not accepted. Feature work ended in November 2026: the maintainer fixes defects and adds no features. See [POSITIONING.md](POSITIONING.md).

## Known limitations

[docs/known_limitations.md](docs/known_limitations.md) lists what ArduScope does not do or does not get right. Three to know before acting on a report:

- A critical is common: 414 of the 560 corpus logs carry one, and its share of false alarms has not been measured.
- Suggested parameters were flown in SITL, 12 rules of 20, and none on an aircraft. Go through a `.param` file before loading it.
- A damaged log is read as far as it goes, and the report does not say it is damaged.

## Project structure

```
arduscope/
├── flight_report.py      CLI entry point (single log)
├── multi_report.py       Multi-log comparison CLI
├── batch_report.py       Batch processing CLI
├── fleet_report.py       Fleet history CLI
├── web_app.py            Web interface (Bottle + Waitress)
├── lib/                  Core libraries
│   ├── pipeline.py       Unified analysis pipeline
│   ├── log_reader.py     pymavlink .BIN parser
│   ├── html_report.py    HTML report generator
│   ├── ai_diagnosis.py   Cross-correlation engine (27 rules)
│   ├── thresholds.py     Threshold configuration
│   ├── i18n.py           Internationalization
│   └── ...
├── analyzers/            32 auto-discovered analyzers
├── locales/              zh.json + en.json
├── tools/                CI, lint, release, baseline utilities
├── tests/                pytest suite (fast, synthetic, real-log and SITL tiers)
└── docs/                 Threshold traceability, suggestion evidence, known limitations
```

## Security

The web interface has no login and listens on 127.0.0.1 unless told
otherwise. [SECURITY.md](SECURITY.md) says what that means and how to report a
vulnerability.

## License

[MIT](LICENSE)
