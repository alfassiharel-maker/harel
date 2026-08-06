"""Tests for the deterministic ranking layer. Standard library only."""

from __future__ import annotations

import unittest

from scout.scoring import DIMENSIONS, Score, ScoringError, rank, score_problem


def dimensions(**overrides: int | None) -> dict[str, int | None]:
    """A complete, mid-range dimension set with selective overrides."""
    base: dict[str, int | None] = {name: 3 for name in DIMENSIONS}
    base.update(overrides)
    return base


class ScoreProblemTests(unittest.TestCase):
    def test_best_possible_case_scores_100(self) -> None:
        """The anchor: maximal pain, cost, reach and potential on empty ground."""
        score = score_problem(
            dimensions(
                severity=5,
                cost=5,
                breadth=5,
                startup_potential=5,
                solution_maturity=0,
            )
        )
        assert score is not None
        self.assertEqual(100.0, score.value)

    def test_worst_possible_case_scores_0(self) -> None:
        score = score_problem(
            dimensions(
                severity=0,
                cost=0,
                breadth=0,
                startup_potential=0,
                solution_maturity=5,
            )
        )
        assert score is not None
        self.assertEqual(0.0, score.value)

    def test_hand_computed_value(self) -> None:
        """severity 5, cost 4, breadth 3, potential 2, maturity 1.

            severity           0.30 * 5/5 * 100 = 30.0
            cost               0.25 * 4/5 * 100 = 20.0
            breadth            0.20 * 3/5 * 100 = 12.0
            startup_potential  0.15 * 2/5 * 100 =  6.0
            solution_maturity  0.10 * (5-1)/5 * 100 =  8.0
                                                  ------
                                                   76.0
        """
        score = score_problem(
            dimensions(
                severity=5,
                cost=4,
                breadth=3,
                startup_potential=2,
                solution_maturity=1,
            )
        )
        assert score is not None
        self.assertEqual(76.0, score.value)

    def test_drivers_sum_to_the_score(self) -> None:
        score = score_problem(dimensions(severity=4, cost=2, solution_maturity=1))
        assert score is not None
        self.assertAlmostEqual(
            score.value, sum(driver.contribution for driver in score.drivers), places=2
        )

    def test_drivers_are_ordered_and_name_the_top_contributor(self) -> None:
        score = score_problem(
            dimensions(severity=1, cost=5, breadth=1, startup_potential=1, solution_maturity=5)
        )
        assert score is not None
        contributions = [driver.contribution for driver in score.drivers]
        self.assertEqual(sorted(contributions, reverse=True), contributions)
        self.assertEqual("cost", score.top_driver.dimension)

    def test_solution_maturity_is_inverted(self) -> None:
        """A crowded field must score below an empty one, all else equal."""
        crowded = score_problem(dimensions(solution_maturity=5))
        empty = score_problem(dimensions(solution_maturity=0))
        assert crowded is not None and empty is not None
        self.assertLess(crowded.value, empty.value)
        driver = next(d for d in empty.drivers if d.dimension == "solution_maturity")
        self.assertTrue(driver.inverted)
        self.assertEqual(0, driver.raw)
        self.assertEqual(5, driver.effective)


class UndefinedScoreTests(unittest.TestCase):
    """A candidate that cannot be scored yields ``None``, never a low score."""

    def test_missing_dimension_yields_none(self) -> None:
        for name in DIMENSIONS:
            with self.subTest(missing=name):
                incomplete = dimensions()
                del incomplete[name]
                self.assertIsNone(score_problem(incomplete))

    def test_explicit_none_dimension_yields_none(self) -> None:
        self.assertIsNone(score_problem(dimensions(cost=None)))

    def test_empty_input_yields_none(self) -> None:
        self.assertIsNone(score_problem({}))

    def test_out_of_range_value_raises(self) -> None:
        """Present but impossible means the producer is broken; do not clamp."""
        with self.assertRaises(ScoringError):
            score_problem(dimensions(severity=6))
        with self.assertRaises(ScoringError):
            score_problem(dimensions(severity=-1))

    def test_non_integer_value_raises(self) -> None:
        for bad in (3.5, "3", True):
            with self.subTest(value=bad):
                with self.assertRaises(ScoringError):
                    score_problem(dimensions(severity=bad))  # type: ignore[arg-type]


class RankTests(unittest.TestCase):
    def _score(self, value: float) -> Score:
        score = score_problem(dimensions(severity=5, cost=5, breadth=5, startup_potential=5, solution_maturity=0))
        assert score is not None
        return Score(value=value, drivers=score.drivers)

    def test_orders_best_first_and_unscorable_last(self) -> None:
        entries = [
            ("mid", self._score(50.0)),
            ("none", None),
            ("high", self._score(90.0)),
            ("low", self._score(10.0)),
        ]
        self.assertEqual(["high", "mid", "low", "none"], rank(entries))

    def test_ties_break_on_id_so_runs_are_reproducible(self) -> None:
        entries = [("b", self._score(50.0)), ("a", self._score(50.0))]
        self.assertEqual(["a", "b"], rank(entries))
        self.assertEqual(["a", "b"], rank(list(reversed(entries))))

    def test_empty_input(self) -> None:
        self.assertEqual([], rank([]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
