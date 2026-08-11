"""The semantic contract the Runtime operates under.

Deliberately minimal. This is the contract needed to run the MVP, not a theory of
contracts. Anything that cannot be defined reliably from what is already built is
named as an open question and excluded from the MVP rather than guessed at.

The whole of version 1 is one sentence: **the Runtime may change how much work it
does, and nothing about what it returns.**

That is a narrow contract on purpose. `CCP_Technical_Clarifications` §7 says the
success criterion is not automatically `output == original output`, and that
allowed/required behaviour and allowed/forbidden transformations have to be
stated per system. Version 1 takes the strictest position available — every
returned byte is bit-exact — because that is the position the Core already
supports and can verify. A looser contract, where output may legitimately differ
from the original, is a real design question and is listed as open, not invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

CONTRACT_VERSION = "1.0"


# --- what goes in ----------------------------------------------------------

INPUT = (
    "A CCP container produced by ccp.core, as bytes or a path. The container is "
    "self-describing and self-verifying: it carries its chunking configuration "
    "and a digest for every unit."
)

# --- what the Runtime may change -------------------------------------------

MAY_CHANGE: Tuple[str, ...] = (
    "how many instructions it executes to answer a request",
    "how many bytes it reads to answer a request",
    "the order in which it visits instructions within one request",
    "whether it materialises a whole unit or only part of one",
)

# --- what must not change --------------------------------------------------

MUST_BE_BIT_EXACT: Tuple[str, ...] = (
    "materialize(uid) equals the original unit, byte for byte",
    "read_range(uid, offset, length) equals materialize(uid)[offset:offset+length]",
    "results do not depend on request order, on how many requests preceded them, "
    "or on any timing",
)

# --- what counts as a valid output -----------------------------------------

VALID_OUTPUT = (
    "Bytes identical to the original unit or to the requested slice of it. A full "
    "materialisation is additionally checked against the digest recorded for that "
    "unit at build time, and fails loudly rather than returning bytes that do not "
    "match. A range read returns a slice of a verified representation: the digest "
    "covers the whole unit, so a range read cannot re-verify it on its own -- call "
    "verify() to establish integrity of the representation, after which the "
    "runtime reports itself as verified."
)

# --- operations ------------------------------------------------------------

SUPPORTED_OPERATIONS: Tuple[str, ...] = (
    "load",
    "verify",
    "materialize",
    "read_range",
    "read_many",
    "units",
    "stat",
    "groups",
    "ledger",
)

UNSUPPORTED_OPERATIONS: Tuple[str, ...] = (
    "mutate: adding, removing or updating a unit in a loaded representation",
    "transform: returning content that is not the original bytes",
    "stream: operating on a container that is not fully available",
    "execute a target program, or decide how one should run",
    "concurrent access from multiple threads to one runtime instance",
)

OPEN_DESIGN_QUESTIONS: Tuple[str, ...] = (
    "a contract where output may legitimately differ from the original bytes "
    "(allowed behaviour, required behaviour, permitted approximation) -- version 1 "
    "takes the strict position instead of guessing at a looser one",
    "execution management: deciding the manner in which a target program runs",
    "lazy or memory-mapped loading, so a representation larger than memory can be "
    "opened -- version 1 loads the whole container",
)


@dataclass(frozen=True)
class SemanticContract:
    """The contract as data, so a caller can inspect it rather than read prose."""

    version: str = CONTRACT_VERSION
    input_description: str = INPUT
    may_change: Tuple[str, ...] = MAY_CHANGE
    must_be_bit_exact: Tuple[str, ...] = MUST_BE_BIT_EXACT
    valid_output: str = VALID_OUTPUT
    supported_operations: Tuple[str, ...] = SUPPORTED_OPERATIONS
    unsupported_operations: Tuple[str, ...] = UNSUPPORTED_OPERATIONS
    open_design_questions: Tuple[str, ...] = OPEN_DESIGN_QUESTIONS

    def supports(self, operation: str) -> bool:
        return operation in self.supported_operations

    def describe(self) -> str:
        lines = [f"CCP Runtime semantic contract v{self.version}", ""]
        lines.append(f"input: {self.input_description}")
        lines.append("")
        lines.append("the runtime may change:")
        lines.extend(f"  - {item}" for item in self.may_change)
        lines.append("")
        lines.append("bit-exact, always:")
        lines.extend(f"  - {item}" for item in self.must_be_bit_exact)
        lines.append("")
        lines.append(f"valid output: {self.valid_output}")
        lines.append("")
        lines.append("supported in this MVP:")
        lines.extend(f"  - {item}" for item in self.supported_operations)
        lines.append("")
        lines.append("not supported yet:")
        lines.extend(f"  - {item}" for item in self.unsupported_operations)
        lines.append("")
        lines.append("open design questions, excluded from the MVP:")
        lines.extend(f"  - {item}" for item in self.open_design_questions)
        return "\n".join(lines)


CONTRACT = SemanticContract()
