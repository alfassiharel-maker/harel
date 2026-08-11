"""Integration-level errors, continuing the product's typed hierarchy.

These subclass `ProductError` rather than starting a second hierarchy, so an
application still catches one base class for everything CCP can raise.
"""

from __future__ import annotations

from ..product import ProductError


class InputError(ProductError):
    """The user's input cannot be used: missing, wrong kind, or over a limit."""


class CancelledError(ProductError):
    """The operation was cancelled by the user before it finished."""


class ExportError(ProductError):
    """A result could not be written to the requested location."""
