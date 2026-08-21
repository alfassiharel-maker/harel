"""Concrete syntax tree for `.sqz` sources.

The parser's only job is shape: `keyword name [: kind] { setting* }`, where a
setting is a key followed by atoms. It does not know what `ratio` or `retain`
mean — that lives in `checker.py`. Keeping meaning out of the parser is what
lets a new setting be added by touching one file instead of three, and it is
why a misspelled setting produces "unknown setting `ration` in codec" from the
checker rather than a parse error pointing at the wrong line.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .units import Quantity

__all__ = [
    "Atom",
    "Block",
    "NumberAtom",
    "QuantityAtom",
    "Setting",
    "SourceFile",
    "StringAtom",
    "WordAtom",
]


@dataclass(frozen=True)
class _Positioned:
    line: int
    column: int


@dataclass(frozen=True)
class QuantityAtom(_Positioned):
    """A number with a unit: `30 days`, `1.2 KiB`, `9 minor`."""

    quantity: Quantity


@dataclass(frozen=True)
class NumberAtom(_Positioned):
    """A bare number: a ratio, a fidelity fraction, a multiplier."""

    value: Fraction


@dataclass(frozen=True)
class WordAtom(_Positioned):
    text: str


@dataclass(frozen=True)
class StringAtom(_Positioned):
    text: str


Atom = QuantityAtom | NumberAtom | WordAtom | StringAtom


@dataclass(frozen=True)
class Setting:
    key: str
    atoms: tuple[Atom, ...]
    line: int
    column: int

    def words(self) -> tuple[str, ...]:
        return tuple(a.text for a in self.atoms if isinstance(a, WordAtom))


@dataclass(frozen=True)
class Block:
    #: `codec`, `tier`, `class` or `policy`.
    keyword: str
    name: str
    #: The `: kind` annotation on a `class` block; `None` elsewhere.
    kind: str | None
    settings: tuple[Setting, ...]
    line: int
    column: int


@dataclass(frozen=True)
class SourceFile:
    version: int | None
    blocks: tuple[Block, ...]
    #: Settings written at file scope (`version`, `currency`).
    preamble: tuple[Setting, ...]
