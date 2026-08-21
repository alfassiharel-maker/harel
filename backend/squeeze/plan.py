"""The plan: what the compiler emits and a human reviews.

A plan is the whole output of the language. It is a data structure, not a
side effect: nothing in this package compresses, deletes or migrates anything.
The plan is reviewed, committed, and then applied by whatever owns the storage,
exactly as `database/migrations/` are reviewed SQL that a person runs.

Two properties are contractual and covered by tests:

  * **Drivers sum to the total.** `sum(d.saved_bytes for d in drivers)` equals
    `saved_bytes` exactly — the same discipline the readiness score follows,
    where contributions must sum to `score - 50`. A driver list that only
    roughly explains its total is a decoration.
  * **Unknowns stay unknown.** A footprint that could not be computed is `None`
    and `coverage` drops below 1. A limit check over incomplete input is `None`,
    never `True`.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .diagnostics import Diagnostic

__all__ = ["Allocation", "Driver", "LimitCheck", "Plan", "RejectedCodec"]


@dataclass(frozen=True)
class RejectedCodec:
    codec: str
    #: Why the planner could not use it, in the words the report prints.
    reason: str


@dataclass(frozen=True)
class Allocation:
    """One class's residency in one tier, and the codec chosen for it."""

    data_class: str
    kind: str
    tier: str
    medium: str
    codec: str | None
    #: How long data lives in this tier, in days. Cumulative retention windows
    #: mean this is `window - previous_window`, clipped to the policy horizon.
    residency_days: Fraction | None
    raw_bytes: int | None
    stored_bytes: int | None
    ratio: Fraction | None
    fidelity: Fraction | None
    monthly_cost_minor: Fraction | None
    rejected: tuple[RejectedCodec, ...]
    #: Set when this allocation could not be sized. `None` when it could.
    unknown_reason: str | None

    @property
    def saved_bytes(self) -> int | None:
        if self.raw_bytes is None or self.stored_bytes is None:
            return None
        return self.raw_bytes - self.stored_bytes


@dataclass(frozen=True)
class Driver:
    """One contribution to the total saving, in bytes."""

    name: str
    saved_bytes: int
    #: Share of the total saving, 0-1. `None` when nothing was saved at all, so
    #: the caller never divides by zero to print "0%" of nothing.
    share: Fraction | None
    detail: str


@dataclass(frozen=True)
class LimitCheck:
    name: str
    limit_bytes: int
    actual_bytes: int | None
    #: `None` when the actual footprint is not fully known — an unprovable limit
    #: is not a satisfied limit.
    satisfied: bool | None


@dataclass(frozen=True)
class Plan:
    policy: str
    currency: str
    allocations: tuple[Allocation, ...]
    total_raw_bytes: int | None
    total_stored_bytes: int | None
    saved_bytes: int | None
    drivers: tuple[Driver, ...]
    per_user_bytes: int | None
    monthly_cost_minor: Fraction | None
    limits: tuple[LimitCheck, ...]
    #: Fraction of allocations that could be sized, 0-1.
    coverage: Fraction
    diagnostics: tuple[Diagnostic, ...]

    @property
    def ratio(self) -> Fraction | None:
        if not self.total_raw_bytes or self.total_stored_bytes in (None, 0):
            return None
        stored = self.total_stored_bytes
        if stored is None:
            return None
        return Fraction(self.total_raw_bytes, stored)

    @property
    def is_provable(self) -> bool:
        """True when every declared allocation was sized and every limit checked."""
        return self.coverage == 1 and all(check.satisfied is not None for check in self.limits)
