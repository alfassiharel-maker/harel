"""Assembly and Markdown rendering of a run's report.

Standard library only. This module makes no decisions about content: it scores
what it is given, orders it, and prints it. The three claim buckets are rendered
under three separate headings and are never interleaved — mixing them in the
output would defeat the validation that keeps them apart in the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Sequence

from scout.models import DomainAnalysis, Problem, RejectedProblem
from scout.scoring import Score, rank, score_problem

#: Hebrew labels for the report. The analyser's audience reads Hebrew; the field
#: names stay English in the payload so the code and the JSON remain greppable.
DIMENSION_LABELS: Final[dict[str, str]] = {
    "severity": "חומרת הבעיה",
    "cost": "עלות שהבעיה גורמת",
    "breadth": "כמה מושפעים",
    "startup_potential": "פוטנציאל לסטארטאפ",
    "solution_maturity": "בגרות פתרונות קיימים (הפוך)",
}

CONFIDENCE_LABELS: Final[dict[str, str]] = {
    "high": "גבוה",
    "medium": "בינוני",
    "low": "נמוך",
}

DISCLAIMER: Final = (
    "> **מה הדוח הזה כן ואינו.** הניתוח מבוסס על ידע תחום של המודל בלבד — "
    "אין בו אחזור מקורות ואין בו אימות חיצוני. לכן כל בעיה מפרידה במפורש בין "
    "*עובדה ידועה* (טענה שאינה מאומתת מול מקור), *השערה* (הקפיצה האנליטית) "
    "ו*כיוון לבדיקה* (מה שצריך לאמת לפני שמשקיעים בזה). "
    "אין לקרוא את ההשערות כנתונים."
)


@dataclass(frozen=True, slots=True)
class RunMeta:
    """Provenance of a run. Never carries a key or any credential."""

    provider: str
    model: str
    generated_at: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd_micros: int | None = None


@dataclass(frozen=True, slots=True)
class RankedProblem:
    problem: Problem
    score: Score


@dataclass(frozen=True, slots=True)
class Report:
    domain: str
    analysis: DomainAnalysis
    ranked: tuple[RankedProblem, ...]
    insufficient: tuple[RejectedProblem, ...]
    meta: RunMeta


def build(
    analysis: DomainAnalysis,
    accepted: Sequence[Problem],
    rejected: Sequence[RejectedProblem],
    meta: RunMeta,
) -> Report:
    """Score the accepted candidates and order them best-first.

    A candidate that cannot be scored is moved to ``insufficient`` rather than
    ranked low, so an unscorable problem never competes with a scored one.
    """
    scores = {problem.id: score_problem(problem.dimensions) for problem in accepted}
    by_id = {problem.id: problem for problem in accepted}

    ranked: list[RankedProblem] = []
    unscorable: list[RejectedProblem] = []
    for problem_id in rank(list(scores.items())):
        score = scores[problem_id]
        problem = by_id[problem_id]
        if score is None:
            unscorable.append(
                RejectedProblem(
                    id=problem.id,
                    title=problem.title,
                    reasons=("dimensions: incomplete, cannot be scored",),
                    raw={},
                )
            )
            continue
        ranked.append(RankedProblem(problem=problem, score=score))

    return Report(
        domain=analysis.domain,
        analysis=analysis,
        ranked=tuple(ranked),
        insufficient=tuple(rejected) + tuple(unscorable),
        meta=meta,
    )


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def _bullets(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _labelled_bullets(heading: str, items: Iterable[str]) -> str:
    return f"**{heading}**\n\n{_bullets(items)}"


def _structure_section(analysis: DomainAnalysis) -> str:
    components = "\n".join(
        f"| {component.name} | {component.role} |"
        for component in analysis.structure.components
    )
    flows = "\n".join(
        f"| {flow.source} | {flow.target} | {flow.payload} |"
        for flow in analysis.structure.data_flows
    )
    return "\n".join(
        [
            "## 1. מבנה התחום",
            "",
            "### רכיבים",
            "",
            "| רכיב | תפקיד |",
            "| --- | --- |",
            components,
            "",
            "### זרימת מידע",
            "",
            "| מ | אל | מה עובר |",
            "| --- | --- | --- |",
            flows,
            "",
            "### כיצד המערכות בנויות",
            "",
            _bullets(analysis.structure.architecture_notes),
        ]
    )


def _engineering_section(analysis: DomainAnalysis) -> str:
    engineering = analysis.engineering
    return "\n\n".join(
        [
            "## 2. הצד ההנדסי",
            "### תהליכים כפי שהם מתבצעים היום",
            _bullets(engineering.processes),
            "### צווארי בקבוק",
            _bullets(engineering.bottlenecks),
            "### בזבוז משאבים",
            _bullets(engineering.waste),
            "### מה עדיין נעשה בצורה לא יעילה",
            _bullets(engineering.inefficiencies),
        ]
    )


def _business_section(analysis: DomainAnalysis) -> str:
    business = analysis.business
    return "\n\n".join(
        [
            "## 3. הצד העסקי",
            "### משתמשים",
            _bullets(business.users),
            "### לקוחות משלמים",
            _bullets(business.customers),
            "### איפה חברות מוציאות כסף",
            _bullets(business.spend_areas),
            "### עלויות גבוהות",
            _bullets(business.high_costs),
            "### תהליכים שיכולים להשתפר",
            _bullets(business.improvable_processes),
        ]
    )


def _summary_table(ranked: Sequence[RankedProblem]) -> str:
    rows = "\n".join(
        f"| {position} | {item.problem.title} | {item.score.value:.1f} | "
        f"{DIMENSION_LABELS[item.score.top_driver.dimension]} |"
        for position, item in enumerate(ranked, start=1)
    )
    return "\n".join(
        [
            "| # | בעיה | ציון | הדרייבר המוביל |",
            "| --- | --- | --- | --- |",
            rows,
        ]
    )


def _problem_block(position: int, item: RankedProblem) -> str:
    problem, score = item.problem, item.score
    dimension_rows = "\n".join(
        f"| {DIMENSION_LABELS[driver.dimension]} | {driver.raw}/5 | "
        f"{driver.contribution:.1f} | {problem.dimension_notes[driver.dimension]} |"
        for driver in score.drivers
    )
    return "\n\n".join(
        [
            f"### {position}. {problem.title}",
            f"**ציון: {score.value:.1f}/100** · "
            f"מוביל: {DIMENSION_LABELS[score.top_driver.dimension]} · "
            f"ביטחון המודל: {CONFIDENCE_LABELS[problem.confidence]}",
            f"**מי משלם את המחיר:** {problem.who_pays}",
            f"**המנגנון — למה זה קורה:** {problem.mechanism}",
            f"**מה עושים היום במקום:** {problem.current_workaround}",
            f"**סימן שניתן לבדוק:** {problem.signal}",
            "\n".join(
                [
                    "| ממד | ערך | תרומה לציון | נימוק |",
                    "| --- | --- | --- | --- |",
                    dimension_rows,
                ]
            ),
            _labelled_bullets("עובדה ידועה", problem.known_facts),
            _labelled_bullets("השערה", problem.hypotheses),
            _labelled_bullets("כיוון לבדיקה", problem.open_questions),
        ]
    )


def _insufficient_section(rejected: Sequence[RejectedProblem]) -> str:
    if not rejected:
        return "\n\n".join(
            [
                "## 5. עדות לא מספקת",
                "כל המועמדים שנוצרו בריצה הזאת עברו את בדיקות העומק.",
            ]
        )
    rows = "\n".join(
        f"| {item.id} | {item.title or '—'} | {'; '.join(item.reasons)} |"
        for item in rejected
    )
    count = (
        "מועמד אחד נדחה ולא קיבל ציון"
        if len(rejected) == 1
        else f"{len(rejected)} מועמדים נדחו ולא קיבלו ציון"
    )
    return "\n\n".join(
        [
            "## 5. עדות לא מספקת",
            f"{count}. הם מוצגים כאן ולא "
            "הושמטו בשקט, כדי שריצה דלה לא תיראה כמו ריצה יסודית.",
            "\n".join(
                ["| מזהה | כותרת | למה נדחה |", "| --- | --- | --- |", rows]
            ),
        ]
    )


def _meta_line(meta: RunMeta) -> str:
    parts = [f"נוצר: {meta.generated_at}", f"ספק: {meta.provider}", f"מודל: {meta.model}"]
    if meta.input_tokens is not None and meta.output_tokens is not None:
        parts.append(f"טוקנים: {meta.input_tokens} קלט / {meta.output_tokens} פלט")
    if meta.cost_usd_micros is not None:
        parts.append(f"עלות: ${meta.cost_usd_micros / 1_000_000:.4f}")
    return " · ".join(parts)


def render_markdown(report: Report) -> str:
    """Render a report as Markdown. Deterministic: same report, same bytes."""
    sections: list[str] = [
        f"# דוח בעיות והזדמנויות — {report.domain}",
        _meta_line(report.meta),
        DISCLAIMER,
        _structure_section(report.analysis),
        _engineering_section(report.analysis),
        _business_section(report.analysis),
        "## 4. בעיות מדורגות",
    ]

    if report.ranked:
        sections.append(_summary_table(report.ranked))
        sections.extend(
            _problem_block(position, item)
            for position, item in enumerate(report.ranked, start=1)
        )
    else:
        sections.append(
            "אף מועמד לא עבר את בדיקות העומק בריצה הזאת. ראה סעיף 5."
        )

    sections.append(_insufficient_section(report.insufficient))
    return "\n\n".join(sections) + "\n"


def to_run_record(report: Report) -> dict[str, object]:
    """The machine-readable twin of the Markdown, for a later run to diff against.

    Scores and drivers are included so a ranking can be re-checked without
    re-running the model.
    """
    return {
        "domain": report.domain,
        "meta": {
            "provider": report.meta.provider,
            "model": report.meta.model,
            "generated_at": report.meta.generated_at,
            "input_tokens": report.meta.input_tokens,
            "output_tokens": report.meta.output_tokens,
            "cost_usd_micros": report.meta.cost_usd_micros,
        },
        "analysis": {
            "structure": {
                "architecture_notes": list(report.analysis.structure.architecture_notes),
                "components": [
                    {"name": component.name, "role": component.role}
                    for component in report.analysis.structure.components
                ],
                "data_flows": [
                    {"source": flow.source, "target": flow.target, "payload": flow.payload}
                    for flow in report.analysis.structure.data_flows
                ],
            },
            "engineering": {
                "processes": list(report.analysis.engineering.processes),
                "bottlenecks": list(report.analysis.engineering.bottlenecks),
                "waste": list(report.analysis.engineering.waste),
                "inefficiencies": list(report.analysis.engineering.inefficiencies),
            },
            "business": {
                "users": list(report.analysis.business.users),
                "customers": list(report.analysis.business.customers),
                "spend_areas": list(report.analysis.business.spend_areas),
                "high_costs": list(report.analysis.business.high_costs),
                "improvable_processes": list(
                    report.analysis.business.improvable_processes
                ),
            },
        },
        "ranked": [
            {
                "rank": position,
                "id": item.problem.id,
                "title": item.problem.title,
                "score": item.score.value,
                "drivers": [
                    {
                        "dimension": driver.dimension,
                        "raw": driver.raw,
                        "effective": driver.effective,
                        "weight": driver.weight,
                        "contribution": driver.contribution,
                        "inverted": driver.inverted,
                    }
                    for driver in item.score.drivers
                ],
                "confidence": item.problem.confidence,
                "mechanism": item.problem.mechanism,
                "who_pays": item.problem.who_pays,
                "current_workaround": item.problem.current_workaround,
                "signal": item.problem.signal,
                "known_facts": list(item.problem.known_facts),
                "hypotheses": list(item.problem.hypotheses),
                "open_questions": list(item.problem.open_questions),
                "dimensions": dict(item.problem.dimensions),
                "dimension_notes": dict(item.problem.dimension_notes),
            }
            for position, item in enumerate(report.ranked, start=1)
        ],
        "insufficient": [
            {"id": item.id, "title": item.title, "reasons": list(item.reasons)}
            for item in report.insufficient
        ],
    }
