from __future__ import annotations

import math
import unittest

from backend.algorithms import zones
from backend.algorithms.types import Sport

from ._fixtures import profile


class TestHeartRateZones(unittest.TestCase):
    def test_lthr_anchored_zones(self) -> None:
        result = zones.hr_zones(profile(lthr=170))
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].anchor, "lthr")
        # Z4 is 95-100% of LTHR.
        self.assertAlmostEqual(result[3].low, 161.5)
        self.assertAlmostEqual(result[3].high, 170.0)
        self.assertEqual(result[4].high, math.inf)

    def test_falls_back_to_hr_max_bands_when_no_threshold_is_known(self) -> None:
        result = zones.hr_zones(profile(lthr=None, hr_max=190))
        self.assertEqual(result[0].anchor, "hr_max")
        self.assertAlmostEqual(result[3].low, 152.0)
        self.assertAlmostEqual(result[3].high, 171.0)

    def test_hr_max_is_estimated_from_age_when_unmeasured(self) -> None:
        # Tanaka: 208 - 0.7 x 35 = 183.5 -> 184
        athlete = profile(hr_max=None, lthr=None, age=35)
        self.assertEqual(athlete.effective_hr_max, 184)
        self.assertTrue(zones.hr_zones(athlete))

    def test_no_zones_without_any_anchor(self) -> None:
        self.assertEqual(zones.hr_zones(profile(hr_max=None, lthr=None, age=None)), ())

    def test_zone_lookup(self) -> None:
        result = zones.hr_zones(profile(lthr=170))
        found = zones.zone_for_value(165.0, result)
        assert found is not None
        self.assertEqual(found.index, 4)

    def test_value_above_the_top_zone_maps_to_the_top_zone(self) -> None:
        result = zones.hr_zones(profile(lthr=170))
        found = zones.zone_for_value(210.0, result)
        assert found is not None
        self.assertEqual(found.index, 5)

    def test_value_below_the_lowest_hr_max_band_is_unclassified(self) -> None:
        # %HRmax bands start at 50% of max; below that there is no zone, and
        # inventing one would misreport a resting reading as training.
        result = zones.hr_zones(profile(lthr=None, hr_max=190))
        self.assertIsNone(zones.zone_for_value(80.0, result))


class TestPowerZones(unittest.TestCase):
    def test_coggan_seven_zones(self) -> None:
        result = zones.power_zones(250.0)
        self.assertEqual(len(result), 7)
        self.assertAlmostEqual(result[3].low, 227.5)
        self.assertAlmostEqual(result[3].high, 265.0)

    def test_rejects_non_positive_ftp(self) -> None:
        with self.assertRaises(ValueError):
            zones.power_zones(0.0)


class TestPaceZones(unittest.TestCase):
    def test_threshold_pace_lands_in_zone_four(self) -> None:
        result = zones.pace_zones(300.0)
        found = zones.zone_for_value(300.0, result)
        assert found is not None
        self.assertEqual(found.index, 4)

    def test_faster_than_threshold_is_zone_five(self) -> None:
        result = zones.pace_zones(300.0)
        found = zones.zone_for_value(250.0, result)
        assert found is not None
        self.assertEqual(found.index, 5)

    def test_much_slower_than_threshold_is_zone_one(self) -> None:
        result = zones.pace_zones(300.0)
        found = zones.zone_for_value(500.0, result)
        assert found is not None
        self.assertEqual(found.index, 1)

    def test_zones_cover_the_whole_pace_range(self) -> None:
        result = zones.pace_zones(300.0)
        for pace in (1.0, 100.0, 294.0, 300.0, 350.0, 400.0, 1000.0):
            self.assertIsNotNone(zones.zone_for_value(pace, result), f"pace {pace} unclassified")

    def test_rejects_non_positive_pace(self) -> None:
        with self.assertRaises(ValueError):
            zones.pace_zones(-1.0)


class TestDistribution(unittest.TestCase):
    def test_zone_seconds_accumulate_by_sample_interval(self) -> None:
        result = zones.hr_zones(profile(lthr=170))
        samples = [120.0] * 10 + [165.0] * 5
        totals = zones.zone_seconds(samples, result, sample_interval_s=2.0)
        self.assertAlmostEqual(totals[1], 20.0)
        self.assertAlmostEqual(totals[4], 10.0)

    def test_rejects_non_positive_interval(self) -> None:
        with self.assertRaises(ValueError):
            zones.zone_seconds([120.0], zones.hr_zones(profile()), sample_interval_s=0.0)

    def test_polarised_week_is_detected(self) -> None:
        distribution = zones.intensity_distribution({1: 100.0, 2: 700.0, 3: 100.0, 4: 100.0})
        self.assertAlmostEqual(distribution.low_pct, 80.0)
        self.assertAlmostEqual(distribution.moderate_pct, 10.0)
        self.assertAlmostEqual(distribution.high_pct, 10.0)
        self.assertTrue(distribution.is_polarised)

    def test_grey_zone_week_is_not_polarised(self) -> None:
        # Plenty of moderate work, little easy work: the classic "always
        # medium-hard" pattern.
        distribution = zones.intensity_distribution({2: 300.0, 3: 600.0, 4: 100.0})
        self.assertFalse(distribution.is_polarised)

    def test_empty_distribution_is_zeroed_not_an_error(self) -> None:
        distribution = zones.intensity_distribution({})
        self.assertEqual(distribution.total_seconds, 0.0)
        self.assertFalse(distribution.is_polarised)


class TestSportDefaults(unittest.TestCase):
    def test_bike_with_ftp_uses_power(self) -> None:
        result = zones.default_zones_for_sport(profile(), Sport.BIKE)
        self.assertEqual(result[0].anchor, "ftp")

    def test_run_with_threshold_hr_uses_heart_rate(self) -> None:
        result = zones.default_zones_for_sport(profile(), Sport.RUN)
        self.assertEqual(result[0].anchor, "lthr")

    def test_run_without_any_hr_anchor_uses_pace(self) -> None:
        athlete = profile(lthr=None, hr_max=None, age=None)
        result = zones.default_zones_for_sport(athlete, Sport.RUN)
        self.assertEqual(result[0].anchor, "threshold_pace")


if __name__ == "__main__":
    unittest.main()
