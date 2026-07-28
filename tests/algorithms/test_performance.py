from __future__ import annotations

import unittest
from datetime import timedelta

from backend.algorithms import performance

from ._fixtures import REF_DAY


class TestRiegel(unittest.TestCase):
    def test_doubling_the_distance_uses_the_fatigue_exponent(self) -> None:
        # 5k in 20:00 -> 10k = 1200 x 2^1.06
        value = performance.riegel_predict(1200.0, 5000.0, 10000.0)
        self.assertAlmostEqual(value, 1200.0 * 2**1.06, places=6)
        self.assertAlmostEqual(value, 2501.92, places=1)

    def test_the_same_distance_returns_the_same_time(self) -> None:
        self.assertAlmostEqual(performance.riegel_predict(1200.0, 5000.0, 5000.0), 1200.0)

    def test_invalid_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            performance.riegel_predict(0.0, 5000.0, 10000.0)

    def test_a_personal_exponent_is_recovered_from_consistent_bests(self) -> None:
        exponent = 1.08
        scale = 1200.0 / 5000.0**exponent
        bests = [(distance, scale * distance**exponent) for distance in (1500.0, 5000.0, 10000.0, 21097.5)]
        fitted, r_squared = performance.fit_riegel_exponent(bests)
        self.assertAlmostEqual(fitted, exponent, places=3)
        self.assertAlmostEqual(r_squared, 1.0, places=6)

    def test_too_few_bests_fall_back_to_the_population_default(self) -> None:
        fitted, r_squared = performance.fit_riegel_exponent([(5000.0, 1200.0)])
        self.assertEqual(fitted, performance.DEFAULT_RIEGEL_EXPONENT)
        self.assertEqual(r_squared, 0.0)

    def test_inconsistent_bests_are_clamped_to_a_plausible_range(self) -> None:
        # A fresh 5k against a stale marathon would otherwise fit a wild exponent.
        bests = [(1500.0, 300.0), (5000.0, 1200.0), (42195.0, 30000.0)]
        fitted, _ = performance.fit_riegel_exponent(bests)
        self.assertGreaterEqual(fitted, 1.00)
        self.assertLessEqual(fitted, 1.20)


class TestVdot(unittest.TestCase):
    def test_a_twenty_minute_5k_matches_the_published_table(self) -> None:
        # Daniels' tables put a 20:00 5k at roughly VDOT 49-50.
        vdot = performance.vdot_from_race(5000.0, 1200.0)
        self.assertAlmostEqual(vdot, 49.8, delta=0.3)

    def test_the_model_round_trips(self) -> None:
        vdot = performance.vdot_from_race(5000.0, 1200.0)
        self.assertAlmostEqual(performance.predict_time_from_vdot(vdot, 5000.0), 1200.0, delta=1.0)

    def test_a_faster_runner_has_a_higher_vdot(self) -> None:
        self.assertGreater(
            performance.vdot_from_race(5000.0, 1000.0),
            performance.vdot_from_race(5000.0, 1400.0),
        )

    def test_a_longer_distance_predicts_a_longer_time(self) -> None:
        vdot = performance.vdot_from_race(5000.0, 1200.0)
        self.assertGreater(
            performance.predict_time_from_vdot(vdot, 10000.0),
            performance.predict_time_from_vdot(vdot, 5000.0),
        )

    def test_invalid_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            performance.vdot_from_race(0.0, 1200.0)
        with self.assertRaises(ValueError):
            performance.predict_time_from_vdot(0.0, 5000.0)


class TestEquivalentTimes(unittest.TestCase):
    def test_both_models_are_reported_as_a_range(self) -> None:
        results = performance.equivalent_times(5000.0, 1200.0, targets=[10000.0])
        self.assertEqual(len(results), 1)
        result = results[0]
        assert result.low is not None and result.high is not None
        self.assertLessEqual(result.low, result.value)
        self.assertLessEqual(result.value, result.high)
        # Roughly 2x the 5k time for a 10k.
        self.assertAlmostEqual(result.value / 1200.0, 2.07, delta=0.1)

    def test_close_agreement_between_models_reads_as_high_confidence(self) -> None:
        results = performance.equivalent_times(5000.0, 1200.0, targets=[10000.0])
        self.assertGreater(results[0].confidence, 0.5)

    def test_standard_distances_are_covered_by_default(self) -> None:
        results = performance.equivalent_times(5000.0, 1200.0)
        self.assertEqual(len(results), len(performance.STANDARD_DISTANCES_M))


class TestCriticalModels(unittest.TestCase):
    def test_critical_speed_from_two_efforts(self) -> None:
        cs, d_prime = performance.critical_speed(1000.0, 200.0, 3000.0, 660.0)
        self.assertAlmostEqual(cs, 4.3478, places=3)
        self.assertAlmostEqual(d_prime, 130.4, places=1)

    def test_critical_power_from_two_efforts(self) -> None:
        cp, w_prime = performance.critical_power(300.0, 300.0, 250.0, 1200.0)
        self.assertAlmostEqual(cp, 233.3, places=1)
        self.assertAlmostEqual(w_prime, 20000.0, places=0)

    def test_identical_durations_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            performance.critical_speed(1000.0, 200.0, 3000.0, 200.0)
        with self.assertRaises(ValueError):
            performance.critical_power(300.0, 300.0, 250.0, 300.0)

    def test_ftp_is_ninety_five_percent_of_a_twenty_minute_effort(self) -> None:
        self.assertAlmostEqual(performance.ftp_from_20min_test(300.0), 285.0)

    def test_ftp_rejects_non_positive_power(self) -> None:
        with self.assertRaises(ValueError):
            performance.ftp_from_20min_test(0.0)


class TestProgressionForecast(unittest.TestCase):
    def _improving_history(self, noise: float = 0.0) -> list[tuple]:
        # 5k times falling by 2 s per week over 10 weeks.
        return [
            (REF_DAY - timedelta(weeks=10 - week), 1250.0 - 2.0 * week + (noise if week % 2 else -noise))
            for week in range(10)
        ]

    def test_a_clean_improving_trend_extrapolates_downward(self) -> None:
        forecast = performance.progression_forecast(self._improving_history(), 28, metric="time_5000m")
        assert forecast is not None
        self.assertLess(forecast.value, 1232.0)
        self.assertIn("improving", forecast.method)

    def test_the_interval_brackets_the_point_estimate(self) -> None:
        forecast = performance.progression_forecast(self._improving_history(noise=4.0), 28)
        assert forecast is not None and forecast.low is not None and forecast.high is not None
        self.assertLess(forecast.low, forecast.value)
        self.assertLess(forecast.value, forecast.high)

    def test_noisier_history_yields_a_wider_interval(self) -> None:
        clean = performance.progression_forecast(self._improving_history(noise=1.0), 28)
        noisy = performance.progression_forecast(self._improving_history(noise=15.0), 28)
        assert clean is not None and noisy is not None
        assert clean.high is not None and clean.low is not None
        assert noisy.high is not None and noisy.low is not None
        self.assertGreater(noisy.high - noisy.low, clean.high - clean.low)

    def test_noisier_history_lowers_confidence(self) -> None:
        clean = performance.progression_forecast(self._improving_history(noise=1.0), 28)
        noisy = performance.progression_forecast(self._improving_history(noise=25.0), 28)
        assert clean is not None and noisy is not None
        self.assertGreater(clean.confidence, noisy.confidence)

    def test_two_points_are_refused(self) -> None:
        history = [(REF_DAY - timedelta(days=14), 1250.0), (REF_DAY, 1240.0)]
        self.assertIsNone(performance.progression_forecast(history, 28))

    def test_a_declining_trend_is_labelled_as_such(self) -> None:
        history = [(REF_DAY - timedelta(weeks=6 - week), 1200.0 + 5.0 * week) for week in range(6)]
        forecast = performance.progression_forecast(history, 28)
        assert forecast is not None
        self.assertIn("declining", forecast.method)


class TestProbabilityOfBeating(unittest.TestCase):
    def test_an_easy_target_is_likely(self) -> None:
        forecast = performance.progression_forecast(
            [(REF_DAY - timedelta(weeks=8 - week), 1250.0 - 2.0 * week + (3.0 if week % 2 else -3.0)) for week in range(8)],
            28,
        )
        assert forecast is not None
        easy = performance.probability_of_beating(forecast.value + 60.0, forecast)
        hard = performance.probability_of_beating(forecast.value - 60.0, forecast)
        self.assertGreater(easy, 0.9)
        self.assertLess(hard, 0.1)

    def test_probabilities_stay_within_bounds(self) -> None:
        forecast = performance.progression_forecast(
            [(REF_DAY - timedelta(weeks=8 - week), 1250.0 - 2.0 * week + (3.0 if week % 2 else -3.0)) for week in range(8)],
            28,
        )
        assert forecast is not None
        for target in (0.0, 600.0, 1200.0, 5000.0):
            probability = performance.probability_of_beating(target, forecast)
            self.assertGreaterEqual(probability, 0.0)
            self.assertLessEqual(probability, 1.0)

    def test_direction_flips_for_higher_is_better_metrics(self) -> None:
        forecast = performance.progression_forecast(
            [(REF_DAY - timedelta(weeks=8 - week), 240.0 + 2.0 * week + (3.0 if week % 2 else -3.0)) for week in range(8)],
            28,
            lower_is_better=False,
            metric="ftp",
            unit="W",
        )
        assert forecast is not None
        lower = performance.probability_of_beating(forecast.value - 30.0, forecast, lower_is_better=False)
        self.assertGreater(lower, 0.9)


class TestTriathlon(unittest.TestCase):
    def test_segments_and_transitions_sum(self) -> None:
        result = performance.triathlon_prediction(1500.0, 7200.0, 3600.0)
        self.assertAlmostEqual(result.value, 1500.0 + 7200.0 + 3600.0 + 240.0)

    def test_the_transition_allowance_is_explicit(self) -> None:
        result = performance.triathlon_prediction(1500.0, 7200.0, 3600.0, transition_allowance_s=0.0)
        self.assertAlmostEqual(result.value, 12300.0)


if __name__ == "__main__":
    unittest.main()
