"""Deterministic commercial calculations using Decimal arithmetic.

All financial values use Python Decimal for exact arithmetic (not binary
floating-point). Same artifact + same contract = identical result, always.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Tuple

from .models import CommercialContract, MeasurementRecord, ResultType, BillingStatus
from uuid import UUID, uuid4
from datetime import datetime


class CommerceCalculation:
    """Calculate savings/overage credits and charges with deterministic precision."""

    def __init__(self, contract: CommercialContract) -> None:
        self.contract = contract

    def calculate(
        self,
        customer_id: UUID,
        artifact_id: UUID,
        artifact_digest: str,
        application_version: str,
        ccp_version: str,
        baseline_bytes: int,
        ccp_bytes: int,
    ) -> MeasurementRecord:
        """
        Create a measurement record with calculated credit/charge.

        Financial values are computed deterministically using Decimal.
        All ratios and amounts are reproducible from the same input.
        """
        if baseline_bytes < 0 or ccp_bytes < 0:
            raise ValueError("byte counts must be non-negative")

        timestamp = datetime.utcnow()
        delta_bytes = ccp_bytes - baseline_bytes

        # Classify result type
        if delta_bytes < 0:
            # Savings: CCP is smaller
            result_type = ResultType.SAVINGS
            savings_bytes = -delta_bytes
            overage_bytes = 0
        elif delta_bytes > 0:
            # Overage: CCP is larger
            result_type = ResultType.OVERAGE
            savings_bytes = 0
            overage_bytes = delta_bytes
        else:
            # No change
            result_type = ResultType.NO_CHANGE
            savings_bytes = 0
            overage_bytes = 0

        # Compute ratios with Decimal
        if baseline_bytes > 0:
            savings_ratio = Decimal(savings_bytes) / Decimal(baseline_bytes)
            overage_ratio = Decimal(overage_bytes) / Decimal(baseline_bytes)
        else:
            # No baseline: neither savings nor overage
            savings_ratio = Decimal(0)
            overage_ratio = Decimal(0)

        # Calculate credit or charge based on result type
        calculated_credit, calculated_charge = self._compute_financial_amounts(
            result_type,
            savings_bytes,
            overage_bytes,
            baseline_bytes,
            ccp_bytes,
            savings_ratio,
            overage_ratio,
        )

        return MeasurementRecord(
            measurement_id=uuid4(),
            customer_id=customer_id,
            artifact_id=artifact_id,
            artifact_digest=artifact_digest,
            application_version=application_version,
            ccp_version=ccp_version,
            timestamp=timestamp,
            baseline_bytes=baseline_bytes,
            ccp_bytes=ccp_bytes,
            delta_bytes=delta_bytes,
            result_type=result_type,
            savings_bytes=savings_bytes,
            overage_bytes=overage_bytes,
            savings_ratio=savings_ratio,
            overage_ratio=overage_ratio,
            pricing_model_version=self.contract.contract_version,
            calculated_credit=calculated_credit,
            calculated_charge=calculated_charge,
            currency=self.contract.currency,
            status=BillingStatus.MEASURED,
        )

    def _compute_financial_amounts(
        self,
        result_type: ResultType,
        savings_bytes: int,
        overage_bytes: int,
        baseline_bytes: int,
        ccp_bytes: int,
        savings_ratio: Decimal,
        overage_ratio: Decimal,
    ) -> Tuple[Decimal, Decimal]:
        """
        Compute credit (for savings) or charge (for overage) in minor units.

        Uses Decimal for exact arithmetic. Rounds to nearest even (ROUND_HALF_UP).
        """
        if result_type == ResultType.SAVINGS:
            # Credit calculation: savings_bytes * unit_price
            # Example formula: (baseline - ccp) / baseline * $price_per_unit
            credit = self._calculate_savings_credit(
                savings_bytes, baseline_bytes, savings_ratio
            )
            charge = Decimal(0)
        elif result_type == ResultType.OVERAGE:
            # Charge calculation: overage_bytes * unit_price
            charge = self._calculate_overage_charge(
                overage_bytes, baseline_bytes, overage_ratio
            )
            credit = Decimal(0)
        else:
            # No change
            credit = Decimal(0)
            charge = Decimal(0)

        # Enforce contract limits
        credit = min(credit, self.contract.maximum_credit)
        charge = min(charge, self.contract.maximum_charge)

        return credit, charge

    def _calculate_savings_credit(
        self, savings_bytes: int, baseline_bytes: int, ratio: Decimal
    ) -> Decimal:
        """Calculate credit for savings (in minor units, e.g. cents)."""
        # Example: $0.01 per 1KB saved = 1 cent per 1024 bytes
        # savings_bytes / 1024 * 100 (cents per dollar)
        if savings_bytes == 0:
            return Decimal(0)

        # Base calculation: (savings_bytes / 1024) * 1 cent = savings / 1024 cents
        amount = Decimal(savings_bytes) / Decimal(1024)

        # Round to nearest cent using ROUND_HALF_UP
        amount = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        return amount

    def _calculate_overage_charge(
        self, overage_bytes: int, baseline_bytes: int, ratio: Decimal
    ) -> Decimal:
        """Calculate charge for overage (in minor units, e.g. cents)."""
        # Example: $0.02 per 1KB overage = 2 cents per 1024 bytes
        if overage_bytes == 0:
            return Decimal(0)

        # Base calculation: (overage_bytes / 1024) * 2 cents
        amount = Decimal(overage_bytes) / Decimal(1024) * Decimal(2)

        # Round to nearest cent
        amount = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        return amount
