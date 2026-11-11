"""
Power / Efficiency Analyzer: energy consumption, efficiency metrics.

Computes energy consumption (Wh), power draw, and efficiency metrics
from battery current and voltage data.

Required: BAT
Optional: GPS

Title: 功耗分析
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.battery_packs import Pack, read_packs, sum_over_packs
from lib.i18n import t
from . import AnalysisResult, BaseAnalyzer


class PowerAnalyzer(BaseAnalyzer):
    """
    Power consumption and efficiency analyzer.

    Generates:
      - Power (W) time series
      - Cumulative energy (Wh) time series
      - Efficiency (Wh/km) if GPS available
      - Current draw distribution histogram
    """

    registry_key = "power"
    sort_order = 65
    required_messages = ["BAT"]
    optional_messages = ["GPS"]

    @staticmethod
    def _pack_energy(pack: Pack) -> tuple[np.ndarray, float]:
        """Cumulative energy of one pack in Wh, and its total."""
        if np.any(pack.curr_tot > 0):
            # CurrTot is in mAh, convert to Wh using average voltage
            live = pack.volt > 0
            avg_volt = float(np.nanmean(pack.volt[live])) if np.any(live) else 0.0
            curve = pack.curr_tot / 1000.0 * avg_volt
            return curve, float(np.nanmax(curve))
        # Numerical integration of power
        dt = np.diff(pack.ts, prepend=pack.ts[0])
        dt = np.clip(dt, 0, 10)  # Cap gaps at 10s
        curve = np.cumsum(pack.volt * pack.curr * dt) / 3600.0  # Watt-seconds to Wh
        return curve, float(curve[-1])

    def analyze(self) -> AnalysisResult:
        details = []
        figures = []

        # -----------------------------------------------------------
        # 1. Read BAT data
        # -----------------------------------------------------------
        # Power and energy cover every pack that measures current. The
        # voltage check stays on instance 0: BATT_LOW_VOLT and BATT_CRT_VOLT
        # belong to it.
        bat = self.reader.get_messages_as_arrays("BAT", ["Inst", "Volt"])
        ts = bat["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.power"),
                status="info",
                summary=t("power.bat_empty"),
            )

        # Filter to instance 0
        mask = np.nan_to_num(bat["Inst"], nan=-1).astype(int) == 0
        if not np.any(mask):
            mask = np.ones(len(ts), dtype=bool)

        t_main = ts[mask]
        v_main = bat["Volt"][mask]

        # Need current data
        packs = read_packs(self.reader)
        if not any(np.any(p.curr > 0) for p in packs):
            return self.make_result(
                title=t("analyzer.power"),
                status="info",
                summary=t("power.no_current"),
            )

        status = "good"

        # -----------------------------------------------------------
        # 2. Power = Voltage * Current
        # -----------------------------------------------------------
        t0 = packs[0].ts
        power = sum_over_packs(packs, [p.volt * p.curr for p in packs])  # Watts
        curr_total = sum_over_packs(packs, [p.curr for p in packs])

        max_power = float(np.nanmax(power))
        mean_power = float(np.nanmean(power[power > 0])) if np.any(power > 0) else 0.0
        details.append(t("power.max_power", val=f"{max_power:.0f}"))
        details.append(t("power.avg_power", val=f"{mean_power:.0f}"))
        if len(packs) > 1:
            details.append(t(
                "battery.packs_summed", count=len(packs),
                ids=", ".join(str(p.instance) for p in packs)))

        # -----------------------------------------------------------
        # 3. Cumulative energy (Wh)
        # -----------------------------------------------------------
        # Use CurrTot if available (mAh from FC), else integrate
        energy = [self._pack_energy(p) for p in packs]
        energy_wh = sum_over_packs(packs, [curve for curve, _ in energy])
        total_wh = sum(total for _, total in energy)

        details.append(t("power.total_energy", val=f"{total_wh:.1f}"))

        # Flight duration for efficiency
        duration_s = float(t0[-1] - t0[0]) if len(t0) > 1 else 0.0
        if duration_s > 0 and mean_power > 0:
            details.append(
                t("power.avg_power_rate", val=f"{total_wh / (duration_s / 3600):.0f}")
            )

        # -----------------------------------------------------------
        # 4. Figure: Power time series
        # -----------------------------------------------------------
        fig_power = self.make_figure(
            title=t("power.fig_power"),
            xaxis_title=t("power.time_s"),
            yaxis_title=t("power.power_w"),
            height=400,
        )
        fig_power.add_trace(go.Scattergl(
            x=t0, y=power,
            mode='lines', name=t("power.trace_power"),
            line=dict(color='#d62728', width=1),
        ))
        figures.append(fig_power)

        # -----------------------------------------------------------
        # 5. Figure: Cumulative energy
        # -----------------------------------------------------------
        fig_energy = self.make_figure(
            title=t("power.fig_cumulative_energy"),
            xaxis_title=t("power.time_s"),
            yaxis_title=t("power.energy_wh"),
            height=350,
        )
        fig_energy.add_trace(go.Scattergl(
            x=t0, y=energy_wh,
            mode='lines', name=t("power.trace_energy"),
            line=dict(color='#ff7f0e', width=1.5),
        ))
        figures.append(fig_energy)

        # -----------------------------------------------------------
        # 6. Figure: Current distribution
        # -----------------------------------------------------------
        valid_curr = curr_total[curr_total > 0]
        if len(valid_curr) > 10:
            fig_hist = self.make_figure(
                title=t("power.fig_current_dist"),
                xaxis_title=t("power.current_a"),
                yaxis_title=t("power.frequency"),
                height=300,
            )
            fig_hist.add_trace(go.Histogram(
                x=valid_curr,
                nbinsx=50,
                marker_color='#1f77b4',
                name=t("power.current"),
            ))
            figures.append(fig_hist)

        # -----------------------------------------------------------
        # 7. Efficiency (Wh/km) if GPS available
        # -----------------------------------------------------------
        armed = self.reader.get_armed_intervals()

        if self.reader.has_message("GPS"):
            gps = self.reader.get_messages_as_arrays("GPS", [
                "I", "Lat", "Lng", "Spd",
            ])
            t_gps = gps["_timestamp"]

            gps_inst = gps["I"]
            all_nan = np.all(np.isnan(gps_inst))
            if all_nan:
                gps_mask = np.ones(len(t_gps), dtype=bool)
            else:
                gps_mask = np.nan_to_num(gps_inst, nan=-1).astype(int) == 0

            if armed:
                armed_mask = np.zeros(len(t_gps), dtype=bool)
                for start, end in armed:
                    armed_mask |= (t_gps >= start) & (t_gps <= end)
                gps_mask &= armed_mask

            t_gps = t_gps[gps_mask]
            spd = gps["Spd"][gps_mask]

            if len(t_gps) > 10:
                # Calculate total distance from GPS speed integration
                dt_gps = np.diff(t_gps, prepend=t_gps[0])
                dt_gps = np.clip(dt_gps, 0, 5)
                dist_m = np.cumsum(spd * dt_gps)
                total_km = float(dist_m[-1]) / 1000.0

                if total_km > 0.1:
                    wh_per_km = total_wh / total_km
                    details.append(t("power.flight_distance", val=f"{total_km:.2f}"))
                    details.append(t("power.energy_efficiency", val=f"{wh_per_km:.1f}"))

                    # Efficiency figure
                    # Interpolate energy to GPS timestamps
                    energy_at_gps = np.interp(t_gps, t0, energy_wh)
                    eff_cumulative = np.full_like(dist_m, np.nan)
                    far_mask = dist_m > 100
                    eff_cumulative[far_mask] = (
                        energy_at_gps[far_mask] / (dist_m[far_mask] / 1000.0)
                    )

                    fig_eff = self.make_figure(
                        title=t("power.fig_efficiency"),
                        xaxis_title=t("power.time_s"),
                        yaxis_title=t("power.fig_efficiency_yaxis"),
                        height=350,
                    )
                    fig_eff.add_trace(go.Scattergl(
                        x=t_gps, y=eff_cumulative,
                        mode='lines', name=t("power.cumulative_efficiency_trace"),
                        line=dict(color='#2ca02c', width=1.5),
                    ))
                    figures.append(fig_eff)

        # -----------------------------------------------------------
        # 8. Voltage safety check (cross-reference with battery thresholds)
        # -----------------------------------------------------------
        # Power analyzer also flags dangerously low voltage during flight.
        low_volt = self.reader.get_param(
            "BATT_LOW_VOLT", self.thresholds.batt_low_volt
        )
        crt_volt = self.reader.get_param(
            "BATT_CRT_VOLT", self.thresholds.batt_crt_volt
        )

        if armed:
            armed_mask = np.zeros(len(t_main), dtype=bool)
            for start, end in armed:
                armed_mask |= (t_main >= start) & (t_main <= end)
            if np.sum(armed_mask) > 10:
                min_volt_armed = float(np.nanmin(v_main[armed_mask]))
            else:
                min_volt_armed = float(np.nanmin(v_main))
        else:
            min_volt_armed = float(np.nanmin(v_main))

        if crt_volt > 0 and min_volt_armed < crt_volt:
            status = self.worst_status(status, "critical")
            details.append(
                t("power.volt_below_crit",
                  min_volt=f"{min_volt_armed:.2f}",
                  crt_volt=f"{crt_volt:.1f}")
            )
        elif low_volt > 0 and min_volt_armed < low_volt:
            status = self.worst_status(status, "warning")
            details.append(
                t("power.volt_below_warn",
                  min_volt=f"{min_volt_armed:.2f}",
                  low_volt=f"{low_volt:.1f}")
            )

        # -----------------------------------------------------------
        # 9. Summary + actionable advice
        # -----------------------------------------------------------
        summary = t("power.summary", total_wh=f"{total_wh:.1f}", mean_power=f"{mean_power:.0f}")

        suggestions = []
        if status == "critical":
            action_level = "safety_stop"
            plain_summary = t("power.plain_critical")
            action_text = t(
                "power.action_critical",
                min_volt=f"{min_volt_armed:.2f}",
                crt_volt=f"{crt_volt:.1f}",
            )
        elif status == "warning":
            # No auto-suggestion here: the old 0.92 × peak-voltage
            # BATT_LOW_VOLT recommendation had no authoritative source
            # (same rationale as its removal from failsafe in 0.4.0).
            action_level = "physical_check"
            plain_summary = t("power.plain_warning")
            low_volt_advice = t("power.check_low_volt",
                                val=f"{min_volt_armed:.2f}")
            action_text = t(
                "power.action_warning",
                min_volt=f"{min_volt_armed:.2f}",
                low_volt=f"{low_volt:.1f}",
                low_volt_advice=low_volt_advice,
            )
        else:
            action_level = "ok"
            plain_summary = t("power.plain_good")
            action_text = ""

        evidence_source = ""
        if status in ("warning", "critical"):
            evidence_source = t("power.evidence")

        return self.make_result(
            title=t("analyzer.power"),
            status=status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            action_text=action_text,
            plain_summary=plain_summary,
            param_suggestions=suggestions,
            evidence_source=evidence_source,
        )
