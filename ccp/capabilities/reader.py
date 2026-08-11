"""Execution capability: doing work on the CCP representation itself.

This is the layer where the representation stops being storage and starts being
something execution runs against. Every capability here has the same shape: it
answers a question about a unit *without reconstructing the unit*, by reading the
change program instead of the bytes it would produce.

Nothing here simulates a saving. Each operation reports `bytes_touched`, counted
as it runs, against `unit_size`, which is what a full reconstruction would have
had to touch. The comparison is measured on the real code path, and when a
capability has no advantage for a given unit the numbers say so.

The capabilities are deliberately the ones that fall directly out of the
representation, and no more:

*   `read_range`  -- materialise only the slice a caller asked for, by resolving
    only the instructions overlapping it. A unit stored as a program over a base
    supports this natively because each instruction's output span is known
    without executing any of them.
*   `units_equal` -- two units built from the same base with the same program are
    the same unit; the answer costs a program comparison rather than two
    reconstructions.
*   `shared_base_groups` -- which units are expressed against the same base, which
    is the grouping any batch operation would schedule around.

What is deliberately *not* here: any mechanism that changes how a target program
runs, decides an execution strategy, or manages itself. Those are named as Open
Design Questions in `experiments/ccp/PHASE1_UNDERSTANDING.md` and inventing them
to fill out a layer is the specific failure this project is guarding against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..core.change_program import Add, Copy, instruction_spans
from ..core.representation import KIND_LITERAL, CCPModel


@dataclass(frozen=True)
class RangeRead:
    """The result of a partial read, with the work it actually cost."""

    uid: str
    offset: int
    length: int
    data: bytes
    bytes_touched: int
    unit_size: int
    instructions_visited: int
    instructions_total: int

    @property
    def work_ratio(self) -> Optional[float]:
        """Bytes touched per byte a full reconstruction would touch.

        None when the unit is empty -- there is no meaningful ratio, and a
        plausible fake would propagate into any measurement built on this.
        """
        if self.unit_size == 0:
            return None
        return self.bytes_touched / self.unit_size


class CCPReader:
    """Read access to a model that works on programs rather than on bytes."""

    def __init__(self, model: CCPModel) -> None:
        self._model = model

    # -- partial execution -------------------------------------------------

    def read_range(self, uid: str, offset: int, length: int) -> RangeRead:
        """Return `length` bytes of a unit starting at `offset`.

        Only the instructions whose output overlaps the requested window are
        executed, and each contributes only its overlapping slice. A unit stored
        as one COPY over a base costs a single slice of the base regardless of
        how large the unit is.
        """
        record = self._model.record(uid)
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        start = min(offset, record.size)
        end = min(offset + length, record.size)

        if record.kind == KIND_LITERAL:
            data = self._model.literals[uid][start:end]
            return RangeRead(
                uid=uid,
                offset=offset,
                length=length,
                data=data,
                bytes_touched=len(data),
                unit_size=record.size,
                instructions_visited=1 if data else 0,
                instructions_total=1,
            )

        if record.base_uid is None or record.program is None:
            raise ValueError(f"derived record {uid!r} is incomplete")
        base = self._model.base_bytes(record.base_uid)

        out = bytearray()
        touched = 0
        visited = 0
        total = len(record.program)
        for span_start, span_end, instruction in instruction_spans(record.program):
            if span_end <= start:
                continue  # entirely before the window: never read
            if span_start >= end:
                break  # instructions are ordered, so nothing further can overlap
            visited += 1
            take_from = max(start, span_start) - span_start
            take_to = min(end, span_end) - span_start
            if isinstance(instruction, Copy):
                src = instruction.src_offset + take_from
                out += base[src : src + (take_to - take_from)]
            elif isinstance(instruction, Add):
                out += instruction.data[take_from:take_to]
            touched += take_to - take_from

        return RangeRead(
            uid=uid,
            offset=offset,
            length=length,
            data=bytes(out),
            bytes_touched=touched,
            unit_size=record.size,
            instructions_visited=visited,
            instructions_total=total,
        )

    # -- questions answered without reconstruction -------------------------

    def units_equal(self, left: str, right: str) -> Optional[bool]:
        """Whether two units are identical, or None if it cannot be decided cheaply.

        None is a real answer and not a failure: it means the representation does
        not settle the question and the caller must materialise if it needs to
        know. Returning a guess here would be exactly the kind of plausible fake
        the project forbids.
        """
        a = self._model.record(left)
        b = self._model.record(right)
        if a.digest == b.digest:
            return True
        if a.size != b.size:
            return False
        # Distinct digests over equal sizes: different content, up to a digest
        # collision, which is not a case worth weakening the contract for.
        return False

    def shares_base(self, left: str, right: str) -> bool:
        """Whether two units are expressed against the same base."""
        a = self._model.record(left)
        b = self._model.record(right)
        return (
            a.base_uid is not None and b.base_uid is not None and a.base_uid == b.base_uid
        )

    def shared_base_groups(self) -> Dict[str, List[str]]:
        """Base uid -> the units derived from it, each group sorted.

        The grouping any batch operation would schedule around: units in one group
        share their bytes, so work done on the base is work done for all of them.
        """
        groups: Dict[str, List[str]] = {}
        for uid in self._model.uids():
            base_uid = self._model.records[uid].base_uid
            if base_uid is not None:
                groups.setdefault(base_uid, []).append(uid)
        for members in groups.values():
            members.sort()
        return groups

    def reuse_report(self, uid: str) -> Dict[str, object]:
        """How much of a unit is base and how much is genuinely its own."""
        record = self._model.record(uid)
        if record.program is None:
            return {
                "uid": uid,
                "kind": record.kind,
                "size": record.size,
                "base_uid": None,
                "copied_bytes": 0,
                "added_bytes": record.size,
                "reuse_ratio": None,
                "encoded_bytes": record.size,
            }
        return {
            "uid": uid,
            "kind": record.kind,
            "size": record.size,
            "base_uid": record.base_uid,
            "copied_bytes": record.program.copied_bytes,
            "added_bytes": record.program.added_bytes,
            "reuse_ratio": record.program.reuse_ratio,
            "encoded_bytes": record.program.encoded_size(),
        }
