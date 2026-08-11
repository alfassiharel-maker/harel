"""Data models for commercial accounting, contracts, and ledger records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4


class ResultType(str, Enum):
    """Outcome of comparing baseline to CCP representation."""
    SAVINGS = "SAVINGS"      # CCP bytes < baseline bytes
    NO_CHANGE = "NO_CHANGE"  # CCP bytes ≈ baseline bytes
    OVERAGE = "OVERAGE"      # CCP bytes > baseline bytes


class BillingStatus(str, Enum):
    """State of a measurement in the financial lifecycle."""
    MEASURED = "MEASURED"      # Recorded, awaiting authorization
    PENDING = "PENDING"        # Authorization in progress
    AUTHORIZED = "AUTHORIZED"  # Amount agreed, awaiting capture
    SETTLED = "SETTLED"        # Payment completed
    CREDITED = "CREDITED"      # Credit applied to account
    FAILED = "FAILED"          # Payment failed
    DISPUTED = "DISPUTED"      # Customer disputed the charge
    CANCELLED = "CANCELLED"    # Transaction cancelled


@dataclass(frozen=True)
class Customer:
    """A customer account for billing."""
    customer_id: UUID
    email: str
    name: str
    created_at: datetime
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Agreement:
    """A subscription or one-time billing agreement."""
    agreement_id: UUID
    customer_id: UUID
    contract_version: int
    created_at: datetime
    effective_from: datetime
    terms_accepted_at: Optional[datetime] = None
    terms_accepted_version: Optional[int] = None
    status: str = "PENDING"  # PENDING, ACTIVE, SUSPENDED, TERMINATED
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Entitlement:
    """What a customer is entitled to measure and be charged for."""
    entitlement_id: UUID
    agreement_id: UUID
    measurement_type: str  # e.g. "savings_credit", "overage_charge"
    enabled: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CommercialContract:
    """Versioned commercial terms that determine billing calculations."""
    contract_version: int
    effective_from: datetime
    baseline_definition: str  # e.g. "original file size in bytes"
    measurement_definition: str  # e.g. "artifact size + measurement overhead"
    savings_formula: str  # e.g. "(baseline - ccp_bytes) / baseline * unit_price_cents"
    overage_formula: str  # e.g. "(ccp_bytes - baseline) / 1024 * unit_price_cents"
    price_model: str  # e.g. "per_kilobyte" or "fixed_tier"
    currency: str  # ISO 4217, e.g. "USD"
    maximum_credit: Decimal  # Max credit per transaction, in minor units (cents)
    maximum_charge: Decimal  # Max charge per transaction, in minor units (cents)
    billing_period: str  # e.g. "monthly", "per_artifact"
    dispute_policy_reference: str  # e.g. "policy_v1_disputes"
    terms_version: int  # Linked to terms acceptance version


@dataclass(frozen=True)
class MeasurementRecord:
    """One artifact measurement and resulting financial calculation."""
    measurement_id: UUID
    customer_id: UUID
    artifact_id: UUID  # Content hash of the artifact
    artifact_digest: str  # Hex digest for reproducibility
    application_version: str  # CCP Forge version
    ccp_version: str  # CCP Core version
    timestamp: datetime

    # Size measurements
    baseline_bytes: int  # Sum of original file sizes
    ccp_bytes: int  # Size of CCP artifact
    delta_bytes: int  # Overhead (ccp_bytes - baseline_bytes if overage, else 0)

    # Result classification
    result_type: ResultType
    savings_bytes: int  # baseline - ccp_bytes if SAVINGS, else 0
    overage_bytes: int  # ccp_bytes - baseline if OVERAGE, else 0
    savings_ratio: Decimal  # (baseline - ccp_bytes) / baseline, or 0.0
    overage_ratio: Decimal  # (ccp_bytes - baseline) / baseline, or 0.0

    # Financial calculations (in minor units, e.g. cents)
    pricing_model_version: int
    calculated_credit: Decimal  # Amount credited to account (>= 0)
    calculated_charge: Decimal  # Amount charged from account (>= 0)
    currency: str  # ISO 4217

    # Billing state
    status: BillingStatus

    # Optional references
    payment_transaction_id: Optional[str] = None
    agreement_id: Optional[UUID] = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentStatus:
    """Result of a payment operation."""
    success: bool
    transaction_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
