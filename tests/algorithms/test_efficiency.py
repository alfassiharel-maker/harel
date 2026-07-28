from __future__ import annotations

import unittest

from backend.algorithms import efficiency
from backend.algorithms.types import HalfSplit

from ._fixtures import bike_activity, profile, run_activity, swim_activity


class TestPrimitives(unittest.TestCase):
    def test_running_efficiency_index(self) -> None:
        # 10 km in 50 min = 200 m/min; at 150 bpm -> 1.3333 m/min/bpm
        value = efficiency.running_efficiency_index(10000.0, 3000.0, 150.0)
        assert value is not None
        self.assertAlmostEqual(value, 1.33333, places=5)

    def test_cycling_efficiency_factor(self) -> None:
        value = efficiency.cycling_efficiency_factor(250.0, 150.0)
        assert value is not None
        self.assertAlmostEqual(value, 1.66667, places=5)

    def test_swolf_is_time_plus_strokes(self) -> None:
        self.assertAlmostEqual(efficiency.swolf(30.0, 20.0), 50.0)

    def test_stroke_index(self) -> None:
        self.assertAlmostEqual(efficiency.stroke_index(1.5, 2.0), 3.0)

    def test_invalid_inputs_return_none_rather_than_raising(self) -> None:
        self.assertIsNone(efficiency.running_efficiency_index(0.0, 3000.0, 150.0))
        self.assertIsNone(efficiency.cycling_efficiency_factor(250.0, 0.0))
        self.assertIsNone(efficiency.swolf(0.0, 20.0))
        self.assertIsNone(efficiency.stroke_index(1.5, 0.0))


class TestDecoupling(unittest.TestCase):
    def test_hr_drift_at_constant_power_is_positive_decoupling(self) -> None:
        first = HalfSplit(avg_hr=140.0, avg_power=200.0)
        second = HalfSplit(avg_hr=150.0, avg_power=200.0)
        value = efficiency.decoupling_pct(first, second)
        assert value is not None
        self.assertAlmostEqual(value, 6.67, places=2)
        self.assertGreater(value, efficiency.GOOD_DECOUPLING_THRESHOLD_PCT)

    def test_a_steady_effort_shows_no_decoupling(self) -> None:
        half = HalfSplit(avg_hr=145.0, avg_power=210.0)
        self.assertAlmostEqual(efficiency.decoupling_pct(half, half), 0.0)

    def test_falls_back_to_speed_when_power_is_absent(self) -> None:
        first = HalfSplit(avg_hr=140.0, avg_speed_m_s=3.5)
        second = HalfSplit(avg_hr=147.0, avg_speed_m_s=3.5)
        value = efficiency.decoupling_pct(first, second)
        assert value is not None
        self.assertGreater(value, 0.0)

    def test_missing_heart_rate_makes_decoupling_undefined(self) -> None:
        self.assertIsNone(
            efficiency.decoupling_pct(HalfSplit(avg_power=200.0), HalfSplit(avg_power=200.0))
        )


class TestOneRepMax(unittest.TestCase):
    def test_both_formulas_are_returned(self) -> None:
        result = efficiency.estimated_1rm(100.0, 5)
        self.assertAlmostEqual(result["epley"], 116.7, places=1)
        self.assertAlmostEqual(result["brzycki"], 112.5, places=1)
        self.assertAlmostEqual(result["mean"], 114.6, places=1)
        self.assertEqual(result["reliable"], 1.0)

    def test_a_single_rep_is_the_max_itself(self) -> None:
        result = efficiency.estimated_1rm(120.0, 1)
        self.assertEqual(result["epley"], 120.0)
        self.assertEqual(result["brzycki"], 120.0)

    def test_high_rep_sets_are_flagged_unreliable(self) -> None:
        self.assertEqual(efficiency.estimated_1rm(60.0, 20)["reliable"], 0.0)

    def test_invalid_input_raises(self) -> None:
        with self.assertRaises(ValueError):
            efficiency.estimated_1rm(0.0, 5)
        with self.assertRaises(ValueError):
            efficiency.estimated_1rm(100.0, 0)


class TestBaselineComparison(unittest.TestCase):
    def test_improvement_is_positive_for_higher_is_better_metrics(self) -> None:
        result = efficiency.compare_to_baseline("ei", 1.10, [1.00, 1.00, 1.00])
        self.assertAlmostEqual(result.delta_pct, 10.0)
        self.assertTrue(result.is_improvement)

    def test_the_spec_example_reads_as_eight_percent_worse(self) -> None:
        """"8% less efficient than your average" must come out as -8."""
        result = efficiency.compare_to_baseline("ei", 0.92, [1.00, 1.00, 1.00])
        self.assertAlmostEqual(result.delta_pct, -8.0)
        self.assertFalse(result.is_improvement)

    def test_lower_is_better_metrics_have_their_sign_flipped(self) -> None:
        # SWOLF 40 against a baseline of 45 is an improvement.
        result = efficiency.compare_to_baseline("swolf", 40.0, [45.0], higher_is_better=False)
        self.assertGreater(result.delta_pct, 0.0)
        self.assertTrue(result.is_improvement)

    def test_no_baseline_yields_no_delta(self) -> None:
        result = efficiency.compare_to_baseline("ei", 1.10, [])
        self.assertIsNone(result.delta_pct)
        self.assertIsNone(result.is_improvement)


class TestSessionDispatch(unittest.TestCase):
    def test_run_produces_an_efficiency_index(self) -> None:
        results = efficiency.session_efficiency(run_activity(), profile(), [1.4, 1.42, 1.38])
        metrics = {result.metric for result in results}
        self.assertIn("running_efficiency_index", metrics)

    def test_bike_produces_an_efficiency_factor(self) -> None:
        results = efficiency.session_efficiency(bike_activity(), profile(), [1.6])
        metrics = {result.metric for result in results}
        self.assertIn("cycling_efficiency_factor", metrics)

    def test_swim_produces_stroke_index_and_swolf(self) -> None:
        results = efficiency.session_efficiency(swim_activity(), profile())
        metrics = {result.metric for result in results}
        self.assertIn("stroke_index", metrics)
        self.assertIn("swolf", metrics)

    def test_decoupling_is_added_when_halves_are_present(self) -> None:
        activity = run_activity(
            first_half=HalfSplit(avg_hr=145.0, avg_speed_m_s=3.9),
            second_half=HalfSplit(avg_hr=155.0, avg_speed_m_s=3.9),
        )
        results = efficiency.session_efficiency(activity, profile())
        metrics = {result.metric for result in results}
        self.assertIn("aerobic_decoupling", metrics)

    def test_a_session_with_no_usable_data_yields_nothing(self) -> None:
        activity = run_activity(avg_hr=None, distance_m=None)
        self.assertEqual(efficiency.session_efficiency(activity, profile()), [])


if __name__ == "__main__":
    unittest.main()
