"""The checked model: what a `.sqz` source means once it is known to be valid.

Every field that the source may legitimately omit is `X | None`, never a
defaulted zero. That is the same rule the analytics engine runs on (CLAUDE.md:
"Missing data is `None`, never `0`"), and it matters more here than there: a
class whose growth rate nobody has measured yet must show up in the plan as
*unknown*, because a footprint of `0 bytes` would be silently approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from .units import Quantity

__all__ = [
    "DATA_KINDS",
    "Codec",
    "CodecKind",
    "DataClass",
    "GrowthRate",
    "Objective",
    "Policy",
    "Program",
    "Requirement",
    "Retention",
    "Sensitivity",
    "Tier",
]

#: The shapes of data a footprint policy can talk about. A kind decides which
#: codecs are even candidates: a delta encoder is excellent on a monotonic
#: timeseries and useless on an already-compressed blob.
DATA_KINDS = frozenset({"timeseries", "relational", "document", "blob", "tensor", "context"})


class CodecKind(str, Enum):
    LOSSLESS = "lossless"
    LOSSY = "lossy"


class Objective(str, Enum):
    BYTES = "bytes"
    COST = "cost"
    LATENCY = "latency"


class Sensitivity(str, Enum):
    """How much freedom the planner has with a class.

    `clinical` is the important one: readiness, HRV and injury-risk inputs feed
    numbers a person makes training decisions on, and a lossy codec would make
    the stored value differ from the measured value. The planner refuses lossy
    codecs on clinical classes rather than trading fidelity for bytes.
    """

    ROUTINE = "routine"
    PERSONAL = "personal"
    CLINICAL = "clinical"


@dataclass(frozen=True)
class Codec:
    name: str
    kind: CodecKind
    #: Declared compression ratio (input bytes / output bytes). `None` when the
    #: author has not measured it — the planner then cannot use this codec.
    ratio: Fraction | None
    #: CPU time to code one MiB. `None` means unmeasured, which makes every
    #: latency constraint involving this codec unprovable rather than satisfied.
    cpu_per_mib: Quantity | None
    #: Retained fraction of the original signal, 0-1. Required for lossy codecs.
    fidelity: Fraction | None
    applies_to: frozenset[str]
    #: Name of a real implementation in `implementations.py`, when one exists.
    #: Without it the declared ratio cannot be verified against measured bytes.
    implementation: str | None
    line: int


@dataclass(frozen=True)
class Tier:
    name: str
    medium: str
    #: Read-latency budget for this tier. `None` = unconstrained.
    latency: Quantity | None
    #: Money (minor units) per GiB per month. `None` = unpriced, so cost totals
    #: for this tier are unknown rather than free.
    unit_cost_per_gib_month: Fraction | None
    codecs: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class GrowthRate:
    """`amount` records per `scope` per `period`."""

    amount: Fraction
    scope: str
    period: Quantity


@dataclass(frozen=True)
class Retention:
    tier: str
    window: Quantity
    line: int


@dataclass(frozen=True)
class DataClass:
    name: str
    kind: str
    record_size: Quantity | None
    growth: GrowthRate | None
    retain: tuple[Retention, ...]
    sensitivity: Sensitivity
    line: int


@dataclass(frozen=True)
class Requirement:
    """A constraint the plan must satisfy, or a flag it must honour.

    `subject op value` for comparisons (`fidelity >= 0.98`), or `subject` alone
    for a flag (`explanation`).
    """

    subject: str
    operator: str | None
    value: Fraction | Quantity | None
    line: int


@dataclass(frozen=True)
class Policy:
    name: str
    objective: Objective
    #: Number of athletes the estimate is for. `None` → per-scope totals only.
    population: Fraction | None
    horizon: Quantity | None
    limits: dict[str, Quantity]
    requirements: tuple[Requirement, ...]
    classes: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Program:
    version: int
    currency: str
    codecs: dict[str, Codec]
    tiers: dict[str, Tier]
    classes: dict[str, DataClass]
    policies: dict[str, Policy]
