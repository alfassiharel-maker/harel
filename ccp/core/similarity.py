"""Base selection: deciding which stored unit a new unit should be built from.

A base is not a synthesised artefact. It is one of the units, stored in full,
that others are expressed against. Choosing it well is the whole economics of the
representation: every byte a unit shares with its base is a byte stored once
instead of twice.

Selection works on shared chunk content. The index maps a chunk digest to the
units carrying that chunk; a candidate's score is the number of *bytes* it shares
with the incoming unit, not the number of chunks, because one long shared chunk
is worth more than several short ones.

Only units stored in full are indexed as candidates, which keeps every unit at
most one hop from real bytes. Chained deltas (a delta against a delta) are
therefore impossible by construction rather than by a check -- the same rule the
storage engine enforced in its decoder, kept here as a property of the build.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from .chunking import Chunk

# A chunk that appears in more units than this carries almost no information
# about similarity -- a run of zeros is in everything -- while dominating the
# scoring loop. Past the cap the digest stops accruing new candidates.
# ENGINEERING DECISION.
MAX_UNITS_PER_CHUNK = 64

# How much of a unit must be shared before a base is worth trying. Below this the
# change program is nearly all ADD instructions and loses to storing the unit in
# full, so the differ is not run at all. The cost gate in the builder is still
# the authority; this only avoids obviously wasted work. ENGINEERING DECISION.
DEFAULT_MIN_SIMILARITY = 0.20


@dataclass
class BaseCandidate:
    """A possible base, with the evidence for choosing it."""

    uid: str
    shared_bytes: int
    unit_size: int

    @property
    def similarity(self) -> Optional[float]:
        """Shared fraction of the incoming unit, or None if it is empty."""
        if self.unit_size == 0:
            return None
        return self.shared_bytes / self.unit_size


@dataclass
class SimilarityIndex:
    """Chunk digest -> uids of full-stored units containing that chunk."""

    _by_digest: Dict[bytes, List[str]] = field(default_factory=dict)
    _sizes: Dict[str, int] = field(default_factory=dict)

    def add_unit(self, uid: str, size: int, chunks: Sequence[Chunk]) -> None:
        """Register a full-stored unit as a future base candidate."""
        self._sizes[uid] = size
        seen: Set[bytes] = set()
        for chunk in chunks:
            if chunk.digest in seen:
                continue  # one vote per digest per unit
            seen.add(chunk.digest)
            holders = self._by_digest.setdefault(chunk.digest, [])
            if len(holders) < MAX_UNITS_PER_CHUNK:
                holders.append(uid)

    def rank_candidates(
        self, chunks: Sequence[Chunk], exclude: Optional[str] = None
    ) -> List[BaseCandidate]:
        """Score every candidate sharing a chunk, best first.

        Scoring counts each of the incoming unit's chunks once, so a unit made of
        one chunk repeated many times cannot inflate a candidate's score beyond
        the bytes actually shared.
        """
        unit_size = sum(chunk.length for chunk in chunks)
        shared: Dict[str, int] = {}
        counted: Set[bytes] = set()
        for chunk in chunks:
            if chunk.digest in counted:
                continue
            counted.add(chunk.digest)
            for uid in self._by_digest.get(chunk.digest, ()):
                if uid == exclude:
                    continue
                shared[uid] = shared.get(uid, 0) + chunk.length

        candidates = [
            BaseCandidate(uid=uid, shared_bytes=count, unit_size=unit_size)
            for uid, count in shared.items()
        ]
        # Deterministic order: by shared bytes, then uid, so equal scores never
        # depend on dictionary iteration order.
        candidates.sort(key=lambda c: (-c.shared_bytes, c.uid))
        return candidates

    def best_candidate(
        self,
        chunks: Sequence[Chunk],
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
        exclude: Optional[str] = None,
    ) -> Optional[BaseCandidate]:
        """The strongest candidate above the threshold, or None if there is none.

        None rather than a weak candidate: "no base is worth using" is a real
        answer, and the builder stores the unit in full when it gets one.
        """
        for candidate in self.rank_candidates(chunks, exclude=exclude):
            similarity = candidate.similarity
            if similarity is not None and similarity >= min_similarity:
                return candidate
        return None
