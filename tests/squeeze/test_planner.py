"""Planner behaviour: hand-computed anchors, refusals, and unknowns.

Every footprint below is computed by hand in the test, from the formula in
`planner.py`, so a change in the arithmetic has to be argued for rather than
absorbed by a regenerated expectation.
"""

from __future__ import annotations

import unittest
from fractions import Fraction

from backend.squeeze import compile_text, plan_policies
from backend.squeeze.plan import Plan

# One class, one tier, one codec, and numbers chosen so the whole calculation
# fits on one line:
#   records  = 10 per user per month * (30 days / 1 month) = 10
#   raw      = 100 bytes * 10 records * 2 users            = 2000 bytes
#   stored   = ceil(2000 / 2)                              = 1000 bytes
ANCHOR = """version 1
codec half { kind lossless; ratio 2; cpu 1 ms per MiB; applies_to timeseries; impl delta_varint }
tier hot { medium memory; unit_cost 1000 minor per GiB per month; codecs half }
class samples : timeseries {
  record  100 bytes
  growth  10 records per user per month
  retain  hot 30 days
}
policy p {
  objective   minimise bytes
  population  2 users
  limit       total 4 KiB
  require     explanation
  covers      samples
}
"""


def single_plan(source: str) -> Plan:
    program, bag = compile_text(source)
    if program is None:  # pragma: no cover - a failing test would show the codes
        raise AssertionError([d.render() for d in bag.errors])
    plans = plan_policies(program)
    return plans[0]


class TestAnchor(unittest.TestCase):
    def test_hand_computed_footprint(self) -> None:
        plan = single_plan(ANCHOR)
        self.assertEqual(plan.total_raw_bytes, 2000)
        self.assertEqual(plan.total_stored_bytes, 1000)
        self.assertEqual(plan.saved_bytes, 1000)
        self.assertEqual(plan.ratio, 2)
        self.assertEqual(plan.coverage, 1)

    def test_per_user_bytes_is_the_fleet_total_divided_by_population(self) -> None:
        self.assertEqual(single_plan(ANCHOR).per_user_bytes, 500)

    def test_monthly_cost_is_priced_on_stored_bytes(self) -> None:
        # 1000 stored bytes at 1000 minor per GiB per month.
        plan = single_plan(ANCHOR)
        self.assertEqual(plan.monthly_cost_minor, Fraction(1000 * 1000, 1024**3))

    def test_limit_is_checked_and_satisfied(self) -> None:
        plan = single_plan(ANCHOR)
        self.assertEqual([(c.name, c.satisfied) for c in plan.limits], [("total", True)])

    def test_drivers_sum_exactly_to_the_total_saving(self) -> None:
        plan = single_plan(ANCHOR)
        self.assertEqual(sum(d.saved_bytes for d in plan.drivers), plan.saved_bytes)
        self.assertEqual(sum(d.share or 0 for d in plan.drivers), 1)

    def test_plan_is_provable(self) -> None:
        self.assertTrue(single_plan(ANCHOR).is_provable)

    def test_compilation_is_reproducible(self) -> None:
        # Same source, same plan — the reason magnitudes are Fractions.
        first, second = single_plan(ANCHOR), single_plan(ANCHOR)
        self.assertEqual(first, second)


class TestUnknowns(unittest.TestCase):
    def test_a_class_with_no_growth_is_unknown_not_zero(self) -> None:
        source = ANCHOR.replace("  growth  10 records per user per month\n", "")
        plan = single_plan(source)
        self.assertIsNone(plan.total_raw_bytes)
        self.assertIsNone(plan.saved_bytes)
        self.assertEqual(plan.coverage, 0)
        self.assertEqual(plan.allocations[0].unknown_reason, "no growth rate declared")

    def test_an_unprovable_limit_is_not_a_satisfied_limit(self) -> None:
        source = ANCHOR.replace("  growth  10 records per user per month\n", "")
        plan = single_plan(source)
        self.assertEqual([c.satisfied for c in plan.limits], [None])
        self.assertFalse(plan.is_provable)

    def test_user_scoped_class_without_population_refuses_to_guess(self) -> None:
        source = ANCHOR.replace("  population  2 users\n", "")
        plan = single_plan(source)
        self.assertIsNone(plan.total_stored_bytes)
        self.assertIsNone(plan.per_user_bytes)

    def test_partial_coverage_is_reported(self) -> None:
        source = ANCHOR.replace(
            "policy p {",
            "class unmeasured : timeseries {\n  record 10 bytes\n  retain hot 30 days\n}\npolicy p {",
        ).replace("covers      samples", "covers      samples, unmeasured")
        plan = single_plan(source)
        self.assertEqual(plan.coverage, Fraction(1, 2))
        self.assertEqual(plan.total_stored_bytes, 1000)
        self.assertFalse(plan.is_provable)

    def test_an_unpriced_tier_makes_the_bill_unknown(self) -> None:
        source = ANCHOR.replace("; unit_cost 1000 minor per GiB per month", "")
        self.assertIsNone(single_plan(source).monthly_cost_minor)


class TestRefusals(unittest.TestCase):
    def test_lossy_codec_is_refused_on_a_clinical_class(self) -> None:
        source = """version 1
codec q { kind lossy; fidelity 0.99; ratio 4; cpu 1 ms per MiB; applies_to relational; impl int8_quantise }
tier warm { medium disk; codecs q }
class wellness : relational {
  record       280 bytes
  growth       30 records per user per month
  retain       warm 30 days
  sensitivity  clinical
}
policy p { objective minimise bytes; population 1 users; covers wellness }
"""
        plan = single_plan(source)
        allocation = plan.allocations[0]
        self.assertIsNone(allocation.codec)
        self.assertEqual(allocation.raw_bytes, allocation.stored_bytes)
        self.assertEqual([r.reason for r in allocation.rejected], ["lossy codec on a clinical class"])

    def test_fidelity_floor_excludes_a_codec(self) -> None:
        source = """version 1
codec q { kind lossy; fidelity 0.90; ratio 4; cpu 1 ms per MiB; applies_to tensor; impl int8_quantise }
tier warm { medium disk; codecs q }
class weights : tensor {
  record  1 MiB
  growth  1 records per fleet per month
  retain  warm 30 days
}
policy p { objective minimise bytes; require fidelity >= 0.99; covers weights }
"""
        plan = single_plan(source)
        self.assertIsNone(plan.allocations[0].codec)
        self.assertIn("below the required", plan.allocations[0].rejected[0].reason)

    def test_latency_budget_excludes_a_slow_codec(self) -> None:
        # 4 MiB record at 10 ms per MiB is 40 ms of CPU, over a 5 ms budget.
        source = """version 1
codec slow { kind lossless; ratio 9; cpu 10 ms per MiB; applies_to blob; impl lzma }
tier hot { medium memory; latency 5 ms; codecs slow }
class blobs : blob {
  record  4 MiB
  growth  1 records per fleet per month
  retain  hot 30 days
}
policy p { objective minimise bytes; covers blobs }
"""
        plan = single_plan(source)
        self.assertIsNone(plan.allocations[0].codec)
        self.assertIn("exceeds the tier budget", plan.allocations[0].rejected[0].reason)

    def test_unmeasured_cpu_fails_a_latency_budget_closed(self) -> None:
        source = """version 1
codec unknown_cpu { kind lossless; ratio 9; applies_to blob; impl lzma }
tier hot { medium memory; latency 5 ms; codecs unknown_cpu }
class blobs : blob {
  record  1 KiB
  growth  1 records per fleet per month
  retain  hot 30 days
}
policy p { objective minimise bytes; covers blobs }
"""
        plan = single_plan(source)
        self.assertIsNone(plan.allocations[0].codec)
        self.assertIn("unprovable", plan.allocations[0].rejected[0].reason)


class TestSelection(unittest.TestCase):
    SOURCE = """version 1
codec small_fast { kind lossless; ratio 3; cpu 1 ms per MiB; applies_to blob; impl zlib }
codec big_slow { kind lossless; ratio 9; cpu 40 ms per MiB; applies_to blob; impl lzma }
tier warm { medium disk; codecs small_fast, big_slow }
class blobs : blob {
  record  1 KiB
  growth  1 records per fleet per month
  retain  warm 30 days
}
policy %s { objective minimise %s; covers blobs }
"""

    def test_bytes_objective_takes_the_strongest_codec(self) -> None:
        plan = single_plan(self.SOURCE % ("p", "bytes"))
        self.assertEqual(plan.allocations[0].codec, "big_slow")

    def test_latency_objective_takes_the_cheapest_cpu(self) -> None:
        plan = single_plan(self.SOURCE % ("p", "latency"))
        self.assertEqual(plan.allocations[0].codec, "small_fast")

    def test_ties_break_by_name_so_plans_are_stable(self) -> None:
        source = """version 1
codec zebra { kind lossless; ratio 4; cpu 1 ms per MiB; applies_to blob; impl zlib }
codec alpha { kind lossless; ratio 4; cpu 1 ms per MiB; applies_to blob; impl zlib }
tier warm { medium disk; codecs zebra, alpha }
class blobs : blob {
  record  1 KiB
  growth  1 records per fleet per month
  retain  warm 30 days
}
policy p { objective minimise bytes; covers blobs }
"""
        self.assertEqual(single_plan(source).allocations[0].codec, "alpha")


class TestRetentionArithmetic(unittest.TestCase):
    SOURCE = """version 1
codec half { kind lossless; ratio 2; cpu 1 ms per MiB; applies_to timeseries; impl delta_varint }
tier hot { medium memory; codecs half }
tier cold { medium object; codecs half }
class samples : timeseries {
  record  100 bytes
  growth  1 records per fleet per day
  retain  hot 30 days, cold 90 days
}
policy p { objective minimise bytes%s; covers samples }
"""

    def test_windows_are_cumulative_so_residency_is_the_difference(self) -> None:
        plan = single_plan(self.SOURCE % "")
        residency = {a.tier: a.residency_days for a in plan.allocations}
        self.assertEqual(residency, {"hot": 30, "cold": 60})
        # 1 record/day * 100 bytes: hot holds 3000 raw, cold 6000 raw.
        stored = {a.tier: a.stored_bytes for a in plan.allocations}
        self.assertEqual(stored, {"hot": 1500, "cold": 3000})

    def test_horizon_clips_the_coldest_window(self) -> None:
        # 45 days after launch, the cold tier can only hold 15 days of data.
        plan = single_plan(self.SOURCE % "; horizon 45 days")
        residency = {a.tier: a.residency_days for a in plan.allocations}
        self.assertEqual(residency, {"hot": 30, "cold": 15})

    def test_a_window_entirely_beyond_the_horizon_holds_nothing(self) -> None:
        plan = single_plan(self.SOURCE % "; horizon 20 days")
        residency = {a.tier: a.residency_days for a in plan.allocations}
        self.assertEqual(residency, {"hot": 20, "cold": 0})
        self.assertEqual({a.tier: a.stored_bytes for a in plan.allocations}["cold"], 0)


class TestExamplePolicy(unittest.TestCase):
    def test_the_committed_policy_plans_within_its_limits(self) -> None:
        from pathlib import Path

        program, bag = compile_text(Path("policies/footprint.sqz").read_text(encoding="utf-8"))
        self.assertIsNotNone(program)
        assert program is not None
        self.assertEqual(bag.errors, [])
        plan = plan_policies(program)[0]
        self.assertEqual(plan.coverage, 1)
        self.assertTrue(plan.is_provable)
        self.assertEqual([c.satisfied for c in plan.limits], [True, True])
        self.assertEqual(sum(d.saved_bytes for d in plan.drivers), plan.saved_bytes)
        # Streams dominate; if that ever stops being true the policy needs a
        # different shape, not a different expectation.
        self.assertTrue(plan.drivers[0].name.startswith("activity_streams@"))


if __name__ == "__main__":
    unittest.main()
