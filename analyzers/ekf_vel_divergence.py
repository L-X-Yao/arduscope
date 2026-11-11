"""
EKF Velocity Divergence Analyzer: finds windows where an EKF core's 3D
velocity runs away from the GPS doppler measurement.

Under an EKF variance event the velocity states can run to 2x the GPS
speed while the GPS itself stays healthy — the fleet's recurring sprint
"GPS 眩晕" signature (five H1 cases by 2026-07-20; 16-40-11: EKF 88.2
m/s vs GPS 43.3 at the same instant, both cores diverged). On the
compassless H1 (GSF yaw) the driver is extreme maneuvers + full-power
vibration degrading gyro/accel integration while GSF yaw observability
is weakest in vertical profiles; on compass-equipped frames mag
distortion can produce the same signature. GPS-vs-EKF disagreement is
checked per core over the armed window and windows >=1 s are reported,
but only the lane that was primary at the time is graded (XKF4.PI): a
standby core is not flying the aircraft, so it gets a line, not a verdict.

Only samples where the comparison means something are graded: the GPS
must hold a 3D fix (without one it reports Spd 0 while the EKF dead
reckons, which reads as a 100% window), the firmware must not be
reporting the vehicle still on the ground, and an EKF sample must have a
GPS sample within a second (np.interp otherwise bridges dropouts and
gated-out stretches). Traced from a 199-log corpus backtest, 2026-07-24.

Required: GPS (EKF velocity messages checked at runtime)

Title: EKF 速度发散
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.ekf_lanes import primary_core_at
from lib.gps_velocity import vz_measured
from lib.i18n import t as _t
from . import AnalysisResult, BaseAnalyzer

# Same tolerance family as the flight-profile 3D-speed gate, with a
# higher absolute floor: a divergence *finding* should mean several m/s,
# not hover jitter.
REL_TOL = 0.15
ABS_TOL = 2.0
MIN_WINDOW_S = 1.0
MIN_PEAK = 5.0           # ignore windows whose faster source stays below
                         # this (ground/touchdown jitter reads as 100% rel)
CRIT_REL = 0.40          # any window peaking ≥40% off GPS
CRIT_TOTAL_S = 5.0       # or ≥5 s of cumulative divergence
MAX_LISTED = 6
MIN_SAMPLES = 10         # finite samples a source needs to be gradeable
GPS_FIX_3D = 3           # A: AP_GPS GPS_OK_FIX_3D — doppler velocity is
                         # only meaningful from a 3D fix; below it the
                         # receiver reports Spd 0
MAX_GPS_GAP_S = 1.0      # D: past a second the nearest GPS sample no
                         # longer describes the EKF sample


class EKFVelDivergenceAnalyzer(BaseAnalyzer):
    """EKF-vs-GPS 3D-speed divergence windows, graded on the primary lane."""

    registry_key = "ekf_vel_divergence"
    sort_order = 73
    required_messages = ["GPS"]

    def analyze(self) -> AnalysisResult:
        gps = self.reader.get_messages_as_arrays(
            "GPS", ["I", "Spd", "VZ", "Status", "NSats"])
        ts = gps["_timestamp"]
        if len(ts) == 0:
            return self._info("ekf_vel_div.no_gps")
        # get_armed_mask is all-True when no window is known, which would
        # grade a parked frame's GPS multipath as EKF divergence.
        if self.reader.never_armed():
            return self._info("ekf_vel_div.never_armed")
        inst = gps["I"]
        gmask = np.ones(len(ts), dtype=bool)
        if not np.all(np.isnan(inst)):
            primary = int(np.nanmin(inst))
            gmask = np.nan_to_num(inst, nan=-1).astype(int) == primary
        gmask &= self.get_armed_mask(ts)
        finite = ~np.isnan(gps["Spd"])
        if np.count_nonzero(gmask & finite) < MIN_SAMPLES:
            return self._info("ekf_vel_div.no_gps")

        # Below a 3D fix the receiver reports Spd 0 while the EKF keeps
        # dead reckoning: a full-scale "divergence" that is the GPS
        # dropping out, not the EKF running away. NSats corroborates —
        # log_43 of the corpus holds Status 4 through a stretch where the
        # satellite count has already fallen to 0, so the status field
        # alone still lets the dropout through.
        status, nsats = gps["Status"], gps["NSats"]
        if not np.all(np.isnan(status)):
            gmask &= np.nan_to_num(status, nan=0.0) >= GPS_FIX_3D
        if not np.all(np.isnan(nsats)):
            gmask &= np.nan_to_num(nsats, nan=0.0) >= self.thresholds.gps_nsats_crit
        if np.count_nonzero(gmask & finite) < MIN_SAMPLES:
            return self._info("ekf_vel_div.no_fix")

        # Armed on the ground — and get_armed_mask is all-True when the
        # log has no ARM event at all — leaves the EKF drifting m/s while
        # the GPS sits at zero. Real drift, but not a flight finding.
        gmask &= self._flying_mask(ts)
        if np.count_nonzero(gmask & finite) < MIN_SAMPLES:
            return self._info("ekf_vel_div.no_airborne")

        # A receiver with no vertical velocity logs VZ 0, and a 3D EKF speed
        # against that reads every climb as divergence. Rows without VZ
        # cannot be graded in 3D, and a log with none is graded horizontally.
        measured = vz_measured(self.reader, ts, inst)
        vertical = np.count_nonzero(gmask & finite & measured) >= MIN_SAMPLES
        if vertical:
            gmask &= measured
        vz = (np.nan_to_num(gps["VZ"][gmask], nan=0.0) if vertical
              else np.zeros(np.count_nonzero(gmask)))
        gps3d = np.sqrt(np.nan_to_num(gps["Spd"][gmask], nan=np.nan) ** 2
                        + vz ** 2)
        g_ts = ts[gmask]
        fin = ~np.isnan(gps3d)
        g_ts, gps3d = g_ts[fin], gps3d[fin]

        cores = self._ekf_cores(vertical)
        if not cores:
            return self._info("ekf_vel_div.no_ekf")

        windows = []      # (t0, t1, core, peak_ekf, gps_at_peak, rel_pct)
        standby = []      # same tuples, from a lane not primary at the time
        figures: list[go.Figure] = []
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=g_ts, y=gps3d, mode="lines",
                                 name=_t("ekf_vel_div.trace_gps")))
        graded = 0
        for core, (c_ts, c_v3d, c_lead) in sorted(cores.items()):
            keep = self._gps_covered(c_ts, g_ts)
            c_ts, c_v3d, c_lead = c_ts[keep], c_v3d[keep], c_lead[keep]
            if len(c_ts) < MIN_SAMPLES:
                continue
            graded += 1
            fig.add_trace(go.Scatter(
                x=c_ts, y=c_v3d, mode="lines",
                name=_t("ekf_vel_div.trace_core", core=core)))
            gps_i = np.interp(c_ts, g_ts, gps3d)
            diff = np.abs(c_v3d - gps_i)
            div = (diff > ABS_TOL) & (diff > REL_TOL * np.maximum(c_v3d, gps_i))
            windows.extend(self._windows(c_ts, c_v3d, gps_i, diff,
                                         div & c_lead, core))
            standby.extend(self._windows(c_ts, c_v3d, gps_i, diff,
                                         div & ~c_lead, core))
        if not graded:
            return self._info("ekf_vel_div.no_overlap")
        fig.update_layout(
            title=_t("ekf_vel_div.fig_title"),
            xaxis_title=_t("ekf_vel_div.time_s"),
            yaxis_title=_t("ekf_vel_div.fig_y"))
        figures.append(fig)

        standby.sort(key=lambda w: -w[5])
        notes = self._window_lines(standby, "ekf_vel_div.standby_window")
        if not vertical:
            notes.append(_t("ekf_vel_div.horizontal_only"))
        if not windows:
            summary = (_t("ekf_vel_div.clean_standby", n=len(standby))
                       if standby else _t("ekf_vel_div.clean"))
            return self.make_result(
                title=_t("analyzer.ekf_vel_divergence"), status="good",
                summary=summary, details=notes, figures=figures,
                action_level="ok")

        windows.sort(key=lambda w: -w[5])
        total_s = self._covered_seconds(windows)
        peak = windows[0]
        details = self._window_lines(windows, "ekf_vel_div.window") + notes
        critical = peak[5] >= CRIT_REL * 100 or total_s >= CRIT_TOTAL_S
        return self.make_result(
            title=_t("analyzer.ekf_vel_divergence"),
            status="critical" if critical else "warning",
            summary=_t("ekf_vel_div.summary",
                       n=len(windows), total=f"{total_s:.1f}",
                       peak=f"{peak[3]:.1f}", pct=f"{peak[5]:.0f}"),
            details=details, figures=figures,
            action_level="physical_check",
            evidence_source=_t("ekf_vel_div.evidence"))

    # ------------------------------------------------------------------

    @staticmethod
    def _window_lines(windows, key: str) -> list[str]:
        """Detail lines for the worst windows, sorted by the caller."""
        lines = [
            _t(key, core=w[2], t0=f"{w[0]:.1f}", t1=f"{w[1]:.1f}",
               ekf=f"{w[3]:.1f}", gps=f"{w[4]:.1f}", pct=f"{w[5]:.0f}")
            for w in windows[:MAX_LISTED]
        ]
        if len(windows) > MAX_LISTED:
            lines.append(_t("ekf_vel_div.more", n=len(windows) - MAX_LISTED))
        return lines

    def _info(self, key: str, **kwargs) -> AnalysisResult:
        """Informational result naming why nothing was gradeable."""
        return self.make_result(
            title=_t("analyzer.ekf_vel_divergence"), status="info",
            summary=_t(key, **kwargs))

    def _flying_mask(self, ts):
        """True where the firmware itself reports the vehicle flying.

        STAT.isFlying is ArduPilot's own flight judgment — the same gate
        pid_tuning uses. No STAT → no gate: a relative-altitude proxy was
        tried on the 2026-07-24 corpus and disqualified 84 logs, low
        hovers included, because a "clearly flying" floor is not a
        "definitely on the ground" test.
        """
        if not self.reader.has_message("STAT"):
            return np.ones(len(ts), dtype=bool)
        stat = self.reader.get_messages_as_arrays("STAT", ["isFlying"])
        s_ts, flying = stat["_timestamp"], stat["isFlying"]
        good = np.isfinite(s_ts) & np.isfinite(flying)
        if np.count_nonzero(good) < 2:
            return np.ones(len(ts), dtype=bool)
        return np.interp(ts, s_ts[good], flying[good]) > 0.5

    @staticmethod
    def _gps_covered(c_ts, g_ts):
        """True where a surviving GPS sample sits within MAX_GPS_GAP_S.

        np.interp clamps past its range and draws a straight line across
        gaps, so without this an EKF sample inside a GPS dropout — or
        inside a stretch the fix/airborne gates removed — still gets
        compared against a GPS value from seconds away.
        """
        idx = np.searchsorted(g_ts, c_ts)
        lo = np.where(idx > 0, g_ts[np.maximum(idx - 1, 0)], -np.inf)
        hi = np.where(idx < len(g_ts),
                      g_ts[np.minimum(idx, len(g_ts) - 1)], np.inf)
        return np.minimum(c_ts - lo, hi - c_ts) <= MAX_GPS_GAP_S

    def _ekf_cores(self, vertical: bool):
        """{core: (ts, speed, is_primary)} over the armed window, finite
        samples only. Speed is 3D, or horizontal when `vertical` is false.
        Every core counts as primary on a log that cannot say which was."""
        for msg, kf4 in (("XKF1", "XKF4"), ("NKF1", "NKF4")):
            if not self.reader.has_message(msg):
                continue
            data = self.reader.get_messages_as_arrays(
                msg, ["C", "VN", "VE", "VD"])
            ts = data["_timestamp"]
            if len(ts) == 0:
                continue
            mask = self.get_armed_mask(ts)
            core = data.get("C")
            core = (np.nan_to_num(core, nan=0).astype(int)
                    if core is not None and not np.all(np.isnan(core))
                    else np.zeros(len(ts), dtype=int))
            v3d = np.sqrt(data["VN"] ** 2 + data["VE"] ** 2
                          + (data["VD"] ** 2 if vertical else 0.0))
            primary = primary_core_at(self.reader, kf4, ts)
            out = {}
            for c in np.unique(core[mask]):
                m = mask & (core == c)
                fin = ~np.isnan(v3d[m])
                if np.count_nonzero(fin) >= MIN_SAMPLES:
                    lead = (primary[m][fin] == c if primary is not None
                            else np.ones(np.count_nonzero(fin), dtype=bool))
                    out[int(c)] = (ts[m][fin], v3d[m][fin], lead)
            if out:
                return out
        return {}

    @staticmethod
    def _windows(c_ts, c_v3d, gps_i, diff, div, core):
        """Contiguous divergent runs ≥ MIN_WINDOW_S as report tuples.

        Adjacent indices are only continuous in time while the step stays
        under MAX_GPS_GAP_S: the coverage filter leaves holes in c_ts, and
        without the step check two brief windows either side of a hole
        merge into one long false window.
        """
        out = []
        i = 0
        n = len(div)
        while i < n:
            if not div[i]:
                i += 1
                continue
            j = i
            while (j + 1 < n and div[j + 1]
                   and c_ts[j + 1] - c_ts[j] <= MAX_GPS_GAP_S):
                j += 1
            t0, t1 = float(c_ts[i]), float(c_ts[j])
            if t1 - t0 >= MIN_WINDOW_S:
                k = i + int(np.argmax(diff[i:j + 1]))
                base = max(float(c_v3d[k]), float(gps_i[k]), 1e-6)
                if base >= MIN_PEAK:
                    # Off GPS, the reference. Below ABS_TOL a GPS speed is
                    # jitter around zero and would only blow the ratio up.
                    off_gps = float(diff[k]) / max(float(gps_i[k]), ABS_TOL)
                    out.append((t0, t1, core, float(c_v3d[k]),
                                float(gps_i[k]), off_gps * 100.0))
            i = j + 1
        return out

    @staticmethod
    def _covered_seconds(windows) -> float:
        """Seconds with any divergent core. A log with no PI column grades
        every lane, and lanes diverging together are one event, not two."""
        total, reach = 0.0, -np.inf
        for t0, t1 in sorted((w[0], w[1]) for w in windows):
            total += max(t1 - max(t0, reach), 0.0)
            reach = max(reach, t1)
        return total
