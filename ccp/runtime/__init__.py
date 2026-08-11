"""CCP Runtime — a loaded representation you can do work against.

Depends on `ccp.core` and `ccp.capabilities`. Contains no algorithm: every byte
it returns is produced by the Core.
"""

from .accounting import OperationRecord, WorkLedger
from .contract import CONTRACT, CONTRACT_VERSION, SemanticContract
from .session import (
    CCPRuntime,
    ReadResult,
    RepresentationInfo,
    RuntimeStateError,
    UnitInfo,
    UnknownUnitError,
    VerificationReport,
    describe_contract,
)

__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
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
    "describe_contract",
]
