"""ArduPilot-compatible battery-voltage scope, persistence and validity checks."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .i18n import t


@dataclass(frozen=True)
class VoltageEvaluation:
    status: str
    reason: str
    instance: int
    param_prefix: str
    timer: float
    min_voltage: float | None
    max_voltage: float | None
    low_duration: float
    critical_duration: float
    parameter: str = ""
    limit: float = 0.0

    @property
    def config_warning(self) -> bool:
        return self.reason in {
            "arm_volt", "sensor_invalid", "config_low", "config_critical"}


def _covered_seconds(timestamps: np.ndarray, voltage: np.ndarray,
                     threshold: float, timer: float) -> tuple[float, bool]:
    """Longest covered low-voltage run; gaps never supply the evidence."""
    if threshold <= 0 or len(timestamps) < 2:
        return 0.0, bool(len(timestamps) and np.any(voltage < threshold))
    deltas = np.diff(timestamps)
    positive = deltas[deltas > 0]
    sample_period = float(np.median(positive)) if len(positive) else 1.0
    longest = 0.0
    current = 0.0
    seen = False
    previous_t = None
    previous_low = False
    for stamp, value in zip(timestamps, voltage):
        low = value < threshold
        if low:
            seen = True
            if previous_low:
                gap = stamp - previous_t
                if gap > timer:
                    current = 0.0
                else:
                    # A telemetry gap contributes no more than the normal
                    # sample period. It cannot manufacture persistence.
                    current += min(gap, sample_period)
            longest = max(longest, current)
        else:
            current = 0.0
        previous_t = stamp
        previous_low = low
    return float(longest), seen


def _reference_voltage(voltage: np.ndarray) -> float | None:
    """Second-highest armed sample, so one spike cannot validate a pack."""
    if not len(voltage):
        return None
    if len(voltage) == 1:
        return float(voltage[0])
    return float(np.partition(voltage, -2)[-2])


def evaluate_voltage(timestamps, voltage, armed_intervals, *,
                     low_volt: float, critical_volt: float, low_timer: float,
                     arm_volt: float, monitor: float,
                     config_max_ratio: float, sensor_min_volt: float,
                     instance: int = 0, param_prefix: str = "BATT",
                     ) -> VoltageEvaluation:
    """Grade one battery instance without reading or mutating a log reader."""
    ts = np.asarray(timestamps, dtype=float)
    volts = np.asarray(voltage, dtype=float)
    finite = np.isfinite(ts) & np.isfinite(volts)
    ts, volts = ts[finite], volts[finite]
    order = np.argsort(ts, kind="stable")
    ts, volts = ts[order], volts[order]

    def result(status, reason, **kwargs):
        return VoltageEvaluation(
            status=status, reason=reason, instance=instance,
            param_prefix=param_prefix, timer=float(low_timer),
            min_voltage=kwargs.get("min_voltage"),
            max_voltage=kwargs.get("max_voltage"),
            low_duration=kwargs.get("low_duration", 0.0),
            critical_duration=kwargs.get("critical_duration", 0.0),
            parameter=kwargs.get("parameter", ""),
            limit=kwargs.get("limit", 0.0),
        )

    if monitor == 0:
        return result("good", "monitor_disabled",
                      parameter=f"{param_prefix}_MONITOR")
    if not armed_intervals:
        return result("good", "no_armed")

    armed = np.zeros(len(ts), dtype=bool)
    for start, end in armed_intervals:
        armed |= (ts >= start) & (ts <= end)
    ts, volts = ts[armed], volts[armed]
    if not len(ts):
        return result("good", "no_armed")

    min_voltage = float(np.min(volts))
    observed_max = _reference_voltage(volts)
    common = {"min_voltage": min_voltage, "max_voltage": observed_max}
    if low_timer <= 0:
        return result("good", "disabled", **common,
                      parameter=f"{param_prefix}_LOW_TIMER")
    if arm_volt > 0 and observed_max < arm_volt:
        return result("warning", "arm_volt", **common,
                      parameter=f"{param_prefix}_ARM_VOLT", limit=arm_volt)
    # A flat sub-volt armed line is still diagnosed, not silently skipped:
    # this is stricter than the firmware's voltage_used > 0 guard, but the
    # offline verdict is sensor/configuration warning, not voltage critical.
    if arm_volt == 0 and observed_max < sensor_min_volt:
        return result("warning", "sensor_invalid", **common,
                      parameter=f"{param_prefix}_ARM_VOLT",
                      limit=sensor_min_volt)
    if critical_volt > 0 and critical_volt >= observed_max:
        return result("warning", "config_critical", **common,
                      parameter=f"{param_prefix}_CRT_VOLT", limit=critical_volt)
    if low_volt > 0 and low_volt >= observed_max * config_max_ratio:
        return result("warning", "config_low", **common,
                      parameter=f"{param_prefix}_LOW_VOLT", limit=low_volt)

    low_duration, low_seen = _covered_seconds(ts, volts, low_volt, low_timer)
    critical_duration, critical_seen = _covered_seconds(
        ts, volts, critical_volt, low_timer)
    common.update(low_duration=low_duration,
                  critical_duration=critical_duration)
    if critical_seen and critical_duration > low_timer:
        return result("critical", "critical", **common,
                      parameter=f"{param_prefix}_CRT_VOLT", limit=critical_volt)
    if low_seen and low_duration > low_timer:
        return result("warning", "low", **common,
                      parameter=f"{param_prefix}_LOW_VOLT", limit=low_volt)
    return result("good", "not_sustained" if low_seen or critical_seen else "ok",
                  **common)


def voltage_details(evaluation: VoltageEvaluation, analyzer: str) -> list[str]:
    """Localized report details for a completed evaluation."""
    e = evaluation
    details = []
    if e.reason == "critical":
        if analyzer == "battery":
            details.append(t("battery.volt_below_crit", inst=e.instance,
                             min_volt=f"{e.min_voltage:.2f}",
                             crt_volt=f"{e.limit:.2f}"))
        else:
            details.append(t("power.volt_below_crit",
                             min_volt=f"{e.min_voltage:.2f}",
                             crt_volt=f"{e.limit:.2f}"))
        details.append(t("battery.volt_sustained",
                         seconds=f"{e.critical_duration:.1f}", timer=e.timer))
    elif e.reason == "low":
        if analyzer == "battery":
            details.append(t("battery.volt_below_warn", inst=e.instance,
                             min_volt=f"{e.min_voltage:.2f}",
                             low_volt=f"{e.limit:.2f}"))
        else:
            details.append(t("power.volt_below_warn",
                             min_volt=f"{e.min_voltage:.2f}",
                             low_volt=f"{e.limit:.2f}"))
        details.append(t("battery.volt_sustained",
                         seconds=f"{e.low_duration:.1f}", timer=e.timer))
    elif e.reason == "not_sustained":
        details.append(t("battery.volt_not_sustained",
                         seconds=f"{max(e.low_duration, e.critical_duration):.1f}",
                         timer=e.timer))
    elif e.reason == "disabled":
        details.append(t("battery.volt_disabled", param=e.parameter))
    elif e.reason == "no_armed":
        details.append(t("battery.volt_no_armed"))
    elif e.reason == "monitor_disabled":
        details.append(t("battery.volt_monitor_disabled", param=e.parameter))
    elif e.reason == "arm_volt":
        details.append(t("battery.volt_arm_invalid", param=e.parameter,
                         limit=f"{e.limit:.2f}", max_v=f"{e.max_voltage:.2f}"))
    elif e.reason == "sensor_invalid":
        details.append(t("battery.volt_sensor_invalid",
                         min_v=f"{e.limit:.2f}", max_v=f"{e.max_voltage:.2f}"))
    elif e.reason in ("config_low", "config_critical"):
        details.append(t("battery.volt_config_invalid", param=e.parameter,
                         limit=f"{e.limit:.2f}", max_v=f"{e.max_voltage:.2f}"))
    return details
