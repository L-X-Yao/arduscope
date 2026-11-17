"""
Regression tests for the vehicle-detection namespace fixes.

Two coupled defects, both from the audit:

1. Thresholds.from_params was fed the raw firmware banner
   (reader.vehicle_type) instead of the canonical, family-augmented
   type. A vendor-renamed ArduPlane banner (no 'Plane' substring) then
   fell through to Copter thresholds — false attitude warnings, wrong
   motor floor.
2. is_quadplane tested Q_ENABLE == 1, missing the documented valid
   value Q_ENABLE == 2 ("Enable VTOL AUTO"), so a fully-enabled
   QuadPlane was classified as plain Plane.

These target LogReader's real property logic and Thresholds.from_params
directly (pure functions), so no .BIN fixture is needed.
"""

from __future__ import annotations

import pytest

from lib.log_reader import LogReader
from lib.thresholds import Thresholds

pytestmark = pytest.mark.fast


def _reader(vehicle_type: str, params: dict) -> LogReader:
    """A LogReader with only the fields the tested properties read.

    canonical_vehicle_type / is_copter / is_plane / is_quadplane read
    only self.vehicle_type and self.params (via get_param), so bypass the
    file-parsing __init__ and set just those.
    """
    r = LogReader.__new__(LogReader)
    r.vehicle_type = vehicle_type
    r.params = params
    return r


class TestQEnableTwo:
    """Q_ENABLE=2 (VTOL AUTO) is a QuadPlane, same as Q_ENABLE=1."""

    def test_q_enable_2_is_quadplane(self):
        assert _reader("ArduPlane", {"Q_ENABLE": 2}).is_quadplane is True

    def test_q_enable_1_is_quadplane(self):
        assert _reader("ArduPlane", {"Q_ENABLE": 1}).is_quadplane is True

    def test_q_enable_0_is_not_quadplane(self):
        assert _reader("ArduPlane", {"Q_ENABLE": 0}).is_quadplane is False


class TestCanonicalVehicleType:
    """Renamed vendor banners carry the ArduPilot family in the string."""

    def test_renamed_plane_banner_augmented(self):
        # No 'Plane' substring, but Q_ENABLE present → is_plane via fallback.
        r = _reader("Acme-FC", {"Q_ENABLE": 2})
        assert r.is_plane is True
        assert r.canonical_vehicle_type == "Acme-FC (ArduPlane)"

    def test_renamed_copter_banner_augmented(self):
        # No 'Copter' substring, but FRAME_CLASS present → is_copter fallback.
        r = _reader("VendorX", {"FRAME_CLASS": 1})
        assert r.is_copter is True
        assert r.canonical_vehicle_type == "VendorX (ArduCopter)"

    def test_standard_banner_unchanged(self):
        r = _reader("ArduCopter", {"FRAME_CLASS": 1})
        assert r.canonical_vehicle_type == "ArduCopter"


class TestFromParamsUsesCanonicalType:
    """Thresholds must pick Plane values for an augmented Plane type."""

    def test_augmented_plane_type_gets_plane_att_thresholds(self):
        # This is the payoff: with the raw banner "Acme-FC" (no
        # 'Plane'), from_params fell through to Copter (2.5°/5°). The
        # canonical type "Acme-FC (ArduPlane)" must select Plane
        # (5°/10°).
        t = Thresholds.from_params({}, "Acme-FC (ArduPlane)")
        assert t.att_rms_warn == 5.0
        assert t.att_rms_crit == 10.0

    def test_raw_renamed_banner_would_misclassify(self):
        # Documents the bug: the raw banner (no 'Plane') yields Copter
        # thresholds. This is why callers must pass canonical_vehicle_type.
        t = Thresholds.from_params({}, "Acme-FC")
        assert t.att_rms_warn == 2.5  # Copter default — the wrong answer

    def test_quadplane_motor_floor_from_q_param(self):
        # Q_M_SPIN_MIN must load into mot_spin_min on an augmented Plane.
        t = Thresholds.from_params(
            {"Q_M_SPIN_MIN": 0.12}, "Acme-FC (ArduPlane)")
        assert t.mot_spin_min == 0.12


def _parse_banner(*messages) -> LogReader:
    reader = LogReader.__new__(LogReader)
    reader.vehicle_type = "Unknown"
    reader.firmware_version = "Unknown"
    for message in messages:
        reader._parse_firmware_msg(message)
    return reader


class TestFirmwareBannerParsing:
    def test_standard_banner(self):
        reader = _parse_banner("ArduCopter V4.6.3")
        assert reader.vehicle_type == "ArduCopter"
        assert reader.firmware_version == "V4.6.3"

    def test_dual_version_vendor_banner_uses_upstream_version(self):
        reader = _parse_banner(
            "FC-FPV-Rocket v4.1.0 (e6553238) [ArduCopter V4.7.0]")
        assert reader.vehicle_type == "FC-FPV-Rocket"
        assert reader.firmware_version == "V4.7.0"

    def test_brackets_and_punctuation_are_allowed(self):
        reader = _parse_banner("FC [ArduPlane(V4.5.1)]")
        assert reader.vehicle_type == "FC"
        assert reader.firmware_version == "V4.5.1"

    def test_family_and_version_matching_is_case_insensitive(self):
        reader = _parse_banner("fc [aRdUcOpTeR: v4.7.0]")
        assert reader.vehicle_type == "fc"
        assert reader.firmware_version == "v4.7.0"

    def test_later_weak_match_cannot_replace_strong_match(self):
        reader = _parse_banner(
            "FC-FPV-Rocket v4.1.0 (e6553238) [ArduCopter V4.7.0]",
            "FC-Other v9.9 [ArduCopter]",
        )
        assert reader.vehicle_type == "FC-FPV-Rocket"
        assert reader.firmware_version == "V4.7.0"

    def test_missing_version_and_invalid_text_do_not_raise(self):
        reader = _parse_banner("", None, 42, "ArduCopter")
        assert reader.vehicle_type == "Unknown"
        assert reader.firmware_version == "Unknown"

        # Four numeric components are outside the accepted version shape;
        # without a strong match the legacy first-two-token fallback remains.
        reader = _parse_banner("FC v1.2 [ArduCopter V4.7.0.1]")
        assert reader.vehicle_type == "FC"
        assert reader.firmware_version == "v1.2"

    def test_version_suffix_is_preserved(self):
        for banner, expected in (("ArduCopter V4.8.0-dev", "V4.8.0-dev"),
                                 ("ArduCopter V4.8.0+xxx", "V4.8.0+xxx")):
            reader = _parse_banner(banner)
            assert reader.vehicle_type == "ArduCopter"
            assert reader.firmware_version == expected

    def test_single_token_family_and_version(self):
        # The old len(parts)>=2 fallback left the version unset in this form.
        for banner, expected in (("ArduCopter-4.6.3", "4.6.3"),
                                 ("ArduCopter_V4.6.3", "V4.6.3")):
            reader = _parse_banner(banner)
            assert reader.vehicle_type == banner
            assert reader.firmware_version == expected

    def test_first_versioned_family_wins_in_multi_family_banner(self):
        reader = _parse_banner(
            "FC v0.1 [ArduPlane V4.5.1] [ArduCopter V4.7.0]")
        assert reader.vehicle_type == "FC"
        assert reader.firmware_version == "V4.5.1"
