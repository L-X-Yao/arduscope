"""What the per-axis PID results claim, and how they print it."""

from __future__ import annotations

import numpy as np
import pytest

from analyzers.pid_tuning import PIDTuningAnalyzer
from lib.i18n import t
from lib.thresholds import Thresholds
from lib.vehicle_detect import VehicleInfo
from test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

GAINS = {"ATC_RAT_RLL_P": 0.2, "ATC_RAT_RLL_I": 0.2, "ATC_RAT_RLL_D": 0.004}


def _vehicle() -> VehicleInfo:
    return VehicleInfo(vehicle_type="ArduCopter", firmware_version="4.6.3",
                       board_name="s", frame_class=1, motor_count=4,
                       imu_count=1, is_quadplane=False)


def _axis(pid, *, armed=None, rate=None):
    """Roll axis result; pid and rate are message dicts."""
    ts = pid["_timestamp"]
    messages = {"PIDR": pid,
                "POS": {"_timestamp": ts,
                        "Alt": np.linspace(0.0, 80.0, len(ts))}}
    if rate is not None:
        messages["RATE"] = rate
    reader = SyntheticReader(messages, GAINS,
                             armed_intervals=armed or [(ts[0], ts[-1])])
    return PIDTuningAnalyzer(reader, Thresholds(),
                             _vehicle()).analyze().sub_results[0]


def _pid(ts, **columns):
    n = len(ts)
    pid = {"_timestamp": ts, "Tar": np.sin(ts), "Act": np.sin(ts),
           "P": 0.1 * np.sin(ts), "I": np.full(n, 0.05), "D": np.zeros(n),
           "FF": np.zeros(n)}
    pid.update(columns)
    return pid


class TestAxisEvidence:
    def test_flagged_axis_cites_its_source(self):
        ts = np.linspace(0.0, 100.0, 2000)
        rate = {"_timestamp": ts, "ROut": np.full(len(ts), 0.5),
                "RDes": np.zeros(len(ts))}
        axis = _axis(_pid(ts), rate=rate)
        assert axis.status == "critical"
        assert axis.evidence_source == t("pid_tuning.evidence")

    def test_good_axis_claims_nothing(self):
        ts = np.linspace(0.0, 100.0, 2000)
        rate = {"_timestamp": ts, "ROut": np.full(len(ts), 0.01),
                "RDes": np.zeros(len(ts))}
        axis = _axis(_pid(ts), rate=rate)
        assert axis.status == "good"
        assert axis.evidence_source == ""


def _oscillating_segments():
    """Two 10 s stretches 300 s apart, tracking error flipping sign 8 times
    a second, well over the 5/s line and the 5 deg/s error floor."""
    ts = np.concatenate([np.arange(10.0, 20.0, 0.02),
                         np.arange(320.0, 330.0, 0.02)])
    tar = 0.2 * np.sin(ts)
    act = tar + 0.3 * np.sign(np.sin(2 * np.pi * 4.0 * ts))
    return ts, tar, act


class TestCrossingRate:
    def test_gap_between_stretches_does_not_dilute_it(self):
        ts, tar, act = _oscillating_segments()
        axis = _axis(_pid(ts, Tar=tar, Act=act),
                     armed=[(10.0, 20.0), (320.0, 330.0)])
        assert axis.status != "info"
        err = tar - act
        flips = np.sum(np.diff(np.sign(err)) != 0)
        assert flips / 20.0 > Thresholds().pid_osc_crossings_warn
        line = f"  • {t('pid_tuning.suggest_tracking_osc', zc=f'{flips / 20.0:.1f}')}"
        assert line in axis.details, axis.details


class TestIntegratorDirection:
    _TS = np.linspace(0.0, 100.0, 2000)

    def _details(self, integrator):
        return _axis(_pid(self._TS, I=integrator)).details

    def _line(self, direction, pct):
        return t("pid_tuning.i_monotonic", direction=t(direction), pct=pct)

    def test_mostly_flat_i_with_balanced_moves_is_not_falling(self):
        # log_92: up on 8% of samples, down on 8%, flat on the rest.
        steps = np.zeros(len(self._TS))
        steps[::25] = 0.001
        steps[12::25] = -0.001
        details = self._details(0.1 + np.cumsum(steps))
        assert not any(t("pid_tuning.i_falling") in d for d in details), details

    def test_falling_i_is_reported_with_its_own_share(self):
        details = self._details(np.linspace(0.2, 0.0, len(self._TS)))
        assert self._line("pid_tuning.i_falling", "100%") in details, details

    def test_growing_i_is_reported(self):
        details = self._details(np.linspace(0.0, 0.2, len(self._TS)))
        assert self._line("pid_tuning.i_growing", "100%") in details, details


class TestPercentWording:
    _TS = np.linspace(0.0, 100.0, 2000)

    def test_shares_print_one_percent_sign(self):
        axis = _axis(_pid(self._TS, P=np.zeros(2000), I=np.full(2000, 0.5),
                          D=np.zeros(2000)))
        assert t("pid_tuning.component_ratio", p="0%", i="100%", d="0%",
                 ff="0%") in axis.details, axis.details
        assert not any("%%" in line for line in axis.details), axis.details

    def test_d_noise_ratio_is_a_percent(self):
        # D is all the output moves by, so the ratio is exactly 1: 100%,
        # and the limit it passes is 50%, not "1.00%" over "0.5%".
        axis = _axis(_pid(self._TS, P=np.zeros(2000), D=np.sin(self._TS)))
        assert t("pid_tuning.d_noise_ratio", val="100%") in axis.details
        assert t("pid_tuning.d_noise_crit", val="100%",
                 thresh="50%") in axis.details, axis.details
