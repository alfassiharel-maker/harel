"""Report assembly and rendering, driven entirely by a recorded fixture.

No network, no API key, no dependencies. If this suite passes, the whole
input → analysis → ranked report path works apart from the model call itself.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scout.models import parse_analysis, parse_problems
from scout.report import (
    RunMeta,
    build,
    render_markdown,
    to_run_record,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ai-infrastructure.json"

META = RunMeta(
    provider="fixture",
    model="fixture",
    generated_at="2026-08-06T00:00:00Z",
    input_tokens=1234,
    output_tokens=5678,
    cost_usd_micros=42_000,
)


def load_report():  # type: ignore[no-untyped-def]
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    analysis = parse_analysis(payload["analysis"])
    accepted, rejected = parse_problems(payload["problems"])
    return build(analysis, accepted, rejected, META), accepted, rejected


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report, self.accepted, self.rejected = load_report()

    def test_the_fixture_splits_three_good_candidates_from_one_shallow_one(self) -> None:
        self.assertEqual(3, len(self.accepted))
        self.assertEqual(1, len(self.rejected))
        self.assertEqual("shallow-example", self.rejected[0].id)

    def test_ranked_best_first(self) -> None:
        scores = [item.score.value for item in self.report.ranked]
        self.assertEqual(sorted(scores, reverse=True), scores)
        self.assertEqual("gpu-idle-billing", self.report.ranked[0].problem.id)

    def test_the_shallow_candidate_never_enters_the_ranking(self) -> None:
        """It scores 5/5 on four dimensions, so only validation keeps it out."""
        ranked_ids = {item.problem.id for item in self.report.ranked}
        self.assertNotIn("shallow-example", ranked_ids)
        self.assertIn("shallow-example", {item.id for item in self.report.insufficient})

    def test_every_ranked_problem_carries_its_drivers(self) -> None:
        for item in self.report.ranked:
            self.assertEqual(5, len(item.score.drivers))
            self.assertAlmostEqual(
                item.score.value,
                sum(driver.contribution for driver in item.score.drivers),
                places=2,
            )


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report, _, _ = load_report()
        self.markdown = render_markdown(self.report)

    def test_all_five_sections_are_present(self) -> None:
        for heading in (
            "## 1. מבנה התחום",
            "## 2. הצד ההנדסי",
            "## 3. הצד העסקי",
            "## 4. בעיות מדורגות",
            "## 5. עדות לא מספקת",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.markdown)

    def test_every_engineering_and_business_question_is_answered(self) -> None:
        for heading in (
            "### תהליכים כפי שהם מתבצעים היום",
            "### צווארי בקבוק",
            "### בזבוז משאבים",
            "### מה עדיין נעשה בצורה לא יעילה",
            "### משתמשים",
            "### לקוחות משלמים",
            "### איפה חברות מוציאות כסף",
            "### עלויות גבוהות",
            "### תהליכים שיכולים להשתפר",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, self.markdown)

    def test_the_three_buckets_are_rendered_separately_for_every_problem(self) -> None:
        self.assertEqual(3, self.markdown.count("**עובדה ידועה**"))
        self.assertEqual(3, self.markdown.count("**השערה**"))
        self.assertEqual(3, self.markdown.count("**כיוון לבדיקה**"))

    def test_the_disclaimer_states_that_nothing_is_source_verified(self) -> None:
        self.assertIn("אין בו אחזור מקורות", self.markdown)

    def test_the_rejection_reason_is_visible_to_the_reader(self) -> None:
        self.assertIn("shallow-example", self.markdown)
        self.assertIn("too shallow", self.markdown)

    def test_scores_and_contributions_are_printed(self) -> None:
        top = self.report.ranked[0]
        self.assertIn(f"**ציון: {top.score.value:.1f}/100**", self.markdown)
        self.assertIn(top.problem.mechanism, self.markdown)

    def test_rendering_is_deterministic(self) -> None:
        again, _, _ = load_report()
        self.assertEqual(self.markdown, render_markdown(again))

    def test_no_placeholder_or_empty_table_rows_leak_out(self) -> None:
        self.assertNotIn("| |", self.markdown)
        self.assertNotIn("None", self.markdown)


class EmptyReportTests(unittest.TestCase):
    """A run where nothing survived validation must say so, not render blank."""

    def test_report_with_no_accepted_candidates(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        analysis = parse_analysis(payload["analysis"])
        _, rejected = parse_problems([payload["problems"][-1]])
        markdown = render_markdown(build(analysis, [], rejected, META))
        self.assertIn("אף מועמד לא עבר את בדיקות העומק", markdown)
        self.assertIn("## 5. עדות לא מספקת", markdown)

    def test_report_with_nothing_rejected_says_so(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        analysis = parse_analysis(payload["analysis"])
        accepted, _ = parse_problems(payload["problems"][:1])
        markdown = render_markdown(build(analysis, accepted, [], META))
        self.assertIn("עברו את בדיקות העומק", markdown)


class RunRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.report, _, _ = load_report()
        self.record = to_run_record(self.report)

    def test_the_record_is_json_serialisable(self) -> None:
        text = json.dumps(self.record, ensure_ascii=False)
        self.assertEqual(self.record, json.loads(text))

    def test_the_record_keeps_scores_drivers_and_rejections(self) -> None:
        ranked = self.record["ranked"]
        assert isinstance(ranked, list)
        self.assertEqual(3, len(ranked))
        self.assertEqual(1, ranked[0]["rank"])
        self.assertEqual(5, len(ranked[0]["drivers"]))
        insufficient = self.record["insufficient"]
        assert isinstance(insufficient, list)
        self.assertEqual(1, len(insufficient))

    def test_the_record_carries_provenance_and_no_secret(self) -> None:
        meta = self.record["meta"]
        assert isinstance(meta, dict)
        self.assertEqual("fixture", meta["provider"])
        self.assertEqual(1234, meta["input_tokens"])
        self.assertNotIn("api_key", json.dumps(self.record).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
