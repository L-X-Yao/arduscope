"""Firmware STATUSTEXT failsafe events on the failsafe card.

ArduPilot announces several failsafes only through STATUSTEXT (`MSG`):
the Plane RC short/long and GCS failsafes are the common ones. The card
used to read only `ERR` records, so a flight with six in-flight RC short
failsafes still came out `good`.
"""

from __future__ import annotations

import numpy as np
import pytest

from arduscope.analyzers.failsafe import FailsafeAnalyzer
from arduscope.lib.i18n import t
from arduscope.lib.log_reader import ErrorEvent, TextMessage
from tests.test_synthetic_coverage import (
    SyntheticReader, _thresholds, _vehicle)

pytestmark = pytest.mark.fast


def _reader(texts=(), armed=((0.0, 100.0),), errors=(),
            vehicle_type="ArduCopter"):
    reader = SyntheticReader(
        messages={}, params={}, armed_intervals=list(armed) or None,
        vehicle_type=vehicle_type)
    # SyntheticReader treats an empty list as "use the default"; this
    # test needs a log with no arming evidence at all.
    reader._armed = list(armed)
    reader.text_messages = [TextMessage(when, text)
                            for when, text in texts]
    reader.errors = list(errors)
    return reader


def _card(reader):
    return FailsafeAnalyzer(
        reader, _thresholds(reader),
        _vehicle(vt=reader.vehicle_type)).analyze()


def _has(res, key: str, **kwargs) -> bool:
    line = t(key, **kwargs)
    return any(line in detail for detail in res.details)


class TestTextFailsafes:
    def test_armed_rc_short_is_critical(self):
        res = _card(_reader([
            (10.0, "RC Short Failsafe On"),
            (12.0, "RC Short Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert res.action_text == t("failsafe.action_text_critical")
        assert _has(res, "failsafe.triggered_count", count=1)
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    end="12.0")

    def test_text_only_battery_keeps_generic_action(self):
        res = _card(_reader([
            (10.0, "Battery Failsafe - Disarming"),
            (12.0, "Battery Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert res.action_text == t("failsafe.action_critical", fs_ekf="0.8")

    def test_unarmed_gcs_is_not_critical(self):
        res = _card(_reader([
            (10.0, "GCS Failsafe On: QRTL"),
            (12.0, "GCS Failsafe Cleared"),
        ], armed=[(0.0, 5.0)]))

        assert res.status != "critical"
        assert _has(res, "failsafe.text_unarmed_count", count=1)
        assert not _has(res, "failsafe.triggered_text_event",
                        name=t("failsafe.subsys_gcs"), time="10.0",
                        end="12.0")

    def test_on_without_clear_is_unresolved_and_critical(self):
        res = _card(_reader([(10.0, "RC Short Failsafe On")]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_unresolved",
                    name=t("failsafe.subsys_radio"), time="10.0")

    def test_err_and_text_are_not_double_counted(self):
        res = _card(_reader(
            [(10.5, "Radio Failsafe")],
            errors=[ErrorEvent(10.0, 5, 1)],
        ))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_count", count=1)
        assert _has(res, "failsafe.triggered_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    code=1)
        assert not _has(res, "failsafe.triggered_text_event",
                        name=t("failsafe.subsys_radio"), time="10.5",
                        end="100.0")

    def test_throttle_and_rc_short_are_one_event(self):
        res = _card(_reader([
            (10.0, "Throttle failsafe on"),
            (10.0, "RC Short Failsafe On"),
            (12.0, "Throttle failsafe off"),
            (12.1, "Short Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_count", count=1)
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    end="12.0")

    def test_copter_radio_failsafe_without_on(self):
        res = _card(_reader([
            (10.0, "Radio Failsafe - Continuing Landing"),
            (12.0, "Radio Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    end="12.0")

    def test_rc_long_on_with_switched_suffix(self):
        res = _card(_reader([
            (10.0, "RC Long Failsafe On: switched to RTL"),
            (12.0, "RC Long Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    end="12.0")

    def test_colon_form_names_terrain(self):
        res = _card(_reader([(10.0, "Failsafe: Terrain data missing")]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_unresolved",
                    name=t("failsafe.subsys_terrain"), time="10.0")

    def test_case_insensitive(self):
        res = _card(_reader([
            (10.0, "rc short failsafe on"),
            (12.0, "short failsafe cleared"),
        ]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_count", count=1)

    def test_unrelated_text_is_ignored(self):
        res = _card(_reader([(10.0, "PreArm: Gyros inconsistent")]))

        assert res.status != "critical"
        assert t("failsafe.none_triggered") in res.details

    def test_clear_without_on_is_ignored(self):
        res = _card(_reader([(10.0, "GCS Failsafe Cleared")]))

        assert res.status != "critical"
        assert t("failsafe.none_triggered") in res.details

    def test_no_armed_window_is_unknown_not_unarmed(self):
        res = _card(_reader([(10.0, "RC Short Failsafe On")], armed=[]))

        assert res.status != "critical"
        assert _has(res, "failsafe.text_unknown_count", count=1)
        assert not _has(res, "failsafe.text_unarmed_count", count=1)
        assert res.summary == t("failsafe.summary_good_unknown", count=1)

    def test_unarmed_unresolved_detail_says_unresolved(self):
        res = _card(_reader(
            [(10.0, "RC Short Failsafe On")], armed=[(0.0, 5.0)]))

        assert res.status != "critical"
        assert _has(res, "failsafe.text_unarmed_unresolved",
                    name=t("failsafe.subsys_radio"), time="10.0")

    def test_lifo_closes_both_events_before_armed_flight(self):
        res = _card(_reader([
            (1.0, "RC Short Failsafe On"),
            (3.0, "RC Short Failsafe On"),
            (5.0, "RC Short Failsafe Cleared"),
            (7.0, "RC Short Failsafe Cleared"),
        ], armed=[(20.0, 30.0)]))

        assert res.status != "critical"
        assert _has(res, "failsafe.text_unarmed_count", count=2)

    def test_out_of_order_on_does_not_collapse(self):
        res = _card(_reader([
            (100.0, "RC Short Failsafe On"),
            (10.0, "RC Short Failsafe On"),
            (11.0, "RC Short Failsafe Cleared"),
            (101.0, "RC Short Failsafe Cleared"),
        ]))

        assert _has(res, "failsafe.triggered_count", count=1)
        assert _has(res, "failsafe.text_unarmed_count", count=1)

    def test_event_at_disarm_boundary_is_not_armed(self):
        res = _card(_reader([
            (2.0, "RC Short Failsafe On"),
            (3.0, "RC Short Failsafe Cleared"),
        ], armed=[(0.0, 2.0)]))

        assert res.status != "critical"
        assert _has(res, "failsafe.text_unarmed_count", count=1)

    def test_combined_radio_battery_name(self):
        res = _card(_reader([
            (10.0, "Radio + Battery Failsafe - Continuing Landing"),
            (12.0, "Radio Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio_battery"),
                    time="10.0", end="12.0")
        assert not _has(res, "failsafe.triggered_text_unresolved",
                        name=t("failsafe.subsys_radio_battery"),
                        time="10.0")

    def test_combined_on_plain_clear_before_armed_is_not_critical(self):
        res = _card(_reader([
            (10.0, "Radio + Battery Failsafe - Continuing Landing"),
            (12.0, "Radio Failsafe Cleared"),
        ], armed=[(20.0, 30.0)]))

        assert res.status != "critical"
        assert _has(res, "failsafe.text_unarmed_count", count=1)

    def test_combined_gcs_battery_name(self):
        res = _card(_reader([
            (10.0, "GCS + Battery Failsafe - Continuing Landing"),
            (12.0, "GCS Failsafe Cleared"),
        ]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_gcs_battery"),
                    time="10.0", end="12.0")
        assert not _has(res, "failsafe.triggered_text_unresolved",
                        name=t("failsafe.subsys_gcs_battery"),
                        time="10.0")

    def test_rover_clear_with_another_name_closes_aggregate(self):
        res = _card(_reader([
            (10.0, "Radio Failsafe"),
            (12.0, "Battery Failsafe Cleared"),
        ], vehicle_type="ArduRover"))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_count", count=1)
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    end="12.0")
        assert not _has(res, "failsafe.triggered_text_unresolved",
                        name=t("failsafe.subsys_radio"), time="10.0")

    def test_rover_vendor_banner_still_gets_aggregate_clear(self):
        res = _card(_reader([
            (0.0, "FC-Ground v1.0 [ArduRover V4.6.0]"),
            (10.0, "Radio Failsafe"),
            (12.0, "Battery Failsafe Cleared"),
        ], vehicle_type="FC-Ground"))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    end="12.0")

    def test_armed_open_at_log_end_is_critical(self):
        res = _card(_reader(
            [(100.0, "RC Short Failsafe On")], armed=[(0.0, 100.0)]))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_text_unresolved",
                    name=t("failsafe.subsys_radio"), time="100.0")

    def test_one_err_suppresses_only_one_text_episode(self):
        res = _card(_reader(
            [(10.5, "Radio Failsafe"), (15.5, "Radio Failsafe")],
            errors=[ErrorEvent(10.0, 5, 1)],
        ))

        assert res.status == "critical"
        assert _has(res, "failsafe.triggered_count", count=2)
        assert _has(res, "failsafe.triggered_event",
                    name=t("failsafe.subsys_radio"), time="10.0",
                    code=1)
        assert _has(res, "failsafe.triggered_text_unresolved",
                    name=t("failsafe.subsys_radio"), time="15.5")

    def test_text_action_not_used_when_battery_margin_is_critical(self):
        reader = _reader([(10.0, "RC Short Failsafe On")])
        reader.params["BATT_LOW_VOLT"] = 12.0
        bat_ts = np.arange(10.0, 30.0, 1.0)
        reader._messages["BAT"] = {
            "_timestamp": bat_ts,
            "Inst": np.zeros(len(bat_ts)),
            "Volt": np.full(len(bat_ts), 11.0),
        }
        res = _card(reader)

        assert res.status == "critical"
        assert res.action_text == t("failsafe.action_critical", fs_ekf="0.8")

    def test_ekf_single_over_headline_kept_with_unarmed_event(self):
        reader = _reader([(10.0, "RC Short Failsafe On")],
                         armed=[(0.0, 5.0)])
        ts = np.arange(0.0, 10.0, 0.1)
        reader._messages["XKF4"] = {
            "_timestamp": ts,
            "C": np.zeros(len(ts)),
            "PI": np.zeros(len(ts)),
            "SV": np.full(len(ts), 0.1),
            "SP": np.full(len(ts), 0.4),
            "SH": np.full(len(ts), 0.1),
            "SM": np.full(len(ts), 1.8),
            "SS": np.full(len(ts), 16.0),
        }
        reader.params["FS_EKF_THRESH"] = 0.8
        res = _card(reader)

        assert res.summary == t("failsafe.summary_good_ekf")
        assert _has(res, "failsafe.text_unarmed_count", count=1)

    def test_non_string_text_is_ignored(self):
        reader = _reader()
        reader.text_messages = [TextMessage(10.0, ["not", "a", "string"])]
        res = _card(reader)

        assert res.status != "critical"
        assert t("failsafe.none_triggered") in res.details
