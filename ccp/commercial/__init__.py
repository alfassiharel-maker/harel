"""Commercial layer for CCP Forge: billing, contracts, and payment integration.

This layer wraps the product layer with customer accounting, deterministic
commercial calculations, append-only measurement ledger, contract versioning,
and payment provider abstraction (sandbox-only, no real money).

Layers:
- models: Customer, Agreement, Contract, MeasurementRecord, PaymentStatus
- accounting: CommerceCalculation with savings/overage formulas using Decimal
- ledger: append-only persistence of measurements and charges
- contracts: CommercialContract versioning and terms acceptance models
- payments: PaymentProvider interface and sandbox implementation
- service: CommerceService orchestrating accounting + payment

All financial calculations use Python Decimal for exact arithmetic.
No randomness in ledger values — reproducible results for same input.
"""

from __future__ import annotations

from .accounting import CommerceCalculation
from .contracts import CommercialContractVersioned, TermsAcceptance
from .ledger import MeasurementLedger
from .models import (
    Agreement,
    BillingStatus,
    CommercialContract,
    Customer,
    Entitlement,
    MeasurementRecord,
    PaymentStatus,
    ResultType,
)
from .payments import PaymentProvider, SandboxPaymentProvider
from .service import CommerceService

__all__ = [
    "Customer",
    "Agreement",
    "Entitlement",
    "CommercialContract",
    "MeasurementRecord",
    "PaymentStatus",
    "ResultType",
    "BillingStatus",
    "CommerceCalculation",
    "MeasurementLedger",
    "CommercialContractVersioned",
    "TermsAcceptance",
    "PaymentProvider",
    "SandboxPaymentProvider",
    "CommerceService",
]
