"""Product-level error model.

The Core and Runtime raise a handful of exceptions (`CCPFormatError`,
`CCPIntegrityError`, `UnknownUnitError`, `ValueError`). Those are correct, but a
product caller should not have to catch a `ValueError` to learn that a range was
invalid, nor guess which layer a `KeyError` came from. This module gives the
product one typed hierarchy, and the engine maps every underlying failure onto
it so a caller catches `ProductError` (or a specific subclass) and nothing else.

The mapping is one-way and lossless: each product error keeps the original
exception on `.cause`, so nothing about the failure is hidden — it is only
renamed into a surface a product can depend on.

The six failure classes named in the product requirements each get a type:

    malformed artifact      -> MalformedArtifactError
    invalid UID             -> UnitNotFoundError
    invalid range           -> InvalidRangeError
    verification failure    -> VerificationError
    unsupported operation   -> UnsupportedOperationError
    resource failure        -> ResourceError
"""

from __future__ import annotations

from typing import Optional


class ProductError(Exception):
    """Base of every error the Product Engine raises. Catch this to catch all."""

    def __init__(self, message: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        # The originating exception, kept rather than discarded. `raise ... from`
        # sets __cause__ for tracebacks; this field makes it available to code.
        self.cause = cause


class MalformedArtifactError(ProductError):
    """The artifact bytes are not a valid CCP container, or are truncated."""


class UnitNotFoundError(ProductError):
    """A unit id that is not present in the artifact."""

    def __init__(self, uid: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(f"unit not found: {uid!r}", cause)
        self.uid = uid


class InvalidRangeError(ProductError):
    """A read range is not expressible (negative offset or length)."""


class VerificationError(ProductError):
    """A reconstruction did not match its recorded digest."""


class UnsupportedOperationError(ProductError):
    """An operation the semantic contract does not offer in this version."""

    def __init__(self, operation: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(f"operation not supported by the contract: {operation!r}", cause)
        self.operation = operation


class ResourceError(ProductError):
    """A resource-level failure: artifact closed, too large, or unreadable."""


class CommercialError(ProductError):
    """Base of commercial-layer errors. Catch this for all billing/payment failures."""


class ContractError(CommercialError):
    """Contract validation or versioning failure."""


class TermsNotAcceptedError(CommercialError):
    """Required terms have not been accepted by the customer."""

    def __init__(self, customer_id: str, terms_version: int) -> None:
        super().__init__(
            f"customer {customer_id!r} has not accepted terms version {terms_version}"
        )
        self.customer_id = customer_id
        self.terms_version = terms_version


class PaymentError(CommercialError):
    """Payment provider returned an error."""

    def __init__(
        self, error_code: str, error_message: str, cause: Optional[BaseException] = None
    ) -> None:
        super().__init__(f"payment error {error_code}: {error_message}", cause)
        self.error_code = error_code


class LedgerError(CommercialError):
    """Ledger persistence or read failure."""
