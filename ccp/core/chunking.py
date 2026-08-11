"""Shared structure detection: content-defined chunking.

The Core has to answer "do these two units share structure?" before it can pick a
base. Cutting units at fixed offsets cannot answer it: inserting one byte at the
front of a file shifts every later boundary and destroys every match, which is
exactly the limitation `experiments/ccp/FINDINGS.md` records for the fixed-region
storage engine ("a repeated structure shifted by even one byte is invisible").

Content-defined chunking cuts at boundaries chosen by the *content* itself. A
rolling hash runs over the bytes and a cut is made wherever the hash matches a
pattern. Insert a byte and the boundaries around the insertion move with it --
every chunk further along is unchanged and still matches. This is the standard
fix, named in the findings as the missing piece, and it is implemented here
rather than assumed.

The rolling function is a gear hash: one table lookup and a shift per byte, so
chunking a unit is a single linear pass with no multiplication. The table is
derived deterministically from a fixed seed, so chunk boundaries are reproducible
on any machine and across runs -- a build that chunked differently on two
machines would produce incompatible representations.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, Iterator, List, Tuple

_MASK64 = (1 << 64) - 1

# Chunk sizing. Small chunks find more shared structure but cost more index
# entries and more instructions; large chunks are cheaper but miss fine-grained
# sharing. These defaults sit where deduplicating systems generally land.
# ENGINEERING DECISION -- exposed as parameters, not hard-coded policy.
DEFAULT_MIN_CHUNK = 512
DEFAULT_AVG_CHUNK = 2048
DEFAULT_MAX_CHUNK = 8192

# Strong chunk identity. 16 bytes: collision probability is negligible at any
# corpus size this will see, and the index stays small. Chunk equality is
# confirmed by direct byte comparison before it is ever acted on, so a collision
# would cost a wasted comparison, never a wrong result.
CHUNK_DIGEST_BYTES = 16


def _build_gear_table(seed: int = 0x43435020) -> Tuple[int, ...]:
    """256 deterministic 64-bit values, one per byte value."""
    table: List[int] = []
    for value in range(256):
        digest = hashlib.blake2b(
            value.to_bytes(2, "little") + seed.to_bytes(8, "little"),
            digest_size=8,
        ).digest()
        table.append(int.from_bytes(digest, "little"))
    return tuple(table)


GEAR_TABLE = _build_gear_table()


@dataclass(frozen=True)
class Chunk:
    """A content-defined span of a unit, with its strong identity."""

    offset: int
    length: int
    digest: bytes

    @property
    def end(self) -> int:
        return self.offset + self.length


def _mask_for(avg_size: int) -> int:
    """A mask whose bit count gives the requested average chunk length."""
    if avg_size < 2:
        raise ValueError("avg_size must be at least 2")
    bits = max(1, (avg_size - 1).bit_length())
    return (1 << bits) - 1


def chunk_boundaries(
    data: bytes,
    min_size: int = DEFAULT_MIN_CHUNK,
    avg_size: int = DEFAULT_AVG_CHUNK,
    max_size: int = DEFAULT_MAX_CHUNK,
) -> Iterator[Tuple[int, int]]:
    """Yield (start, end) spans cut at content-defined boundaries."""
    if not (0 < min_size <= avg_size <= max_size):
        raise ValueError("require 0 < min_size <= avg_size <= max_size")
    length = len(data)
    if length == 0:
        return

    mask = _mask_for(avg_size)
    table = GEAR_TABLE
    start = 0
    digest = 0
    index = 0
    while index < length:
        digest = ((digest << 1) + table[data[index]]) & _MASK64
        index += 1
        span = index - start
        if span < min_size:
            continue
        # A cut happens on a content match, or is forced at max_size so a
        # pathological input cannot produce one unbounded chunk.
        if (digest & mask) == 0 or span >= max_size:
            yield start, index
            start = index
            digest = 0
    if start < length:
        yield start, length


def chunk_unit(
    data: bytes,
    min_size: int = DEFAULT_MIN_CHUNK,
    avg_size: int = DEFAULT_AVG_CHUNK,
    max_size: int = DEFAULT_MAX_CHUNK,
) -> List[Chunk]:
    """Chunk a unit and compute each chunk's strong digest."""
    chunks: List[Chunk] = []
    for start, end in chunk_boundaries(data, min_size, avg_size, max_size):
        digest = hashlib.blake2b(
            data[start:end], digest_size=CHUNK_DIGEST_BYTES
        ).digest()
        chunks.append(Chunk(offset=start, length=end - start, digest=digest))
    return chunks


def chunk_offsets_by_digest(chunks: List[Chunk]) -> Dict[bytes, List[int]]:
    """Map each chunk digest to the offsets where it occurs.

    A list, not a single offset: a repeated chunk inside one unit is exactly the
    internal redundancy the fixed-region engine could not see, and keeping every
    occurrence lets the differ pick the one that extends furthest.
    """
    index: Dict[bytes, List[int]] = {}
    for chunk in chunks:
        index.setdefault(chunk.digest, []).append(chunk.offset)
    return index
