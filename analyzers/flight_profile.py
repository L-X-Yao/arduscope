"""
Flight Profile Analyzer: descriptive flight-envelope dimensions.

Unlike the diagnostic analyzers, this one raises no warnings — it
summarizes how the aircraft actually flew: altitude, ground/air speed,
climb rate, distance travelled, range from home, and attitude envelope.
All statistics are computed over the armed (in-flight) window only.

Required: GPS
Title: 飞行包络
"""

from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from lib.gps_velocity import vz_measured
from lib.i18n import t

from . import AnalysisResult, BaseAnalyzer

_EARTH_R = 6371000.0  # mean Earth radius (m)


class FlightProfileAnalyzer(BaseAnalyzer):
    """Descriptive flight-envelope summary (info-level, no suggestions)."""

    registry_key = "flight_profile"
    sort_order = 15  # right after summary (10), before attitude (20)
    required_messages = ["GPS"]
    # Everything read below. A type that is not declared is not loaded with
    # the rest, and asking for it reads the whole file again: XKF1 did, twice.
    optional_messages = ["POS", "BARO", "XKF1", "NKF1", "ARSP", "ATT"]

    def analyze(self) -> AnalysisResult:
        details: list[str] = []
        figures: list[go.Figure] = []

        gps = self.reader.get_messages_as_arrays(
            "GPS", ["I", "Spd", "Lat", "Lng", "Alt", "VZ"])
        ts = gps["_timestamp"]
        if len(ts) == 0:
            return self.make_result(
                title=t("analyzer.flight_profile"),
                status="info",
                summary=t("flight_profile.empty"),
            )

        # Primary GPS instance, armed window, valid fix.
        inst = np.nan_to_num(gps["I"], nan=0).astype(int)
        primary = int(inst.min()) if len(inst) else 0
        armed = self.get_armed_mask(ts)
        valid = (
            ~np.isnan(gps["Lat"]) & ~np.isnan(gps["Lng"])
            & (gps["Lat"] != 0) & (gps["Lng"] != 0)
        )
        mask = armed & (inst == primary) & valid
        if np.sum(mask) < 10:
            return self.make_result(
                title=t("analyzer.flight_profile"),
                status="info",
                summary=t("common.never_armed" if self.reader.never_armed()
                          else "flight_profile.empty"),
            )

        ts_m = ts[mask]
        spd = gps["Spd"][mask]
        lat = gps["Lat"][mask]
        lng = gps["Lng"][mask]

        # --- #3 ground speed (max / cruise) ---
        spd_valid = spd[~np.isnan(spd)]
        max_speed = float(np.max(spd_valid)) if len(spd_valid) else 0.0
        moving = spd_valid[spd_valid > 1.0]
        cruise_speed = float(np.mean(moving)) if len(moving) else 0.0
        details.append(t("flight_profile.ground_speed",
                         mx=f"{max_speed:.1f}", cruise=f"{cruise_speed:.1f}"))

        # --- #5 total ground distance (integrate speed over time) ---
        dt = np.diff(ts_m)
        # Drop cross-gap intervals (logging pauses between arm cycles).
        good_dt = (dt > 0) & (dt < 5.0)
        # nansum: a NaN speed sample contributes 0, never "nan km".
        distance_km = float(
            np.nansum(spd[:-1][good_dt] * dt[good_dt])) / 1000.0
        details.append(t("flight_profile.distance", km=f"{distance_km:.2f}"))

        # --- #6 max range from home (first valid armed fix) ---
        home_lat, home_lng = float(lat[0]), float(lng[0])
        max_range = float(np.nanmax(
            self._haversine(home_lat, home_lng, lat, lng)))
        details.append(t("flight_profile.max_range", m=f"{max_range:.0f}"))

        # --- #1 max relative altitude + #2 altitude profile ---
        alt_ts, alt, alt_src = self._altitude_series()
        max_alt = None
        if alt is not None and len(alt):
            max_alt = float(np.nanmax(alt))
            details.append(t("flight_profile.max_alt",
                             alt=f"{max_alt:.1f}", src=alt_src))
            figures.append(self._alt_figure(alt_ts, alt))

        # --- #4 max climb / descent rate (GPS/EKF cross-checked) ---
        climb, descent, climb_note = self._climb_rates()
        if climb is not None:
            details.append(t("flight_profile.climb_rate",
                             up=f"{climb:.1f}", down=f"{descent:.1f}"))
            if climb_note:
                details.append(climb_note)

        # --- #4b max 3D speed (ground speed + vertical combined) ---
        # GPS.Spd is horizontal-only; during a hard climb or dive the
        # vehicle's true speed along its path is √(Spd²+VZ²). Dual
        # sources: GPS (Spd,VZ) and EKF (VN,VE,VD), cross-checked.
        vz = np.where(vz_measured(self.reader, ts, gps["I"])[mask],
                      gps["VZ"][mask], np.nan)
        gps3d = np.sqrt(spd ** 2 + vz ** 2)
        details.extend(self._speed3d_lines(ts_m, gps3d, spd, vz))

        # --- #7 airspeed (plane) + #9 speed profile ---
        asp_ts, asp = self._airspeed_series()
        if asp is not None and len(asp):
            details.append(t("flight_profile.airspeed",
                             mx=f"{float(np.nanmax(asp)):.1f}"))
        figures.append(self._speed_figure(ts_m, spd, asp_ts, asp, gps3d))

        # --- #8 attitude envelope ---
        max_roll, max_pitch = self._attitude_envelope()
        if max_roll is not None:
            details.append(t("flight_profile.attitude_env",
                             roll=f"{max_roll:.0f}", pitch=f"{max_pitch:.0f}"))

        summary = t("flight_profile.summary",
                    alt=f"{max_alt:.0f}" if max_alt is not None else "?",
                    spd=f"{max_speed:.0f}",
                    dist=f"{distance_km:.1f}")

        return self.make_result(
            title=t("analyzer.flight_profile"),
            status="good",
            summary=summary,
            details=details,
            figures=figures,
            action_level="ok",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _haversine(lat0: float, lon0: float,
                   lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Great-circle distance (m) from (lat0,lon0) to each (lat,lon)."""
        p0, p1 = np.radians(lat0), np.radians(lat)
        dphi = np.radians(lat - lat0)
        dlmb = np.radians(lon - lon0)
        a = (np.sin(dphi / 2) ** 2
             + np.cos(p0) * np.cos(p1) * np.sin(dlmb / 2) ** 2)
        return 2 * _EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    def _armed_values(self, msg: str, field: str):
        """Return (ts, values) for a field over the armed window, or None."""
        if not self.reader.has_message(msg):
            return None
        data = self.reader.get_messages_as_arrays(msg, [field])
        ts = data["_timestamp"]
        if len(ts) == 0 or np.all(np.isnan(data[field])):
            return None
        m = self.get_armed_mask(ts)
        return ts[m], data[field][m]

    def _altitude_series(self):
        """Relative-to-home altitude series, with source label.

        D1 = relative-to-home. Prefer POS.RelHomeAlt; fall back to
        barometric (relative to arm) then GPS AMSL minus first sample.
        """
        rel = self._armed_values("POS", "RelHomeAlt")
        if rel is not None:
            return rel[0], rel[1], "POS.RelHomeAlt"
        baro = self._armed_values("BARO", "Alt")
        if baro is not None:
            return baro[0], baro[1], "BARO.Alt"
        gps_alt = self._armed_values("GPS", "Alt")
        if gps_alt is not None:
            ts, a = gps_alt
            finite = a[~np.isnan(a)]
            base = float(finite[0]) if len(finite) else 0.0
            return ts, a - base, "GPS.Alt"
        return None, None, ""

    def _ekf_vd_series(self):
        """(ts, VD) for the lowest-numbered EKF core over the armed
        window, or None. Down-positive."""
        for msg in ("XKF1", "NKF1"):
            if not self.reader.has_message(msg):
                continue
            data = self.reader.get_messages_as_arrays(msg, ["C", "VD"])
            ts = data["_timestamp"]
            if len(ts) == 0 or np.all(np.isnan(data["VD"])):
                continue
            mask = self.get_armed_mask(ts)
            core = data.get("C")
            if core is not None and not np.all(np.isnan(core)):
                lowest = int(np.nanmin(core))
                mask = mask & (np.nan_to_num(core, nan=-1).astype(int)
                               == lowest)
            vd = data["VD"][mask]
            fin = ~np.isnan(vd)
            if np.count_nonzero(fin):
                return ts[mask][fin], vd[fin]
        return None

    def _climb_rates(self):
        """Max climb / descent as positive magnitudes (m/s).

        VD/VZ are NED down-positive, so climb rate = -VD; both extremes
        reported as positive numbers.

        When GPS.VZ and EKF VD both exist the extremes are taken only
        where the two agree (same tolerance as the 3D-speed gate) and
        reported from the GPS side: a diverged EKF velocity state
        distorts the raw extreme in either direction (2026-07-20
        16-40-11: EKF-only read 24.6 m/s climb while GPS + altitude
        slope showed ~31). Single-source logs keep the raw extreme.
        """
        ekf = self._ekf_vd_series()
        gvz = None
        if self.reader.has_message("GPS"):
            gps = self.read_primary_instance("GPS", ["I", "VZ"], "I")
            keep = (self.get_armed_mask(gps["_timestamp"])
                    & vz_measured(self.reader, gps["_timestamp"], gps["I"])
                    & ~np.isnan(gps["VZ"]))
            if np.any(keep):
                g_ts, gvz = gps["_timestamp"][keep], gps["VZ"][keep]

        if gvz is not None and ekf is not None and len(ekf[0]) >= 2:
            ekf_i = np.interp(g_ts, ekf[0], ekf[1])
            diff = np.abs(gvz - ekf_i)
            consistent = ((diff <= self._SPEED3D_ABS_TOL)
                          | (diff <= self._SPEED3D_REL_TOL
                             * np.maximum(np.abs(gvz), np.abs(ekf_i))))
            vals = gvz[consistent]
            if len(vals):
                climb, descent = float(-np.min(vals)), float(np.max(vals))
                # transparency: a real GPS climb peak inside an EKF
                # divergence window is excluded above (can't be double-
                # confirmed) — say so instead of dropping it silently
                note = None
                raw_climb = float(-np.min(gvz))
                if raw_climb > max(climb, 0.1) * 1.1:
                    k = int(np.argmin(gvz))
                    note = t("flight_profile.climb_gps_peak",
                             v=f"{raw_climb:.1f}", t=f"{g_ts[k]:.1f}")
                return climb, descent, note
            gvz = None   # never agreed: fall through to EKF raw
        if ekf is not None:
            vals = ekf[1]
            return float(-np.min(vals)), float(np.max(vals)), None
        if gvz is not None:
            return float(-np.min(gvz)), float(np.max(gvz)), None
        return None, None, None

    def _ekf_speed3d_series(self):
        """(ts, v3d, vn, ve, vd) over the armed window, or None.

        Filtered to the lowest-numbered EKF core so the series is one
        coherent state, not an interleave. That is NOT a primary-lane
        guarantee (lane switches are events, not logged per row) — a
        diverged core is caught by the caller's dual-source gate, not
        here.
        """
        for msg in ("XKF1", "NKF1"):
            if not self.reader.has_message(msg):
                continue
            data = self.reader.get_messages_as_arrays(
                msg, ["C", "VN", "VE", "VD"])
            ts = data["_timestamp"]
            if len(ts) == 0:
                continue
            mask = self.get_armed_mask(ts)
            core = data.get("C")
            if core is not None and not np.all(np.isnan(core)):
                lowest = int(np.nanmin(core))
                mask = mask & (np.nan_to_num(core, nan=-1).astype(int)
                               == lowest)
            vn, ve, vd = (data[f][mask] for f in ("VN", "VE", "VD"))
            v3d = np.sqrt(vn ** 2 + ve ** 2 + vd ** 2)
            if np.all(np.isnan(v3d)):
                continue
            return ts[mask], v3d, vn, ve, vd
        return None

    # A GPS/EKF 3D-speed sample pair is "consistent" when the two agree
    # within 15% (or 1 m/s absolute, so hover noise never trips the
    # ratio). An EKF velocity state diverging under a variance event can
    # run to 2x the GPS doppler speed (2026-07-20 16-40-11: EKF 88.2 vs
    # GPS 43.3 m/s) — such samples must not become the headline max.
    _SPEED3D_REL_TOL = 0.15
    _SPEED3D_ABS_TOL = 1.0

    def _speed3d_lines(self, ts_m, gps3d, spd, vz):
        """Detail lines for max 3D speed: [] when neither source carries
        a vertical component, one headline line, plus one line when a
        divergent-source peak was excluded (never drop data silently).

        Dual-source path takes the max only over consistent samples and
        headlines the GPS-side value there (doppler speed keeps its
        quality through EKF variance events; the EKF state is the one
        that runs away).
        """
        gps_ok = not np.all(np.isnan(gps3d))
        ekf = self._ekf_speed3d_series()
        if not gps_ok and ekf is None:
            return []

        # Single-source fallbacks (old logs): no cross-check possible.
        if ekf is None:
            i = int(np.nanargmax(gps3d))
            return [self._speed3d_line_single(
                float(gps3d[i]), float(spd[i]), float(vz[i]), "GPS")]
        ts_e, v3d, vn, ve, vd = ekf
        fin_e = ~np.isnan(ts_e) & ~np.isnan(v3d)
        if not gps_ok or np.count_nonzero(fin_e) < 2:
            if np.all(np.isnan(v3d)):
                return []
            j = int(np.nanargmax(v3d))
            return [self._speed3d_line_single(
                float(v3d[j]), float(np.hypot(vn[j], ve[j])),
                float(vd[j]), "EKF")]

        # Dual source: compare pointwise on the GPS timeline.
        ekf_i = np.interp(ts_m, ts_e[fin_e], v3d[fin_e])
        fin = ~np.isnan(gps3d)
        diff = np.abs(gps3d - ekf_i)
        consistent = fin & (
            (diff <= self._SPEED3D_ABS_TOL)
            | (diff <= self._SPEED3D_REL_TOL * np.maximum(gps3d, ekf_i)))

        if np.any(consistent):
            gated = np.where(consistent, gps3d, np.nan)
            i = int(np.nanargmax(gated))
        else:
            i = int(np.nanargmax(np.where(fin, gps3d, np.nan)))
        mx, horiz, vdown = float(gps3d[i]), float(spd[i]), float(vz[i])
        delta = float(diff[i]) / max(mx, float(ekf_i[i]), 1e-6) * 100.0
        dir_label = t("flight_profile.dir_climb" if vdown < 0
                      else "flight_profile.dir_descent")
        lines = [t("flight_profile.speed_3d",
                   mx=f"{mx:.1f}", h=f"{horiz:.1f}", dir=dir_label,
                   v=f"{abs(vdown):.1f}", delta=f"{delta:.1f}")]

        # Transparency: when the raw peak meaningfully exceeds the
        # headline AND that peak itself fails the cross-source check,
        # say so (never drop data silently). A higher-but-consistent
        # peak is just in-tolerance source spread — no line for that.
        ekf_max = float(np.nanmax(v3d))
        gps_max = float(np.nanmax(np.where(fin, gps3d, -np.inf)))
        raw_max = max(ekf_max, gps_max)
        if raw_max > mx * 1.05:
            if ekf_max >= gps_max:
                j = int(np.nanargmax(v3d))
                peak_t, peak_src = float(ts_e[j]), "EKF"
                other = (float(np.interp(peak_t, ts_m[fin], gps3d[fin]))
                         if np.any(fin) else 0.0)
            else:
                j = int(np.nanargmax(np.where(fin, gps3d, -np.inf)))
                peak_t, peak_src = float(ts_m[j]), "GPS"
                other = float(ekf_i[j])
            rdiff = abs(raw_max - other)
            if rdiff > max(self._SPEED3D_ABS_TOL,
                           self._SPEED3D_REL_TOL * max(raw_max, other)):
                rdelta = rdiff / max(raw_max, 1e-6) * 100.0
                lines.append(t("flight_profile.speed_3d_rejected",
                               rmx=f"{raw_max:.1f}", src=peak_src,
                               at=f"{peak_t:.1f}", rdelta=f"{rdelta:.0f}"))
        return lines

    @staticmethod
    def _speed3d_line_single(mx, horiz, vd, src):
        dir_label = t("flight_profile.dir_climb" if vd < 0
                      else "flight_profile.dir_descent")
        return t("flight_profile.speed_3d_single",
                 mx=f"{mx:.1f}", h=f"{horiz:.1f}", dir=dir_label,
                 v=f"{abs(vd):.1f}", src=src)

    def _airspeed_series(self):
        """Armed airspeed of the sensor in use. ARSP logs one row per
        sensor, and a dead second pitot must not set the max."""
        if not self.reader.has_message("ARSP"):
            return None, None
        arsp = self.reader.get_messages_as_arrays(
            "ARSP", ["I", "Primary", "Airspeed"])
        in_use = arsp["I"] == arsp["Primary"]
        if np.any(in_use):
            ts, asp = arsp["_timestamp"][in_use], arsp["Airspeed"][in_use]
        else:
            first = self.read_primary_instance("ARSP", ["Airspeed"], "I")
            ts, asp = first["_timestamp"], first["Airspeed"]
        if len(ts) == 0 or np.all(np.isnan(asp)):
            return None, None
        armed = self.get_armed_mask(ts)
        return ts[armed], asp[armed]

    def _attitude_envelope(self):
        if not self.reader.has_message("ATT"):
            return None, None
        att = self.reader.get_messages_as_arrays("ATT", ["Roll", "Pitch"])
        ts = att["_timestamp"]
        if len(ts) == 0:
            return None, None
        m = self.get_armed_mask(ts)
        roll, pitch = att["Roll"][m], att["Pitch"][m]
        if np.all(np.isnan(roll)):
            return None, None
        return (float(np.nanmax(np.abs(roll))),
                float(np.nanmax(np.abs(pitch))))

    def _alt_figure(self, ts, alt) -> go.Figure:
        fig = self.make_figure(
            title=t("flight_profile.fig_alt_title"),
            xaxis_title=t("flight_profile.time_s"),
            yaxis_title=t("flight_profile.alt_m"),
            height=320,
        )
        fig.add_trace(go.Scattergl(
            x=ts, y=alt, mode="lines",
            name=t("flight_profile.alt_m"),
            line=dict(color="#1f77b4", width=1.5),
        ))
        return fig

    def _speed_figure(self, spd_ts, spd, asp_ts, asp,
                      spd3d=None) -> go.Figure:
        fig = self.make_figure(
            title=t("flight_profile.fig_speed_title"),
            xaxis_title=t("flight_profile.time_s"),
            yaxis_title=t("flight_profile.speed_ms"),
            height=320,
        )
        fig.add_trace(go.Scattergl(
            x=spd_ts, y=spd, mode="lines",
            name=t("flight_profile.trace_ground_speed"),
            line=dict(color="#2ca02c", width=1.5),
        ))
        # 3D speed only diverges from ground speed while climbing or
        # descending; skip the trace when GPS.VZ is absent (all-NaN).
        if spd3d is not None and not np.all(np.isnan(spd3d)):
            fig.add_trace(go.Scattergl(
                x=spd_ts, y=spd3d, mode="lines",
                name=t("flight_profile.trace_speed_3d"),
                line=dict(color="#d62728", width=1.0),
            ))
        if asp is not None and len(asp):
            fig.add_trace(go.Scattergl(
                x=asp_ts, y=asp, mode="lines",
                name=t("flight_profile.trace_airspeed"),
                line=dict(color="#ff7f0e", width=1.5),
            ))
        return fig
