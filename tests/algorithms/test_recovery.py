from __future__ import annotations

import unittest
from datetime import timedelta

from backend.algorithms import recovery
from backend.algorithms.types import DailyWellness, ReadinessBand

from ._fixtures import REF_DAY, constant_loads, profile, steady_wellness


def _baseline_hrv_mean(history: list[DailyWellness]) -> float:
    window = [
        entry.hrv_rmssd_ms
        for entry in history
        if entry.hrv_rmssd_ms and REF_DAY - timedelta(days=28) <= entry.day < REF_DAY
    ]
    return sum(window) / len(window)


def _today(**overrides) -> DailyWellness:
    defaults = dict(
        day=REF_DAY,
        hrv_rmssd_ms=60.0,
        resting_hr=50.0,
        sleep_total_min=480.0,
        sleep_deep_min=105.0,
        sleep_rem_min=115.0,
        sleep_efficiency_pct=92.0,
        soreness=2,
        mood=2,
        stress=2,
        fatigue=2,
    )
    defaults.update(overrides)
    return DailyWellness(**defaults)


class TestReadinessComposite(unittest.TestCase):
    def test_a_normal_day_scores_in_the_upper_bands(self) -> None:
        history = steady_wellness(40)
        history.append(_today(hrv_rmssd_ms=_baseline_hrv_mean(history)))
        result = recovery.readiness(profile(), history, constant_loads(), REF_DAY)
        self.assertGreaterEqual(result.score, 60.0)
        self.assertEqual(result.data_quality, 1.0)

    def test_a_bad_day_scores_far_below_a_good_one(self) -> None:
        history = steady_wellness(40)
        good = list(history) + [_today(hrv_rmssd_ms=_baseline_hrv_mean(history))]
        bad = list(history) + [
            _today(
                hrv_rmssd_ms=38.0,
                resting_hr=60.0,
                sleep_total_min=300.0,
                sleep_deep_min=40.0,
                sleep_rem_min=45.0,
                sleep_efficiency_pct=74.0,
                soreness=4,
                mood=4,
                stress=5,
                fatigue=5,
            )
        ]
        good_result = recovery.readiness(profile(), good, constant_loads(), REF_DAY)
        bad_result = recovery.readiness(profile(), bad, constant_loads(), REF_DAY)
        self.assertGreater(good_result.score - bad_result.score, 25.0)
        self.assertLess(bad_result.score, 50.0)

    def test_driver_contributions_sum_to_the_score_offset_from_neutral(self) -> None:
        """The decomposition must be arithmetically exact, or the AI layer's
        explanation will not match the number shown to the athlete."""
        history = steady_wellness(40)
        history.append(_today(hrv_rmssd_ms=45.0, sleep_total_min=360.0))
        result = recovery.readiness(profile(), history, constant_loads(), REF_DAY)
        total = sum(driver.contribution for driver in result.drivers)
        self.assertAlmostEqual(total, result.score - 50.0, places=1)

    def test_drivers_are_ordered_by_influence(self) -> None:
        history = steady_wellness(40)
        history.append(_today(hrv_rmssd_ms=40.0))
        result = recovery.readiness(profile(), history, constant_loads(), REF_DAY)
        magnitudes = [abs(driver.contribution) for driver in result.drivers]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))
        self.assertEqual(result.drivers[0].name, "hrv")


class TestSmallestWorthwhileChange(unittest.TestCase):
    def test_an_hrv_move_inside_the_noise_band_is_reported_as_neutral(self) -> None:
        history = steady_wellness(40)
        history.append(_today(hrv_rmssd_ms=_baseline_hrv_mean(history)))
        result = recovery.readiness(profile(), history, constant_loads(), REF_DAY)
        hrv_driver = next(d for d in result.drivers if d.name == "hrv")
        self.assertAlmostEqual(hrv_driver.score, 50.0, places=6)

    def test_an_hrv_move_beyond_the_noise_band_moves_the_score(self) -> None:
        history = steady_wellness(40)
        history.append(_today(hrv_rmssd_ms=_baseline_hrv_mean(history) * 0.75))
        result = recovery.readiness(profile(), history, constant_loads(), REF_DAY)
        hrv_driver = next(d for d in result.drivers if d.name == "hrv")
        self.assertLess(hrv_driver.score, 25.0)


class TestMissingData(unittest.TestCase):
    def test_no_data_at_all_is_neutral_with_zero_quality(self) -> None:
        result = recovery.readiness(profile(), [], {}, REF_DAY)
        self.assertEqual(result.score, 50.0)
        self.assertEqual(result.data_quality, 0.0)
        self.assertEqual(result.drivers, ())
        self.assertIs(result.band, ReadinessBand.MODERATE)

    def test_weights_renormalise_over_available_components(self) -> None:
        history = steady_wellness(40)
        history.append(
            DailyWellness(day=REF_DAY, hrv_rmssd_ms=_baseline_hrv_mean(history))
        )
        result = recovery.readiness(profile(), history, {}, REF_DAY)
        # HRV only: 0.30 of the model is present.
        self.assertAlmostEqual(result.data_quality, 0.30)
        self.assertAlmostEqual(result.drivers[0].weight, 1.0)

    def test_a_missing_input_is_not_treated_as_a_bad_input(self) -> None:
        history = steady_wellness(40)
        full = list(history) + [_today(hrv_rmssd_ms=_baseline_hrv_mean(history))]
        without_sleep = list(history) + [
            _today(
                hrv_rmssd_ms=_baseline_hrv_mean(history),
                sleep_total_min=None,
                sleep_deep_min=None,
                sleep_rem_min=None,
                sleep_efficiency_pct=None,
            )
        ]
        full_result = recovery.readiness(profile(), full, constant_loads(), REF_DAY)
        partial_result = recovery.readiness(profile(), without_sleep, constant_loads(), REF_DAY)
        self.assertLess(partial_result.data_quality, full_result.data_quality)
        # Dropping a healthy sleep reading must not crater the score.
        self.assertGreater(partial_result.score, full_result.score - 15.0)

    def test_short_baselines_are_refused_rather_than_guessed(self) -> None:
        history = steady_wellness(3)
        history.append(_today())
        result = recovery.readiness(profile(), history, {}, REF_DAY)
        names = {driver.name for driver in result.drivers}
        self.assertNotIn("hrv", names)
        self.assertNotIn("resting_hr", names)


class TestLoadComponent(unittest.TestCase):
    def test_deep_fatigue_lowers_the_load_component(self) -> None:
        history = steady_wellness(40)
        history.append(_today(hrv_rmssd_ms=_baseline_hrv_mean(history)))
        fresh = {REF_DAY - timedelta(days=offset): 20.0 for offset in range(60)}
        buried = {REF_DAY - timedelta(days=offset): 20.0 for offset in range(60, 14, -1)}
        buried.update({REF_DAY - timedelta(days=offset): 200.0 for offset in range(14)})
        fresh_result = recovery.readiness(profile(), history, fresh, REF_DAY)
        buried_result = recovery.readiness(profile(), history, buried, REF_DAY)
        fresh_load = next(d for d in fresh_result.drivers if d.name == "training_load")
        buried_load = next(d for d in buried_result.drivers if d.name == "training_load")
        self.assertGreater(fresh_load.score, buried_load.score)
        self.assertGreater(fresh_result.score, buried_result.score)


class TestBands(unittest.TestCase):
    def test_band_thresholds(self) -> None:
        cases = [
            (10.0, ReadinessBand.COMPROMISED),
            (30.0, ReadinessBand.LIMITED),
            (60.0, ReadinessBand.MODERATE),
            (75.0, ReadinessBand.GOOD),
            (95.0, ReadinessBand.PRIME),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertIs(recovery._band(score), expected)

    def test_weights_sum_to_one(self) -> None:
        self.assertAlmostEqual(sum(recovery.COMPONENT_WEIGHTS.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
