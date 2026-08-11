"""The stable public interface to CCP.

Everything an external caller needs is here, and nothing here exposes how the
Core works. Callers import from `ccp.api` and never from `ccp.core` or
`ccp.runtime`; those are free to change as long as this surface does not.

    from ccp.api import build, open_representation

    container = build.from_directory("./project")     # bytes
    rt = open_representation(container, verify=True)

    whole  = rt.materialize("src/main.py")            # bit-exact, digest-checked
    window = rt.read_range("src/main.py", 8000, 256)  # only the covering work
    print(window.bytes_touched, window.unit_size)

The contract the runtime enforces is `ccp.api.CONTRACT`; `describe_contract()`
renders it. Read it before depending on behaviour it does not promise.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Union

from ..core import (
    BuildConfig,
    CCPFormatError,
    CCPIntegrityError,
    DirectoryUnitSource,
    InMemoryUnitSource,
    build_model,
    serialize,
)
from ..runtime import (
    CONTRACT,
    CCPRuntime,
    OperationRecord,
    ReadResult,
    RepresentationInfo,
    RuntimeStateError,
    SemanticContract,
    UnitInfo,
    UnknownUnitError,
    VerificationReport,
    WorkLedger,
    describe_contract,
)

__all__ = [
    "CONTRACT",
    "BuildConfig",
    "CCPFormatError",
    "CCPIntegrityError",
    "CCPRuntime",
    "OperationRecord",
    "ReadResult",
    "RepresentationInfo",
    "RuntimeStateError",
    "SemanticContract",
    "UnitInfo",
    "UnknownUnitError",
    "VerificationReport",
    "WorkLedger",
    "build",
    "describe_contract",
    "open_representation",
]


class build:
    """Producing a representation. A namespace, not something to instantiate."""

    @staticmethod
    def from_directory(
        path: str, config: Optional[BuildConfig] = None
    ) -> bytes:
        """Build a container from every file under `path`, one unit per file."""
        model = build_model(DirectoryUnitSource(path), config)
        return serialize(model)

    @staticmethod
    def from_units(
        units: Mapping[str, bytes], config: Optional[BuildConfig] = None
    ) -> bytes:
        """Build a container from units held in memory."""
        model = build_model(InMemoryUnitSource(dict(units)), config)
        return serialize(model)


def open_representation(
    source: Union[bytes, bytearray, memoryview, str],
    verify: bool = False,
) -> CCPRuntime:
    """Open a representation for use.

    `source` is either container bytes or a path to a container file. Pass
    `verify=True` to reconstruct and check every unit before the call returns;
    it costs a full pass and is the right default for anything that came from
    outside the caller's control.
    """
    if isinstance(source, str):
        return CCPRuntime.open(source, verify=verify)
    return CCPRuntime.load(bytes(source), verify=verify)
