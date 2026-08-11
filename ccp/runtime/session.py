"""The CCP Runtime: a loaded representation you can do work against.

The Runtime owns loading, integrity, and accounting. It owns no algorithm. Every
byte it returns is produced by `ccp.core` and `ccp.capabilities`; there is no
second implementation here that reproduces what the Core does, because two
implementations of one algorithm can disagree and nothing would say which was
right.

What the Runtime adds on top of the Core is the part a caller actually needs to
use it as infrastructure:

*   a loaded, validated session with an explicit integrity state,
*   selective access -- reading a window executes only the instructions covering
    it, rather than reconstructing a unit and slicing it,
*   measured work on every operation, aggregated into a ledger,
*   errors that name what is wrong with a representation instead of failing late.

The distinction the Runtime exists to make real is:

    representation -> selective reconstruction        (what this does)
    representation -> reconstruct everything -> slice (what it does not do)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..capabilities import CCPReader
from ..core import (
    CCPFormatError,
    CCPIntegrityError,
    CCPModel,
    deserialize,
)
from .accounting import OperationRecord, WorkLedger
from .contract import CONTRACT, SemanticContract

# A container larger than this is refused rather than loaded, because version 1
# holds the whole representation in memory and a caller deserves a named limit
# instead of an out-of-memory kill. Lazy loading is a listed open question.
DEFAULT_MAX_CONTAINER_BYTES = 4 << 30


class RuntimeStateError(RuntimeError):
    """An operation was requested that this runtime cannot serve as loaded."""


class UnknownUnitError(KeyError):
    """A uid that is not in the loaded representation."""


@dataclass(frozen=True)
class ReadResult:
    """Bytes returned by the runtime, with the work they cost."""

    uid: str
    offset: int
    length: int
    data: bytes
    bytes_touched: int
    bytes_returned: int
    instructions_visited: int
    instructions_total: int
    unit_size: int
    elapsed_seconds: float

    @property
    def work_ratio(self) -> Optional[float]:
        """Bytes touched per byte of the whole unit, or None if the unit is empty."""
        if self.unit_size == 0:
            return None
        return self.bytes_touched / self.unit_size


@dataclass(frozen=True)
class UnitInfo:
    """What the representation records about one unit."""

    uid: str
    kind: str
    size: int
    base_uid: Optional[str]
    stored_bytes: int
    instructions: int
    copied_bytes: int
    added_bytes: int
    reuse_ratio: Optional[float]


@dataclass(frozen=True)
class VerificationReport:
    """The outcome of reconstructing and checking every unit."""

    units_checked: int
    units_ok: int
    failures: Tuple[Tuple[str, str], ...]
    bytes_verified: int
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class RepresentationInfo:
    """Sizes of the loaded representation, all measured."""

    units: int
    literals: int
    derived: int
    original_bytes: int
    container_bytes: int
    payload_bytes: int
    index_bytes: int

    @property
    def saving(self) -> Optional[float]:
        """Container against original, index included. None when there is no input."""
        if self.original_bytes == 0:
            return None
        return 1.0 - (self.container_bytes / self.original_bytes)


class CCPRuntime:
    """A loaded CCP representation.

    Not thread-safe: the ledger is mutated on every operation. Concurrency is
    named as unsupported in the contract rather than half-provided.
    """

    def __init__(
        self, model: CCPModel, container_bytes: int, contract: SemanticContract = CONTRACT
    ) -> None:
        self._model = model
        self._reader = CCPReader(model)
        self._container_bytes = container_bytes
        self._contract = contract
        self._ledger = WorkLedger()
        self._verified = False

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(
        cls,
        data: bytes,
        verify: bool = False,
        max_container_bytes: int = DEFAULT_MAX_CONTAINER_BYTES,
    ) -> "CCPRuntime":
        """Load a representation from container bytes.

        Raises `CCPFormatError` for anything malformed. Structural validity is
        established here -- every base exists, is stored in full, and every
        program produces the size its record declares -- so later operations do
        not have to re-check the shape of the representation on every call.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("container must be bytes")
        payload = bytes(data)
        if len(payload) > max_container_bytes:
            raise CCPFormatError(
                f"container is {len(payload)} bytes, above the "
                f"{max_container_bytes}-byte limit this runtime will load"
            )
        model = deserialize(payload)
        runtime = cls(model, container_bytes=len(payload))
        if verify:
            report = runtime.verify()
            if not report.ok:
                first_uid, first_error = report.failures[0]
                raise CCPIntegrityError(
                    f"representation failed verification on load "
                    f"({len(report.failures)} unit(s)); first: {first_uid}: {first_error}"
                )
        return runtime

    @classmethod
    def open(
        cls,
        path: str,
        verify: bool = False,
        max_container_bytes: int = DEFAULT_MAX_CONTAINER_BYTES,
    ) -> "CCPRuntime":
        """Load a representation from a file."""
        size = os.path.getsize(path)
        if size > max_container_bytes:
            raise CCPFormatError(
                f"{path} is {size} bytes, above the {max_container_bytes}-byte "
                f"limit this runtime will load"
            )
        with open(path, "rb") as handle:
            return cls.load(
                handle.read(), verify=verify, max_container_bytes=max_container_bytes
            )

    # -- inspection -------------------------------------------------------

    @property
    def contract(self) -> SemanticContract:
        return self._contract

    @property
    def is_verified(self) -> bool:
        """Whether every unit has been reconstructed and checked in this session."""
        return self._verified

    @property
    def ledger(self) -> WorkLedger:
        return self._ledger

    def reset_ledger(self) -> None:
        self._ledger.reset()

    def units(self) -> List[str]:
        return self._model.uids()

    def __contains__(self, uid: str) -> bool:
        return uid in self._model

    def __len__(self) -> int:
        return len(self._model)

    def info(self) -> RepresentationInfo:
        stats = self._model.stats
        return RepresentationInfo(
            units=stats.units,
            literals=stats.literals,
            derived=stats.derived,
            original_bytes=stats.original_bytes,
            container_bytes=self._container_bytes,
            payload_bytes=stats.stored_payload_bytes,
            index_bytes=self._container_bytes - stats.stored_payload_bytes,
        )

    def stat(self, uid: str) -> UnitInfo:
        record = self._require_unit(uid)
        program = record.program
        return UnitInfo(
            uid=uid,
            kind=record.kind,
            size=record.size,
            base_uid=record.base_uid,
            stored_bytes=record.stored_bytes(),
            instructions=0 if program is None else len(program),
            copied_bytes=0 if program is None else program.copied_bytes,
            added_bytes=record.size if program is None else program.added_bytes,
            reuse_ratio=None if program is None else program.reuse_ratio,
        )

    def groups(self) -> Dict[str, List[str]]:
        """Base uid -> units derived from it. What a batch operation groups on."""
        return self._reader.shared_base_groups()

    # -- integrity --------------------------------------------------------

    def verify(self) -> VerificationReport:
        """Reconstruct every unit and check it against its recorded digest.

        Collects every failure rather than stopping at the first, so a damaged
        representation can be reported in full.
        """
        started = time.perf_counter()
        failures: List[Tuple[str, str]] = []
        verified_bytes = 0
        uids = self._model.uids()
        for uid in uids:
            try:
                data = self._model.materialize(uid, verify=True)
            except (CCPIntegrityError, CCPFormatError) as error:
                failures.append((uid, str(error)))
                continue
            verified_bytes += len(data)
        elapsed = time.perf_counter() - started
        self._verified = not failures
        return VerificationReport(
            units_checked=len(uids),
            units_ok=len(uids) - len(failures),
            failures=tuple(failures),
            bytes_verified=verified_bytes,
            elapsed_seconds=elapsed,
        )

    # -- work -------------------------------------------------------------

    def materialize(self, uid: str, verify: bool = True) -> bytes:
        """Reconstruct a whole unit, checked against its digest by default."""
        record = self._require_unit(uid)
        started = time.perf_counter()
        data = self._model.materialize(uid, verify=verify)
        elapsed = time.perf_counter() - started
        instructions = 1 if record.program is None else len(record.program)
        self._ledger.record(
            OperationRecord(
                operation="materialize",
                uid=uid,
                # A full reconstruction touches every byte of the unit; that is
                # the baseline every selective read is measured against.
                bytes_touched=record.size,
                bytes_returned=len(data),
                instructions_visited=instructions,
                instructions_total=instructions,
                unit_size=record.size,
                elapsed_seconds=elapsed,
            )
        )
        return data

    def read_range(self, uid: str, offset: int, length: int) -> ReadResult:
        """Read a window of a unit, executing only the instructions covering it.

        This is the operation the Runtime exists for. The work is proportional to
        the window, not to the unit, and the result says so.
        """
        self._require_unit(uid)
        if offset < 0 or length < 0:
            raise ValueError("offset and length must be non-negative")
        started = time.perf_counter()
        window = self._reader.read_range(uid, offset, length)
        elapsed = time.perf_counter() - started
        self._ledger.record(
            OperationRecord(
                operation="read_range",
                uid=uid,
                bytes_touched=window.bytes_touched,
                bytes_returned=len(window.data),
                instructions_visited=window.instructions_visited,
                instructions_total=window.instructions_total,
                unit_size=window.unit_size,
                elapsed_seconds=elapsed,
            )
        )
        return ReadResult(
            uid=uid,
            offset=offset,
            length=length,
            data=window.data,
            bytes_touched=window.bytes_touched,
            bytes_returned=len(window.data),
            instructions_visited=window.instructions_visited,
            instructions_total=window.instructions_total,
            unit_size=window.unit_size,
            elapsed_seconds=elapsed,
        )

    def read_many(
        self, requests: Iterable[Tuple[str, int, int]]
    ) -> List[ReadResult]:
        """Serve several windows. Results are in request order, always.

        Requests against units sharing a base are served together so the base is
        visited once per group instead of once per request -- a scheduling
        freedom the contract grants. Ordering of *results* is not a freedom: they
        come back in the order asked for, so output never depends on grouping.
        """
        ordered = list(requests)
        by_group: Dict[str, List[int]] = {}
        for index, (uid, _, _) in enumerate(ordered):
            record = self._require_unit(uid)
            key = record.base_uid if record.base_uid is not None else uid
            by_group.setdefault(key, []).append(index)

        results: List[Optional[ReadResult]] = [None] * len(ordered)
        for key in sorted(by_group):
            for index in by_group[key]:
                uid, offset, length = ordered[index]
                results[index] = self.read_range(uid, offset, length)

        for index, result in enumerate(results):
            if result is None:  # pragma: no cover - defensive
                raise RuntimeStateError(f"request {index} was not served")
        return [result for result in results if result is not None]

    def units_equal(self, left: str, right: str) -> Optional[bool]:
        """Whether two units are identical, without reconstructing either."""
        self._require_unit(left)
        self._require_unit(right)
        return self._reader.units_equal(left, right)

    # -- internals --------------------------------------------------------

    def _require_unit(self, uid: str):
        if uid not in self._model:
            raise UnknownUnitError(uid)
        return self._model.record(uid)


def describe_contract() -> str:
    """Human-readable rendering of the contract this runtime enforces."""
    return CONTRACT.describe()
