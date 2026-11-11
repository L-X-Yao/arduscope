"""
Compass Consistency Analyzer: multi-compass field magnitude comparison.

Compares magnetic field readings between multiple compass instances by
computing RMS difference of field magnitudes. Detects inter-compass
disagreement that may indicate interference, calibration error, or
hardware fault.

Required: MAG
Optional: (none)

Title: Compass Consistency
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.i18n import t as _t

from . import AnalysisResult, BaseAnalyzer


# MAG instance number to its use flag (AP_Compass)
_USE_PARAMS = {0: "COMPASS_USE", 1: "COMPASS_USE2", 2: "COMPASS_USE3"}


class CompassConsistencyAnalyzer(BaseAnalyzer):
    """
    Compass consistency analyzer.

    Compares field magnitude (sqrt(X^2+Y^2+Z^2)) across compass instances.
    High RMS difference indicates disagreement between compasses.

    Generates:
      - Field magnitude per instance over time
    """

    registry_key = "compass_consistency"
    sort_order = 82
    required_messages = ["MAG"]
    optional_messages: list[str] = []

    @staticmethod
    def _outlier(ids: list[int],
                 critical_pairs: list[tuple[int, int]]) -> int | None:
        """The one compass that disagrees with every other, if there is one.

        Two compasses that disagree say nothing about which is wrong. With
        three or more, the outlier is the instance found in every critical
        pair while the other compasses agree among themselves.
        """
        if len(ids) < 3:
            return None
        suspects = [i for i in ids
                    if all(i in pair for pair in critical_pairs)]
        if len(suspects) != 1:
            return None
        others = [i for i in ids if i != suspects[0]]
        if len(critical_pairs) != len(others):
            return None
        return suspects[0]

    def analyze(self) -> AnalysisResult:
        details: list[str] = []
        figures: list[go.Figure] = []
        param_suggestions: list[dict] = []

        # -----------------------------------------------------------
        # 1. Read MAG data
        # -----------------------------------------------------------
        mag = self.reader.get_messages_as_arrays("MAG", [
            "I", "MagX", "MagY", "MagZ", "OfsX", "OfsY", "OfsZ",
        ])
        ts = mag["_timestamp"]

        if len(ts) == 0:
            return self.make_result(
                title=_t("analyzer.compass_consistency"),
                status="info",
                summary=_t("compass_consistency.empty"),
            )

        inst = mag["I"]

        # -----------------------------------------------------------
        # 2. Separate by compass instance
        # -----------------------------------------------------------
        unique_instances = sorted(
            set(int(i) for i in inst if not np.isnan(i))
        )

        if len(unique_instances) < 2:
            return self.make_result(
                title=_t("analyzer.compass_consistency"),
                status="info",
                summary=_t("compass_consistency.single_compass",
                            n=len(unique_instances)),
            )

        details.append(_t("compass_consistency.instance_count",
                           n=len(unique_instances)))

        # -----------------------------------------------------------
        # 3. Compute field magnitude per instance
        # -----------------------------------------------------------
        instance_colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
        instance_data: dict[int, tuple[np.ndarray, np.ndarray]] = {}

        fig = self.make_figure(
            title=_t("compass_consistency.fig_field_mag"),
            xaxis_title=_t("compass_consistency.time_s"),
            yaxis_title=_t("compass_consistency.field_mgauss"),
            height=400,
        )

        for inst_id in unique_instances:
            mask = np.nan_to_num(inst, nan=-1).astype(int) == inst_id
            if not np.any(mask):
                continue

            t_inst = ts[mask]
            # Subtract hard-iron offsets; treat NaN offsets as 0 so that
            # logs without compass calibration data still produce valid
            # field magnitudes (raw field used rather than crashing).
            ofs_x = np.nan_to_num(mag["OfsX"][mask], nan=0.0)
            ofs_y = np.nan_to_num(mag["OfsY"][mask], nan=0.0)
            ofs_z = np.nan_to_num(mag["OfsZ"][mask], nan=0.0)
            mx = mag["MagX"][mask] - ofs_x
            my = mag["MagY"][mask] - ofs_y
            mz = mag["MagZ"][mask] - ofs_z

            field_mag = np.sqrt(mx**2 + my**2 + mz**2)
            instance_data[inst_id] = (t_inst, field_mag)

            color = instance_colors[inst_id % len(instance_colors)]
            step = max(1, len(t_inst) // 5000)

            fig.add_trace(go.Scattergl(
                x=t_inst[::step], y=field_mag[::step],
                mode='lines', name=f"MAG{inst_id}",
                line=dict(color=color, width=1),
            ))

        figures.append(fig)

        # -----------------------------------------------------------
        # 4. Pair-wise RMS difference of field magnitudes
        # -----------------------------------------------------------
        overall_status = "good"
        critical_pairs: list[tuple[int, int]] = []

        ids = sorted(instance_data.keys())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                id_a, id_b = ids[i], ids[j]
                t_a, mag_a = instance_data[id_a]
                t_b, mag_b = instance_data[id_b]

                # Interpolate mag_b onto t_a timestamps
                good_b = np.isfinite(t_b) & np.isfinite(mag_b)
                if np.sum(good_b) < 2:
                    continue
                mag_b_interp = np.interp(t_a, t_b[good_b], mag_b[good_b])

                diff = mag_a - mag_b_interp
                valid = np.isfinite(diff)
                if np.sum(valid) == 0:
                    continue
                # Armed window only: ground handling is not flight, and
                # long bench time dilutes a real in-flight disagreement.
                in_flight = valid & self.get_armed_mask(t_a)
                if np.any(in_flight):
                    valid = in_flight

                rms = float(np.sqrt(np.nanmean(diff[valid] ** 2)))
                details.append(
                    _t("compass_consistency.pair_rms",
                        id0=id_a, id1=id_b, rms=f"{rms:.1f}")
                )

                # Threshold check
                if rms > self.thresholds.compass_consist_crit:
                    critical_pairs.append((id_a, id_b))
                    overall_status = self.worst_status(
                        overall_status, "critical")
                    details.append(
                        _t("compass_consistency.rms_crit",
                            id0=id_a, id1=id_b,
                            rms=f"{rms:.1f}",
                            thresh=self.thresholds.compass_consist_crit)
                    )
                elif rms > self.thresholds.compass_consist_warn:
                    overall_status = self.worst_status(
                        overall_status, "warning")
                    details.append(
                        _t("compass_consistency.rms_warn",
                            id0=id_a, id1=id_b,
                            rms=f"{rms:.1f}",
                            thresh=self.thresholds.compass_consist_warn)
                    )

        # Threshold lines omitted from magnitude figure — the warn/crit
        # thresholds are for inter-compass RMS DIFFERENCE, not for
        # absolute field magnitude. Drawing them on this chart would be
        # visually misleading (e.g. 100 mGauss threshold on a 400 mGauss
        # scale). The thresholds appear in the details text instead.

        # -----------------------------------------------------------
        # 5. Param suggestions
        # -----------------------------------------------------------
        if overall_status == "critical":
            outlier = self._outlier(ids, critical_pairs)
            if outlier is None:
                details.append(_t("compass_consistency.outlier_unknown"))
            else:
                param = _USE_PARAMS.get(outlier)
                if param and self.reader.get_param(param, 1) == 1:
                    param_suggestions.append({
                        "param": param,
                        "current": 1,
                        "suggested": 0,
                        "reason": _t("compass_consistency.disable_reason",
                                     id=outlier),
                    })

        # -----------------------------------------------------------
        # 6. Summary
        # -----------------------------------------------------------
        if overall_status == "critical":
            summary = _t("compass_consistency.summary_critical")
            action_level = ("apply_param" if param_suggestions
                            else "physical_check")
            plain_summary = _t("compass_consistency.critical_plain")
            action_text = _t("compass_consistency.critical_action")
        elif overall_status == "warning":
            summary = _t("compass_consistency.summary_warn")
            action_level = "physical_check"
            plain_summary = _t("compass_consistency.warn_plain")
            action_text = _t("compass_consistency.warn_action")
        else:
            summary = _t("compass_consistency.summary_good",
                          n=len(unique_instances))
            action_level = "ok"
            plain_summary = ""
            action_text = ""

        # Evidence source
        if overall_status in ("warning", "critical"):
            evidence_source = _t("compass_consistency.evidence")
        else:
            evidence_source = ""

        return self.make_result(
            title=_t("analyzer.compass_consistency"),
            status=overall_status,
            summary=summary,
            details=details,
            figures=figures,
            action_level=action_level,
            plain_summary=plain_summary,
            action_text=action_text,
            evidence_source=evidence_source,
            param_suggestions=param_suggestions,
        )
