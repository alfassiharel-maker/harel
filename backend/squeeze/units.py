"""Dimensioned quantities for the Squeeze policy language.

A footprint policy is arithmetic over four physical dimensions — bytes, time,
counts and money — and the whole point of giving it a language rather than a
config file is that `30 days` and `30 GiB` must not be addable. Every literal in
a `.sqz` source therefore carries a unit, and every unit maps to exactly one
dimension here.

Magnitudes are `Fraction`, never `float`. Two reasons, both load-bearing:

  * A byte count is a discrete amount of a physical resource. `0.1 GiB + 0.2 GiB`
    must equal `0.3 GiB` exactly, the same way the ledger refuses floats for
    money (CLAUDE.md, "Money is BIGINT minor units").
  * A compiled plan is compared against the previous compiled plan in review. If
    a plan's totals drifted by one ULP between two runs on the same source, the
    diff would be noise and reviewers would stop reading it.

`month` is defined as exactly 30 days and `year` as exactly 365 days. A policy
compiler has no calendar and must not acquire one: retention arithmetic that
depended on which months a window happened to span would make the same source
compile to different plans on different days.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

__all__ = [
    "BYTES_PER_GIB",
    "BYTES_PER_MIB",
    "SECONDS_PER_DAY",
    "UNITS",
    "Dimension",
    "Quantity",
    "UnknownUnitError",
    "unit_dimension",
]


class Dimension(str, Enum):
    """What a quantity measures. Mixing two of these is a compile error."""

    BYTES = "bytes"
    TIME = "time"
    COUNT = "count"
    MONEY = "money"
    #: Dimensionless — compression ratios, fidelity fractions, multipliers.
    SCALAR = "scalar"


BYTES_PER_KIB = 1024
BYTES_PER_MIB = 1024 * 1024
BYTES_PER_GIB = 1024 * 1024 * 1024
BYTES_PER_TIB = 1024 * 1024 * 1024 * 1024
SECONDS_PER_DAY = 86_400

# The unit table. Binary prefixes are powers of 1024 and decimal prefixes are
# powers of 1000, because a storage bill quotes GB and a memory limit quotes GiB;
# a language that silently conflated them would understate a cold-tier estimate
# by 7%.
UNITS: dict[str, tuple[Dimension, Fraction]] = {
    "bytes": (Dimension.BYTES, Fraction(1)),
    "byte": (Dimension.BYTES, Fraction(1)),
    "B": (Dimension.BYTES, Fraction(1)),
    "KiB": (Dimension.BYTES, Fraction(BYTES_PER_KIB)),
    "MiB": (Dimension.BYTES, Fraction(BYTES_PER_MIB)),
    "GiB": (Dimension.BYTES, Fraction(BYTES_PER_GIB)),
    "TiB": (Dimension.BYTES, Fraction(BYTES_PER_TIB)),
    "KB": (Dimension.BYTES, Fraction(1_000)),
    "MB": (Dimension.BYTES, Fraction(1_000_000)),
    "GB": (Dimension.BYTES, Fraction(1_000_000_000)),
    "TB": (Dimension.BYTES, Fraction(1_000_000_000_000)),
    "us": (Dimension.TIME, Fraction(1, 1_000_000)),
    "ms": (Dimension.TIME, Fraction(1, 1_000)),
    "s": (Dimension.TIME, Fraction(1)),
    "sec": (Dimension.TIME, Fraction(1)),
    "min": (Dimension.TIME, Fraction(60)),
    "h": (Dimension.TIME, Fraction(3_600)),
    "hours": (Dimension.TIME, Fraction(3_600)),
    "day": (Dimension.TIME, Fraction(SECONDS_PER_DAY)),
    "days": (Dimension.TIME, Fraction(SECONDS_PER_DAY)),
    "week": (Dimension.TIME, Fraction(7 * SECONDS_PER_DAY)),
    "weeks": (Dimension.TIME, Fraction(7 * SECONDS_PER_DAY)),
    "month": (Dimension.TIME, Fraction(30 * SECONDS_PER_DAY)),
    "months": (Dimension.TIME, Fraction(30 * SECONDS_PER_DAY)),
    "year": (Dimension.TIME, Fraction(365 * SECONDS_PER_DAY)),
    "years": (Dimension.TIME, Fraction(365 * SECONDS_PER_DAY)),
    "records": (Dimension.COUNT, Fraction(1)),
    "record": (Dimension.COUNT, Fraction(1)),
    "rows": (Dimension.COUNT, Fraction(1)),
    "users": (Dimension.COUNT, Fraction(1)),
    "messages": (Dimension.COUNT, Fraction(1)),
    "params": (Dimension.COUNT, Fraction(1)),
    # Money is minor units of one currency, named by the policy's `currency`
    # setting. The language never converts between currencies: an FX rate is
    # a runtime fact, and a compiler that embedded one would produce plans that
    # were wrong the next day.
    "minor": (Dimension.MONEY, Fraction(1)),
}


class UnknownUnitError(ValueError):
    """Raised for a unit token that is not in `UNITS`."""


def unit_dimension(unit: str) -> Dimension:
    entry = UNITS.get(unit)
    if entry is None:
        raise UnknownUnitError(unit)
    return entry[0]


@dataclass(frozen=True, order=False)
class Quantity:
    """An exact amount in canonical units for its dimension.

    Canonical units are: bytes, seconds, one (count), minor currency units.
    `unit` keeps the token the author wrote so diagnostics and the emitted
    report can echo `30 days` rather than `2592000 s`.
    """

    magnitude: Fraction
    dimension: Dimension
    unit: str

    @classmethod
    def parse(cls, amount: Fraction, unit: str) -> Quantity:
        entry = UNITS.get(unit)
        if entry is None:
            raise UnknownUnitError(unit)
        dimension, scale = entry
        return cls(magnitude=amount * scale, dimension=dimension, unit=unit)

    @classmethod
    def scalar(cls, amount: Fraction) -> Quantity:
        return cls(magnitude=amount, dimension=Dimension.SCALAR, unit="")

    def bytes_exact(self) -> int:
        """Byte count, rounded up.

        Rounding up rather than to nearest: a footprint plan that under-reports
        is worse than one that over-reports by a byte, because the number is
        compared against a hard limit.
        """
        self._require(Dimension.BYTES)
        return _ceil(self.magnitude)

    def seconds(self) -> Fraction:
        self._require(Dimension.TIME)
        return self.magnitude

    def count(self) -> Fraction:
        self._require(Dimension.COUNT)
        return self.magnitude

    def as_fraction(self) -> Fraction:
        return self.magnitude

    def _require(self, dimension: Dimension) -> None:
        if self.dimension is not dimension:
            raise TypeError(f"expected a {dimension.value} quantity, got {self.dimension.value}")

    def __str__(self) -> str:
        if self.dimension is Dimension.SCALAR:
            return _format_fraction(self.magnitude)
        scale = UNITS[self.unit][1] if self.unit in UNITS else Fraction(1)
        return f"{_format_fraction(self.magnitude / scale)} {self.unit}".strip()


def _ceil(value: Fraction) -> int:
    whole = value.numerator // value.denominator
    return whole if value.denominator == 1 else whole + 1


def _format_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{float(value):.4g}"
