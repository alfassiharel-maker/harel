"""Commercial integration for the application controller.

Wraps AppController to add commercial measurement, ledger persistence,
and billing workflows. Maintains separation: AppController handles artifacts,
CommerceController handles measurement and payment.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from ..commercial import CommerceService, MeasurementLedger, SandboxPaymentProvider
from ..commercial.contracts import CommercialContractVersioned
from ..commercial.models import Agreement, Customer
from .controller import AppController


@dataclass
class CommercialState:
    """Customer commercial account and agreement state."""
    customer_id: Optional[UUID] = None
    customer_name: Optional[str] = None
    agreement_id: Optional[UUID] = None
    contract_version: int = 1
    terms_accepted: bool = False
    terms_accepted_at: Optional[str] = None
    account_balance: Decimal = Decimal(0)
    total_credits: Decimal = Decimal(0)
    total_charges: Decimal = Decimal(0)


@dataclass
class ArtifactMeasurement:
    """Measurement of an artifact's financial impact."""
    measurement_id: UUID
    artifact_digest: str
    baseline_bytes: int
    ccp_bytes: int
    result_type: str  # SAVINGS, NO_CHANGE, OVERAGE
    calculated_credit: Decimal
    calculated_charge: Decimal
    status: str  # MEASURED, AUTHORIZED, SETTLED, etc.


class CommerceController:
    """Orchestrates commercial operations alongside the application controller.

    This controller runs a CommerceService with a persistent measurement ledger
    and sandbox payment provider. It coordinates with AppController to measure
    artifacts automatically as they are built.
    """

    def __init__(
        self,
        app_controller: AppController,
        ledger_path: str,
    ) -> None:
        self.app_controller = app_controller
        self._ledger = MeasurementLedger(ledger_path)
        self._payment_provider = SandboxPaymentProvider()
        self._service = CommerceService(self._ledger, self._payment_provider)
        self._lock = threading.Lock()

        # Commercial state
        self._commercial_state = CommercialState()

        # Register the default contract (v1)
        self._register_default_contract()

    def _register_default_contract(self) -> None:
        """Register the default commercial contract."""
        from datetime import datetime

        contract = CommercialContractVersioned(
            contract_version=1,
            effective_from=datetime.utcnow(),
            content_hash="contract_v1_default",
            baseline_definition="Original artifact file sizes summed",
            measurement_definition="CCP container size",
            savings_formula="(baseline_bytes - ccp_bytes) / baseline_bytes * unit_price",
            overage_formula="(ccp_bytes - baseline_bytes) / 1024 * unit_price_per_kb",
            price_model="per_kilobyte",
            currency="USD",
            maximum_credit=Decimal(10000),  # $100 max credit per artifact
            maximum_charge=Decimal(50000),  # $500 max charge per artifact
            billing_period="per_artifact",
            dispute_policy_reference="default_policy",
            terms_version=1,
        )
        self._service.register_contract(contract)

    def set_customer(
        self, customer_id: UUID, customer_name: str, email: str
    ) -> CommercialState:
        """Set the current customer for measurement."""
        with self._lock:
            self._commercial_state.customer_id = customer_id
            self._commercial_state.customer_name = customer_name

            # Create customer in payment system (sandbox)
            self._payment_provider.create_customer(
                email=email, name=customer_name, customer_id=customer_id
            )

            # Load customer ledger
            self._refresh_customer_balance()

            return self._commercial_state

    def _refresh_customer_balance(self) -> None:
        """Recalculate customer balance from ledger."""
        if not self._commercial_state.customer_id:
            return

        customer_id = self._commercial_state.customer_id
        self._commercial_state.account_balance = self._service.customer_balance(customer_id)
        self._commercial_state.total_credits = self._service.customer_total_credits(
            customer_id
        )
        self._commercial_state.total_charges = self._service.customer_total_charges(
            customer_id
        )

    def accept_terms(self, terms_version: int) -> CommercialState:
        """Record customer's acceptance of commercial terms."""
        if not self._commercial_state.customer_id:
            raise ValueError("no customer set")

        with self._lock:
            from datetime import datetime

            self._commercial_state.terms_accepted = True
            self._commercial_state.terms_accepted_at = datetime.utcnow().isoformat()
            self._commercial_state.contract_version = terms_version

            return self._commercial_state

    def measure_artifact(
        self, artifact_digest: str, baseline_bytes: int, ccp_bytes: int
    ) -> ArtifactMeasurement:
        """
        Measure an artifact and record to ledger.

        Creates a MeasurementRecord and persists it. Returns the financial summary.
        """
        if not self._commercial_state.customer_id:
            raise ValueError("no customer set")

        if not self._commercial_state.terms_accepted:
            raise ValueError("customer has not accepted terms")

        with self._lock:
            record = self._service.measure_artifact(
                customer_id=self._commercial_state.customer_id,
                artifact_id=uuid4(),
                artifact_digest=artifact_digest,
                application_version="1.0.0",  # Should be from app metadata
                ccp_version="1.0.0",  # Should be from CCP version
                baseline_bytes=baseline_bytes,
                ccp_bytes=ccp_bytes,
                contract_version=self._commercial_state.contract_version,
            )

            self._refresh_customer_balance()

            return ArtifactMeasurement(
                measurement_id=record.measurement_id,
                artifact_digest=record.artifact_digest,
                baseline_bytes=record.baseline_bytes,
                ccp_bytes=record.ccp_bytes,
                result_type=record.result_type.value,
                calculated_credit=record.calculated_credit,
                calculated_charge=record.calculated_charge,
                status=record.status.value,
            )

    def get_commercial_state(self) -> CommercialState:
        """Get the current commercial state for the UI."""
        with self._lock:
            return CommercialState(
                customer_id=self._commercial_state.customer_id,
                customer_name=self._commercial_state.customer_name,
                agreement_id=self._commercial_state.agreement_id,
                contract_version=self._commercial_state.contract_version,
                terms_accepted=self._commercial_state.terms_accepted,
                terms_accepted_at=self._commercial_state.terms_accepted_at,
                account_balance=self._commercial_state.account_balance,
                total_credits=self._commercial_state.total_credits,
                total_charges=self._commercial_state.total_charges,
            )

    def get_customer_measurements(self) -> list[ArtifactMeasurement]:
        """Get all measurements for the current customer."""
        if not self._commercial_state.customer_id:
            return []

        with self._lock:
            all_records = self._ledger.read_for_customer(self._commercial_state.customer_id)

            return [
                ArtifactMeasurement(
                    measurement_id=record.measurement_id,
                    artifact_digest=record.artifact_digest,
                    baseline_bytes=record.baseline_bytes,
                    ccp_bytes=record.ccp_bytes,
                    result_type=record.result_type.value,
                    calculated_credit=record.calculated_credit,
                    calculated_charge=record.calculated_charge,
                    status=record.status.value,
                )
                for record in all_records
            ]
