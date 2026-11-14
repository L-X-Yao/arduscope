"""What counts as a flight when hover thrust and motor noise are measured."""

from __future__ import annotations

import numpy as np
import pytest

from lib.hover import airborne, hover_thrust
from lib.log_reader import Event
from lib.thresholds import Thresholds
from tests.test_synthetic_coverage import SyntheticReader

pytestmark = pytest.mark.fast

ARMED, DISARMED, LAND_COMPLETE, NOT_LANDED = 10, 11, 18, 28


def _log(alt, throttle, vehicle="ArduCopter", params=None, events=(),
         armed=((10.0, 90.0),), message="CTUN") -> SyntheticReader:
    """100 s at 10 Hz. alt and throttle are functions of time."""
    ts = np.arange(0.0, 100.0, 0.1)
    n = len(ts)
    reader = SyntheticReader(
        messages={
            message: {"_timestamp": ts, "ThO": throttle(ts), "Alt": alt(ts),
                      "CRt": np.zeros(n)},
            "ATT": {"_timestamp": ts, "Roll": np.zeros(n),
                    "Pitch": np.zeros(n)},
        },
        params=params or {}, vehicle_type=vehicle,
        armed_intervals=[tuple(span) for span in armed])
    reader.events = [Event(timestamp=when, event_id=event_id)
                     for when, event_id in events]
    return reader


def _between(low, high, inside, outside=0.0):
    return lambda ts: np.where((ts >= low) & (ts < high), inside, outside)


def _rows(mask, ts=np.arange(0.0, 100.0, 0.1)):
    """(first, last) second of the rows a mask keeps."""
    kept = ts[mask]
    return (round(float(kept[0]), 1), round(float(kept[-1]), 1))


TS = np.arange(0.0, 100.0, 0.1)


class TestCopterLandDetector:
    """Copter logs the state update_throttle_hover goes by."""

    def test_flight_runs_from_not_landed_to_land_complete(self):
        reader = _log(alt=lambda ts: np.zeros(len(ts)),
                      throttle=_between(10, 90, 0.3),
                      events=[(20.0, NOT_LANDED), (70.0, LAND_COMPLETE)])

        assert _rows(airborne(reader, Thresholds(), TS)) == (20.0, 69.9)

    def test_altitude_has_no_say_once_the_detector_speaks(self):
        """An IMU failure test left the altitude wandering by 40 m with the
        aircraft on the ground."""
        reader = _log(alt=lambda ts: 40.0 * np.sin(ts / 10.0),
                      throttle=_between(10, 90, 0.2),
                      events=[(95.0, NOT_LANDED)])
        heights = reader.get_messages_as_arrays("CTUN", ["Alt"])["Alt"]
        assert np.max(heights - np.min(heights)) > 2.0  # precondition

        assert hover_thrust(reader, Thresholds()) is None

    def test_motors_at_idle_carry_nothing(self):
        """The same test, carried about with the land detector let go and
        the motors at 1%: 533 rows of "level hover" at 0.013, and
        MOT_THST_HOVER 0.125 to set."""
        reader = _log(alt=lambda ts: np.zeros(len(ts)),
                      throttle=_between(10, 90, 0.013),
                      events=[(12.0, NOT_LANDED)])

        assert hover_thrust(reader, Thresholds()) is None

    def test_log_that_opens_in_flight(self):
        reader = _log(alt=lambda ts: np.full(len(ts), 30.0),
                      throttle=_between(0, 60, 0.3), armed=[(0.0, 60.0)],
                      events=[(50.0, LAND_COMPLETE)])

        assert _rows(airborne(reader, Thresholds(), TS)) == (0.0, 49.9)

    def test_log_that_ends_in_flight(self):
        reader = _log(alt=lambda ts: np.zeros(len(ts)),
                      throttle=_between(10, 100, 0.3), armed=[(10.0, 99.9)],
                      events=[(20.0, NOT_LANDED)])

        assert _rows(airborne(reader, Thresholds(), TS)) == (20.0, 99.9)

    def test_hover_thrust_is_taken_in_the_air(self):
        reader = _log(alt=lambda ts: np.zeros(len(ts)),
                      throttle=lambda ts: np.where(ts < 40, 0.10, 0.34),
                      events=[(40.0, NOT_LANDED), (90.0, LAND_COMPLETE)])

        assert hover_thrust(reader, Thresholds()) == pytest.approx(0.34)


class TestHeightAboveArming:
    """Plane logs no land detector, so the altitude is the judge."""

    PLANE = {"vehicle": "ArduPlane", "params": {"Q_ENABLE": 1},
             "message": "QTUN"}

    def test_ground_run_that_reads_a_little_high_is_no_flight(self):
        reader = _log(alt=_between(20, 80, 0.2),
                      throttle=_between(12, 88, 0.18), **self.PLANE)

        assert not np.any(airborne(reader, Thresholds(), TS))

    def test_ground_run_under_a_sinking_baro_is_no_flight(self):
        """A QuadPlane ground run: lift rotors in ground effect pulled the baro
        height down 2 m while the aircraft stood still. Measured from the
        lowest point of the log, it began 2 m up: 205 rows of hover at 18%
        throttle, on an aircraft that hovers at 32%."""
        reader = _log(alt=lambda ts: -2.2 * np.clip((ts - 30.0) / 60.0, 0, 1),
                      throttle=_between(12, 90, 0.18), **self.PLANE)
        alt = reader.get_messages_as_arrays("QTUN", ["Alt"])["Alt"]
        assert (alt - np.min(alt))[150] > 2.0  # precondition: throttle up

        assert not np.any(airborne(reader, Thresholds(), TS))
        assert hover_thrust(reader, Thresholds()) is None

    def test_low_hover_is_a_flight(self):
        """The lowest hover in 46 QuadPlane logs was at 1.7 m, and the
        highest ground run at 0.2 m."""
        reader = _log(alt=_between(20, 80, 1.7),
                      throttle=_between(12, 88, 0.33), **self.PLANE)

        assert _rows(airborne(reader, Thresholds(), TS)) == (20.0, 79.9)
        assert hover_thrust(reader, Thresholds()) == pytest.approx(0.33)

    def test_height_counts_from_where_the_aircraft_armed(self):
        reader = _log(alt=lambda ts: 100.0 + _between(20, 80, 5.0)(ts),
                      throttle=_between(12, 88, 0.33), **self.PLANE)

        assert _rows(airborne(reader, Thresholds(), TS)) == (20.0, 79.9)

    def test_cruise_with_lift_motors_stopped_is_not_carried(self):
        reader = _log(alt=_between(20, 80, 50.0),
                      throttle=_between(20, 30, 0.33), **self.PLANE)

        assert _rows(airborne(reader, Thresholds(), TS)) == (20.0, 29.9)

    def test_disarmed_rows_are_out(self):
        reader = _log(alt=lambda ts: np.full(len(ts), 50.0),
                      throttle=lambda ts: np.full(len(ts), 0.3),
                      armed=[(40.0, 60.0)], **self.PLANE)

        assert not np.any(airborne(reader, Thresholds(), TS)[TS < 40.0])


class TestWhatTheLogCannotSay:
    def test_fixed_wing_has_no_lift_throttle(self):
        reader = _log(alt=_between(20, 80, 50.0), throttle=_between(20, 80, 45.0),
                      vehicle="ArduPlane")

        assert airborne(reader, Thresholds(), TS) is None
        assert hover_thrust(reader, Thresholds()) is None

    def test_no_altitude_and_no_detector_leaves_the_throttle(self):
        reader = _log(alt=lambda ts: np.full(len(ts), np.nan),
                      throttle=_between(20, 80, 0.3))

        assert _rows(airborne(reader, Thresholds(), TS)) == (20.0, 79.9)
        assert hover_thrust(reader, Thresholds()) is None

    def test_rows_at_other_rates_take_the_state_before_them(self):
        reader = _log(alt=_between(20, 80, 10.0),
                      throttle=_between(12, 88, 0.3),
                      vehicle="ArduPlane", params={"Q_ENABLE": 1},
                      message="QTUN")
        fast = np.arange(0.0, 100.0, 0.001)

        kept = fast[airborne(reader, Thresholds(), fast)]

        assert (kept[0], kept[-1]) == pytest.approx((20.0, 79.999), abs=1e-6)
