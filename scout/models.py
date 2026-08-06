"""The data the analyser passes around, and the validation that guards it.

Standard library only. Everything a language model returns arrives here first;
nothing downstream sees an unvalidated payload.

Two different failure postures live in this module, on purpose:

* A malformed **domain analysis** raises. Without a map there is no report, and
  producing one anyway would mean inventing the map.
* A malformed **problem candidate** does not raise. It is rejected with reasons
  and reported under "insufficient evidence", because dropping it silently would
  make a thin run look like a thorough one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Sequence

from scout.scoring import DIMENSIONS

CONFIDENCE_LEVELS: Final[tuple[str, ...]] = ("high", "medium", "low")

#: Words that turn an assertion into a guess. Their presence in ``known_facts``
#: is the exact mixing of fact and hypothesis the specification forbids, so it is
#: a rejection rather than a warning. Hebrew markers are included because the
#: model may answer in either language.
HEDGE_MARKERS: Final[tuple[str, ...]] = (
    "probably",
    "possibly",
    "perhaps",
    "maybe",
    "likely",
    "might",
    "may be",
    "could be",
    "i think",
    "i suspect",
    "seems",
    "appears to",
    "arguably",
    "presumably",
    "roughly",
    "אולי",
    "ייתכן",
    "כנראה",
    "סביר ש",
    "נראה ש",
    "יכול להיות",
)

_HEDGE_RE: Final = re.compile(
    "|".join(re.escape(marker) for marker in HEDGE_MARKERS), re.IGNORECASE
)

#: Below this a "mechanism" or "signal" is a label, not an explanation. Chosen
#: against the specification's own bad example — "an AI company uses cloud" is
#: 26 characters — so the floor sits just above a bare restatement while staying
#: far below any real causal sentence.
MIN_EXPLANATION_CHARS: Final = 40


class ValidationError(ValueError):
    """A payload that cannot be used at all."""


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    role: str


@dataclass(frozen=True, slots=True)
class DataFlow:
    source: str
    target: str
    payload: str


@dataclass(frozen=True, slots=True)
class Structure:
    """How systems in the domain are built and how data moves between them."""

    architecture_notes: tuple[str, ...]
    components: tuple[Component, ...]
    data_flows: tuple[DataFlow, ...]


@dataclass(frozen=True, slots=True)
class Engineering:
    processes: tuple[str, ...]
    bottlenecks: tuple[str, ...]
    waste: tuple[str, ...]
    inefficiencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Business:
    users: tuple[str, ...]
    customers: tuple[str, ...]
    spend_areas: tuple[str, ...]
    high_costs: tuple[str, ...]
    improvable_processes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DomainAnalysis:
    domain: str
    structure: Structure
    engineering: Engineering
    business: Business


@dataclass(frozen=True, slots=True)
class Problem:
    """A candidate problem that passed every depth and separation check."""

    id: str
    title: str
    mechanism: str
    who_pays: str
    current_workaround: str
    signal: str
    confidence: str
    known_facts: tuple[str, ...]
    hypotheses: tuple[str, ...]
    open_questions: tuple[str, ...]
    dimensions: Mapping[str, int]
    dimension_notes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RejectedProblem:
    """A candidate that failed validation, kept with its reasons.

    The raw payload is carried so a reader can judge the rejection themselves.
    """

    id: str
    title: str | None
    reasons: tuple[str, ...]
    raw: Mapping[str, Any] = field(repr=False)


# --------------------------------------------------------------------------- #
# primitive coercion
# --------------------------------------------------------------------------- #


def _require_mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{where}: expected an object, got {type(value).__name__}")
    return value


def _text(payload: Mapping[str, Any], key: str, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{where}.{key}: expected a non-empty string")
    return value.strip()


def _text_list(payload: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationError(f"{where}.{key}: expected a list of strings")
    items: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise ValidationError(f"{where}.{key}[{index}]: expected a non-empty string")
        items.append(entry.strip())
    if not items:
        raise ValidationError(f"{where}.{key}: must not be empty")
    return tuple(items)


# --------------------------------------------------------------------------- #
# domain analysis
# --------------------------------------------------------------------------- #


def parse_analysis(payload: object) -> DomainAnalysis:
    """Validate the first model call's output. Raises on any defect."""
    root = _require_mapping(payload, "analysis")

    structure_raw = _require_mapping(root.get("structure"), "analysis.structure")
    components_raw = structure_raw.get("components")
    if not isinstance(components_raw, Sequence) or not components_raw:
        raise ValidationError("analysis.structure.components: must be a non-empty list")
    components: list[Component] = []
    for index, entry in enumerate(components_raw):
        where = f"analysis.structure.components[{index}]"
        item = _require_mapping(entry, where)
        components.append(
            Component(name=_text(item, "name", where), role=_text(item, "role", where))
        )

    flows_raw = structure_raw.get("data_flows")
    if not isinstance(flows_raw, Sequence) or not flows_raw:
        raise ValidationError("analysis.structure.data_flows: must be a non-empty list")
    data_flows: list[DataFlow] = []
    for index, entry in enumerate(flows_raw):
        where = f"analysis.structure.data_flows[{index}]"
        item = _require_mapping(entry, where)
        data_flows.append(
            DataFlow(
                source=_text(item, "source", where),
                target=_text(item, "target", where),
                payload=_text(item, "payload", where),
            )
        )

    engineering_raw = _require_mapping(root.get("engineering"), "analysis.engineering")
    business_raw = _require_mapping(root.get("business"), "analysis.business")

    return DomainAnalysis(
        domain=_text(root, "domain", "analysis"),
        structure=Structure(
            architecture_notes=_text_list(
                structure_raw, "architecture_notes", "analysis.structure"
            ),
            components=tuple(components),
            data_flows=tuple(data_flows),
        ),
        engineering=Engineering(
            processes=_text_list(engineering_raw, "processes", "analysis.engineering"),
            bottlenecks=_text_list(
                engineering_raw, "bottlenecks", "analysis.engineering"
            ),
            waste=_text_list(engineering_raw, "waste", "analysis.engineering"),
            inefficiencies=_text_list(
                engineering_raw, "inefficiencies", "analysis.engineering"
            ),
        ),
        business=Business(
            users=_text_list(business_raw, "users", "analysis.business"),
            customers=_text_list(business_raw, "customers", "analysis.business"),
            spend_areas=_text_list(business_raw, "spend_areas", "analysis.business"),
            high_costs=_text_list(business_raw, "high_costs", "analysis.business"),
            improvable_processes=_text_list(
                business_raw, "improvable_processes", "analysis.business"
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# problem candidates
# --------------------------------------------------------------------------- #

#: Free-text fields every accepted problem must carry, and the minimum length
#: demanded of each. ``title`` and ``who_pays`` are naturally short; the two
#: fields that carry the actual thinking are held to the explanation floor.
_REQUIRED_TEXT: Final[Mapping[str, int]] = {
    "title": 10,
    "mechanism": MIN_EXPLANATION_CHARS,
    "who_pays": 10,
    "current_workaround": 20,
    "signal": MIN_EXPLANATION_CHARS,
}

_REQUIRED_BUCKETS: Final[tuple[str, ...]] = (
    "known_facts",
    "hypotheses",
    "open_questions",
)


def _collect_text(
    payload: Mapping[str, Any], key: str, minimum: int, reasons: list[str]
) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        reasons.append(f"{key}: missing")
        return None
    text = value.strip()
    if len(text) < minimum:
        reasons.append(f"{key}: too shallow ({len(text)} chars, need {minimum})")
        return None
    return text


def _collect_bucket(
    payload: Mapping[str, Any], key: str, reasons: list[str]
) -> tuple[str, ...] | None:
    value = payload.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        reasons.append(f"{key}: missing")
        return None
    items = [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]
    if not items:
        reasons.append(f"{key}: must not be empty")
        return None
    return tuple(items)


def _collect_dimensions(
    payload: Mapping[str, Any], reasons: list[str]
) -> tuple[dict[str, int], dict[str, str]]:
    """Read the five dimensions and their notes.

    A dimension the model omitted is left out of the returned mapping rather
    than defaulted — scoring then yields ``None`` for the whole candidate, which
    is the honest outcome. A dimension present but unusable is a reason.
    """
    raw = payload.get("dimensions")
    notes_raw = payload.get("dimension_notes")
    if not isinstance(raw, Mapping):
        reasons.append("dimensions: missing")
        return {}, {}
    notes_map: Mapping[str, Any] = notes_raw if isinstance(notes_raw, Mapping) else {}

    values: dict[str, int] = {}
    notes: dict[str, str] = {}
    for dimension in DIMENSIONS:
        value = raw.get(dimension)
        if value is None:
            reasons.append(f"dimensions.{dimension}: not provided")
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            reasons.append(f"dimensions.{dimension}: not an integer")
            continue
        if not 0 <= value <= 5:
            reasons.append(f"dimensions.{dimension}: {value} outside 0..5")
            continue
        note = notes_map.get(dimension)
        if not isinstance(note, str) or not note.strip():
            # A score with no stated reason cannot be argued with, so it does not
            # count as provided.
            reasons.append(f"dimension_notes.{dimension}: missing justification")
            continue
        values[dimension] = value
        notes[dimension] = note.strip()
    return values, notes


def parse_problem(payload: object, fallback_id: str) -> Problem | RejectedProblem:
    """Validate one candidate. Never raises — a defect becomes a rejection."""
    if not isinstance(payload, Mapping):
        return RejectedProblem(
            id=fallback_id,
            title=None,
            reasons=("payload: expected an object",),
            raw={},
        )

    reasons: list[str] = []
    raw_id = payload.get("id")
    problem_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else fallback_id

    texts: dict[str, str | None] = {
        key: _collect_text(payload, key, minimum, reasons)
        for key, minimum in _REQUIRED_TEXT.items()
    }

    confidence = payload.get("confidence")
    if not isinstance(confidence, str) or confidence.strip().lower() not in CONFIDENCE_LEVELS:
        reasons.append(f"confidence: must be one of {', '.join(CONFIDENCE_LEVELS)}")
        confidence = None
    else:
        confidence = confidence.strip().lower()

    buckets: dict[str, tuple[str, ...] | None] = {
        key: _collect_bucket(payload, key, reasons) for key in _REQUIRED_BUCKETS
    }

    for fact in buckets["known_facts"] or ():
        hedge = _HEDGE_RE.search(fact)
        if hedge is not None:
            reasons.append(
                f"known_facts: hedged claim ('{hedge.group(0)}') belongs in hypotheses"
            )

    dimensions, notes = _collect_dimensions(payload, reasons)

    if reasons:
        return RejectedProblem(
            id=problem_id,
            title=texts.get("title") or None,
            reasons=tuple(reasons),
            raw=dict(payload),
        )

    return Problem(
        id=problem_id,
        title=texts["title"],  # type: ignore[arg-type]
        mechanism=texts["mechanism"],  # type: ignore[arg-type]
        who_pays=texts["who_pays"],  # type: ignore[arg-type]
        current_workaround=texts["current_workaround"],  # type: ignore[arg-type]
        signal=texts["signal"],  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        known_facts=buckets["known_facts"],  # type: ignore[arg-type]
        hypotheses=buckets["hypotheses"],  # type: ignore[arg-type]
        open_questions=buckets["open_questions"],  # type: ignore[arg-type]
        dimensions=dimensions,
        dimension_notes=notes,
    )


def parse_problems(payload: object) -> tuple[list[Problem], list[RejectedProblem]]:
    """Validate the second model call's output into accepted and rejected lists."""
    if isinstance(payload, Mapping):
        payload = payload.get("problems")
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise ValidationError("problems: expected a list of objects")

    accepted: list[Problem] = []
    rejected: list[RejectedProblem] = []
    for index, entry in enumerate(payload):
        result = parse_problem(entry, fallback_id=f"p{index + 1:02d}")
        if isinstance(result, Problem):
            accepted.append(result)
        else:
            rejected.append(result)
    return accepted, rejected
