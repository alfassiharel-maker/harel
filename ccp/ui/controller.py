"""The application controller: everything the UI does, minus the pixels.

The controller owns the application's state machine and its single artifact. It
is deliberately free of any presentation technology, which is what makes the
product's whole workflow testable without a display: the tests drive this class
directly, and the view is a thin layer over it.

Two rules it enforces, both from the layers below:

*   **One artifact at a time, and it is closed before another replaces it.**
    Resource ownership is explicit; nothing waits for a garbage collector.
*   **Serialised access.** The semantic contract lists concurrent access to one
    artifact instance as unsupported. A user interface is inherently concurrent
    (a request arrives while a build runs), so the controller holds a lock across
    every artifact operation. That *respects* the contract rather than widening
    it: the artifact still only ever sees one caller at a time.

State transitions are explicit, and the application stays usable after any
recoverable failure -- an error moves it to ERROR while keeping a loaded artifact
open and readable, so a bad range does not cost the user their build.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from ..integration import (
    BuildPipeline,
    CancellationToken,
    InputAnalysis,
    InputLimits,
    Progress,
    RecentArtifacts,
    RecentEntry,
    Stage,
    analyse_input,
    atomic_write,
    default_output_dir,
    safe_export_path,
)
from ..integration.errors import CancelledError, ExportError, InputError
from ..product import (
    CCPArtifact,
    MaterializeResult,
    ProductEngine,
    ProductError,
    ReadResult,
    ResourceError,
    UnitNotFoundError,
)

# A selective read is meant to be selective. Allowing an unbounded length would
# let the UI quietly turn one into a full materialisation, which is the exact
# confusion the product exists to make visible, so it is capped and the cap is
# reported rather than silently applied.
MAX_SELECTIVE_READ_BYTES = 8 << 20

# How much of a result is sent to the view for display. The full bytes stay in
# the controller for export; only a preview crosses to the UI, so a large read
# cannot be turned into a huge payload by accident.
PREVIEW_BYTES = 4096


class AppState(str, Enum):
    """Where the application is. Exactly one of these at any moment."""

    NO_INPUT = "no_input"
    ANALYSING = "analysing"
    BUILDING = "building"
    VERIFYING = "verifying"
    READY = "ready"
    READING = "reading"
    MATERIALIZING = "materializing"
    EXPORTING = "exporting"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class UserFacingError:
    """An error as the user should see it: what, why, and what to do next."""

    title: str
    what: str
    why: str
    next_step: str
    kind: str

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "what": self.what,
            "why": self.why,
            "next": self.next_step,
            "kind": self.kind,
        }


@dataclass
class BuildStatus:
    """Live progress of the running (or last) build."""

    stage: str = Stage.ANALYSING.value
    message: str = ""
    current: int = 0
    total: int = 0
    running: bool = False

    @property
    def fraction(self) -> Optional[float]:
        if self.total <= 0:
            return None
        return min(1.0, self.current / self.total)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "fraction": self.fraction,
            "running": self.running,
        }


@dataclass
class ArtifactSummary:
    """The artifact as the overview screen shows it."""

    label: str
    source: str
    units: int
    literals: int
    derived: int
    groups: int
    original_bytes: int
    artifact_bytes: int
    saving: Optional[float]
    verified: bool
    saved_path: Optional[str]
    build_seconds: float

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "source": self.source,
            "units": self.units,
            "literals": self.literals,
            "derived": self.derived,
            "groups": self.groups,
            "original_bytes": self.original_bytes,
            "artifact_bytes": self.artifact_bytes,
            "saving": self.saving,
            "verified": self.verified,
            "saved_path": self.saved_path,
            "build_seconds": self.build_seconds,
        }


def describe_error(error: BaseException) -> UserFacingError:
    """Turn any exception into something a person can act on.

    Every typed product error gets a specific explanation. An unexpected internal
    failure is still surfaced -- never swallowed -- but framed as a fault in the
    application rather than as something the user did wrong.
    """
    from ..product import (
        InvalidRangeError,
        MalformedArtifactError,
        UnsupportedOperationError,
        VerificationError,
    )

    if isinstance(error, InputError):
        return UserFacingError(
            title="That input can't be used",
            what=str(error),
            why="The folder or archive is missing, empty, of an unsupported kind, "
            "or larger than this version supports.",
            next_step="Choose a project folder or a .zip archive that fits the "
            "supported limits, then try again.",
            kind="input",
        )
    if isinstance(error, MalformedArtifactError):
        return UserFacingError(
            title="This file isn't a CCP artifact",
            what=str(error),
            why="The file is not in the CCP container format, or it has been "
            "truncated or damaged since it was written.",
            next_step="Open a .ccp file produced by this application, or rebuild "
            "it from the original input.",
            kind="malformed",
        )
    if isinstance(error, UnitNotFoundError):
        return UserFacingError(
            title="That item isn't in this artifact",
            what=str(error),
            why="The artifact has no unit with that name. It may have been "
            "renamed, or the artifact may be a different build than expected.",
            next_step="Pick an item from the list of units in this artifact.",
            kind="unit",
        )
    if isinstance(error, InvalidRangeError):
        return UserFacingError(
            title="That range isn't valid",
            what=str(error),
            why="Offset and length must both be zero or greater.",
            next_step="Enter a non-negative offset and length, then read again.",
            kind="range",
        )
    if isinstance(error, VerificationError):
        return UserFacingError(
            title="Verification failed",
            what=str(error),
            why="At least one unit did not match the digest recorded when the "
            "artifact was built. The artifact is damaged and its contents "
            "cannot be trusted.",
            next_step="Rebuild the artifact from the original input. Do not use "
            "this copy.",
            kind="verification",
        )
    if isinstance(error, UnsupportedOperationError):
        return UserFacingError(
            title="Not supported in this version",
            what=str(error),
            why="The semantic contract for this version does not offer that "
            "operation.",
            next_step="See the documented list of supported operations.",
            kind="unsupported",
        )
    if isinstance(error, CancelledError):
        return UserFacingError(
            title="Cancelled",
            what="The operation was cancelled.",
            why="You asked it to stop before it finished.",
            next_step="Start again whenever you are ready.",
            kind="cancelled",
        )
    if isinstance(error, ExportError):
        return UserFacingError(
            title="Couldn't save that",
            what=str(error),
            why="The destination could not be written: it may already exist, be "
            "read-only, be out of space, or lie outside the allowed folder.",
            next_step="Choose a different destination, or allow overwriting.",
            kind="export",
        )
    if isinstance(error, ResourceError):
        return UserFacingError(
            title="Not enough room to do that safely",
            what=str(error),
            why="The artifact or the request is larger than this version can "
            "handle in memory, or the artifact has been closed.",
            next_step="Work with a smaller input, or reopen the artifact.",
            kind="resource",
        )
    if isinstance(error, ProductError):
        return UserFacingError(
            title="That didn't work",
            what=str(error),
            why="CCP refused the operation.",
            next_step="Check the details and try a different request.",
            kind="product",
        )
    if isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return UserFacingError(
            title="File or folder not found",
            what=str(error),
            why="The path does not exist, or is not the kind of thing expected.",
            next_step="Check the location and choose it again.",
            kind="filesystem",
        )
    if isinstance(error, PermissionError):
        return UserFacingError(
            title="Permission denied",
            what=str(error),
            why="This account is not allowed to read or write that location.",
            next_step="Choose a location you have access to.",
            kind="filesystem",
        )
    if isinstance(error, MemoryError):
        return UserFacingError(
            title="Ran out of memory",
            what="The operation needed more memory than is available.",
            why="The input or the request is too large for this machine.",
            next_step="Try a smaller input or a smaller read.",
            kind="resource",
        )
    if isinstance(error, OSError):
        return UserFacingError(
            title="A file system error occurred",
            what=str(error),
            why="The operating system refused the operation.",
            next_step="Check the location, permissions and free space.",
            kind="filesystem",
        )
    return UserFacingError(
        title="Something went wrong inside CCP",
        what=f"{type(error).__name__}: {error}",
        why="This is an unexpected internal failure, not something you did wrong.",
        next_step="The application is still running. Try the operation again, or "
        "rebuild the artifact.",
        kind="internal",
    )


class AppController:
    """The whole application, without a view.

    Owns at most one artifact and one build at a time. Every method that touches
    the artifact takes the lock, so the artifact never sees two callers at once
    even though a UI serves requests concurrently.
    """

    def __init__(
        self,
        engine: Optional[ProductEngine] = None,
        limits: Optional[InputLimits] = None,
        recents: Optional[RecentArtifacts] = None,
        output_dir: Optional[str] = None,
    ) -> None:
        self._limits = limits or InputLimits()
        self._pipeline = BuildPipeline(engine or ProductEngine(), self._limits)
        self._recents = recents if recents is not None else RecentArtifacts()
        self._output_dir = output_dir or default_output_dir()

        self._lock = threading.RLock()
        self._state = AppState.NO_INPUT
        self._artifact: Optional[CCPArtifact] = None
        self._summary: Optional[ArtifactSummary] = None
        self._analysis: Optional[InputAnalysis] = None
        self._error: Optional[UserFacingError] = None
        self._status = BuildStatus()
        self._cancel: Optional[CancellationToken] = None
        self._build_thread: Optional[threading.Thread] = None
        # The container bytes of a built artifact, kept so the user can save it.
        # None for an artifact opened from a file: it is already on disk.
        self._container: Optional[bytes] = None
        # The bytes of the last full materialisation, held so the user can export
        # what they just looked at without materialising it a second time. One
        # unit only -- bounded, and dropped whenever the artifact changes.
        self._last_materialized: Optional[Tuple[str, bytes]] = None

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def limits(self) -> InputLimits:
        return self._limits

    @property
    def output_dir(self) -> str:
        return self._output_dir

    def _set_state(self, state: AppState) -> None:
        with self._lock:
            self._state = state

    def _fail(self, error: BaseException) -> UserFacingError:
        described = describe_error(error)
        with self._lock:
            self._error = described
            # A failure while an artifact is loaded must not cost the user their
            # artifact: stay usable and let them try another request.
            self._state = AppState.READY if self._artifact is not None else AppState.ERROR
        return described

    def snapshot(self) -> dict:
        """Everything a view needs to render, in one consistent read."""
        with self._lock:
            return {
                "state": self._state.value,
                "status": self._status.as_dict(),
                "artifact": self._summary.as_dict() if self._summary else None,
                "error": self._error.as_dict() if self._error else None,
                "analysis": (
                    {
                        "kind": self._analysis.kind,
                        "path": self._analysis.path,
                        "unit_count": self._analysis.unit_count,
                        "total_bytes": self._analysis.total_bytes,
                        "largest_unit_bytes": self._analysis.largest_unit_bytes,
                        "skipped": [
                            {"name": name, "reason": reason}
                            for name, reason in self._analysis.skipped[:50]
                        ],
                    }
                    if self._analysis
                    else None
                ),
                "output_dir": self._output_dir,
                "limits": {
                    "max_units": self._limits.max_units,
                    "max_total_bytes": self._limits.max_total_bytes,
                    "max_unit_bytes": self._limits.max_unit_bytes,
                    "max_selective_read_bytes": MAX_SELECTIVE_READ_BYTES,
                },
            }

    def clear_error(self) -> None:
        with self._lock:
            self._error = None

    # -- input ------------------------------------------------------------

    def analyse(self, path: str) -> dict:
        """Look at an input and report what it contains. Builds nothing."""
        self._set_state(AppState.ANALYSING)
        try:
            analysis = analyse_input(path, self._limits)
        except Exception as error:  # noqa: BLE001 - mapped to a typed user error
            self._fail(error)
            with self._lock:
                if self._artifact is None:
                    self._state = AppState.NO_INPUT
            raise
        with self._lock:
            self._analysis = analysis
            self._error = None
            self._state = AppState.READY if self._artifact else AppState.NO_INPUT
        return self.snapshot()

    # -- build ------------------------------------------------------------

    def build(self, path: str, label: Optional[str] = None) -> ArtifactSummary:
        """Run the whole pipeline synchronously and adopt the result."""
        token = CancellationToken()
        with self._lock:
            if self._status.running:
                raise ResourceError("a build is already running")
            self._cancel = token
            self._status = BuildStatus(running=True, message="Starting")
            self._error = None
            self._state = AppState.ANALYSING

        def on_progress(progress: Progress) -> None:
            with self._lock:
                self._status = BuildStatus(
                    stage=progress.stage.value,
                    message=progress.message,
                    current=progress.current,
                    total=progress.total,
                    running=True,
                )
                if progress.stage is Stage.BUILDING:
                    self._state = AppState.BUILDING
                elif progress.stage is Stage.VERIFYING:
                    self._state = AppState.VERIFYING

        try:
            outcome = self._pipeline.run(path, on_progress=on_progress, cancel=token)
        except Exception as error:  # noqa: BLE001 - mapped to a typed user error
            stage = (
                Stage.CANCELLED.value
                if isinstance(error, CancelledError)
                else Stage.FAILED.value
            )
            with self._lock:
                self._status = BuildStatus(
                    stage=stage, message=str(error), running=False
                )
                self._cancel = None
            self._fail(error)
            with self._lock:
                if self._artifact is None:
                    self._state = AppState.NO_INPUT if stage == Stage.CANCELLED.value else AppState.ERROR
            raise

        info = outcome.artifact.info()
        summary = ArtifactSummary(
            label=label or os.path.basename(os.path.normpath(path)) or "artifact",
            source=outcome.analysis.path,
            units=info.units,
            literals=info.literals,
            derived=info.derived,
            groups=len(outcome.artifact.groups()),
            original_bytes=info.original_bytes,
            artifact_bytes=info.container_bytes,
            saving=info.saving,
            verified=outcome.artifact.is_verified,
            saved_path=None,
            build_seconds=outcome.total_seconds,
        )

        with self._lock:
            self._adopt(outcome.artifact)
            self._summary = summary
            self._analysis = outcome.analysis
            self._container = outcome.container
            self._status = BuildStatus(
                stage=Stage.COMPLETE.value,
                message=f"Verified {outcome.units_verified} units",
                current=outcome.units_verified,
                total=info.units,
                running=False,
            )
            self._cancel = None
            self._state = AppState.READY
        return summary

    def build_async(self, path: str, label: Optional[str] = None) -> None:
        """Start a build on a worker thread. Progress is read from `snapshot()`."""
        with self._lock:
            if self._status.running:
                raise ResourceError("a build is already running")

        def worker() -> None:
            try:
                self.build(path, label)
            except Exception:  # noqa: BLE001 - already recorded for the UI
                # `build` has recorded a user-facing error and reset the running
                # flag; the thread must not raise into nothing.
                pass

        thread = threading.Thread(target=worker, name="ccp-build", daemon=True)
        with self._lock:
            self._build_thread = thread
        thread.start()

    def cancel(self) -> bool:
        """Ask a running build to stop. Returns whether there was one."""
        with self._lock:
            token = self._cancel
        if token is None:
            return False
        token.cancel()
        return True

    def wait_for_build(self, timeout: Optional[float] = None) -> bool:
        """Wait for an async build to finish. Returns False on timeout."""
        with self._lock:
            thread = self._build_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- open / close -----------------------------------------------------

    def open_artifact(self, path: str) -> ArtifactSummary:
        """Open an artifact that was saved earlier."""
        try:
            artifact = self._pipeline.engine.open(path, verify=False)
        except Exception as error:  # noqa: BLE001
            self._fail(error)
            raise
        info = artifact.info()
        summary = ArtifactSummary(
            label=os.path.basename(path),
            source=path,
            units=info.units,
            literals=info.literals,
            derived=info.derived,
            groups=len(artifact.groups()),
            original_bytes=info.original_bytes,
            artifact_bytes=info.container_bytes,
            saving=info.saving,
            verified=artifact.is_verified,
            saved_path=os.path.abspath(path),
            build_seconds=0.0,
        )
        with self._lock:
            self._adopt(artifact)
            self._summary = summary
            self._analysis = None
            self._container = None
            self._error = None
            self._state = AppState.READY
        return summary

    def _adopt(self, artifact: CCPArtifact) -> None:
        """Take ownership of `artifact`, closing whatever it replaces."""
        previous = self._artifact
        self._artifact = artifact
        self._last_materialized = None
        if previous is not None and previous is not artifact:
            previous.close()

    def close(self) -> None:
        """Release the artifact and return to the start. Idempotent."""
        with self._lock:
            if self._artifact is not None:
                self._artifact.close()
            self._artifact = None
            self._summary = None
            self._analysis = None
            self._last_materialized = None
            self._error = None
            self._status = BuildStatus()
            self._state = AppState.CLOSED

    def _require_artifact(self) -> CCPArtifact:
        artifact = self._artifact
        if artifact is None or not artifact.is_open:
            raise ResourceError("no artifact is open")
        return artifact

    # -- browse -----------------------------------------------------------

    def units(
        self, query: str = "", offset: int = 0, limit: int = 200
    ) -> Dict[str, object]:
        """Browse units, optionally filtered by a substring of the name."""
        with self._lock:
            artifact = self._require_artifact()
            names = artifact.units()
            if query:
                needle = query.lower()
                names = [name for name in names if needle in name.lower()]
            total = len(names)
            if offset < 0 or limit < 0:
                raise ValueError("offset and limit must be non-negative")
            page = names[offset : offset + limit]
            rows = []
            for uid in page:
                info = artifact.stat(uid)
                rows.append(
                    {
                        "uid": uid,
                        "kind": info.kind,
                        "size": info.size,
                        "stored_bytes": info.stored_bytes,
                        "base_uid": info.base_uid,
                        "instructions": info.instructions,
                        "reuse_ratio": info.reuse_ratio,
                    }
                )
            return {"total": total, "offset": offset, "units": rows}

    def groups(self) -> List[dict]:
        """Shared-base groups: which units were represented against which base."""
        with self._lock:
            artifact = self._require_artifact()
            out = []
            for base_uid, members in sorted(artifact.groups().items()):
                out.append(
                    {
                        "base_uid": base_uid,
                        "base_size": artifact.stat(base_uid).size,
                        "members": sorted(members),
                        "member_count": len(members),
                    }
                )
            return out

    def unit_detail(self, uid: str) -> dict:
        with self._lock:
            artifact = self._require_artifact()
            info = artifact.stat(uid)
            return {
                "uid": info.uid,
                "kind": info.kind,
                "size": info.size,
                "stored_bytes": info.stored_bytes,
                "base_uid": info.base_uid,
                "instructions": info.instructions,
                "copied_bytes": info.copied_bytes,
                "added_bytes": info.added_bytes,
                "reuse_ratio": info.reuse_ratio,
            }

    # -- read -------------------------------------------------------------

    def read_range(self, uid: str, offset: int, length: int) -> dict:
        """Selective reconstruction: read a window, executing only what covers it."""
        if length > MAX_SELECTIVE_READ_BYTES:
            raise ResourceError(
                f"a selective read is capped at {MAX_SELECTIVE_READ_BYTES} bytes; "
                f"use full materialisation for more"
            )
        with self._lock:
            artifact = self._require_artifact()
            self._state = AppState.READING
            try:
                result = artifact.read_range(uid, offset, length)
            except Exception as error:  # noqa: BLE001
                self._fail(error)
                raise
            self._state = AppState.READY
            self._error = None
            return self._read_payload(result)

    def materialize(self, uid: str) -> dict:
        """Full materialisation: the whole unit, checked against its digest."""
        with self._lock:
            artifact = self._require_artifact()
            size = artifact.stat(uid).size
            if size > self._limits.max_unit_bytes:
                raise ResourceError(
                    f"{uid!r} is {size} bytes, above the "
                    f"{self._limits.max_unit_bytes}-byte materialisation limit"
                )
            self._state = AppState.MATERIALIZING
            try:
                result = artifact.materialize(uid)
            except Exception as error:  # noqa: BLE001
                self._fail(error)
                raise
            self._last_materialized = (uid, result.data)
            self._state = AppState.READY
            self._error = None
            return self._materialize_payload(result)

    @staticmethod
    def _preview(data: bytes) -> dict:
        head = data[:PREVIEW_BYTES]
        try:
            text = head.decode("utf-8")
            printable = True
        except UnicodeDecodeError:
            text = ""
            printable = False
        return {
            "hex": head.hex(),
            "text": text,
            "is_text": printable,
            "preview_bytes": len(head),
            "truncated": len(data) > len(head),
        }

    def _read_payload(self, result: ReadResult) -> dict:
        report = result.report
        return {
            "mode": "selective",
            "uid": result.uid,
            "offset": result.offset,
            "length": result.length,
            "preview": self._preview(result.data),
            "metrics": {
                "bytes_requested": report.bytes_requested,
                "bytes_returned": report.bytes_returned,
                "bytes_touched": report.bytes_touched,
                "work_ratio": report.work_ratio,
                "units_touched": report.units_touched,
                "instructions_visited": report.instructions_visited,
                "instructions_total": report.instructions_total,
                "verification": report.verification.value,
                "artifact_verified": report.artifact_verified,
                "elapsed_seconds": report.elapsed_seconds,
            },
        }

    def _materialize_payload(self, result: MaterializeResult) -> dict:
        report = result.report
        return {
            "mode": "full",
            "uid": result.uid,
            "size": len(result.data),
            "preview": self._preview(result.data),
            "metrics": {
                "bytes_requested": report.bytes_requested,
                "bytes_returned": report.bytes_returned,
                "bytes_touched": report.bytes_touched,
                "work_ratio": report.work_ratio,
                "units_touched": report.units_touched,
                "instructions_visited": report.instructions_visited,
                "instructions_total": report.instructions_total,
                "verification": report.verification.value,
                "artifact_verified": report.artifact_verified,
                "elapsed_seconds": report.elapsed_seconds,
            },
        }

    # -- verify -----------------------------------------------------------

    def verify(self) -> dict:
        """Reconstruct and check every unit against its recorded digest."""
        with self._lock:
            artifact = self._require_artifact()
            self._state = AppState.VERIFYING
            started = time.perf_counter()
            try:
                report = artifact.validate()
            except Exception as error:  # noqa: BLE001
                self._fail(error)
                raise
            elapsed = time.perf_counter() - started
            if self._summary is not None:
                self._summary.verified = True
            self._state = AppState.READY
            self._error = None
            return {
                "ok": report.ok,
                "units_checked": report.units_checked,
                "units_ok": report.units_ok,
                "bytes_verified": report.bytes_verified,
                "elapsed_seconds": elapsed,
                "failures": [
                    {"uid": uid, "error": message} for uid, message in report.failures
                ],
            }

    # -- export -----------------------------------------------------------

    def save_artifact(self, path: str, overwrite: bool = False) -> str:
        """Write the built artifact to disk, atomically."""
        with self._lock:
            self._require_artifact()
            container = self._container
            if container is None:
                raise ExportError(
                    "this artifact was opened from a file rather than built, so "
                    "there is nothing new to save"
                )
            self._state = AppState.EXPORTING
            try:
                written = atomic_write(path, container, overwrite=overwrite)
            except Exception as error:  # noqa: BLE001
                self._fail(error)
                raise
            if self._summary is not None:
                self._summary.saved_path = os.path.abspath(written)
                self._recents.remember(
                    RecentEntry(
                        path=os.path.abspath(written),
                        label=self._summary.label,
                        units=self._summary.units,
                        original_bytes=self._summary.original_bytes,
                        artifact_bytes=self._summary.artifact_bytes,
                    )
                )
            self._state = AppState.READY
            return os.path.abspath(written)

    def export_unit(
        self,
        uid: str,
        destination_dir: Optional[str] = None,
        name: Optional[str] = None,
        overwrite: bool = False,
    ) -> dict:
        """Write one unit's exact bytes to a file inside `destination_dir`.

        The destination path is resolved through `safe_export_path`, so a unit id
        that names `../../something` cannot write outside the chosen folder.
        """
        with self._lock:
            artifact = self._require_artifact()
            target_dir = destination_dir or self._output_dir
            self._state = AppState.EXPORTING
            try:
                cached = self._last_materialized
                if cached is not None and cached[0] == uid:
                    data = cached[1]
                    verified = True
                else:
                    result = artifact.materialize(uid)
                    data = result.data
                    verified = True
                    self._last_materialized = (uid, data)
                destination = safe_export_path(target_dir, name or uid)
                written = atomic_write(destination, data, overwrite=overwrite)
            except Exception as error:  # noqa: BLE001
                self._fail(error)
                raise
            self._state = AppState.READY
            self._error = None
            return {
                "uid": uid,
                "path": written,
                "bytes": len(data),
                "digest_checked": verified,
            }

    # -- recents ----------------------------------------------------------

    def recents(self) -> List[dict]:
        return [entry.as_dict() for entry in self._recents.load()]
