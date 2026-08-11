"""Work accounting for the Runtime.

Every operation records what it actually cost. The counts come from the Core's
own report of what it did -- the Runtime aggregates them and does not re-derive
them, because a second implementation of the accounting would be free to drift
from the first and there would be no way to tell which was right.

Two properties are load-bearing:

*   `bytes_touched` and `instructions_visited` are **counted**, not modelled. They
    are the numbers a caller can use to justify a deployment.
*   `elapsed_seconds` is recorded but is never used to decide anything. Timing is
    reported for the user's benefit; correctness never depends on it, and neither
    does any assertion in the test suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class OperationRecord:
    """What a single runtime operation cost."""

    operation: str
    uid: Optional[str]
    bytes_touched: int
    bytes_returned: int
    instructions_visited: int
    instructions_total: int
    unit_size: int
    elapsed_seconds: float

    @property
    def work_ratio(self) -> Optional[float]:
        """Bytes touched per byte a full reconstruction would have touched.

        None when the unit is empty: there is no meaningful ratio, and a
        plausible fake would propagate into whatever a caller measured with it.
        """
        if self.unit_size == 0:
            return None
        return self.bytes_touched / self.unit_size


@dataclass
class WorkLedger:
    """Running totals across a runtime session."""

    operations: int = 0
    bytes_touched: int = 0
    bytes_returned: int = 0
    instructions_visited: int = 0
    elapsed_seconds: float = 0.0
    records: List[OperationRecord] = field(default_factory=list)

    # Keeping every record forever would make a long-running session grow without
    # bound. The totals are always exact; only the per-operation tail is capped.
    max_records: int = 4096

    def record(self, entry: OperationRecord) -> None:
        self.operations += 1
        self.bytes_touched += entry.bytes_touched
        self.bytes_returned += entry.bytes_returned
        self.instructions_visited += entry.instructions_visited
        self.elapsed_seconds += entry.elapsed_seconds
        self.records.append(entry)
        if len(self.records) > self.max_records:
            del self.records[: len(self.records) - self.max_records]

    def reset(self) -> None:
        self.operations = 0
        self.bytes_touched = 0
        self.bytes_returned = 0
        self.instructions_visited = 0
        self.elapsed_seconds = 0.0
        self.records.clear()

    def summary(self) -> str:
        return (
            f"{self.operations} operation(s): "
            f"{self.bytes_touched} bytes touched, "
            f"{self.bytes_returned} bytes returned, "
            f"{self.instructions_visited} instructions visited, "
            f"{self.elapsed_seconds * 1000:.2f} ms"
        )
