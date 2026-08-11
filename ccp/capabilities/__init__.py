"""Execution capabilities over the CCP representation.

Depends on `ccp.core` and on nothing above it.
"""

from .reader import CCPReader, RangeRead

__all__ = ["CCPReader", "RangeRead"]
