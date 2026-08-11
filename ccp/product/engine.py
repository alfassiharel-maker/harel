"""The CCP Product Engine: artifacts with a lifecycle.

This is the application layer. It turns the Runtime — a loaded representation with
operations — into an *artifact* a program opens, uses under an explicit lifecycle,
and closes. It adds four things the Runtime deliberately does not:

*   a lifecycle (open, inspect, validate, close) with deterministic ownership,
*   a typed product error model, so a caller catches `ProductError` and never a
    bare `ValueError` or `KeyError` from a lower layer,
*   an observation on every operation (bytes requested/touched, units touched,
    reconstruction work, verification state, time),
*   bounded, explicit resource ownership with no global state.

It contains **no algorithm and no second reconstruction path**. Every byte comes
from the Runtime, which gets it from the Core. The engine goes through the public
API (`ccp.api`) for building and opening, exactly as any external integration
would — if the public surface were insufficient for the product, it would be
insufficient for everyone.

    from ccp.product import ProductEngine

    engine = ProductEngine()
    with engine.open("model.ccp", verify=True) as artifact:
        result = artifact.read_range("src/main.py", 8000, 256)
        print(result.data, result.report.bytes_touched)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from ..api import (
    CONTRACT,
    BuildConfig,
    CCPFormatError,
    CCPIntegrityError,
    CCPRuntime,
    UnknownUnitError,
    build,
    open_representation,
)
from ..api import RepresentationInfo, UnitInfo, VerificationReport
from .errors import (
    InvalidRangeError,
    MalformedArtifactError,
    ProductError,
    ResourceError,
    UnitNotFoundError,
    UnsupportedOperationError,
    VerificationError,
)
from .observability import OperationReport, ProductLedger, VerificationState

# Default ceiling on artifact size the engine will open. Version 1 loads the
# whole representation into memory, so a caller is given a named, catchable limit
# instead of an allocator kill. Lazy loading is an OPEN question, not a promise.
DEFAULT_MAX_ARTIFACT_BYTES = 2 << 30

BytesLike = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True)
class ReadResult:
    """Bytes returned by a product read, with the report of what it cost."""

    uid: str
    offset: int
    length: int
    data: bytes
    report: OperationReport


@dataclass(frozen=True)
class MaterializeResult:
    """A whole unit, digest-checked, with the report of what it cost."""

    uid: str
    data: bytes
    report: OperationReport


@dataclass(frozen=True)
class BatchReadResult:
    """Several reads in request order, with a per-read and an aggregate report."""

    reads: Tuple[ReadResult, ...]
    report: OperationReport


class CCPArtifact:
    """An opened CCP artifact.

    Owns exactly one Runtime and one product ledger. Not thread-safe: the ledger
    mutates on every call, and the semantic contract names concurrent access to
    one instance as unsupported. Use one artifact per thread; concurrency across
    instances is fine because there is no shared state between them.
    """

    def __init__(self, runtime: CCPRuntime, origin: str) -> None:
        self._runtime: Optional[CCPRuntime] = runtime
        self._origin = origin
        self._ledger = ProductLedger()
        self._closed = False

    # -- lifecycle --------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return not self._closed

    @property
    def origin(self) -> str:
        """Where the artifact came from — a path, or a description for in-memory."""
        return self._origin

    def close(self) -> None:
        """Release the runtime. Idempotent. After this, operations raise.

        Ownership is explicit: the reference is dropped here, not left to a
        finaliser, so a caller controls exactly when the representation stops
        occupying memory.
        """
        self._runtime = None
        self._closed = True

    def __enter__(self) -> "CCPArtifact":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _live(self) -> CCPRuntime:
        if self._closed or self._runtime is None:
            raise ResourceError("artifact is closed")
        return self._runtime

    # -- inspection -------------------------------------------------------

    def info(self) -> RepresentationInfo:
        """Sizes of the artifact, all measured."""
        return self._live().info()

    def units(self) -> List[str]:
        """Every unit id, sorted. Discovery without materialising anything."""
        return self._live().units()

    def groups(self) -> Dict[str, List[str]]:
        """Base uid -> units derived from it. The batch-scheduling grouping."""
        return self._live().groups()

    def stat(self, uid: str) -> UnitInfo:
        """Per-unit breakdown. Raises `UnitNotFoundError` for an unknown uid."""
        runtime = self._live()
        try:
            return runtime.stat(uid)
        except UnknownUnitError as error:
            raise UnitNotFoundError(uid, cause=error) from error

    def __contains__(self, uid: str) -> bool:
        return uid in self._live()

    def __len__(self) -> int:
        return len(self._live())

    @property
    def is_verified(self) -> bool:
        """Whether every unit has been reconstructed and checked this session."""
        return self._live().is_verified

    @property
    def ledger(self) -> ProductLedger:
        return self._ledger

    def reset_ledger(self) -> None:
        self._ledger.reset()

    def contract_supports(self, operation: str) -> bool:
        return CONTRACT.supports(operation)

    def require_supported(self, operation: str) -> None:
        """Raise `UnsupportedOperationError` unless the contract offers `operation`.

        The single choke point for requirement 5's "unsupported operation" case:
        a caller can ask before attempting, and every unsupported path lands on
        one typed error rather than on an `AttributeError` for a missing method.
        """
        if not CONTRACT.supports(operation):
            raise UnsupportedOperationError(operation)

    # -- validation -------------------------------------------------------

    def validate(self) -> VerificationReport:
        """Reconstruct and digest-check every unit. Raises on failure.

        Distinct from `stat`/`info`, which only read metadata. This does the full
        pass and raises `VerificationError` if any unit does not match, so a
        caller can gate on a clean artifact.
        """
        runtime = self._live()
        report = runtime.verify()
        if not report.ok:
            first_uid, first_error = report.failures[0]
            raise VerificationError(
                f"{len(report.failures)} unit(s) failed verification; "
                f"first: {first_uid}: {first_error}"
            )
        return report

    # -- data access ------------------------------------------------------

    def materialize(self, uid: str) -> MaterializeResult:
        """Reconstruct a whole unit, checked against its digest."""
        runtime = self._live()
        started = time.perf_counter()
        try:
            data = runtime.materialize(uid, verify=True)
        except UnknownUnitError as error:
            raise UnitNotFoundError(uid, cause=error) from error
        except CCPIntegrityError as error:
            raise VerificationError(str(error), cause=error) from error
        elapsed = time.perf_counter() - started

        stat = runtime.stat(uid)
        report = OperationReport(
            operation="materialize",
            bytes_requested=stat.size,
            bytes_returned=len(data),
            # A full reconstruction touches every byte of the unit: that is the
            # baseline a selective read is measured against.
            bytes_touched=stat.size,
            units_touched=1,
            instructions_visited=stat.instructions,
            instructions_total=stat.instructions,
            verification=VerificationState.DIGEST_CHECKED,
            artifact_verified=runtime.is_verified,
            elapsed_seconds=elapsed,
        )
        self._ledger.record(report)
        return MaterializeResult(uid=uid, data=data, report=report)

    def read_range(self, uid: str, offset: int, length: int) -> ReadResult:
        """Read a window of a unit, executing only the covering instructions.

        This is the product-facing selective-access operation. The whole artifact
        is never materialised to serve it; the work is proportional to the window
        and the report says so.
        """
        runtime = self._live()
        started = time.perf_counter()
        try:
            window = runtime.read_range(uid, offset, length)
        except UnknownUnitError as error:
            raise UnitNotFoundError(uid, cause=error) from error
        except ValueError as error:
            raise InvalidRangeError(
                f"invalid range for {uid!r}: offset={offset} length={length}",
                cause=error,
            ) from error
        elapsed = time.perf_counter() - started

        report = OperationReport(
            operation="read_range",
            bytes_requested=length,
            bytes_returned=window.bytes_returned,
            bytes_touched=window.bytes_touched,
            units_touched=1,
            instructions_visited=window.instructions_visited,
            instructions_total=window.instructions_total,
            verification=VerificationState.SLICE_UNVERIFIED,
            artifact_verified=runtime.is_verified,
            elapsed_seconds=elapsed,
        )
        self._ledger.record(report)
        return ReadResult(
            uid=uid, offset=offset, length=length, data=window.data, report=report
        )

    def read_many(
        self, requests: Iterable[Tuple[str, int, int]]
    ) -> BatchReadResult:
        """Serve several windows. Results are always in request order.

        Delegates to the Runtime's `read_many`, which may group requests sharing a
        base to touch it once, but returns results in the order asked for — so the
        output never depends on the grouping. The aggregate report sums the work.
        """
        runtime = self._live()
        ordered = list(requests)
        started = time.perf_counter()
        try:
            windows = runtime.read_many(ordered)
        except UnknownUnitError as error:
            raise UnitNotFoundError(str(error), cause=error) from error
        except ValueError as error:
            raise InvalidRangeError(str(error), cause=error) from error
        elapsed = time.perf_counter() - started

        reads: List[ReadResult] = []
        distinct_units = set()
        touched = 0
        returned = 0
        requested = 0
        instructions = 0
        for (uid, offset, length), window in zip(ordered, windows):
            distinct_units.add(uid)
            touched += window.bytes_touched
            returned += window.bytes_returned
            requested += length
            instructions += window.instructions_visited
            per_read = OperationReport(
                operation="read_range",
                bytes_requested=length,
                bytes_returned=window.bytes_returned,
                bytes_touched=window.bytes_touched,
                units_touched=1,
                instructions_visited=window.instructions_visited,
                instructions_total=window.instructions_total,
                verification=VerificationState.SLICE_UNVERIFIED,
                artifact_verified=runtime.is_verified,
                elapsed_seconds=0.0,  # per-read time is not separable in a batch
            )
            reads.append(
                ReadResult(
                    uid=uid,
                    offset=offset,
                    length=length,
                    data=window.data,
                    report=per_read,
                )
            )

        aggregate = OperationReport(
            operation="read_many",
            bytes_requested=requested,
            bytes_returned=returned,
            bytes_touched=touched,
            units_touched=len(distinct_units),
            instructions_visited=instructions,
            instructions_total=instructions,
            verification=VerificationState.SLICE_UNVERIFIED,
            artifact_verified=runtime.is_verified,
            elapsed_seconds=elapsed,
        )
        self._ledger.record(aggregate)
        return BatchReadResult(reads=tuple(reads), report=aggregate)

    def units_equal(self, left: str, right: str) -> Optional[bool]:
        """Whether two units are identical, without reconstructing either."""
        runtime = self._live()
        try:
            return runtime.units_equal(left, right)
        except UnknownUnitError as error:
            raise UnitNotFoundError(str(error), cause=error) from error


class ProductEngine:
    """Creates and opens artifacts. Holds no artifact state itself.

    A factory, not a registry: it does not track the artifacts it hands out, so
    there is no global or hidden state and no shared ownership. The caller owns
    every artifact it is given and is responsible for closing it (or using it as
    a context manager).
    """

    def __init__(self, max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES) -> None:
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self._max_artifact_bytes = max_artifact_bytes

    @property
    def max_artifact_bytes(self) -> int:
        return self._max_artifact_bytes

    # -- create -----------------------------------------------------------

    def build_from_directory(
        self, path: str, config: Optional[BuildConfig] = None
    ) -> bytes:
        """Build artifact bytes from a directory. Does not open anything."""
        if not os.path.isdir(path):
            raise ResourceError(f"not a directory: {path}")
        try:
            return build.from_directory(path, config)
        except OSError as error:
            raise ResourceError(f"could not read {path}: {error}", cause=error) from error

    def build_from_units(
        self, units: Mapping[str, bytes], config: Optional[BuildConfig] = None
    ) -> bytes:
        """Build artifact bytes from in-memory units. Does not open anything."""
        return build.from_units(units, config)

    def create_from_directory(
        self,
        path: str,
        config: Optional[BuildConfig] = None,
        save_to: Optional[str] = None,
        verify: bool = True,
    ) -> CCPArtifact:
        """Build from a directory and open the result as an artifact."""
        container = self.build_from_directory(path, config)
        if save_to is not None:
            self._write(save_to, container)
        return self._open_bytes(container, origin=save_to or f"dir:{path}", verify=verify)

    def create_from_units(
        self,
        units: Mapping[str, bytes],
        config: Optional[BuildConfig] = None,
        save_to: Optional[str] = None,
        verify: bool = True,
    ) -> CCPArtifact:
        """Build from in-memory units and open the result as an artifact."""
        container = self.build_from_units(units, config)
        if save_to is not None:
            self._write(save_to, container)
        return self._open_bytes(container, origin=save_to or "units:memory", verify=verify)

    # -- open -------------------------------------------------------------

    def open(
        self, source: Union[str, BytesLike], verify: bool = False
    ) -> CCPArtifact:
        """Open an existing artifact from a path or from container bytes."""
        if isinstance(source, str):
            return self._open_path(source, verify=verify)
        return self._open_bytes(bytes(source), origin="bytes:memory", verify=verify)

    # -- internals --------------------------------------------------------

    def _open_path(self, path: str, verify: bool) -> CCPArtifact:
        if not os.path.isfile(path):
            raise ResourceError(f"artifact file not found: {path}")
        try:
            size = os.path.getsize(path)
        except OSError as error:
            raise ResourceError(f"could not stat {path}: {error}", cause=error) from error
        self._check_size(size, path)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as error:
            raise ResourceError(f"could not read {path}: {error}", cause=error) from error
        return self._open_bytes(data, origin=path, verify=verify)

    def _open_bytes(self, data: bytes, origin: str, verify: bool) -> CCPArtifact:
        self._check_size(len(data), origin)
        try:
            runtime = CCPRuntime.load(
                data, verify=verify, max_container_bytes=self._max_artifact_bytes
            )
        except CCPIntegrityError as error:
            raise VerificationError(str(error), cause=error) from error
        except CCPFormatError as error:
            raise MalformedArtifactError(str(error), cause=error) from error
        return CCPArtifact(runtime, origin=origin)

    def _check_size(self, size: int, origin: str) -> None:
        if size > self._max_artifact_bytes:
            raise ResourceError(
                f"artifact {origin!r} is {size} bytes, above the engine limit of "
                f"{self._max_artifact_bytes} bytes"
            )

    @staticmethod
    def _write(path: str, container: bytes) -> None:
        try:
            with open(path, "wb") as handle:
                handle.write(container)
        except OSError as error:
            raise ResourceError(f"could not write {path}: {error}", cause=error) from error
