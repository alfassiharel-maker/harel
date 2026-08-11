"""Observability for product operations.

Every product operation returns an `OperationReport`: what was asked for, what it
cost, whether the bytes were integrity-checked, and how long it took. The report
is self-contained — it is built from the values the Runtime returns for that one
operation, not by diffing a shared ledger, so it is correct regardless of what
else the artifact has done.

`elapsed_seconds` is present because a caller needs it. It is never used to
decide anything, and no assertion anywhere in the test suite depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class VerificationState(str, Enum):
    """Whether the bytes an operation returned were integrity-checked.

    The distinction matters and is honest about what a digest covers. A digest is
    over a whole unit, so only a full materialisation can be checked against it.
    A slice is returned from the representation as loaded; it is trustworthy to
    the extent the whole artifact was verified, which `artifact_verified` records
    separately.
    """

    DIGEST_CHECKED = "digest_checked"      # a whole unit, verified against its digest
    SLICE_UNVERIFIED = "slice_unverified"  # a window; the unit digest was not re-checked
    NOT_APPLICABLE = "not_applicable"      # an operation that returns no unit bytes


@dataclass(frozen=True)
class OperationReport:
    """What a single product operation asked for and cost."""

    operation: str
    bytes_requested: int
    bytes_returned: int
    bytes_touched: int
    units_touched: int
    instructions_visited: int
    instructions_total: int
    verification: VerificationState
    artifact_verified: bool
    elapsed_seconds: float

    @property
    def work_ratio(self) -> Optional[float]:
        """Bytes touched per byte returned, or None when nothing was returned.

        Below 1.0 means the operation reached its result by touching less than it
        handed back — the selective-access advantage, measured, not modelled.
        None rather than a fabricated 0.0 when nothing was returned.
        """
        if self.bytes_returned == 0:
            return None
        return self.bytes_touched / self.bytes_returned

    def as_dict(self) -> dict:
        return {
            "operation": self.operation,
            "bytes_requested": self.bytes_requested,
            "bytes_returned": self.bytes_returned,
            "bytes_touched": self.bytes_touched,
            "units_touched": self.units_touched,
            "instructions_visited": self.instructions_visited,
            "instructions_total": self.instructions_total,
            "verification": self.verification.value,
            "artifact_verified": self.artifact_verified,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass
class ProductLedger:
    """Running totals of product operations on one artifact.

    Instance-scoped: it lives on the artifact, so there is no global state and
    two artifacts never share counters. The per-operation tail is bounded; the
    totals are always exact.
    """

    operations: int = 0
    bytes_requested: int = 0
    bytes_returned: int = 0
    bytes_touched: int = 0
    units_touched: int = 0
    instructions_visited: int = 0
    elapsed_seconds: float = 0.0
    reports: List[OperationReport] = field(default_factory=list)
    max_reports: int = 4096

    def record(self, report: OperationReport) -> None:
        self.operations += 1
        self.bytes_requested += report.bytes_requested
        self.bytes_returned += report.bytes_returned
        self.bytes_touched += report.bytes_touched
        self.units_touched += report.units_touched
        self.instructions_visited += report.instructions_visited
        self.elapsed_seconds += report.elapsed_seconds
        self.reports.append(report)
        if len(self.reports) > self.max_reports:
            del self.reports[: len(self.reports) - self.max_reports]

    def reset(self) -> None:
        self.operations = 0
        self.bytes_requested = 0
        self.bytes_returned = 0
        self.bytes_touched = 0
        self.units_touched = 0
        self.instructions_visited = 0
        self.elapsed_seconds = 0.0
        self.reports.clear()

    def summary(self) -> str:
        return (
            f"{self.operations} product op(s): "
            f"{self.bytes_returned} bytes returned, "
            f"{self.bytes_touched} touched, "
            f"{self.units_touched} unit-touches, "
            f"{self.instructions_visited} instructions, "
            f"{self.elapsed_seconds * 1000:.2f} ms"
        )
