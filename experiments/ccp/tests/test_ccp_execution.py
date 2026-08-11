"""Tests for the CCP execution experiment.

Runs with no dependencies installed:

    cd experiments/ccp && python3 -m unittest discover -s tests -t .

The tests assert on operation counts and bit-exact correctness only, never on
wall-clock time — a timing assertion would be flaky, and a flaky experiment is
worse than none.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ccp_execution_experiment as ex  # noqa: E402


class TestDeterminism(unittest.TestCase):
    """Same seed -> identical data, or the experiment is not reproducible."""

    def test_operator_is_deterministic(self) -> None:
        a1 = ex.make_operator(16, 24, seed=5)
        a2 = ex.make_operator(16, 24, seed=5)
        self.assertEqual(a1, a2)

    def test_different_seed_differs(self) -> None:
        a1 = ex.make_operator(16, 24, seed=5)
        a2 = ex.make_operator(16, 24, seed=6)
        self.assertNotEqual(a1, a2)

    def test_delta_positions_are_distinct_and_counted(self) -> None:
        base = ex.make_base_input(64, seed=3)
        delta = ex.make_delta(64, 10, base, seed=9)
        self.assertEqual(len(delta), 10)
        self.assertTrue(all(v != 0 for v in delta.values()))
        self.assertTrue(all(0 <= p < 64 for p in delta))


class TestIncrementalIdentity(unittest.TestCase):
    """The anchor case: y0 + A*delta must equal A*(x0+delta), exactly."""

    def test_matches_full_recompute(self) -> None:
        a = ex.make_operator(20, 32, seed=11)
        x0 = ex.make_base_input(32, seed=12)
        y0, _ = ex.matvec_full(a, x0)
        for k in (1, 3, 8, 32):
            delta = ex.make_delta(32, k, x0, seed=100 + k)
            xi = ex.apply_delta(x0, delta)
            full, _ = ex.matvec_full(a, xi)
            inc, _ = ex.matvec_incremental(a, y0, delta)
            self.assertEqual(full, inc, f"identity failed at k={k}")

    def test_empty_delta_reproduces_base(self) -> None:
        # The undefined/degenerate case: no change at all. The incremental result
        # must be exactly the base result, at zero extra multiply-adds.
        a = ex.make_operator(12, 16, seed=1)
        x0 = ex.make_base_input(16, seed=2)
        y0, _ = ex.matvec_full(a, x0)
        inc, madds = ex.matvec_incremental(a, y0, {})
        self.assertEqual(inc, y0)
        self.assertEqual(madds, 0)


class TestHandComputedCost(unittest.TestCase):
    """One value worked out by hand, against both the counter and the model."""

    def test_known_operation_counts(self) -> None:
        # A 3x2 operator, one base input, two derived inputs each changing k=1
        # position. Full: 2 inputs * (3*2) = 12 madds. CCP: base 3*2=6, plus
        # 2 inputs * (k=1 * m=3) = 6, total 12 — deliberately at parity so the
        # arithmetic is checkable, not flattering.
        m, n, reuse, k = 3, 2, 2, 1
        self.assertEqual(ex.model_full_madds(m, n, reuse), 12)
        self.assertEqual(ex.model_ccp_madds(m, n, reuse, k), 12)

    def test_counter_matches_model(self) -> None:
        a = ex.make_operator(3, 2, seed=4)
        x0 = ex.make_base_input(2, seed=5)
        y0, base_ops = ex.matvec_full(a, x0)
        self.assertEqual(base_ops, 6)
        delta = ex.make_delta(2, 1, x0, seed=6)
        _, inc_ops = ex.matvec_incremental(a, y0, delta)
        self.assertEqual(inc_ops, 3)  # k=1 * m=3

    def test_sparse_delta_saves_operations(self) -> None:
        # 1 base position changed out of 128, reused 64 times: CCP must do far
        # fewer madds than full recomputation.
        m, n, reuse, k = 128, 128, 64, 1
        full = ex.model_full_madds(m, n, reuse)
        ccp = ex.model_ccp_madds(m, n, reuse, k)
        self.assertLess(ccp, full)
        self.assertGreater(full / ccp, 4.0)


class TestControls(unittest.TestCase):
    """The controls the hypothesis must survive — and the one it must not."""

    def test_dense_delta_does_not_win(self) -> None:
        t = ex.run_trial(m=24, n=24, reuse=16, k=24, seed=7, reps=1, label="dense")
        self.assertTrue(t.correct)
        self.assertGreaterEqual(t.ops_ccp, t.ops_full)
        self.assertEqual(t.verdict, "NO SAVING")

    def test_no_reuse_does_not_win(self) -> None:
        t = ex.run_trial(m=32, n=32, reuse=1, k=1, seed=8, reps=1, label="single")
        self.assertTrue(t.correct)
        # One base matvec (m*n) plus one k*m update always exceeds a single m*n.
        self.assertGreaterEqual(t.ops_ccp, t.ops_full)

    def test_nonlinearity_breaks_the_identity(self) -> None:
        # This is a control that must FAIL correctness: a ReLU-like clamp after
        # the linear map makes base+delta != full recompute. The scope of the
        # exact contract is the linear operator, and this proves it.
        t = ex.run_trial(
            m=32, n=32, reuse=16, k=4, seed=9, reps=1, label="nl", nonlinear=True
        )
        self.assertFalse(t.correct)
        self.assertEqual(t.verdict, "IDENTITY BROKEN")

    def test_sparse_linear_is_correct_and_fewer_ops(self) -> None:
        t = ex.run_trial(m=64, n=64, reuse=32, k=1, seed=10, reps=1, label="sparse")
        self.assertTrue(t.correct)
        self.assertLess(t.ops_ccp, t.ops_full)


if __name__ == "__main__":
    unittest.main()
