"""CommerceService: orchestrating accounting, ledger, and payment integration."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional
from uuid import UUID

from .accounting import CommerceCalculation
from .contracts import CommercialContractVersioned, TermsAcceptance
from .ledger import MeasurementLedger
from .models import Agreement, BillingStatus, Customer, MeasurementRecord
from .payments import PaymentProvider


class CommerceService:
    """Handles all commercial operations: measurement, billing, and payments.

    This service is the application's interface to commercial functionality.
    It orchestrates accounting calculations, ledger persistence, and payment
    authorization/capture. All financial authority comes through here.
    """

    def __init__(
        self,
        ledger: MeasurementLedger,
        payment_provider: PaymentProvider,
    ) -> None:
        self.ledger = ledger
        self.payment_provider = payment_provider
        self._contracts: dict[int, CommercialContractVersioned] = {}
        self._current_contract_version = 1

    def register_contract(self, contract: CommercialContractVersioned) -> None:
        """Register a versioned contract for use in calculations."""
        self._contracts[contract.contract_version] = contract
        # Update current version if this is newer
        if contract.contract_version > self._current_contract_version:
            self._current_contract_version = contract.contract_version

    def get_contract(self, version: int) -> Optional[CommercialContractVersioned]:
        """Retrieve a specific contract version."""
        return self._contracts.get(version)

    def measure_artifact(
        self,
        customer_id: UUID,
        artifact_id: UUID,
        artifact_digest: str,
        application_version: str,
        ccp_version: str,
        baseline_bytes: int,
        ccp_bytes: int,
        contract_version: Optional[int] = None,
    ) -> MeasurementRecord:
        """
        Measure an artifact and calculate resulting credit/charge.

        The measurement is recorded in the ledger immediately. The status
        starts as MEASURED and flows through the payment pipeline from there.
        """
        version = contract_version or self._current_contract_version
        contract = self._contracts.get(version)

        if not contract:
            raise ValueError(f"no contract version {version}")

        calc = CommerceCalculation(contract.to_commercial_contract())
        record = calc.calculate(
            customer_id=customer_id,
            artifact_id=artifact_id,
            artifact_digest=artifact_digest,
            application_version=application_version,
            ccp_version=ccp_version,
            baseline_bytes=baseline_bytes,
            ccp_bytes=ccp_bytes,
        )

        # Persist to ledger
        self.ledger.append(record)

        return record

    def authorize_measurement(
        self,
        measurement_id: UUID,
        customer_id: UUID,
        agreement_id: UUID,
        payment_method_reference: str,
    ) -> MeasurementRecord:
        """
        Authorize payment for a measurement.

        Moves status from MEASURED to AUTHORIZED if payment provider accepts.
        """
        # Find the measurement
        all_records = self.ledger.read_for_customer(customer_id)
        measurement = None
        for record in all_records:
            if record.measurement_id == measurement_id:
                measurement = record
                break

        if not measurement:
            raise ValueError(f"measurement {measurement_id} not found")

        if measurement.status != BillingStatus.MEASURED:
            raise ValueError(
                f"can only authorize MEASURED records; status is {measurement.status}"
            )

        # Determine amount to charge/credit
        if measurement.calculated_charge > 0:
            amount = measurement.calculated_charge
        elif measurement.calculated_credit > 0:
            amount = measurement.calculated_credit
        else:
            # No financial transaction needed
            amount = Decimal(0)

        if amount > 0:
            # Authorize with payment provider
            status = self.payment_provider.authorize_charge(
                customer_id=customer_id,
                amount_minor_units=amount,
                currency=measurement.currency,
                description=f"Artifact {measurement.artifact_id}",
                idempotency_key=str(measurement_id),
            )

            if not status.success:
                # Record the failed authorization attempt
                # Status stays MEASURED (not advanced to PENDING)
                pass
            else:
                # Create updated record with AUTHORIZED status
                updated = MeasurementRecord(
                    measurement_id=measurement.measurement_id,
                    customer_id=measurement.customer_id,
                    artifact_id=measurement.artifact_id,
                    artifact_digest=measurement.artifact_digest,
                    application_version=measurement.application_version,
                    ccp_version=measurement.ccp_version,
                    timestamp=measurement.timestamp,
                    baseline_bytes=measurement.baseline_bytes,
                    ccp_bytes=measurement.ccp_bytes,
                    delta_bytes=measurement.delta_bytes,
                    result_type=measurement.result_type,
                    savings_bytes=measurement.savings_bytes,
                    overage_bytes=measurement.overage_bytes,
                    savings_ratio=measurement.savings_ratio,
                    overage_ratio=measurement.overage_ratio,
                    pricing_model_version=measurement.pricing_model_version,
                    calculated_credit=measurement.calculated_credit,
                    calculated_charge=measurement.calculated_charge,
                    currency=measurement.currency,
                    status=BillingStatus.AUTHORIZED,
                    payment_transaction_id=status.transaction_id,
                    agreement_id=agreement_id,
                )
                self.ledger.append(updated)
                return updated

        return measurement

    def settle_measurement(self, measurement_id: UUID, customer_id: UUID) -> MeasurementRecord:
        """
        Settle a measurement: capture the charge or issue the credit.

        Moves status from AUTHORIZED to SETTLED.
        """
        all_records = self.ledger.read_for_customer(customer_id)
        measurement = None
        for record in all_records:
            if record.measurement_id == measurement_id:
                measurement = record
                break

        if not measurement:
            raise ValueError(f"measurement {measurement_id} not found")

        if measurement.status != BillingStatus.AUTHORIZED:
            raise ValueError(
                f"can only settle AUTHORIZED records; status is {measurement.status}"
            )

        # Execute the settlement
        if measurement.calculated_charge > 0 and measurement.payment_transaction_id:
            status = self.payment_provider.capture_charge(
                transaction_id=measurement.payment_transaction_id,
                amount_minor_units=measurement.calculated_charge,
            )
        elif measurement.calculated_credit > 0:
            status = self.payment_provider.issue_credit(
                customer_id=customer_id,
                amount_minor_units=measurement.calculated_credit,
                currency=measurement.currency,
                description=f"Artifact {measurement.artifact_id}",
                idempotency_key=str(measurement_id),
            )
        else:
            # No financial transaction
            status = type('obj', (object,), {'success': True, 'transaction_id': None})()

        if status.success:
            updated = MeasurementRecord(
                measurement_id=measurement.measurement_id,
                customer_id=measurement.customer_id,
                artifact_id=measurement.artifact_id,
                artifact_digest=measurement.artifact_digest,
                application_version=measurement.application_version,
                ccp_version=measurement.ccp_version,
                timestamp=measurement.timestamp,
                baseline_bytes=measurement.baseline_bytes,
                ccp_bytes=measurement.ccp_bytes,
                delta_bytes=measurement.delta_bytes,
                result_type=measurement.result_type,
                savings_bytes=measurement.savings_bytes,
                overage_bytes=measurement.overage_bytes,
                savings_ratio=measurement.savings_ratio,
                overage_ratio=measurement.overage_ratio,
                pricing_model_version=measurement.pricing_model_version,
                calculated_credit=measurement.calculated_credit,
                calculated_charge=measurement.calculated_charge,
                currency=measurement.currency,
                status=BillingStatus.SETTLED,
                payment_transaction_id=status.transaction_id or measurement.payment_transaction_id,
                agreement_id=measurement.agreement_id,
            )
            self.ledger.append(updated)
            return updated

        return measurement

    def customer_balance(self, customer_id: UUID) -> Decimal:
        """Get the customer's account balance (credits - charges)."""
        return self.ledger.balance_for_customer(customer_id)

    def customer_total_credits(self, customer_id: UUID) -> Decimal:
        """Get total credits issued to a customer."""
        return self.ledger.total_credits_for_customer(customer_id)

    def customer_total_charges(self, customer_id: UUID) -> Decimal:
        """Get total charges applied to a customer."""
        return self.ledger.total_charges_for_customer(customer_id)
