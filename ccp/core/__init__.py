"""CCP Core Engine — the real implementation of the CCP algorithm.

Standard library only, and no imports from anywhere else in this repository. The
Core is the one place the algorithm lives; every layer above it is a consumer.
"""

from .change_program import (
    Add,
    CCPFormatError,
    ChangeProgram,
    Copy,
    Instruction,
    paste,
)
from .chunking import Chunk, chunk_unit
from .container import container_overhead, deserialize, serialize
from .differ import build_change_program
from .representation import (
    KIND_DERIVED,
    KIND_LITERAL,
    BuildConfig,
    BuildStats,
    CCPIntegrityError,
    CCPModel,
    UnitRecord,
    build_model,
)
from .similarity import SimilarityIndex
from .units import (
    DirectoryUnitSource,
    InMemoryUnitSource,
    Unit,
    UnitSource,
    unit_digest,
)

__all__ = [
    "Add",
    "BuildConfig",
    "BuildStats",
    "CCPFormatError",
    "CCPIntegrityError",
    "CCPModel",
    "ChangeProgram",
    "Chunk",
    "Copy",
    "DirectoryUnitSource",
    "InMemoryUnitSource",
    "Instruction",
    "KIND_DERIVED",
    "KIND_LITERAL",
    "SimilarityIndex",
    "Unit",
    "UnitRecord",
    "UnitSource",
    "build_change_program",
    "build_model",
    "chunk_unit",
    "container_overhead",
    "deserialize",
    "paste",
    "serialize",
    "unit_digest",
]
