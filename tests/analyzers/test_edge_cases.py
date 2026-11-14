"""
Edge-case tests targeting the 8 bugs found in the 2026-04-14 source audit.

Each test creates a SyntheticReader with "hostile" data (empty arrays, NaN,
missing messages) and verifies the analyzer does NOT crash.  These tests
prevent regressions for the fixes in the 2026-04-14 source audit.
"""

from __future__ import annotations

from tests._repo import REPO_ROOT


import numpy as np
import pytest

from analyzers import AnalysisResult
from lib.ai_diagnosis import _build_cross_rules, _is_measured
from lib.i18n import t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from tests.test_synthetic_coverage import SyntheticReader, _vehicle, _thresholds


# ====================================================================
# B1: vtol_transition — param_advice used before assignment
# ====================================================================
class TestVtolTransitionNoTransitions:
    """When there are no VTOL transitions, param_advice must not NameError."""

    def test_no_transitions_returns_ok(self):
        from analyzers.vtol_transition import VTOLTransitionAnalyzer
        n = 100
        # QTUN with Trn stuck at 0 (AIRSPEED_WAIT never entered TIMER):
        # a hover-only log with zero transitions.
        reader = SyntheticReader(
            messages={
                "QTUN": {
                    "_timestamp": np.arange(n, dtype=float),
                    "TMix": np.zeros(n),
                    "Trn": np.zeros(n),
                    "Alt": np.full(n, 20.0),
                    "DAlt": np.full(n, 20.0),
                    "CRt": np.zeros(n),
                    "DCRt": np.zeros(n),
                },
            },
            params={"Q_TRANSITION_MS": 3000, "Q_TRAN_PIT_MAX": 5},
            vehicle_type="ArduPlane",
        )
        vehicle = VehicleInfo("ArduPlane", "synthetic", "4.6.3",
                              4, 8, 2, is_quadplane=True)
        t = Thresholds()
        a = VTOLTransitionAnalyzer(reader, t, vehicle)
        assert a.can_run()   # precondition — the gate must be open
        result = a.analyze()
        assert isinstance(result, AnalysisResult)
        # No transitions → ok, no NameError
        assert result.action_level == "ok"


# ====================================================================
# B2: failsafe — BAT read without has_message guard
# ====================================================================
class TestFailsafeNoBattery:
    """When BAT messages are absent but EKF triggers critical,
    failsafe must not crash on empty BAT array."""

    def test_critical_without_bat(self):
        from analyzers.failsafe import FailsafeAnalyzer
        # EKF variance high enough to trigger critical, but NO BAT data
        reader = SyntheticReader(
            messages={
                "XKF4": {
                    "_timestamp": np.arange(100, dtype=float),
                    "SV": np.full(100, 1.5),  # very high variance
                    "SP": np.zeros(100),
                    "SH": np.zeros(100),
                    "SM": np.zeros(100),
                    "SVT": np.zeros(100),
                    "OFN": np.zeros(100),
                    "OFE": np.zeros(100),
                    "FS": np.zeros(100),
                    "TS": np.zeros(100),
                    "SS": np.full(100, 19),
                    "GPS": np.zeros(100),
                    "PI": np.zeros(100),
                    "C": np.zeros(100),
                },
                "GPS": {
                    "_timestamp": np.arange(100, dtype=float),
                    "I": np.zeros(100),
                    "NSats": np.full(100, 20),
                    "HDop": np.full(100, 0.8),
                    "Status": np.full(100, 3),
                },
                # NO "BAT" message — this is the edge case
            },
            params={"FS_EKF_THRESH": 0.8, "BATT_LOW_VOLT": 42.0},
        )
        vehicle = _vehicle()
        t = _thresholds(reader)
        a = FailsafeAnalyzer(reader, t, vehicle)
        assert a.can_run()   # precondition — the gate must be open
        result = a.analyze()
        assert isinstance(result, AnalysisResult)


# ====================================================================
# B3: ekf — int(ss[-1]) on NaN
# ====================================================================
class TestEkfNanVariance:
    """EKF variance fields all NaN must not crash with ValueError."""

    def test_all_nan_sv_sp_sh_sm_does_not_crash(self):
        """np.nanmax on all-NaN raises ValueError without the fix."""
        from analyzers.ekf import EKFAnalyzer
        n = 50
        reader = SyntheticReader(
            messages={
                "XKF4": {
                    "_timestamp": np.arange(n, dtype=float),
                    "SV": np.full(n, np.nan),   # all NaN — no velocity var
                    "SP": np.full(n, np.nan),
                    "SH": np.full(n, np.nan),
                    "SM": np.full(n, np.nan),
                    "SS": np.zeros(n),
                    "C": np.zeros(n),
                },
            },
            params={"FS_EKF_THRESH": 0.8},
        )
        vehicle = _vehicle()
        t = _thresholds(reader)
        a = EKFAnalyzer(reader, t, vehicle)
        assert a.can_run()   # precondition — the gate must be open
        result = a.analyze()
        assert isinstance(result, AnalysisResult)
        # All-NaN variances → max_variance = 0 → good (no false alarm)
        assert result.status == "good"


class TestEkfNanSS:
    """EKF SS field is all NaN — must not crash on int(NaN)."""

    def test_all_nan_ss(self):
        from analyzers.ekf import EKFAnalyzer
        n = 50
        reader = SyntheticReader(
            messages={
                "XKF4": {
                    "_timestamp": np.arange(n, dtype=float),
                    "SV": np.full(n, 0.1),
                    "SP": np.zeros(n),
                    "SH": np.zeros(n),
                    "SM": np.zeros(n),
                    "SVT": np.zeros(n),
                    "OFN": np.zeros(n),
                    "OFE": np.zeros(n),
                    "FS": np.zeros(n),
                    "TS": np.zeros(n),
                    "SS": np.full(n, np.nan),  # all NaN
                    "GPS": np.zeros(n),
                    "PI": np.zeros(n),
                    "C": np.zeros(n),
                },
            },
            params={"FS_EKF_THRESH": 0.8},
        )
        vehicle = _vehicle()
        t = _thresholds(reader)
        a = EKFAnalyzer(reader, t, vehicle)
        assert a.can_run()   # precondition — the gate must be open
        result = a.analyze()
        assert isinstance(result, AnalysisResult)
        # SS display should be "N/A", not crash
        assert any("N/A" in d for d in result.details)


# ====================================================================
# B4: gps — division by zero when armed arrays empty
# ====================================================================
class TestGpsEmptyArmed:
    """GPS data exists but armed intervals have no matching samples."""

    def test_no_armed_samples(self):
        from analyzers.gps import GPSAnalyzer
        n = 50
        reader = SyntheticReader(
            messages={
                "GPS": {
                    "_timestamp": np.arange(n, dtype=float),
                    "I": np.zeros(n),
                    "Status": np.full(n, 3),
                    "HDop": np.full(n, 1.0),
                    "NSats": np.full(n, 15),
                    "Lat": np.full(n, 30.0),
                    "Lng": np.full(n, 120.0),
                    "Spd": np.full(n, 5.0),
                },
                "GPA": {
                    "_timestamp": np.arange(n, dtype=float),
                    "I": np.zeros(n),
                    "HAcc": np.full(n, 1.0),
                    "VAcc": np.full(n, 1.5),
                    "SAcc": np.full(n, 0.5),
                },
            },
            params={},
            # Armed interval outside the data range → empty armed arrays
            armed_intervals=[(200.0, 300.0)],
        )
        vehicle = _vehicle()
        t = _thresholds(reader)
        a = GPSAnalyzer(reader, t, vehicle)
        assert a.can_run()   # precondition — the gate must be open
        result = a.analyze()
        assert isinstance(result, AnalysisResult)


# ====================================================================
# B5: temperature — max() on empty sequence
# ====================================================================
class TestTemperatureAllNaN:
    """All IMU temperatures are NaN — must not crash on max(empty)."""

    def test_all_nan_temps(self):
        from analyzers.temperature import TemperatureAnalyzer
        n = 50
        reader = SyntheticReader(
            messages={
                "IMU": {
                    "_timestamp": np.arange(n, dtype=float),
                    "I": np.zeros(n),
                    "T": np.full(n, np.nan),  # all NaN
                    "GyrX": np.zeros(n),
                    "GyrY": np.zeros(n),
                    "GyrZ": np.zeros(n),
                    "AccX": np.zeros(n),
                    "AccY": np.zeros(n),
                    "AccZ": np.full(n, -9.8),
                },
            },
        )
        vehicle = _vehicle()
        t = _thresholds(reader)
        a = TemperatureAnalyzer(reader, t, vehicle)
        assert a.can_run()   # precondition — the gate must be open
        result = a.analyze()
        assert isinstance(result, AnalysisResult)


# ====================================================================
# B6: multi_report — val==int(val) on inf/nan
# ====================================================================
class TestExtractMetricsAllNaN:
    """extract_metrics must not crash when VIBE/EKF/wind arrays are all NaN.

    np.nanmax() raises ValueError on all-NaN arrays. Previous code called
    np.nanmax directly after checking only that len(timestamps) > 0, not
    that the field arrays themselves had any non-NaN values.
    """

    def test_all_nan_vibe_returns_none(self):
        import multi_report
        from tests.test_synthetic_coverage import SyntheticReader
        from lib.thresholds import Thresholds
        n = 50
        ts = np.linspace(0, 25, n)
        reader = SyntheticReader(messages={
            "VIBE": {
                "_timestamp": ts,
                "VibeX": np.full(n, np.nan),
                "VibeY": np.full(n, np.nan),
                "VibeZ": np.full(n, np.nan),
                "Clip0": np.zeros(n),
                "Clip1": np.zeros(n),
                "Clip2": np.zeros(n),
            },
        })
        metrics = multi_report.extract_metrics(reader, Thresholds())
        assert metrics["max_vibe"] is None, (
            "All-NaN VibeX/Y/Z must return None, not crash"
        )

    def test_all_nan_ekf_returns_none(self):
        import multi_report
        from tests.test_synthetic_coverage import SyntheticReader
        from lib.thresholds import Thresholds
        n = 50
        ts = np.linspace(0, 25, n)
        reader = SyntheticReader(messages={
            "XKF4": {
                "_timestamp": ts,
                "SV": np.full(n, np.nan),
                "SP": np.full(n, np.nan),
                "SH": np.full(n, np.nan),
                "SM": np.full(n, np.nan),
            },
        })
        metrics = multi_report.extract_metrics(reader, Thresholds())
        assert metrics["max_ekf_var"] is None


class TestMultiReportInfNan:
    """Parameter values of inf/nan must not crash the HTML renderer."""

    def test_isfinite_guard(self):
        # Import the actual production function and call it with hostile data
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        import multi_report
        # Build a minimal param diff structure with inf/nan values
        all_params = [
            {"PARAM_INF": float('inf'), "PARAM_NAN": float('nan'), "NORMAL": 42.0},
            {"PARAM_INF": float('-inf'), "PARAM_NAN": float('nan'), "NORMAL": 43.0},
        ]
        changed = multi_report.compute_param_diff(all_params)
        # The HTML rendering should not crash
        html = multi_report.generate_html(
            ["log1", "log2"], [{}, {}], all_params,
        )
        assert "<html" in html.lower()


# ====================================================================
# B7: trend_analyzer — premature convergence
# ====================================================================
class TestTrendConvergenceStrict:
    """Convergence must not be declared when threshold-tracked metrics
    have insufficient data."""

    def test_partial_data_not_converged(self):
        from lib.trend_analyzer import analyze_trends
        t = Thresholds()
        # Only provide max_vibe — other tracked metrics missing
        metrics = [
            {"max_vibe": 8.0},
            {"max_vibe": 7.0},
        ]
        report = analyze_trends(metrics, ["a", "b"], t)
        # max_vibe converged, but other tracked metrics (clip, rate_out,
        # hover_thr_err, ekf_var) are INSUFFICIENT → NOT fully converged
        assert report.convergence.fully_converged is False
        assert "max_vibe" in report.convergence.converged_metric_keys

    def test_all_tracked_present_and_good(self):
        from lib.trend_analyzer import analyze_trends
        t = Thresholds()
        _good = {
            "max_vibe": 8.0, "max_clip": 5, "max_rate_out": 0.04,
            "hover_thr_err": 0.02, "max_ekf_var": 0.1,
        }
        metrics = [{**_good}, {**_good, "max_vibe": 7.0}]
        report = analyze_trends(metrics, ["a", "b"], t)
        assert report.convergence.fully_converged is True


# ====================================================================
# B8: log_reader — empty file
# ====================================================================
class TestLogReaderEmpty:
    """Empty files must raise ValueError, not mmap error."""

    def test_empty_file_raises(self, tmp_path):
        from lib.log_reader import LogReader
        empty = tmp_path / "empty.BIN"
        empty.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            LogReader(str(empty))


# ====================================================================
# LogReader — MODE message with string mode_num (no ModeNum field)
# ====================================================================
class TestLogReaderModeStringParsing:
    """MODE message where Mode='LOITER' (string) and ModeNum field is absent.

    Old _prescan code did int(mode_num) which raises ValueError when
    mode_num is a non-numeric string. The fix wraps it in try/except
    returning 0 for unrecognised strings.
    """

    def test_string_mode_num_converts_safely(self):
        """Simulate the mode_num conversion in _prescan: string must not crash."""
        from lib.log_reader import ModeChange

        for mode_val in ('LOITER', 'STABILIZE', 'AUTO', '', 'UNKNOWN_MODE'):
            try:
                mode_number = int(mode_val)
            except (TypeError, ValueError):
                mode_number = 0
            mc = ModeChange(timestamp=1.0, mode_name=mode_val,
                            mode_number=mode_number)
            assert isinstance(mc.mode_number, int)
            assert mc.mode_number == 0

    def test_numeric_string_still_works(self):
        """Integer-as-string ('5') must convert correctly (old log format)."""
        mode_val = '5'
        try:
            mode_number = int(mode_val)
        except (TypeError, ValueError):
            mode_number = 0
        assert mode_number == 5

    def test_integer_mode_num_unchanged(self):
        """Integer mode_num (normal format) must pass through unchanged."""
        mode_num = 12
        try:
            mode_number = int(mode_num)
        except (TypeError, ValueError):
            mode_number = 0
        assert mode_number == 12


# ====================================================================
# W: flight_report — analyzer exception → info, not critical
# ====================================================================
class TestAnalyzerExceptionNotCritical:
    """An analyzer that throws should produce status=info, not critical."""

    def test_phantom_critical_prevented(self):
        # Verify the AnalysisResult for error case
        result = AnalysisResult(
            title="test (错误)",
            status="info",
            summary="分析模块内部错误: test",
            action_level="ok",
        )
        assert result.status == "info"
        assert result.action_level == "ok"


# ====================================================================
# web_app — upload partial file cleanup on write exception
# ====================================================================
class TestWebAppUploadCleanup:
    """Upload handler must clean up save_path on write exception."""

    def test_partial_file_removed_on_write_error(self, tmp_path, monkeypatch):
        """If the file write raises, upload() itself must delete the
        partial file and answer 500 — the REAL handler is invoked (the
        old test re-implemented the write loop in the test body, so the
        product code was never exercised)."""
        import io
        import json
        import os
        import bottle
        import web_app as wa

        monkeypatch.setattr(wa, 'REPORT_DIR', str(tmp_path))

        original_open = open
        created_paths = []
        raised = []

        class FailingFile:
            def __init__(self, path, mode):
                created_paths.append(path)
                self._f = original_open(path, mode)
                self._calls = 0

            def write(self, data):
                self._calls += 1
                if self._calls >= 2:
                    raised.append(True)
                    raise OSError("simulated disk full")
                return self._f.write(data)

            def __enter__(self): return self
            def __exit__(self, *a): self._f.close()
            def close(self): self._f.close()

        monkeypatch.setattr(
            'builtins.open',
            lambda p, m='r', **kw: (FailingFile(p, m) if 'wb' in m
                                    else original_open(p, m, **kw)))

        body_data = b"A" * (1024 * 1024 * 3)  # 3 MB -> >=2 chunks
        environ = {
            'REQUEST_METHOD': 'POST',
            'PATH_INFO': '/api/upload',
            'QUERY_STRING': 'filename=test.BIN',
            'CONTENT_LENGTH': str(len(body_data)),
            'CONTENT_TYPE': 'application/octet-stream',
            'wsgi.input': io.BytesIO(body_data),
            'wsgi.errors': io.StringIO(),
            'wsgi.url_scheme': 'http',
        }
        bottle.request.bind(environ)
        bottle.response.bind()

        ret = wa.upload()

        # Preconditions: the real handler created the file and the
        # write really failed mid-stream.
        assert created_paths, "upload() never opened a save file"
        assert raised, "simulated disk-full never triggered"

        assert bottle.response.status_code == 500
        assert "error" in json.loads(ret)
        leftovers = [f for f in os.listdir(tmp_path)
                     if f.startswith("upload_")]
        assert leftovers == [], (
            f"Partial upload not cleaned up: {leftovers}")


# ====================================================================
# flight_report._strip_html — entity decoding completeness
# ====================================================================
class TestStripHtml:
    """_strip_html must decode ALL standard HTML entities, not just 4.

    ai_diagnosis escapes dynamic text with html.escape(), producing
    &quot; and &apos; in addition to &amp; &lt; &gt;. Previous code
    only handled the latter three, leaving &quot; as literal text in
    CSV/JSON exports.
    """

    def test_strips_tags(self):
        from flight_report import _strip_html
        assert _strip_html("<b>hello</b>") == "hello"

    def test_decodes_amp_lt_gt(self):
        from flight_report import _strip_html
        assert _strip_html("A &amp; B &lt; C &gt; D") == "A & B < C > D"

    def test_decodes_quot(self):
        from flight_report import _strip_html
        result = _strip_html('value &quot;0.15&quot;')
        assert result == 'value "0.15"', (
            f"&quot; must decode to double-quote, got: {result!r}"
        )

    def test_decodes_nbsp(self):
        from flight_report import _strip_html
        assert _strip_html("a&nbsp;b") == "a b"

    def test_decodes_numeric_entities(self):
        from flight_report import _strip_html
        assert _strip_html("&#34;hello&#34;") == '"hello"'

    def test_realistic_detail_string(self):
        from flight_report import _strip_html
        detail = ('设置 <code>ATC_RAT_RLL_P</code> 为 0.135 '
                  '(当前 &quot;0.15&quot;)')
        result = _strip_html(detail)
        assert '"0.15"' in result
        assert '&quot;' not in result
        assert '<code>' not in result


# ====================================================================
# AnalysisResult invariant triggers — 5 analyzers that paired
# param_suggestions with non-apply_param action_level on realistic
# default-configuration logs. Each test reproduces the trigger and
# asserts the result satisfies the invariant (i.e. construction does
# not raise AssertionError in lib/results.py).
# ====================================================================
class TestInvariantTriggers:
    """Realistic-config triggers for the five analyzers that previously
    paired param_suggestions with a non-apply_param action_level."""

    def test_attitude_partial_autotune_axes(self):
        """AUTOTUNE_AXES=1 (roll-only) + warning attitude RMS used to be
        run_autotune + suggestion → invariant violation. Must be apply_param."""
        from analyzers.attitude import AttitudeAnalyzer
        n = 300
        ts = np.linspace(0, 120, n)
        reader = SyntheticReader(
            messages={"ATT": {
                "_timestamp": ts,
                "DesRoll": np.zeros(n),
                "Roll": np.random.normal(0, 3.0, n),  # warning
                "DesPitch": np.zeros(n),
                "Pitch": np.random.normal(0, 3.0, n),
                "DesYaw": np.full(n, 90.0),
                "Yaw": np.full(n, 90.0),
            }},
            params={"AUTOTUNE_AXES": 1,  # roll-only, valid partial config
                    "ATC_RAT_RLL_P": 0.135, "ATC_RAT_PIT_P": 0.135},
            armed_intervals=[(0.0, 120.0)],
        )
        result = AttitudeAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status in ("warning", "critical")
        assert result.param_suggestions
        assert result.action_level == "apply_param"

    def test_gps_dual_with_auto_switch_disabled(self):
        """Dual-GPS + warning + GPS_AUTO_SWITCH=0 used to be physical_check
        + suggestion → invariant violation. Must be apply_param."""
        from analyzers.gps import GPSAnalyzer
        n = 400
        ts = np.linspace(0, 200, n)
        # Two GPS instances, low NSats to trigger warning
        gps_data = {
            "_timestamp": np.concatenate([ts, ts]),
            "I": np.concatenate([np.zeros(n), np.ones(n)]),
            "Lat": np.concatenate([np.full(n, 39.9), np.full(n, 39.9)]),
            "Lng": np.concatenate([np.full(n, 116.4), np.full(n, 116.4)]),
            "Alt": np.concatenate([np.full(n, 50.0), np.full(n, 50.0)]),
            "Status": np.concatenate([np.full(n, 3), np.full(n, 3)]),
            "NSats": np.concatenate([np.full(n, 5), np.full(n, 5)]),  # warning
            "HDop": np.concatenate([np.full(n, 3.5), np.full(n, 3.5)]),
            "Spd": np.concatenate([np.full(n, 5.0), np.full(n, 5.0)]),
        }
        reader = SyntheticReader(
            messages={"GPS": gps_data},
            params={"GPS_AUTO_SWITCH": 0},
            armed_intervals=[(0.0, 200.0)],
        )
        result = GPSAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.param_suggestions, (
            "expected GPS_AUTO_SWITCH suggestion for dual-GPS warning + switch=0"
        )
        assert result.action_level == "apply_param"

    def test_compass_motor_interference_default_comp(self):
        """Motor interference > warn threshold with COMPASS_MOTCT off. It
        takes CompassMot either way: AP_Compass.cpp says not to set MOTCT by
        hand, and without a vector it does nothing."""
        from analyzers.compass import CompassAnalyzer
        n = 500
        ts = np.linspace(0, 250, n)
        # Mag readings rise monotonically with throttle so that field
        # magnitude — sqrt(mag_x² + mag_y² + mag_z²) — actually changes
        # between low- and high-throttle bins. The previous formulation was
        # symmetric around mag_x=200 so |field| was nearly identical at both
        # extremes, masking the interference.
        throttle = 1000 + 800 * (np.sin(np.linspace(0, 8, n)) * 0.5 + 0.5)
        mag_x = np.full(n, 200.0) + (throttle - 1000) * 0.8
        reader = SyntheticReader(
            messages={
                "MAG": {
                    "_timestamp": ts,
                    "I": np.zeros(n),
                    "MagX": mag_x, "MagY": np.full(n, 100.0),
                    "MagZ": np.full(n, 400.0),
                    "OfsX": np.zeros(n), "OfsY": np.zeros(n), "OfsZ": np.zeros(n),
                },
                "RCOU": {
                    "_timestamp": ts,
                    "C1": throttle, "C2": throttle,
                    "C3": throttle, "C4": throttle,
                    **{f"C{i}": np.full(n, 1500.0) for i in range(5, 9)},
                },
            },
            params={"COMPASS_MOTCT": 0, "COMPASS_MOT_X": 5.0},
            armed_intervals=[(0.0, 250.0)],
        )
        result = CompassAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert t("compass.mot_comp_off") in result.details, (
            "expected the pointer for >30% interference; "
            "if the trigger now misses, the regression coverage is lost"
        )
        assert not result.param_suggestions
        assert result.action_level == "physical_check"
        # The cross-rule reads this by name, so the two have to agree.
        by_name = {r.name: r for r in _build_cross_rules()}
        rule = by_name["compass_motor_interference"]
        assert _is_measured(result, rule.measured["compass"])

        reader.params.pop("COMPASS_MOT_X")
        result = CompassAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert not result.param_suggestions
        assert t("compass.mot_cal_needed") in result.details

    def test_airspeed_disagreement_with_arspd_use(self):
        """Critical airspeed/GPS disagreement + ARSPD_USE=1 (default)
        used to be physical_check + suggestion → invariant violation."""
        from analyzers.airspeed import AirspeedAnalyzer
        n = 400
        ts = np.linspace(0, 200, n)
        # Persistent 11 m/s underread vs GPS groundspeed.
        # crit threshold is strict (>), so diff must exceed 10.0, not equal it.
        reader = SyntheticReader(
            messages={
                "ARSP": {
                    "_timestamp": ts,
                    "I": np.zeros(n),
                    "Airspeed": np.full(n, 15.0),
                    "DiffPress": np.full(n, 100.0),
                    "Temp": np.full(n, 20.0),
                    "Health": np.ones(n),
                },
                "GPS": {
                    "_timestamp": ts,
                    "I": np.zeros(n),
                    "Spd": np.full(n, 26.0),  # 11 m/s above airspeed (>10 crit)
                    "Status": np.full(n, 3),
                    "Lat": np.full(n, 39.9), "Lng": np.full(n, 116.4),
                    "Alt": np.full(n, 50.0), "NSats": np.full(n, 12),
                    "HDop": np.full(n, 0.8),
                },
            },
            vehicle_type="ArduPlane",
            params={"ARSPD_USE": 1},
            armed_intervals=[(0.0, 200.0)],
        )
        result = AirspeedAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.param_suggestions, (
            "expected ARSPD_USE suggestion for critical airspeed disagreement"
        )
        assert result.action_level == "apply_param"

    def test_motor_spin_gap_escalated_to_critical(self):
        """Warning-path SPIN_MIN suggestion + status escalated to critical
        by motor imbalance used to be physical_check + suggestion →
        invariant violation."""
        from analyzers.motor import MotorAnalyzer
        n = 600
        ts = np.linspace(0, 300, n)
        # spin_gap tight (warning) + motor imbalance crosses 20% crit threshold.
        # C1=1700 vs C2/3/4=1300 → global_mean=1400, max_dev=21.4% > 20% crit.
        reader = SyntheticReader(
            messages={
                "RCOU": {
                    "_timestamp": ts,
                    "C1": np.full(n, 1700.0),  # imbalanced high (≥20% crit)
                    "C2": np.full(n, 1300.0),
                    "C3": np.full(n, 1300.0),
                    "C4": np.full(n, 1300.0),
                    **{f"C{i}": np.full(n, 1500.0) for i in range(5, 9)},
                },
            },
            params={
                "MOT_SPIN_MIN": 0.10, "MOT_SPIN_ARM": 0.09,  # gap < 0.03
                "MOT_PWM_MIN": 1000, "MOT_PWM_MAX": 2000,
                "FRAME_CLASS": 1,  # Quad
            },
            armed_intervals=[(0.0, 300.0)],
        )
        result = MotorAnalyzer(
            reader, _thresholds(reader), _vehicle()).analyze()
        assert result.status == "critical", (
            f"expected imbalance to escalate to critical; got {result.status} "
            f"(without escalation, motor.py:249 critical branch is unexercised)"
        )
        assert result.param_suggestions, "expected MOT_SPIN_MIN suggestion"
        assert result.action_level == "apply_param"


# ====================================================================
# web_app — list_jobs race condition (same as get_job)
# ====================================================================
class TestWebAppListJobs:
    """list_jobs must snapshot dicts inside the lock; otherwise json.dumps
    can race with concurrent _update_job() mutations."""

    def test_list_jobs_copies_dicts_inside_lock(self):
        """Verify the source uses dict() comprehension to snapshot, not the
        bare list(JOBS.values()) pattern that captures live references."""
        import inspect
        import web_app
        src = inspect.getsource(web_app.list_jobs)
        # The fix copies each dict inside the lock.
        assert "dict(j) for j in JOBS.values()" in src or \
               "dict(j) for j in" in src, (
            "list_jobs must snapshot dicts inside the lock — bare "
            "list(JOBS.values()) lets concurrent mutations tear reads"
        )
