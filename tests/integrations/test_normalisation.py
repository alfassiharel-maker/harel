"""Normalisation helper tests.

These functions are small, but two of them are load-bearing in a way that is easy to
underestimate:

* `local_date_of` decides which calendar day a session belongs to. Get it wrong and
  a late-evening run lands on tomorrow, corrupting both days' load totals and the
  athlete's streak.
* `coerce_positive` is what keeps the project-wide "missing is `None`, never zero"
  rule true at the point data enters the system. Providers emit `0`, `-1` and `null`
  interchangeably for "no sensor", and a `0` average power reaching the engine is
  treated as a real measurement.
"""

from __future__ import annotations

import datetime as dt
import unittest

from backend.integrations.normalisation import (
    checksum,
    coerce_positive,
    local_date_of,
    map_sport,
    pace_to_speed,
    safe_ratio,
    speed_from_distance_duration,
    speed_to_pace,
)


class SportMappingTests(unittest.TestCase):
    def test_maps_known_provider_vocabularies(self) -> None:
        self.assertEqual(map_sport("RUNNING"), "run")
        self.assertEqual(map_sport("indoor_cycling"), "bike")
        self.assertEqual(map_sport("LAP_SWIMMING"), "swim")
        self.assertEqual(map_sport("STRENGTH_TRAINING"), "strength")
        self.assertEqual(map_sport("TRIATHLON"), "multisport")

    def test_indoor_and_outdoor_variants_agree(self) -> None:
        # Both must pick the same load precedence and zone model.
        self.assertEqual(map_sport("CYCLING"), map_sport("INDOOR_CYCLING"))
        self.assertEqual(map_sport("RUNNING"), map_sport("TREADMILL_RUNNING"))

    def test_unknown_sport_falls_back_to_other_rather_than_guessing(self) -> None:
        # `other` makes the engine use RPE-based load, which is honest. Guessing
        # `run` from a substring would silently apply the wrong pace model.
        self.assertEqual(map_sport("PADEL"), "other")
        self.assertEqual(map_sport("SOME_FUTURE_GARMIN_SPORT"), "other")

    def test_missing_sport_is_other(self) -> None:
        self.assertEqual(map_sport(None), "other")
        self.assertEqual(map_sport(""), "other")

    def test_is_case_and_whitespace_insensitive(self) -> None:
        self.assertEqual(map_sport("  Running  "), "run")


class LocalDateTests(unittest.TestCase):
    def test_late_evening_activity_belongs_to_the_athletes_day(self) -> None:
        # The anchor case. 23:30 in Jerusalem (UTC+3) is 20:30 UTC the same day —
        # but a 23:30 *local* run recorded at 21:30 UTC must stay on the local day.
        started = dt.datetime(2026, 7, 29, 20, 30, tzinfo=dt.UTC)
        self.assertEqual(local_date_of(started, 3 * 3600), dt.date(2026, 7, 29))

    def test_activity_after_utc_midnight_stays_on_the_local_day(self) -> None:
        # 00:30 UTC on the 30th is 03:30 local on the 30th — same day both ways.
        # The interesting case is the reverse: 22:00 UTC on the 29th is 01:00 local
        # on the 30th, and the athlete considers that a session on the 30th.
        started = dt.datetime(2026, 7, 29, 22, 0, tzinfo=dt.UTC)
        self.assertEqual(local_date_of(started, 3 * 3600), dt.date(2026, 7, 30))

    def test_negative_offset_shifts_backwards(self) -> None:
        # 02:00 UTC on the 30th is 21:00 on the 29th in UTC-5.
        started = dt.datetime(2026, 7, 30, 2, 0, tzinfo=dt.UTC)
        self.assertEqual(local_date_of(started, -5 * 3600), dt.date(2026, 7, 29))

    def test_missing_offset_falls_back_to_utc_date(self) -> None:
        started = dt.datetime(2026, 7, 29, 20, 30, tzinfo=dt.UTC)
        self.assertEqual(local_date_of(started, None), dt.date(2026, 7, 29))

    def test_naive_datetime_is_rejected(self) -> None:
        # A naive timestamp has no defined instant, so the local date is unknowable.
        # Failing loudly beats silently assuming UTC.
        with self.assertRaises(ValueError):
            local_date_of(dt.datetime(2026, 7, 29, 20, 30), 0)


class CoercePositiveTests(unittest.TestCase):
    def test_keeps_a_real_measurement(self) -> None:
        self.assertEqual(coerce_positive(235.5), 235.5)

    def test_maps_the_three_provider_spellings_of_absent_to_none(self) -> None:
        # This is the rule the whole analytics layer depends on.
        self.assertIsNone(coerce_positive(None))
        self.assertIsNone(coerce_positive(0))
        self.assertIsNone(coerce_positive(-1))

    def test_rejects_non_finite(self) -> None:
        self.assertIsNone(coerce_positive(float("nan")))
        self.assertIsNone(coerce_positive(float("inf")))

    def test_rejects_above_upper_bound(self) -> None:
        # An out-of-range value is a sensor fault or a unit mismatch (bpm reported as
        # milli-bpm), not a measurement.
        self.assertIsNone(coerce_positive(9000, upper=260))
        self.assertEqual(coerce_positive(180, upper=260), 180.0)

    def test_rejects_unparseable(self) -> None:
        self.assertIsNone(coerce_positive("not a number"))  # type: ignore[arg-type]


class RatioAndSpeedTests(unittest.TestCase):
    def test_safe_ratio_returns_none_on_zero_denominator(self) -> None:
        self.assertIsNone(safe_ratio(10.0, 0.0))
        self.assertIsNone(safe_ratio(None, 5.0))
        self.assertIsNone(safe_ratio(10.0, None))

    def test_derived_speed_is_distance_over_duration(self) -> None:
        # Hand-computed: 10 km in 2400 s is 4.1666… m/s (a 4:00/km pace).
        self.assertAlmostEqual(speed_from_distance_duration(10_000.0, 2400.0), 4.166667, places=5)

    def test_pace_and_speed_are_inverse(self) -> None:
        # 4:00/km = 240 s/km = 4.1666… m/s, and back again.
        self.assertAlmostEqual(pace_to_speed(240.0), 4.166667, places=5)
        self.assertAlmostEqual(speed_to_pace(pace_to_speed(240.0)), 240.0, places=6)

    def test_non_positive_pace_and_speed_are_none(self) -> None:
        self.assertIsNone(pace_to_speed(0))
        self.assertIsNone(speed_to_pace(0))
        self.assertIsNone(pace_to_speed(None))


class ChecksumTests(unittest.TestCase):
    def test_is_stable_and_content_sensitive(self) -> None:
        self.assertEqual(checksum(b"samples"), checksum(b"samples"))
        self.assertNotEqual(checksum(b"samples"), checksum(b"samples "))

    def test_is_a_sha256_hex_digest(self) -> None:
        self.assertEqual(len(checksum(b"x")), 64)


if __name__ == "__main__":
    unittest.main()
