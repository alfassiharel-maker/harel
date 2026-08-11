"""Change detection: turn (base, target) into a change program.

This is the Change step of Copy-Change-Paste. The algorithm is deterministic and
single-pass over the target:

    1. Chunk the base and index every chunk digest to the offsets it occurs at.
    2. Walk the target chunk by chunk. A chunk whose digest is in the index is a
       candidate match.
    3. Confirm the candidate by direct byte comparison, then extend the match as
       far as it will go -- forward through whatever follows, and backward into
       bytes already queued as literal. Extension is what turns a chunk-sized
       coincidence into a long COPY, and it is why the output does not depend on
       boundaries falling in convenient places.
    4. Anything not covered by a confirmed match becomes an ADD.

Matches are always confirmed by comparing bytes, never trusted from the digest
alone, so a hash collision costs one wasted comparison and can never produce a
wrong unit.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .change_program import Add, ChangeProgram, Copy, Instruction, merge_adjacent
from .chunking import (
    DEFAULT_AVG_CHUNK,
    DEFAULT_MAX_CHUNK,
    DEFAULT_MIN_CHUNK,
    chunk_offsets_by_digest,
    chunk_unit,
)

# A digest occurring at very many offsets (a run of zero-filled chunks, say)
# would make match selection quadratic. Only this many candidate offsets are
# tried per position; the cap costs a little match quality on degenerate input
# and keeps differencing linear. ENGINEERING DECISION.
MAX_CANDIDATE_OFFSETS = 16


def _extend_forward(
    base: bytes, base_pos: int, target: bytes, target_pos: int
) -> int:
    """Bytes that continue to agree after the two positions given."""
    length = 0
    limit = min(len(base) - base_pos, len(target) - target_pos)
    while length < limit and base[base_pos + length] == target[target_pos + length]:
        length += 1
    return length


def _extend_backward(
    base: bytes, base_pos: int, target: bytes, target_pos: int, floor: int
) -> int:
    """Bytes that agree before the two positions, not passing `floor` in target.

    Extending backward absorbs bytes already queued as literal into the COPY,
    which is what stops a match that begins mid-chunk from paying twice.
    """
    length = 0
    while (
        base_pos - length - 1 >= 0
        and target_pos - length - 1 >= floor
        and base[base_pos - length - 1] == target[target_pos - length - 1]
    ):
        length += 1
    return length


def _best_match(
    base: bytes,
    target: bytes,
    target_pos: int,
    literal_floor: int,
    offsets: List[int],
) -> Optional[Tuple[int, int, int]]:
    """Pick the longest confirmed match. Returns (src_offset, start, length).

    `start` may precede `target_pos` when the match extended backward.
    """
    best: Optional[Tuple[int, int, int]] = None
    for base_offset in offsets[:MAX_CANDIDATE_OFFSETS]:
        forward = _extend_forward(base, base_offset, target, target_pos)
        if forward == 0:
            continue  # digest agreed, bytes did not: a collision, correctly ignored
        backward = _extend_backward(
            base, base_offset, target, target_pos, literal_floor
        )
        total = forward + backward
        if best is None or total > best[2]:
            best = (base_offset - backward, target_pos - backward, total)
    return best


def build_change_program(
    base: bytes,
    target: bytes,
    min_chunk: int = DEFAULT_MIN_CHUNK,
    avg_chunk: int = DEFAULT_AVG_CHUNK,
    max_chunk: int = DEFAULT_MAX_CHUNK,
    base_index: Optional[Dict[bytes, List[int]]] = None,
) -> ChangeProgram:
    """Produce a program that reconstructs `target` from `base`.

    `base_index` may be supplied when one base serves many targets, so the base
    is chunked once instead of once per target. That is the common case in a
    build and it is the difference between linear and quadratic work.
    """
    if base_index is None:
        base_index = chunk_offsets_by_digest(
            chunk_unit(base, min_chunk, avg_chunk, max_chunk)
        )

    instructions: List[Instruction] = []
    literal_start = 0  # first target byte not yet emitted
    target_chunks = chunk_unit(target, min_chunk, avg_chunk, max_chunk)

    for chunk in target_chunks:
        if chunk.offset < literal_start:
            # A previous match already swallowed this chunk.
            continue
        offsets = base_index.get(chunk.digest)
        if not offsets:
            continue
        match = _best_match(base, target, chunk.offset, literal_start, offsets)
        if match is None:
            continue
        src_offset, match_start, length = match
        if match_start > literal_start:
            instructions.append(Add(target[literal_start:match_start]))
        instructions.append(Copy(src_offset, length))
        literal_start = match_start + length

    if literal_start < len(target):
        instructions.append(Add(target[literal_start:]))

    return ChangeProgram(merge_adjacent(instructions))
