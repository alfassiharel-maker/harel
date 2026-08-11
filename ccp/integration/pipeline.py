"""The build pipeline: real input in, verified artifact out.

This is the automatic path the product runs on the user's behalf:

    input -> analyse -> build representation -> verify -> artifact

It orchestrates the layers below it and implements none of them. The
representation is built by the Core through `ccp.product`; verification is the
Product Engine's `validate()`, which is the Runtime's full digest pass. Nothing
here reconstructs, diffs, or re-checks anything itself.

Progress is reported through a callback so a user interface can show what is
happening, and cancellation is cooperative: the pipeline checks the token between
stages and between units, so a cancelled build stops promptly and leaves nothing
half-written.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..api import BuildConfig
from ..product import CCPArtifact, ProductEngine, ProductError
from .errors import CancelledError, InputError
from .sources import InputAnalysis, InputLimits, analyse_input, source_for


class Stage(str, Enum):
    """Where a build has got to. The application shows these to the user."""

    ANALYSING = "analysing"
    READING = "reading"
    BUILDING = "building"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Progress:
    """One progress notification."""

    stage: Stage
    message: str
    current: int = 0
    total: int = 0

    @property
    def fraction(self) -> Optional[float]:
        """Completed fraction, or None when the total is not yet known.

        None rather than 0.0: a caller must be able to tell "no progress yet"
        from "indeterminate", and a fabricated 0.0 hides the difference.
        """
        if self.total <= 0:
            return None
        return min(1.0, self.current / self.total)


ProgressCallback = Callable[[Progress], None]


class CancellationToken:
    """Cooperative cancellation, safe to set from another thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError("the operation was cancelled")


@dataclass(frozen=True)
class BuildOutcome:
    """Everything the application needs after a successful build."""

    artifact: CCPArtifact
    analysis: InputAnalysis
    container: bytes
    units_verified: int
    analyse_seconds: float
    build_seconds: float
    verify_seconds: float

    @property
    def total_seconds(self) -> float:
        return self.analyse_seconds + self.build_seconds + self.verify_seconds


class BuildPipeline:
    """Runs the automatic input-to-verified-artifact path.

    Holds no state between runs: every call gets its own units, container and
    artifact, so two pipelines never interfere and there is nothing global to
    reset.
    """

    def __init__(
        self,
        engine: Optional[ProductEngine] = None,
        limits: InputLimits = InputLimits(),
        config: Optional[BuildConfig] = None,
    ) -> None:
        self._engine = engine or ProductEngine()
        self._limits = limits
        self._config = config

    @property
    def engine(self) -> ProductEngine:
        return self._engine

    @property
    def limits(self) -> InputLimits:
        return self._limits

    def run(
        self,
        path: str,
        on_progress: Optional[ProgressCallback] = None,
        cancel: Optional[CancellationToken] = None,
    ) -> BuildOutcome:
        """Analyse, build and verify. Raises a `ProductError` subclass on failure."""
        report = on_progress or (lambda _progress: None)
        token = cancel or CancellationToken()

        # --- analyse ------------------------------------------------------
        token.raise_if_cancelled()
        report(Progress(Stage.ANALYSING, "Looking at the input"))
        started = time.perf_counter()
        analysis = analyse_input(path, self._limits)
        analyse_seconds = time.perf_counter() - started
        report(
            Progress(
                Stage.ANALYSING,
                f"Found {analysis.unit_count} files, {analysis.total_bytes} bytes",
                analysis.unit_count,
                analysis.unit_count,
            )
        )

        # --- read units ---------------------------------------------------
        # Units are collected here rather than streamed straight into the build
        # so the pipeline can report progress per file and honour cancellation
        # between files. The input has already been checked against the limits,
        # so this is bounded by the analysis above and not by whatever the user
        # happened to point at.
        token.raise_if_cancelled()
        started = time.perf_counter()
        collected = {}
        for index, unit in enumerate(source_for(path, self._limits).units(), start=1):
            token.raise_if_cancelled()
            collected[unit.uid] = unit.data
            if index % 25 == 0 or index == analysis.unit_count:
                report(
                    Progress(
                        Stage.READING,
                        f"Reading {index}/{analysis.unit_count}",
                        index,
                        analysis.unit_count,
                    )
                )
        if not collected:
            raise InputError(f"no usable files found in {path}")

        # --- build --------------------------------------------------------
        token.raise_if_cancelled()
        report(
            Progress(
                Stage.BUILDING,
                f"Finding shared structure across {len(collected)} units",
            )
        )
        container = self._engine.build_from_units(collected, self._config)
        build_seconds = time.perf_counter() - started
        report(
            Progress(
                Stage.BUILDING,
                f"Representation built: {len(container)} bytes",
                len(collected),
                len(collected),
            )
        )

        # --- verify -------------------------------------------------------
        token.raise_if_cancelled()
        report(Progress(Stage.VERIFYING, "Checking every unit against its digest"))
        started = time.perf_counter()
        artifact = self._engine.open(container, verify=False)
        try:
            verification = artifact.validate()
        except ProductError:
            artifact.close()
            raise
        verify_seconds = time.perf_counter() - started

        if token.cancelled:
            # Cancelled after the work completed: release the artifact rather
            # than handing back a resource the caller did not ask to own.
            artifact.close()
            raise CancelledError("the operation was cancelled")

        report(
            Progress(
                Stage.COMPLETE,
                f"Verified {verification.units_ok} units",
                verification.units_ok,
                verification.units_checked,
            )
        )
        return BuildOutcome(
            artifact=artifact,
            analysis=analysis,
            container=container,
            units_verified=verification.units_ok,
            analyse_seconds=analyse_seconds,
            build_seconds=build_seconds,
            verify_seconds=verify_seconds,
        )
