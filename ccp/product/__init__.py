"""CCP Product Engine — artifacts with a lifecycle, over the public API.

The application layer. It consumes `ccp.api` (and through it the Runtime and
Core); it contains no algorithm and no second reconstruction path. External code
uses `ccp.product` for artifact-oriented access, or `ccp.api` for the lower-level
runtime surface.
"""

from .engine import (
    DEFAULT_MAX_ARTIFACT_BYTES,
    BatchReadResult,
    CCPArtifact,
    MaterializeResult,
    ProductEngine,
    ReadResult,
)
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

__all__ = [
    "BatchReadResult",
    "CCPArtifact",
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "InvalidRangeError",
    "MalformedArtifactError",
    "MaterializeResult",
    "OperationReport",
    "ProductEngine",
    "ProductError",
    "ProductLedger",
    "ReadResult",
    "ResourceError",
    "UnitNotFoundError",
    "UnsupportedOperationError",
    "VerificationError",
    "VerificationState",
]
