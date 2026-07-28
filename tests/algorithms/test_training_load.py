from __future__ import annotations

import math
import unittest
from datetime import timedelta

from backend.algorithms import training_load as tl
from backend.algorithms.types import LoadSource, Sex, Sport

from ._fixtures import (
    REF_DAY,
    bike_activity,
    constant_loads,
    profile,
    run_activity,
    swim_activity,
)


class TestTrimp(unittest.TestCase):
    def test_male_formula_matches_banister(self) -> None:
        # 60 min, HR 150, rest 50, max 200 -> HRr = 0.6667
        value = tl.trimp(60.0, 150.0, 50.0, 200.0, Sex.MALE)
        expected = 60.0 * (2.0 / 3.0) * 0.64 * math.exp(1.92 * (2.0 / 3.0))
        self.assertAlmostEqual(value, expected, places=6)
        self.assertAlmostEqual(value, 92.074, places=2)

    def test_female_formula_differs_from_male(self) -> None:
        male = tl.trimp(60.0, 150.0, 50.0, 200.0, Sex.MALE)
        female = tl.trimp(60.0, 150.0, 50.0, 200.0, Sex.FEMALE)
        self.assertNotAlmostEqual(male, female, places=1)
        self.assertAlmostEqual(female, 104.730, places=2)

    def test_unspecified_sex_averages_both_forms(self) -> None:
        male = tl.trimp(60.0, 150.0, 50.0, 200.0, Sex.MALE)
        female = tl.trimp(60.0, 150.0, 50.0, 200.0, Sex.FEMALE)
        unspecified = tl.trimp(60.0, 150.0, 50.0, 200.0, Sex.UNSPECIFIED)
        self.assertAlmostEqual(unspecified, (male + female) / 2.0, places=9)

    def test_hr_reserve_is_clamped(self) -> None:
        # An HR above max must not produce a super-linear load.
        capped = tl.trimp(60.0, 250.0, 50.0, 200.0, Sex.MALE)
        at_max = tl.trimp(60.0, 200.0, 50.0, 200.0, Sex.MALE)
        self.assertAlmostEqual(capped, at_max, places=9)

    def test_zero_duration_is_zero_load(self) -> None:
        self.assertEqual(tl.trimp(0.0, 150.0, 50.0, 200.0), 0.0)

    def test_invalid_hr_bounds_raise(self) -> None:
        with self.assertRaises(ValueError):
            tl.trimp(60.0, 150.0, 200.0, 200.0)


class TestScaleAnchor(unittest.TestCase):
    """Every source must put one hour at threshold at exactly 100."""

    def test_power_tss_anchor(self) -> None:
        self.assertAlmostEqual(tl.power_tss(3600, 250.0, 250.0), 100.0, places=9)

    def test_hr_tss_anchor(self) -> None:
        value = tl.hr_tss(60.0, 170.0, profile())
        assert value is not None
        self.assertAlmostEqual(value, 100.0, places=6)

    def test_pace_tss_anchor(self) -> None:
        self.assertAlmostEqual(tl.pace_tss(3600, 240.0, 240.0), 100.0, places=9)

    def test_swim_tss_anchor(self) -> None:
        self.assertAlmostEqual(tl.swim_tss(3600, 95.0, 95.0), 100.0, places=9)

    def test_session_rpe_anchor(self) -> None:
        self.assertAlmostEqual(tl.session_rpe_load(60.0, 7.0), 100.0, places=9)

    def test_sources_agree_within_a_reasonable_band_at_threshold(self) -> None:
        """A triathlete's weekly total is only meaningful if the sources agree."""
        athlete = profile()
        hr_value = tl.hr_tss(60.0, athlete.lthr, athlete)
        assert hr_value is not None
        for other in (
            tl.power_tss(3600, athlete.ftp_watts, athlete.ftp_watts),
            tl.pace_tss(3600, athlete.threshold_pace_s_per_km, athlete.threshold_pace_s_per_km),
            tl.swim_tss(3600, athlete.css_s_per_100m, athlete.css_s_per_100m),
        ):
            self.assertAlmostEqual(hr_value, other, delta=0.01)


class TestTssScaling(unittest.TestCase):
    def test_power_tss_scales_with_the_square_of_intensity(self) -> None:
        at_threshold = tl.power_tss(3600, 250.0, 250.0)
        at_80_pct = tl.power_tss(3600, 200.0, 250.0)
        self.assertAlmostEqual(at_80_pct / at_threshold, 0.8**2, places=9)

    def test_swim_tss_scales_with_the_cube_of_intensity(self) -> None:
        at_threshold = tl.swim_tss(3600, 95.0, 95.0)
        slower = tl.swim_tss(3600, 95.0 / 0.9, 95.0)
        self.assertAlmostEqual(slower / at_threshold, 0.9**3, places=9)

    def test_rpe_out_of_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            tl.session_rpe_load(60.0, 11.0)

    def test_hr_tss_returns_none_without_thresholds(self) -> None:
        bare = profile(hr_max=None, lthr=None, age=None)
        self.assertIsNone(tl.hr_tss(60.0, 150.0, bare))


class TestNormalizedPower(unittest.TestCase):
    def test_constant_power_normalises_to_itself(self) -> None:
        value = tl.normalized_power([200.0] * 600)
        assert value is not None
        self.assertAlmostEqual(value, 200.0, places=6)

    def test_variable_power_exceeds_the_average(self) -> None:
        # Alternating hard/easy: NP must exceed the arithmetic mean.
        samples = ([300.0] * 60 + [100.0] * 60) * 10
        value = tl.normalized_power(samples)
        assert value is not None
        self.assertGreater(value, 200.0)

    def test_too_short_a_stream_is_undefined(self) -> None:
        self.assertIsNone(tl.normalized_power([200.0] * 10))

    def test_empty_stream_is_undefined(self) -> None:
        self.assertIsNone(tl.normalized_power([]))


class TestSourceSelection(unittest.TestCase):
    def test_bike_prefers_power(self) -> None:
        result = tl.training_load(bike_activity(), profile())
        self.assertIs(result.source, LoadSource.POWER)
        self.assertGreater(result.confidence, 0.9)

    def test_bike_falls_back_to_heart_rate_without_ftp(self) -> None:
        result = tl.training_load(bike_activity(), profile(ftp_watts=None))
        self.assertIs(result.source, LoadSource.HEART_RATE)

    def test_run_prefers_heart_rate_over_raw_pace(self) -> None:
        # Raw pace is not grade-adjusted, so HR is the more trustworthy signal.
        result = tl.training_load(run_activity(), profile())
        self.assertIs(result.source, LoadSource.HEART_RATE)

    def test_run_uses_pace_when_heart_rate_is_missing(self) -> None:
        result = tl.training_load(run_activity(avg_hr=None), profile())
        self.assertIs(result.source, LoadSource.PACE)

    def test_cycling_ftp_is_never_applied_to_running_power(self) -> None:
        """Regression guard: FTP and running threshold power share a unit but
        are different quantities."""
        athlete = profile(ftp_watts=250.0, run_threshold_power_w=None)
        result = tl.training_load(run_activity(avg_power=300.0, avg_hr=None), athlete)
        self.assertIsNot(result.source, LoadSource.POWER)

    def test_running_power_is_used_when_its_own_threshold_exists(self) -> None:
        athlete = profile(run_threshold_power_w=300.0)
        result = tl.training_load(run_activity(avg_power=300.0, avg_hr=None), athlete)
        self.assertIs(result.source, LoadSource.POWER)
        self.assertAlmostEqual(result.score, 100.0, places=2)

    def test_swim_prefers_pace_over_heart_rate(self) -> None:
        result = tl.training_load(swim_activity(), profile())
        self.assertIs(result.source, LoadSource.PACE)

    def test_rpe_is_the_last_resort(self) -> None:
        bare = profile(hr_max=None, lthr=None, age=None, ftp_watts=None, threshold_pace_s_per_km=None)
        result = tl.training_load(run_activity(avg_hr=None, distance_m=None, rpe=7.0), bare)
        self.assertIs(result.source, LoadSource.RPE)
        self.assertAlmostEqual(result.score, 100.0, places=2)

    def test_unscoreable_activity_is_explicit_not_zero_effort(self) -> None:
        bare = profile(hr_max=None, lthr=None, age=None, ftp_watts=None, threshold_pace_s_per_km=None)
        result = tl.training_load(run_activity(avg_hr=None, distance_m=None), bare)
        self.assertIs(result.source, LoadSource.NONE)
        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.confidence, 0.0)


class TestDailySeries(unittest.TestCase):
    def test_two_sessions_on_one_day_sum(self) -> None:
        athlete = profile()
        series = tl.daily_load_series([bike_activity(), run_activity()], athlete)
        self.assertEqual(list(series), [REF_DAY])
        expected = (
            tl.training_load(bike_activity(), athlete).score
            + tl.training_load(run_activity(), athlete).score
        )
        self.assertAlmostEqual(series[REF_DAY], expected, places=6)


class TestFitnessFatigue(unittest.TestCase):
    def test_constant_load_converges_to_balanced_form(self) -> None:
        loads = constant_loads(daily=50.0, days=400, end=REF_DAY)
        points = tl.fitness_fatigue(loads, REF_DAY - timedelta(days=399), REF_DAY)
        self.assertAlmostEqual(points[-1].ctl, 50.0, delta=0.05)
        self.assertAlmostEqual(points[-1].atl, 50.0, delta=0.05)
        self.assertAlmostEqual(points[-1].tsb, 0.0, delta=0.1)

    def test_fatigue_responds_faster_than_fitness(self) -> None:
        loads = constant_loads(daily=100.0, days=14, end=REF_DAY)
        points = tl.fitness_fatigue(loads, REF_DAY - timedelta(days=13), REF_DAY)
        self.assertGreater(points[-1].atl, points[-1].ctl)
        self.assertLess(points[-1].tsb, 0.0)

    def test_rest_days_are_counted_as_zeros_not_skipped(self) -> None:
        """A layoff must decay fitness for every calendar day, not every
        recorded day."""
        loads = {REF_DAY - timedelta(days=30): 100.0}
        points = tl.fitness_fatigue(loads, REF_DAY - timedelta(days=30), REF_DAY)
        self.assertEqual(len(points), 31)
        self.assertLess(points[-1].atl, points[0].atl)

    def test_seed_resumes_from_stored_state(self) -> None:
        loads = {REF_DAY: 0.0}
        seeded = tl.fitness_fatigue(loads, REF_DAY, REF_DAY, seed_ctl=60.0)
        self.assertGreater(seeded[0].ctl, 55.0)

    def test_reversed_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            tl.fitness_fatigue({}, REF_DAY, REF_DAY - timedelta(days=1))


class TestAcwr(unittest.TestCase):
    def test_constant_load_gives_a_ratio_of_one(self) -> None:
        result = tl.acwr(constant_loads(days=60), REF_DAY)
        assert result.ratio is not None
        self.assertAlmostEqual(result.ratio, 1.0, places=6)
        self.assertEqual(result.zone, "sweet_spot")
        self.assertTrue(result.is_reliable)

    def test_a_spike_lands_in_the_danger_zone(self) -> None:
        loads = {REF_DAY - timedelta(days=offset): 20.0 for offset in range(28, 6, -1)}
        loads.update({REF_DAY - timedelta(days=offset): 120.0 for offset in range(7)})
        result = tl.acwr(loads, REF_DAY)
        assert result.ratio is not None
        self.assertGreater(result.ratio, 1.5)
        self.assertEqual(result.zone, "danger")

    def test_thin_history_is_flagged_unreliable(self) -> None:
        loads = {REF_DAY - timedelta(days=offset): 50.0 for offset in range(5)}
        result = tl.acwr(loads, REF_DAY)
        self.assertFalse(result.is_reliable)

    def test_no_chronic_load_yields_no_ratio(self) -> None:
        result = tl.acwr({}, REF_DAY)
        self.assertIsNone(result.ratio)
        self.assertEqual(result.zone, "unknown")

    def test_ewma_method_is_available(self) -> None:
        result = tl.acwr(constant_loads(days=60), REF_DAY, method="ewma")
        assert result.ratio is not None
        self.assertAlmostEqual(result.ratio, 1.0, delta=0.05)

    def test_invalid_windows_and_methods_raise(self) -> None:
        with self.assertRaises(ValueError):
            tl.acwr({}, REF_DAY, acute_days=28, chronic_days=7)
        with self.assertRaises(ValueError):
            tl.acwr({}, REF_DAY, method="bogus")


class TestMonotonyAndRamp(unittest.TestCase):
    def test_flat_week_has_undefined_monotony(self) -> None:
        # Zero dispersion: the metric is undefined, and must not be reported as 0.
        monotony, strain = tl.monotony_strain(constant_loads(days=14), REF_DAY)
        self.assertIsNone(monotony)
        self.assertIsNone(strain)

    def test_varied_week_has_lower_monotony_than_a_near_flat_one(self) -> None:
        varied = {REF_DAY - timedelta(days=offset): (100.0 if offset % 2 else 10.0) for offset in range(7)}
        near_flat = {REF_DAY - timedelta(days=offset): (55.0 if offset % 2 else 50.0) for offset in range(7)}
        varied_monotony, _ = tl.monotony_strain(varied, REF_DAY)
        flat_monotony, _ = tl.monotony_strain(near_flat, REF_DAY)
        assert varied_monotony is not None and flat_monotony is not None
        self.assertLess(varied_monotony, flat_monotony)

    def test_strain_is_weekly_load_times_monotony(self) -> None:
        loads = {REF_DAY - timedelta(days=offset): (100.0 if offset % 2 else 10.0) for offset in range(7)}
        monotony, strain = tl.monotony_strain(loads, REF_DAY)
        assert monotony is not None and strain is not None
        self.assertAlmostEqual(strain, sum(loads.values()) * monotony, delta=0.5)

    def test_ramp_percentage(self) -> None:
        loads = {REF_DAY - timedelta(days=offset): 100.0 for offset in range(7, 14)}
        loads.update({REF_DAY - timedelta(days=offset): 110.0 for offset in range(7)})
        self.assertAlmostEqual(tl.weekly_ramp_pct(loads, REF_DAY), 10.0, places=1)

    def test_ramp_from_zero_is_undefined_not_infinite(self) -> None:
        loads = {REF_DAY - timedelta(days=offset): 100.0 for offset in range(7)}
        self.assertIsNone(tl.weekly_ramp_pct(loads, REF_DAY))


if __name__ == "__main__":
    unittest.main()
