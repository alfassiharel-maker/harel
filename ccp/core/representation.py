"""The CCP representation, and the build that produces it.

This module is the algorithm end to end:

    units in
      -> chunk each unit                       (shared structure detection)
      -> score it against already-stored units (base selection -- the Copy)
      -> difference it against its base        (the Change)
      -> keep whichever storage is smaller     (the decision)
      -> representation out

and, in the other direction, `materialize` executes a change program against its
base to hand back the exact original bytes -- the Paste.

Two rules are enforced rather than assumed, both carried over from the storage
experiment where they were load-bearing:

*   **No unit is ever forced into a delta.** A unit is stored as a change program
    only when the encoded program is genuinely smaller than the unit. Otherwise it
    is stored in full. The decision is made on measured encoded length, not on an
    estimate.
*   **Every reconstruction is verified.** Each record carries the digest of the
    unit it must produce, and `materialize` checks it. A representation that
    cannot give back what it was given is not a representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .change_program import ChangeProgram, paste
from .chunking import (
    DEFAULT_AVG_CHUNK,
    DEFAULT_MAX_CHUNK,
    DEFAULT_MIN_CHUNK,
    chunk_offsets_by_digest,
    chunk_unit,
)
from .differ import build_change_program
from .similarity import DEFAULT_MIN_SIMILARITY, SimilarityIndex
from .units import Unit, UnitSource, unit_digest

KIND_LITERAL = "literal"
KIND_DERIVED = "derived"


class CCPIntegrityError(RuntimeError):
    """A reconstruction did not match the digest recorded for it."""


@dataclass(frozen=True)
class BuildConfig:
    """Parameters of a build. Recorded in the container so a read can check them."""

    min_chunk: int = DEFAULT_MIN_CHUNK
    avg_chunk: int = DEFAULT_AVG_CHUNK
    max_chunk: int = DEFAULT_MAX_CHUNK
    min_similarity: float = DEFAULT_MIN_SIMILARITY


@dataclass(frozen=True)
class UnitRecord:
    """How one unit is represented.

    A record is either the unit's bytes, or a base reference plus the program
    that turns that base into the unit. Nothing else.
    """

    uid: str
    kind: str
    size: int
    digest: bytes
    base_uid: Optional[str] = None
    program: Optional[ChangeProgram] = None

    @property
    def is_derived(self) -> bool:
        return self.kind == KIND_DERIVED

    def stored_bytes(self) -> int:
        """Payload this record contributes, excluding index overhead."""
        if self.program is not None:
            return self.program.encoded_size()
        return self.size


@dataclass
class BuildStats:
    """What the build actually did. Every field is counted, never estimated."""

    units: int = 0
    literals: int = 0
    derived: int = 0
    original_bytes: int = 0
    stored_payload_bytes: int = 0
    copied_bytes: int = 0
    added_bytes: int = 0

    @property
    def payload_saving(self) -> Optional[float]:
        """Fraction of payload avoided, or None when there was no input.

        Payload only -- the container index is counted separately by
        `CCPContainer`, so this number is never quietly flattered by leaving
        overhead out.
        """
        if self.original_bytes == 0:
            return None
        return 1.0 - (self.stored_payload_bytes / self.original_bytes)


@dataclass
class CCPModel:
    """A set of units represented as bases plus change programs."""

    config: BuildConfig = field(default_factory=BuildConfig)
    records: Dict[str, UnitRecord] = field(default_factory=dict)
    literals: Dict[str, bytes] = field(default_factory=dict)
    stats: BuildStats = field(default_factory=BuildStats)

    # -- inspection -------------------------------------------------------

    def __contains__(self, uid: str) -> bool:
        return uid in self.records

    def __len__(self) -> int:
        return len(self.records)

    def uids(self) -> List[str]:
        return sorted(self.records)

    def record(self, uid: str) -> UnitRecord:
        try:
            return self.records[uid]
        except KeyError:
            raise KeyError(f"unknown unit {uid!r}") from None

    def base_bytes(self, uid: str) -> bytes:
        """The stored bytes of a full-stored unit."""
        try:
            return self.literals[uid]
        except KeyError:
            raise KeyError(f"{uid!r} is not stored in full") from None

    # -- Paste ------------------------------------------------------------

    def materialize(self, uid: str, verify: bool = True) -> bytes:
        """Reconstruct a unit exactly. This is the Paste step."""
        record = self.record(uid)
        if record.kind == KIND_LITERAL:
            data = self.literals[uid]
        else:
            if record.base_uid is None or record.program is None:
                raise CCPIntegrityError(f"derived record {uid!r} is incomplete")
            base = self.literals.get(record.base_uid)
            if base is None:
                raise CCPIntegrityError(
                    f"base {record.base_uid!r} of {uid!r} is not stored in full"
                )
            data = paste(base, record.program)
        if verify and unit_digest(data) != record.digest:
            raise CCPIntegrityError(
                f"reconstruction of {uid!r} does not match its recorded digest"
            )
        return data

    def materialize_all(self, verify: bool = True) -> Iterator[Tuple[str, bytes]]:
        for uid in self.uids():
            yield uid, self.materialize(uid, verify=verify)


def build_model(
    source: UnitSource, config: Optional[BuildConfig] = None
) -> CCPModel:
    """Run the CCP algorithm over a unit source and return the representation."""
    config = config or BuildConfig()
    model = CCPModel(config=config)
    index = SimilarityIndex()
    # Base chunk indexes are reused across targets: one base commonly serves many
    # units, and re-chunking it per target would make the build quadratic.
    base_indexes: Dict[str, Dict[bytes, List[int]]] = {}

    for unit in source.units():
        _absorb_unit(unit, model, index, base_indexes, config)
    return model


def _absorb_unit(
    unit: Unit,
    model: CCPModel,
    index: SimilarityIndex,
    base_indexes: Dict[str, Dict[bytes, List[int]]],
    config: BuildConfig,
) -> None:
    if unit.uid in model.records:
        raise ValueError(f"duplicate unit id {unit.uid!r}")

    model.stats.units += 1
    model.stats.original_bytes += unit.size

    chunks = chunk_unit(unit.data, config.min_chunk, config.avg_chunk, config.max_chunk)
    candidate = index.best_candidate(chunks, min_similarity=config.min_similarity)

    program: Optional[ChangeProgram] = None
    base_uid: Optional[str] = None
    if candidate is not None:
        base_uid = candidate.uid
        base_data = model.literals[base_uid]
        if base_uid not in base_indexes:
            base_indexes[base_uid] = chunk_offsets_by_digest(
                chunk_unit(
                    base_data, config.min_chunk, config.avg_chunk, config.max_chunk
                )
            )
        program = build_change_program(
            base_data,
            unit.data,
            config.min_chunk,
            config.avg_chunk,
            config.max_chunk,
            base_index=base_indexes[base_uid],
        )

    # The decision. A delta is kept only when it is really smaller; a unit is
    # never forced into one to make a statistic look better.
    if program is not None and base_uid is not None and program.encoded_size() < unit.size:
        model.records[unit.uid] = UnitRecord(
            uid=unit.uid,
            kind=KIND_DERIVED,
            size=unit.size,
            digest=unit.digest,
            base_uid=base_uid,
            program=program,
        )
        model.stats.derived += 1
        model.stats.stored_payload_bytes += program.encoded_size()
        model.stats.copied_bytes += program.copied_bytes
        model.stats.added_bytes += program.added_bytes
        return

    model.records[unit.uid] = UnitRecord(
        uid=unit.uid, kind=KIND_LITERAL, size=unit.size, digest=unit.digest
    )
    model.literals[unit.uid] = unit.data
    model.stats.literals += 1
    model.stats.stored_payload_bytes += unit.size
    # Only full-stored units become base candidates, so every derived unit stays
    # exactly one hop from real bytes and delta chains cannot form.
    index.add_unit(unit.uid, unit.size, chunks)
