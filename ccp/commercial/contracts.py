"""Contract versioning and terms acceptance models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from .models import CommercialContract


@dataclass(frozen=True)
class CommercialContractVersioned:
    """A versioned commercial contract with full content for reproducibility."""
    contract_version: int
    effective_from: datetime
    content_hash: str  # SHA256 of contract terms, for audit trail

    # Full contract terms
    baseline_definition: str
    measurement_definition: str
    savings_formula: str
    overage_formula: str
    price_model: str
    currency: str
    maximum_credit: Decimal
    maximum_charge: Decimal
    billing_period: str
    dispute_policy_reference: str
    terms_version: int

    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    deprecated_at: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def to_commercial_contract(self) -> CommercialContract:
        """Convert to CommercialContract for use in calculations."""
        return CommercialContract(
            contract_version=self.contract_version,
            effective_from=self.effective_from,
            baseline_definition=self.baseline_definition,
            measurement_definition=self.measurement_definition,
            savings_formula=self.savings_formula,
            overage_formula=self.overage_formula,
            price_model=self.price_model,
            currency=self.currency,
            maximum_credit=self.maximum_credit,
            maximum_charge=self.maximum_charge,
            billing_period=self.billing_period,
            dispute_policy_reference=self.dispute_policy_reference,
            terms_version=self.terms_version,
        )


@dataclass(frozen=True)
class TermsAcceptance:
    """Record that a customer accepted specific terms at a specific time."""
    acceptance_id: UUID
    customer_id: UUID
    agreement_id: UUID
    terms_version: int
    contract_version: int
    accepted_at: datetime
    ip_address: str  # For audit/compliance
    user_agent: str  # For audit/compliance

    metadata: dict = field(default_factory=dict)

    def is_current(self, current_terms_version: int) -> bool:
        """Check if this acceptance is for the current terms version."""
        return self.terms_version == current_terms_version
