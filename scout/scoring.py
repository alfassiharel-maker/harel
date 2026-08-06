"""Deterministic ranking of problem candidates.

Standard library only. This module imports nothing from this project and no
third-party package, so the ranking stays runnable and testable on a machine
with no dependencies installed.

The scoring function takes primitive values rather than a project dataclass,
which is what keeps that promise: nothing here needs to know how a problem was
parsed or where it came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, Sequence

#: Every dimension is an integer on 0..5. The narrow scale is deliberate: a
#: language model cannot meaningfully distinguish 63 from 71, and pretending it
#: can would launder noise into a ranking.
DIMENSION_MIN: Final = 0
DIMENSION_MAX: Final = 5

#: Weights chosen against a single question: what makes a problem worth a
#: founder's next two years?
#:
#: - ``severity`` leads because a painful problem gets a budget; a mild one gets
#:   a "nice idea" and no purchase order.
#: - ``cost`` follows closely — it is the number that appears in a business case.
#: - ``breadth`` matters, but weighs less than pain: a small number of desperate
#:   buyers is a better first market than a large number of indifferent ones.
#: - ``startup_potential`` is separate from the three above because a real,
#:   expensive, widespread problem can still be a bad company (regulated,
#:   structurally owned by an incumbent, or a feature rather than a product).
#: - ``solution_maturity`` is the smallest weight and the only inverted one. It
#:   discounts crowded ground without vetoing it — mature solutions are evidence
#:   that a market exists, so the penalty is a tilt, not a wall.
WEIGHTS: Final[Mapping[str, float]] = {
    "severity": 0.30,
    "cost": 0.25,
    "breadth": 0.20,
    "startup_potential": 0.15,
    "solution_maturity": 0.10,
}

#: Dimensions where a high raw value means a *worse* opportunity, and so enter
#: the sum as ``DIMENSION_MAX - raw``.
INVERTED: Final[frozenset[str]] = frozenset({"solution_maturity"})

DIMENSIONS: Final[tuple[str, ...]] = tuple(WEIGHTS)

if abs(sum(WEIGHTS.values()) - 1.0) > 1e-9:  # pragma: no cover - import guard
    raise ValueError("scoring weights must sum to 1.0")


class ScoringError(ValueError):
    """A dimension was present but not a usable 0..5 integer.

    Distinct from a missing dimension: absence is an expected outcome that
    yields ``None``, whereas an out-of-range value means the producer is broken
    and silently clamping it would hide that.
    """


@dataclass(frozen=True, slots=True)
class Driver:
    """One dimension's contribution to a score, in final score points."""

    dimension: str
    raw: int
    effective: int
    weight: float
    contribution: float
    inverted: bool


@dataclass(frozen=True, slots=True)
class Score:
    """A composite score together with the drivers that produced it.

    Every composite score in this project reports its drivers; a rank a reader
    cannot take apart is a rank they cannot argue with.
    """

    value: float
    drivers: tuple[Driver, ...]

    @property
    def top_driver(self) -> Driver:
        return self.drivers[0]


def _validated(dimension: str, raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ScoringError(f"{dimension}: expected an int, got {type(raw).__name__}")
    if not DIMENSION_MIN <= raw <= DIMENSION_MAX:
        raise ScoringError(
            f"{dimension}: {raw} is outside {DIMENSION_MIN}..{DIMENSION_MAX}"
        )
    return raw


def score_problem(dimensions: Mapping[str, int | None]) -> Score | None:
    """Score one candidate, or return ``None`` when it cannot be scored.

    Returns ``None`` if any dimension is absent or ``None``. A partial score
    would be a plausible fake — the caller is expected to route these to an
    "insufficient evidence" list rather than rank them low.

    Raises :class:`ScoringError` for a value that is present but unusable.
    """
    values: dict[str, int] = {}
    for dimension in DIMENSIONS:
        raw = dimensions.get(dimension)
        if raw is None:
            return None
        values[dimension] = _validated(dimension, raw)

    drivers: list[Driver] = []
    for dimension, raw in values.items():
        inverted = dimension in INVERTED
        effective = DIMENSION_MAX - raw if inverted else raw
        weight = WEIGHTS[dimension]
        drivers.append(
            Driver(
                dimension=dimension,
                raw=raw,
                effective=effective,
                weight=weight,
                contribution=round(100.0 * weight * effective / DIMENSION_MAX, 2),
                inverted=inverted,
            )
        )

    # Sum the rounded contributions rather than re-deriving from the raw floats,
    # so the printed drivers always add up to the printed score.
    total = round(sum(driver.contribution for driver in drivers), 2)
    drivers.sort(key=lambda driver: (-driver.contribution, driver.dimension))
    return Score(value=total, drivers=tuple(drivers))


def rank(entries: Sequence[tuple[str, Score | None]]) -> list[str]:
    """Order candidate ids best-first; unscorable ids come last.

    Ties break on id so that two runs over the same data print the same order.
    """
    scored = [(cid, s) for cid, s in entries if s is not None]
    unscored = sorted(cid for cid, s in entries if s is None)
    scored.sort(key=lambda pair: (-pair[1].value, pair[0]))  # type: ignore[union-attr]
    return [cid for cid, _ in scored] + unscored
