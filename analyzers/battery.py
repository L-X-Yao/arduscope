"""
Battery Analyzer: voltage, current, and remaining capacity monitoring.

Evaluates battery health by comparing voltage against BATT_LOW_VOLT and
BATT_CRT_VOLT thresholds. Supports multiple battery instances (up to 4
for multi-battery setups).

Required: BAT
Optional: (none)

Title: 电池状态
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


class BatteryAnalyzer(BaseAnalyzer):
    """
    Battery state analyzer.

    Generates:
      - Voltage time series per instance with BATT_LOW_VOLT/BATT_CRT_VOLT lines
      - Current time series (if current monitoring available)
      - Remaining percentage time series
    """

    registry_key = "battery"
    sort_order = 60
    required_messages = ["BAT"]
    optional_messages: list[str] = []

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read BAT data with instance field
        # -----------------------------------------------------------
        bat = self.reader.get_messages_as_arrays("BAT", [
            "Inst", "Volt", "Curr", "CurrTot", "Rem",
        ])
        ts = bat["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.battery"),
                status="info",
                summary=t("battery.bat_empty"),
            )

        inst = bat["Inst"]
        volt = bat["Volt"]
        curr = bat["Curr"]
        curr_tot = bat["CurrTot"]
        rem = bat["Rem"]

        # -----------------------------------------------------------
        # 2. Separate by battery instance
        # -----------------------------------------------------------
        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )
        if not unique_instances:
            unique_instances = [0]

        details.append(t("battery.instance_count", count=len(unique_instances)))

        # -----------------------------------------------------------
        # 3. Get threshold parameters (per instance)
        # -----------------------------------------------------------
        # ArduPilot uses BATTn_ prefix for instance n (BATT_ for first)
        def get_batt_param(param_suffix: str, instance: int,
                           default: float) -> float:
            """Get battery parameter for a specific instance."""
            if instance == 0:
                return self.reader.get_param(
                    f"BATT_{param_suffix}", default
                )
            else:
                return self.reader.get_param(
                    f"BATT{instance + 1}_{param_suffix}",
                    self.reader.get_param(f"BATT_{param_suffix}", default),
                )

        # -----------------------------------------------------------
        # 4. Analyze each battery instance
        # -----------------------------------------------------------
        status = "good"
        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']

        # Voltage figure
        fig_volt = self.make_figure(
            title=t("battery.fig_voltage"),
            xaxis_title=t("battery.time_s"),
            yaxis_title=t("battery.voltage_v"),
            height=400,
        )

        # Current figure (only if current data is meaningful)
        has_current = False
        fig_curr = self.make_figure(
            title=t("battery.fig_current"),
            xaxis_title=t("battery.time_s"),
            yaxis_title=t("battery.current_a"),
            height=350,
        )

        # Remaining figure
        has_remaining = False
        fig_rem = self.make_figure(
            title=t("battery.fig_remaining"),
            xaxis_title=t("battery.time_s"),
            yaxis_title=t("battery.remaining_pct"),
            height=300,
        )

        armed = self.reader.get_armed_intervals()

        for inst_id in unique_instances:
            # No Inst column at all means a single-battery log.
            if np.all(np.isnan(inst)):
                mask = np.ones(len(ts), dtype=bool)
            else:
                mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            t_inst = ts[mask]
            v_inst = volt[mask]
            c_inst = curr[mask]
            r_inst = rem[mask]

            color = instance_colors[inst_id % len(instance_colors)]
            label = (
                t("battery.inst_label", id=inst_id)
                if len(unique_instances) > 1
                else t("battery.voltage")
            )

            # Voltage statistics — use armed intervals for min_volt
            # so ground-idle voltage doesn't skew status decisions.
            # Guard against all-NaN arrays: nanmin/nanmax return NaN,
            # and NaN comparisons silently evaluate to False.
            if np.all(np.isnan(v_inst)):
                details.append(t("battery.volt_all_nan", inst=inst_id))
                continue
            if armed:
                armed_mask = np.zeros(len(t_inst), dtype=bool)
                for start, end in armed:
                    armed_mask |= (t_inst >= start) & (t_inst <= end)
                if np.sum(armed_mask) > 10:
                    v_armed = v_inst[armed_mask]
                    if np.all(np.isnan(v_armed)):
                        min_volt = float(np.nanmin(v_inst))
                    else:
                        min_volt = float(np.nanmin(v_armed))
                else:
                    min_volt = float(np.nanmin(v_inst))
            else:
                min_volt = float(np.nanmin(v_inst))
            max_volt = float(np.nanmax(v_inst))
            avg_volt = float(np.nanmean(v_inst))

            details.append(
                t("battery.volt_range",
                  inst=inst_id,
                  min_v=f"{min_volt:.2f}",
                  max_v=f"{max_volt:.2f}",
                  avg_v=f"{avg_volt:.2f}")
            )

            # Get thresholds for this instance
            low_volt = get_batt_param("LOW_VOLT", inst_id,
                                      self.thresholds.batt_low_volt)
            crt_volt = get_batt_param("CRT_VOLT", inst_id,
                                      self.thresholds.batt_crt_volt)
            low_mah = get_batt_param("LOW_MAH", inst_id,
                                     self.thresholds.batt_low_mah)
            crt_mah = get_batt_param("CRT_MAH", inst_id,
                                     self.thresholds.batt_crt_mah)
            capacity = get_batt_param("CAPACITY", inst_id, 0.0)

            # Check if BATT_LOW_VOLT seems misconfigured (too high
            # relative to max observed voltage).
            if max_volt > 0 and low_volt > max_volt * 0.95:
                details.append(t("battery.low_volt_too_high_reason",
                                 low_volt=f"{low_volt:.1f}",
                                 max_volt=f"{max_volt:.1f}"))

            # Status check — voltage
            if crt_volt > 0 and min_volt < crt_volt:
                status = self.worst_status(status, "critical")
                details.append(
                    t("battery.volt_below_crit",
                      inst=inst_id,
                      min_volt=f"{min_volt:.2f}",
                      crt_volt=f"{crt_volt:.2f}")
                )
            elif low_volt > 0 and min_volt < low_volt:
                status = self.worst_status(status, "warning")
                details.append(
                    t("battery.volt_below_warn",
                      inst=inst_id,
                      min_volt=f"{min_volt:.2f}",
                      low_volt=f"{low_volt:.2f}")
                )

            # Status check — consumed mAh vs failsafe thresholds.
            # ArduPilot triggers low/crt failsafe when remaining capacity
            # (= BATT_CAPACITY - CurrTot) drops below LOW_MAH/CRT_MAH.
            # 0 means failsafe disabled, skip the check.
            ct_inst = curr_tot[mask]
            ct_valid = ct_inst[~np.isnan(ct_inst)]
            if (capacity > 0 and len(ct_valid) > 0):
                max_consumed = float(np.nanmax(ct_valid))
                remaining = capacity - max_consumed
                details.append(
                    t("battery.consumed_capacity",
                      inst=inst_id,
                      consumed=f"{max_consumed:.0f}",
                      capacity=f"{capacity:.0f}",
                      remaining=f"{remaining:.0f}")
                )
                if crt_mah > 0 and remaining < crt_mah:
                    status = self.worst_status(status, "critical")
                    details.append(
                        t("battery.cap_below_crit",
                          inst=inst_id,
                          remaining=f"{remaining:.0f}",
                          crt_mah=f"{crt_mah:.0f}")
                    )
                elif low_mah > 0 and remaining < low_mah:
                    status = self.worst_status(status, "warning")
                    details.append(
                        t("battery.cap_below_warn",
                          inst=inst_id,
                          remaining=f"{remaining:.0f}",
                          low_mah=f"{low_mah:.0f}")
                    )

            # Charge-backfill calibration aid — analog monitors
            # (BATT_MONITOR=4) integrate CurrTot through AMP_PERVLT,
            # so the charger's refill reading after a full recharge
            # verifies the coefficient end-to-end. Info only, never
            # drives status. Factory-calibrated monitors (SMBus,
            # DroneCAN) don't need it and are skipped.
            monitor = get_batt_param("MONITOR", inst_id, 0.0)
            amp_pervlt = get_batt_param("AMP_PERVLT", inst_id, 0.0)
            if int(monitor) == 4 and amp_pervlt > 0 and len(ct_valid) > 0:
                consumed = float(np.nanmax(ct_valid))
                if consumed >= self.thresholds.backfill_min_consumed_mah:
                    pervlt_param = (
                        "BATT_AMP_PERVLT" if inst_id == 0
                        else f"BATT{inst_id + 1}_AMP_PERVLT"
                    )
                    details.append(
                        t("battery.backfill_hint",
                          inst=inst_id,
                          consumed=f"{consumed:.0f}",
                          param=pervlt_param,
                          pervlt=f"{amp_pervlt:g}")
                    )

            # Voltage trace
            fig_volt.add_trace(go.Scattergl(
                x=t_inst, y=v_inst,
                mode='lines', name=label,
                line=dict(color=color, width=1),
            ))

            if low_volt > 0:
                self.add_threshold_hline(
                    fig_volt, low_volt,
                    t("battery.low_volt_line", val=f"{low_volt:.1f}"),
                    color="#f0ad4e",
                )
            if crt_volt > 0:
                self.add_threshold_hline(
                    fig_volt, crt_volt,
                    t("battery.crt_volt_line", val=f"{crt_volt:.1f}"),
                    color="#d9534f",
                )

            # Current trace
            if np.any(c_inst > 0):
                has_current = True
                avg_curr = float(np.nanmean(c_inst[c_inst > 0]))
                max_curr = float(np.nanmax(c_inst))
                details.append(
                    t("battery.current_stats",
                      inst=inst_id,
                      avg_curr=f"{avg_curr:.1f}",
                      max_curr=f"{max_curr:.1f}")
                )

                c_label = (
                    t("battery.inst_label", id=inst_id)
                    if len(unique_instances) > 1
                    else t("battery.current")
                )
                fig_curr.add_trace(go.Scattergl(
                    x=t_inst, y=c_inst,
                    mode='lines', name=c_label,
                    line=dict(color=color, width=1),
                ))

            # Remaining trace
            valid_rem = r_inst[~np.isnan(r_inst)]
            if len(valid_rem) > 0 and np.any(valid_rem > 0):
                has_remaining = True
                min_rem = float(np.nanmin(r_inst))
                details.append(t("battery.min_remaining", inst=inst_id, val=f"{min_rem:.1f}"))

                r_label = (
                    t("battery.inst_label", id=inst_id)
                    if len(unique_instances) > 1
                    else t("battery.remaining")
                )
                fig_rem.add_trace(go.Scattergl(
                    x=t_inst, y=r_inst,
                    mode='lines', name=r_label,
                    line=dict(color=color, width=1),
                ))

        figures.append(fig_volt)
        if has_current:
            figures.append(fig_curr)
        if has_remaining:
            figures.append(fig_rem)

        # -----------------------------------------------------------
        # 5. Summary + action level
        # -----------------------------------------------------------
        # critical → safety_stop (real risk of mid-air failure), overrides
        #            apply_param even if param_suggestions is non-empty.
        # warning + param suggestions → apply_param (fix threshold first)
        # warning only → physical_check (charge/replace)
        # good + param suggestions → apply_param (misconfigured threshold)
        if status == "critical":
            summary = t("battery.summary_critical")
            plain_summary = t("battery.plain_critical")
            action_text = t("battery.action_critical")
            action_level = "safety_stop"
        elif status == "warning":
            summary = t("battery.summary_warning")
            plain_summary = t("battery.plain_warning")
            action_text = t("battery.action_warning")
            action_level = "apply_param" if param_suggestions else "physical_check"
        else:
            summary = t("battery.summary_good", count=len(unique_instances))
            plain_summary = t("battery.plain_good")
            action_text = ""
            action_level = "apply_param" if param_suggestions else "ok"

        # Evidence source — battery thresholds from FC config and chemistry
        if status == "critical":
            evidence_source = t("battery.evidence_critical")
        elif status == "warning":
            evidence_source = t("battery.evidence_warning")
        else:
            evidence_source = ""

        return self.make_result(
            title=t("analyzer.battery"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            evidence_source=evidence_source,
            param_suggestions=param_suggestions,
        )
