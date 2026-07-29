from __future__ import annotations

import math
import unittest
from datetime import timedelta

from backend.algorithms import injury_risk
from backend.algorithms.types import RiskBand

from ._fixtures import REF_DAY, constant_loads, profile, spiking_loads, steady_wellness


class TestBaseRate(unittest.TestCase):
    def test_an_athlete_with_no_risk_patterns_sits_near_the_base_rate(self) -> None:
        result = injury_risk.assess(profile(), {}, [], REF_DAY)
        # Intercept -2.60 plus the training-age protection (-0.40) -> logit -3.0.
        self.assertAlmostEqual(result.probability, 1.0 / (1.0 + math.exp(3.0)), places=4)
        self.assertIs(result.band, RiskBand.LOW)

    def test_steady_training_stays_low_risk(self) -> None:
        result = injury_risk.assess(
            profile(), constant_loads(daily=50.0, days=90), steady_wellness(60), REF_DAY
        )
        self.assertLess(result.probability, 0.15)


class TestRiskPatterns(unittest.TestCase):
    def test_a_load_spike_raises_risk_substantially(self) -> None:
        steady = injury_risk.assess(
            profile(), constant_loads(daily=50.0, days=90), steady_wellness(60), REF_DAY
        )
        spiked = injury_risk.assess(profile(), spiking_loads(), steady_wellness(60), REF_DAY)
        self.assertGreater(spiked.probability, steady.probability * 2.0)
        self.assertIn(spiked.band, (RiskBand.HIGH, RiskBand.VERY_HIGH))

    def test_prior_injury_is_the_single_largest_term(self) -> None:
        self.assertEqual(
            max(injury_risk.COEFFICIENTS, key=lambda name: injury_risk.COEFFICIENTS[name]),
            "prior_injury",
        )
        without = injury_risk.assess(profile(injuries_last_12m=0), {}, [], REF_DAY)
        with_injury = injury_risk.assess(profile(injuries_last_12m=1), {}, [], REF_DAY)
        self.assertGreater(with_injury.probability, without.probability)

    def test_training_age_is_protective(self) -> None:
        novice = injury_risk.assess(profile(training_age_years=0.0), {}, [], REF_DAY)
        veteran = injury_risk.assess(profile(training_age_years=10.0), {}, [], REF_DAY)
        self.assertLess(veteran.probability, novice.probability)

    def test_sleep_debt_raises_risk(self) -> None:
        rested = steady_wellness(30, sleep_min=480.0)
        deprived = steady_wellness(30, sleep_min=330.0)
        loads = constant_loads(daily=50.0, days=90)
        self.assertGreater(
            injury_risk.assess(profile(), loads, deprived, REF_DAY).probability,
            injury_risk.assess(profile(), loads, rested, REF_DAY).probability,
        )

    def test_a_short_layoff_shows_as_low_chronic_load(self) -> None:
        detrained = {REF_DAY - timedelta(days=offset): 5.0 for offset in range(60)}
        features = injury_risk.extract_features(profile(), detrained, [], REF_DAY)
        self.assertGreater(features["low_chronic_load"], 0.5)


class TestFeatureHygiene(unittest.TestCase):
    def test_every_declared_coefficient_has_a_feature(self) -> None:
        features = injury_risk.extract_features(profile(), spiking_loads(), steady_wellness(60), REF_DAY)
        self.assertEqual(set(features), set(injury_risk.COEFFICIENTS))

    def test_features_are_capped_so_one_input_cannot_saturate_the_model(self) -> None:
        absurd = {REF_DAY - timedelta(days=offset): 5.0 for offset in range(60, 7, -1)}
        absurd.update({REF_DAY - timedelta(days=offset): 5000.0 for offset in range(7)})
        features = injury_risk.extract_features(profile(), absurd, [], REF_DAY)
        for name, value in features.items():
            with self.subTest(feature=name):
                self.assertLessEqual(value, 3.0)

    def test_missing_inputs_contribute_zero(self) -> None:
        features = injury_risk.extract_features(profile(training_age_years=None, age=None), {}, [], REF_DAY)
        self.assertEqual(features["acwr_excess"], 0.0)
        self.assertEqual(features["sleep_debt_h"], 0.0)
        self.assertEqual(features["training_age"], 0.0)
        self.assertEqual(features["age_over_40"], 0.0)


class TestAttribution(unittest.TestCase):
    def test_driver_contributions_reconstruct_the_logit_exactly(self) -> None:
        """Log-odds are additive; probability points are not. The decomposition
        must reconstruct the logit or the explanation is fiction."""
        loads = spiking_loads()
        wellness = steady_wellness(60)
        athlete = profile(injuries_last_12m=1)
        result = injury_risk.assess(athlete, loads, wellness, REF_DAY)
        logit = injury_risk.INTERCEPT + sum(driver.contribution for driver in result.drivers)
        rebuilt = 1.0 / (1.0 + math.exp(-logit))
        self.assertAlmostEqual(rebuilt, result.probability, places=3)

    def test_drivers_are_ordered_by_magnitude(self) -> None:
        result = injury_risk.assess(
            profile(injuries_last_12m=1), spiking_loads(), steady_wellness(60), REF_DAY
        )
        magnitudes = [abs(driver.contribution) for driver in result.drivers]
        self.assertEqual(magnitudes, sorted(magnitudes, reverse=True))

    def test_zero_valued_features_are_omitted_from_the_explanation(self) -> None:
        result = injury_risk.assess(profile(), {}, [], REF_DAY)
        for driver in result.drivers:
            self.assertNotEqual(driver.contribution, 0.0)


class TestHonesty(unittest.TestCase):
    def test_the_result_declares_itself_unvalidated(self) -> None:
        """Non-negotiable: this is a heuristic index, and the API contract
        requires it to say so."""
        result = injury_risk.assess(profile(), constant_loads(), steady_wellness(), REF_DAY)
        self.assertFalse(result.is_clinically_validated)
        self.assertEqual(result.model_version, "heuristic-v0")

    def test_probability_is_always_a_valid_probability(self) -> None:
        scenarios = [
            (profile(), {}, []),
            (
                profile(injuries_last_12m=3, training_age_years=0.0, age=55),
                spiking_loads(),
                steady_wellness(60, sleep_min=300.0),
            ),
            (profile(), constant_loads(daily=500.0, days=90), steady_wellness(60)),
        ]
        for athlete, loads, wellness in scenarios:
            with self.subTest(athlete=athlete.injuries_last_12m):
                probability = injury_risk.assess(athlete, loads, wellness, REF_DAY).probability
                self.assertGreaterEqual(probability, 0.0)
                self.assertLessEqual(probability, 1.0)

    def test_predict_is_a_pure_function_of_the_feature_vector(self) -> None:
        features = {name: 0.0 for name in injury_risk.COEFFICIENTS}
        first = injury_risk.predict(features)
        second = injury_risk.predict(dict(features))
        self.assertEqual(first, second)
        self.assertAlmostEqual(first, 1.0 / (1.0 + math.exp(-injury_risk.INTERCEPT)), places=9)

    def test_predict_tolerates_an_unknown_feature_set(self) -> None:
        # The seam a trained model plugs into must not explode on a partial dict.
        self.assertGreater(injury_risk.predict({}), 0.0)


if __name__ == "__main__":
    unittest.main()
