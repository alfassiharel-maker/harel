"""Tests for payload validation. Standard library only.

The point of these tests is the specification's two hard rules: a shallow
candidate must not reach the report, and a hedged claim must not sit in the
known-fact bucket.
"""

from __future__ import annotations

import copy
import unittest
from typing import Any

from scout.models import (
    Problem,
    RejectedProblem,
    ValidationError,
    parse_analysis,
    parse_problem,
    parse_problems,
)

GOOD_PROBLEM: dict[str, Any] = {
    "id": "gpu-idle",
    "title": "Reserved GPU capacity is paid for while utilisation is partial",
    "mechanism": (
        "Training and inference fleets are provisioned for peak concurrency, but "
        "arrival of jobs is bursty, so a reserved instance bills continuously "
        "while its accelerators sit below full occupancy between jobs."
    ),
    "who_pays": "ML platform teams inside mid-size AI product companies",
    "current_workaround": (
        "Teams hand-tune reservation sizes each quarter and run scavenger jobs to "
        "soak up leftover capacity."
    ),
    "signal": (
        "Cloud bills show reserved accelerator hours materially exceeding the "
        "accelerator-busy hours reported by the scheduler over the same period."
    ),
    "confidence": "medium",
    "known_facts": [
        "Accelerator instances are billed per wall-clock hour once reserved.",
        "Schedulers report per-job accelerator occupancy separately from instance uptime.",
    ],
    "hypotheses": [
        "The gap between reserved hours and busy hours is large enough to fund a "
        "dedicated optimisation product.",
    ],
    "open_questions": [
        "What is the median reserved-versus-busy gap across ten real fleets?",
        "Do existing schedulers already close most of the gap without new tooling?",
    ],
    "dimensions": {
        "severity": 4,
        "cost": 5,
        "breadth": 4,
        "solution_maturity": 3,
        "startup_potential": 4,
    },
    "dimension_notes": {
        "severity": "Wasted spend is continuous rather than incidental.",
        "cost": "Accelerator capacity is the largest line item for these teams.",
        "breadth": "Applies to any company training or serving its own models.",
        "solution_maturity": "Schedulers and spot brokers cover part of this already.",
        "startup_potential": "Measurable savings make the sale concrete.",
    },
}

GOOD_ANALYSIS: dict[str, Any] = {
    "domain": "AI infrastructure",
    "structure": {
        "architecture_notes": ["Training and serving planes are usually separate."],
        "components": [
            {"name": "Scheduler", "role": "Places jobs onto accelerator nodes."},
            {"name": "Object store", "role": "Holds datasets and checkpoints."},
        ],
        "data_flows": [
            {
                "source": "Object store",
                "target": "Training job",
                "payload": "Dataset shards and resumed checkpoints.",
            }
        ],
    },
    "engineering": {
        "processes": ["Datasets are prepared, then a job is queued."],
        "bottlenecks": ["Queue wait when the reserved pool is saturated."],
        "waste": ["Idle accelerator time between queued jobs."],
        "inefficiencies": ["Reservation sizing is decided by hand each quarter."],
    },
    "business": {
        "users": ["ML engineers"],
        "customers": ["AI product companies"],
        "spend_areas": ["Accelerator capacity"],
        "high_costs": ["Reserved accelerator hours"],
        "improvable_processes": ["Capacity planning"],
    },
}


def problem_without(*keys: str) -> dict[str, Any]:
    payload = copy.deepcopy(GOOD_PROBLEM)
    for key in keys:
        payload.pop(key)
    return payload


def problem_with(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(GOOD_PROBLEM)
    payload.update(overrides)
    return payload


class AcceptedProblemTests(unittest.TestCase):
    def test_a_complete_candidate_is_accepted(self) -> None:
        result = parse_problem(GOOD_PROBLEM, fallback_id="p01")
        self.assertIsInstance(result, Problem)
        assert isinstance(result, Problem)
        self.assertEqual("gpu-idle", result.id)
        self.assertEqual("medium", result.confidence)
        self.assertEqual(5, len(result.dimensions))

    def test_the_three_buckets_stay_separate(self) -> None:
        result = parse_problem(GOOD_PROBLEM, fallback_id="p01")
        assert isinstance(result, Problem)
        self.assertEqual(2, len(result.known_facts))
        self.assertEqual(1, len(result.hypotheses))
        self.assertEqual(2, len(result.open_questions))
        self.assertFalse(set(result.known_facts) & set(result.hypotheses))

    def test_missing_id_falls_back_without_rejecting(self) -> None:
        result = parse_problem(problem_without("id"), fallback_id="p07")
        assert isinstance(result, Problem)
        self.assertEqual("p07", result.id)


class DepthRejectionTests(unittest.TestCase):
    def test_each_mandatory_field_is_required(self) -> None:
        for key in (
            "title",
            "mechanism",
            "who_pays",
            "current_workaround",
            "signal",
            "confidence",
            "known_facts",
            "hypotheses",
            "open_questions",
            "dimensions",
        ):
            with self.subTest(missing=key):
                result = parse_problem(problem_without(key), fallback_id="p01")
                self.assertIsInstance(result, RejectedProblem)
                assert isinstance(result, RejectedProblem)
                self.assertTrue(any(key in reason for reason in result.reasons))

    def test_the_specifications_bad_example_is_rejected_as_shallow(self) -> None:
        result = parse_problem(
            problem_with(mechanism="An AI company uses cloud."), fallback_id="p01"
        )
        self.assertIsInstance(result, RejectedProblem)
        assert isinstance(result, RejectedProblem)
        self.assertTrue(any("too shallow" in reason for reason in result.reasons))

    def test_a_rejected_candidate_keeps_its_reasons_and_payload(self) -> None:
        result = parse_problem(problem_without("signal", "hypotheses"), fallback_id="p01")
        assert isinstance(result, RejectedProblem)
        self.assertEqual(2, len(result.reasons))
        self.assertEqual(GOOD_PROBLEM["title"], result.title)
        self.assertIn("mechanism", result.raw)

    def test_unknown_confidence_is_rejected(self) -> None:
        result = parse_problem(problem_with(confidence="very high"), fallback_id="p01")
        self.assertIsInstance(result, RejectedProblem)

    def test_a_non_object_candidate_is_rejected_not_raised(self) -> None:
        result = parse_problem("a problem", fallback_id="p01")
        self.assertIsInstance(result, RejectedProblem)


class SeparationRejectionTests(unittest.TestCase):
    """Hedged language in ``known_facts`` is a hypothesis in disguise."""

    def test_hedged_known_fact_is_rejected(self) -> None:
        for hedged in (
            "GPU fleets are probably underutilised most of the time.",
            "It seems that reservations exceed demand.",
            "כנראה שהניצולת נמוכה מהתפוסה שנרכשה.",
        ):
            with self.subTest(fact=hedged):
                result = parse_problem(
                    problem_with(known_facts=[hedged]), fallback_id="p01"
                )
                self.assertIsInstance(result, RejectedProblem)
                assert isinstance(result, RejectedProblem)
                self.assertTrue(
                    any("belongs in hypotheses" in reason for reason in result.reasons)
                )

    def test_hedged_language_is_fine_inside_hypotheses(self) -> None:
        result = parse_problem(
            problem_with(hypotheses=["Utilisation is probably below 60%."]),
            fallback_id="p01",
        )
        self.assertIsInstance(result, Problem)

    def test_an_empty_bucket_is_rejected(self) -> None:
        for bucket in ("known_facts", "hypotheses", "open_questions"):
            with self.subTest(bucket=bucket):
                result = parse_problem(problem_with(**{bucket: []}), fallback_id="p01")
                self.assertIsInstance(result, RejectedProblem)


class DimensionValidationTests(unittest.TestCase):
    def test_a_dimension_without_a_note_does_not_count(self) -> None:
        notes = dict(GOOD_PROBLEM["dimension_notes"])
        del notes["cost"]
        result = parse_problem(problem_with(dimension_notes=notes), fallback_id="p01")
        self.assertIsInstance(result, RejectedProblem)
        assert isinstance(result, RejectedProblem)
        self.assertTrue(any("missing justification" in r for r in result.reasons))

    def test_out_of_range_and_non_integer_dimensions_are_rejected(self) -> None:
        for value in (6, -1, "4", 4.5, None):
            with self.subTest(value=value):
                dims = dict(GOOD_PROBLEM["dimensions"])
                dims["severity"] = value
                result = parse_problem(problem_with(dimensions=dims), fallback_id="p01")
                self.assertIsInstance(result, RejectedProblem)

    def test_zero_is_a_legitimate_dimension_value(self) -> None:
        """Zero means "measured as none", which is different from absent."""
        dims = dict(GOOD_PROBLEM["dimensions"])
        dims["solution_maturity"] = 0
        result = parse_problem(problem_with(dimensions=dims), fallback_id="p01")
        assert isinstance(result, Problem)
        self.assertEqual(0, result.dimensions["solution_maturity"])


class ParseProblemsTests(unittest.TestCase):
    def test_splits_accepted_from_rejected(self) -> None:
        accepted, rejected = parse_problems(
            {"problems": [GOOD_PROBLEM, problem_without("signal")]}
        )
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))

    def test_accepts_a_bare_list(self) -> None:
        accepted, rejected = parse_problems([GOOD_PROBLEM])
        self.assertEqual(1, len(accepted))
        self.assertEqual([], rejected)

    def test_fallback_ids_are_positional_and_stable(self) -> None:
        _, rejected = parse_problems([problem_without("id", "signal")])
        self.assertEqual("p01", rejected[0].id)

    def test_a_non_list_payload_raises(self) -> None:
        with self.assertRaises(ValidationError):
            parse_problems("problems")


class AnalysisTests(unittest.TestCase):
    def test_a_complete_analysis_is_parsed(self) -> None:
        analysis = parse_analysis(GOOD_ANALYSIS)
        self.assertEqual("AI infrastructure", analysis.domain)
        self.assertEqual(2, len(analysis.structure.components))
        self.assertEqual("Object store", analysis.structure.data_flows[0].source)
        self.assertEqual(1, len(analysis.engineering.waste))

    def test_each_missing_section_raises(self) -> None:
        for section in ("structure", "engineering", "business", "domain"):
            with self.subTest(section=section):
                payload = copy.deepcopy(GOOD_ANALYSIS)
                payload.pop(section)
                with self.assertRaises(ValidationError):
                    parse_analysis(payload)

    def test_each_missing_list_raises(self) -> None:
        cases = {
            "engineering": ("processes", "bottlenecks", "waste", "inefficiencies"),
            "business": (
                "users",
                "customers",
                "spend_areas",
                "high_costs",
                "improvable_processes",
            ),
        }
        for section, keys in cases.items():
            for key in keys:
                with self.subTest(section=section, key=key):
                    payload = copy.deepcopy(GOOD_ANALYSIS)
                    payload[section].pop(key)
                    with self.assertRaises(ValidationError):
                        parse_analysis(payload)

    def test_an_empty_component_list_raises(self) -> None:
        payload = copy.deepcopy(GOOD_ANALYSIS)
        payload["structure"]["components"] = []
        with self.assertRaises(ValidationError):
            parse_analysis(payload)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
