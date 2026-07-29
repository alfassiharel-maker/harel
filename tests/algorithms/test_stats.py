from __future__ import annotations

import math
import unittest

from backend.algorithms import stats


class TestDescriptives(unittest.TestCase):
    def test_mean_and_sample_stdev(self) -> None:
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        self.assertAlmostEqual(stats.mean(values), 5.0)
        # Sample (n-1) standard deviation, not population.
        self.assertAlmostEqual(stats.stdev(values), 2.13809, places=4)

    def test_stdev_of_single_point_is_zero_not_an_error(self) -> None:
        self.assertEqual(stats.stdev([5.0]), 0.0)

    def test_empty_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            stats.mean([])
        with self.assertRaises(ValueError):
            stats.median([])

    def test_median_even_and_odd(self) -> None:
        self.assertEqual(stats.median([3.0, 1.0, 2.0]), 2.0)
        self.assertEqual(stats.median([4.0, 1.0, 3.0, 2.0]), 2.5)

    def test_mad_is_scaled_to_estimate_sigma(self) -> None:
        # Median 3, absolute deviations [2,1,0,1,2] -> median 1 -> 1.4826 x 1.
        self.assertAlmostEqual(stats.mad([1.0, 2.0, 3.0, 4.0, 5.0]), 1.4826, places=4)

    def test_percentile_interpolates(self) -> None:
        self.assertAlmostEqual(stats.percentile([1.0, 2.0, 3.0, 4.0], 50.0), 2.5)
        self.assertAlmostEqual(stats.percentile([1.0, 2.0, 3.0, 4.0], 0.0), 1.0)
        self.assertAlmostEqual(stats.percentile([1.0, 2.0, 3.0, 4.0], 100.0), 4.0)

    def test_percentile_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            stats.percentile([1.0, 2.0], 101.0)


class TestStandardScores(unittest.TestCase):
    def test_z_score_basic(self) -> None:
        baseline = [10.0, 12.0, 14.0, 16.0, 18.0]
        # mean 14, sample sd 3.1623
        self.assertAlmostEqual(stats.z_score(20.0, baseline), 1.8974, places=4)

    def test_z_score_returns_none_on_flat_baseline(self) -> None:
        # A baseline with no dispersion is missing information, not neutral.
        self.assertIsNone(stats.z_score(10.0, [5.0, 5.0, 5.0]))

    def test_z_score_returns_none_on_insufficient_baseline(self) -> None:
        self.assertIsNone(stats.z_score(10.0, [5.0]))

    def test_robust_z_score_ignores_a_single_outlier(self) -> None:
        clean = [50.0, 51.0, 49.0, 50.0, 51.0, 49.0, 50.0]
        polluted = [*clean, 200.0]
        plain = stats.z_score(45.0, polluted)
        robust = stats.robust_z_score(45.0, polluted)
        assert plain is not None and robust is not None
        # The outlier inflates the SD, so the plain score understates the drop.
        self.assertLess(abs(plain), abs(robust))

    def test_swc_is_half_the_cv(self) -> None:
        values = [60.0, 62.0, 58.0, 61.0, 59.0]
        self.assertAlmostEqual(
            stats.smallest_worthwhile_change(values),
            0.5 * stats.coefficient_of_variation(values),
        )


class TestSeries(unittest.TestCase):
    def test_ewma_uses_impulse_response_alpha(self) -> None:
        # alpha = 1 - exp(-1/7); one unit impulse from zero lands on alpha.
        series = stats.ewma([1.0], 7.0)
        self.assertAlmostEqual(series[0], 1.0 - math.exp(-1.0 / 7.0), places=10)

    def test_ewma_converges_to_a_constant_input(self) -> None:
        # Residual after n days is value x exp(-n / tc): 50 x exp(-400/42) ~ 0.0037,
        # so the tolerance is set by the decay, not chosen arbitrarily.
        series = stats.ewma([50.0] * 400, 42.0)
        expected_residual = 50.0 * math.exp(-400.0 / 42.0)
        self.assertAlmostEqual(series[-1], 50.0 - expected_residual, places=9)
        self.assertLess(50.0 - series[-1], 0.01)

    def test_ewma_seed_is_respected(self) -> None:
        seeded = stats.ewma([0.0], 7.0, seed=100.0)
        # With zero input the seed decays rather than resetting.
        self.assertAlmostEqual(seeded[0], 100.0 * math.exp(-1.0 / 7.0), places=6)

    def test_ewma_rejects_non_positive_time_constant(self) -> None:
        with self.assertRaises(ValueError):
            stats.ewma([1.0], 0.0)

    def test_rolling_sum_uses_partial_leading_windows(self) -> None:
        self.assertEqual(stats.rolling_sum([1.0, 2.0, 3.0, 4.0], 2), [1.0, 3.0, 5.0, 7.0])

    def test_rolling_mean_uses_partial_leading_windows(self) -> None:
        self.assertEqual(stats.rolling_mean([1.0, 2.0, 3.0], 2), [1.0, 1.5, 2.5])


class TestRegression(unittest.TestCase):
    def test_perfect_line_is_recovered_exactly(self) -> None:
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [1.0, 3.0, 5.0, 7.0, 9.0]
        fit = stats.linear_regression(xs, ys)
        self.assertAlmostEqual(fit.slope, 2.0)
        self.assertAlmostEqual(fit.intercept, 1.0)
        self.assertAlmostEqual(fit.r_squared, 1.0)
        self.assertAlmostEqual(fit.residual_sd, 0.0, places=9)
        self.assertAlmostEqual(fit.predict(10.0), 21.0)

    def test_weights_pull_the_fit_toward_recent_points(self) -> None:
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [0.0, 1.0, 2.0, 10.0]
        unweighted = stats.linear_regression(xs, ys)
        weighted = stats.linear_regression(xs, ys, [0.1, 0.1, 0.1, 5.0])
        self.assertGreater(weighted.slope, unweighted.slope)

    def test_prediction_sd_widens_with_extrapolation_distance(self) -> None:
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [1.0, 2.9, 5.2, 6.8, 9.1]
        fit = stats.linear_regression(xs, ys)
        self.assertLess(fit.prediction_sd(2.0), fit.prediction_sd(50.0))

    def test_regression_input_validation(self) -> None:
        with self.assertRaises(ValueError):
            stats.linear_regression([1.0], [1.0])
        with self.assertRaises(ValueError):
            stats.linear_regression([1.0, 2.0], [1.0])


class TestNormal(unittest.TestCase):
    def test_cdf_anchors(self) -> None:
        self.assertAlmostEqual(stats.normal_cdf(0.0), 0.5)
        self.assertAlmostEqual(stats.normal_cdf(1.96), 0.975, places=3)

    def test_probability_below_is_directional(self) -> None:
        # A target 2 SD faster than the forecast is unlikely.
        self.assertLess(stats.probability_below(80.0, 100.0, 10.0), 0.05)

    def test_zero_sigma_is_a_step_function(self) -> None:
        self.assertEqual(stats.normal_cdf(5.0, 1.0, 0.0), 1.0)
        self.assertEqual(stats.normal_cdf(-5.0, 1.0, 0.0), 0.0)

    def test_clamp(self) -> None:
        self.assertEqual(stats.clamp(5.0, 0.0, 1.0), 1.0)
        self.assertEqual(stats.clamp(-5.0, 0.0, 1.0), 0.0)
        self.assertEqual(stats.clamp(0.5, 0.0, 1.0), 0.5)


if __name__ == "__main__":
    unittest.main()
