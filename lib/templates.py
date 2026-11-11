"""
Scenario-based analysis templates for ArduPilot flight controllers.

Each template pre-configures which analyzers to run, custom threshold
overrides, and a descriptive title — encoding ArduPilot domain knowledge
into reusable analysis profiles.

Usage:
    from lib.templates import get_template, list_templates

    tpl = get_template("quadplane-accept")
    # tpl.analyzers  -> ["summary", "vtol_transition", "motor", ...]
    # tpl.thresholds -> {"att_rms_warn": 1.5, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lib.i18n import t


@dataclass
class ScenarioTemplate:
    """A predefined analysis scenario."""
    name: str               # CLI key, e.g. "quadplane-accept"
    display_name: str       # Localized display name
    description: str        # One-line description
    analyzers: list[str]    # Analyzer keys to run (empty = all)
    thresholds: dict[str, float] = field(default_factory=dict)


# Static data for each template (everything except display_name/description).
# Threshold overrides are class E: stricter than the cited defaults for this
# scenario, with no published source. The report says so (lib/pipeline.py).
_TEMPLATE_DEFS: list[dict] = [
    # 1. QuadPlane acceptance
    {
        "name": "quadplane-accept",
        "analyzers": [
            "summary", "attitude", "motor", "vibration", "ekf",
            "battery", "gps", "compass", "failsafe",
            "imu_consistency", "vtol_transition", "airspeed",
            "wind", "log_health", "rc_input",
        ],
        "thresholds": {
            "att_rms_warn": 1.5,  # E: acceptance margin, default 2.5 (Plane 5)
            "att_rms_crit": 4.0,  # E: acceptance margin, default 5 (Plane 10)
            "vibe_warn": 12.0,  # E: acceptance margin under METHODIC's 15
            "vibe_crit": 25.0,  # E: acceptance margin under METHODIC's 30
            "motor_imbalance_warn": 0.08,  # E: acceptance margin under 10%
            "motor_imbalance_crit": 0.15,  # E: acceptance margin under 20%
        },
    },
    # 2. Tuning comparison
    {
        "name": "tuning-compare",
        "analyzers": [
            "summary", "attitude", "pid_tuning", "vibration",
            "motor", "log_health",
        ],
        "thresholds": {},
    },
    # 3. Crash / anomaly diagnosis
    {
        "name": "crash-diagnosis",
        "analyzers": [],  # empty = run ALL analyzers
        "thresholds": {
            "vibe_warn": 10.0,  # E: show precursors, METHODIC's "good" top
            "ekf_variance_warn": 0.3,  # E: show precursors, half the default
            "motor_saturation_warn": 0.03,  # E: show precursors under 10%
            "motor_imbalance_warn": 0.05,  # E: show precursors, half of 10%
        },
    },
    # 4. Battery aging
    {
        "name": "battery-aging",
        "analyzers": [
            "summary", "battery", "power", "motor",
        ],
        "thresholds": {},
    },
    # 5. Copter factory acceptance
    {
        "name": "copter-factory",
        "analyzers": [
            "summary", "attitude", "motor", "vibration", "ekf",
            "battery", "gps", "compass", "imu_consistency",
            "temperature", "log_health", "rc_input",
        ],
        "thresholds": {
            "att_rms_warn": 1.5,  # E: factory margin under the 2.5 default
            "att_rms_crit": 3.0,  # E: factory margin under the 5.0 default
            "vibe_warn": 10.0,  # E: factory margin, METHODIC's "good" top
            "vibe_crit": 20.0,  # E: factory margin under METHODIC's 30
            "imu_accel_diff_warn": 0.3,  # E: factory margin under 0.5
            "imu_accel_diff_crit": 0.6,  # E: factory margin under 1.0
        },
    },
    # 6. Vibration & tuning diagnosis
    {
        "name": "tuning",
        "analyzers": [
            "summary", "vibration", "notch_tuning", "pid_tuning",
            "motor", "attitude",
        ],
        "thresholds": {},
    },
]


def _build_templates() -> dict[str, ScenarioTemplate]:
    """Build template dict, resolving i18n strings at call time."""
    result: dict[str, ScenarioTemplate] = {}
    for defn in _TEMPLATE_DEFS:
        name = defn["name"]
        tpl = ScenarioTemplate(
            name=name,
            display_name=t(f"template.{name}.display_name"),
            description=t(f"template.{name}.description"),
            analyzers=defn["analyzers"],
            thresholds=defn["thresholds"],
        )
        result[name] = tpl
    return result


# =================================================================
# Public API
# =================================================================

# Backward-compatible module-level accessor (lazy, rebuilt on each access
# so that locale changes are respected).
class _TemplatesProxy(dict):
    """Dict-like proxy that rebuilds from i18n on every access."""
    def _refresh(self) -> dict[str, ScenarioTemplate]:
        return _build_templates()

    def __getitem__(self, key):
        return self._refresh()[key]

    def get(self, key, default=None):
        return self._refresh().get(key, default)

    def __contains__(self, key):
        return key in self._refresh()

    def __iter__(self):
        return iter(self._refresh())

    def __len__(self):
        return len(self._refresh())

    def values(self):
        return self._refresh().values()

    def items(self):
        return self._refresh().items()

    def keys(self):
        return self._refresh().keys()


TEMPLATES: dict[str, ScenarioTemplate] = _TemplatesProxy()  # type: ignore[assignment]


def get_template(name: str) -> ScenarioTemplate | None:
    """Get a template by name. Returns None if not found."""
    return _build_templates().get(name)


def list_templates() -> list[ScenarioTemplate]:
    """Return all available templates."""
    return list(_build_templates().values())
