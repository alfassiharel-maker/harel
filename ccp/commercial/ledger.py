"""Append-only measurement ledger for financial record-keeping.

The ledger is write-once: records are appended, never modified. This is
enforced at the interface level. All financial transactions flow through
the ledger, creating an auditable trail.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from .models import BillingStatus, MeasurementRecord


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal and UUID types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class MeasurementLedger:
    """Append-only ledger of measurements and financial transactions.

    This is the source of truth for all customer billing. Records are
    immutable once written. Refunds are recorded as new (compensating) entries.
    """

    def __init__(self, ledger_path: str) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Create file if it doesn't exist
        if not self.ledger_path.exists():
            self.ledger_path.touch()

    def append(self, record: MeasurementRecord) -> None:
        """Append a measurement record to the ledger (immutable once written)."""
        entry = {
            "measurement_id": str(record.measurement_id),
            "customer_id": str(record.customer_id),
            "artifact_id": str(record.artifact_id),
            "artifact_digest": record.artifact_digest,
            "application_version": record.application_version,
            "ccp_version": record.ccp_version,
            "timestamp": record.timestamp.isoformat(),
            "baseline_bytes": record.baseline_bytes,
            "ccp_bytes": record.ccp_bytes,
            "delta_bytes": record.delta_bytes,
            "result_type": record.result_type.value,
            "savings_bytes": record.savings_bytes,
            "overage_bytes": record.overage_bytes,
            "savings_ratio": str(record.savings_ratio),
            "overage_ratio": str(record.overage_ratio),
            "pricing_model_version": record.pricing_model_version,
            "calculated_credit": str(record.calculated_credit),
            "calculated_charge": str(record.calculated_charge),
            "currency": record.currency,
            "status": record.status.value,
            "payment_transaction_id": record.payment_transaction_id,
            "agreement_id": str(record.agreement_id) if record.agreement_id else None,
        }

        # Append as newline-delimited JSON
        with open(self.ledger_path, "a") as f:
            f.write(json.dumps(entry, cls=DecimalEncoder) + "\n")

    def read_all(self) -> List[MeasurementRecord]:
        """Read all measurement records from the ledger."""
        records: List[MeasurementRecord] = []

        if not self.ledger_path.exists():
            return records

        with open(self.ledger_path, "r") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    record = self._entry_to_record(entry)
                    records.append(record)
                except (json.JSONDecodeError, ValueError) as e:
                    # Skip malformed entries (should never happen in production)
                    continue

        return records

    def read_for_customer(self, customer_id: UUID) -> List[MeasurementRecord]:
        """Read all measurements for a specific customer."""
        all_records = self.read_all()
        return [r for r in all_records if r.customer_id == customer_id]

    def read_for_customer_and_status(
        self, customer_id: UUID, status: BillingStatus
    ) -> List[MeasurementRecord]:
        """Read measurements for a customer with a specific billing status."""
        all_records = self.read_all()
        return [r for r in all_records if r.customer_id == customer_id and r.status == status]

    def _entry_to_record(self, entry: dict) -> MeasurementRecord:
        """Convert a ledger entry to a MeasurementRecord."""
        from .models import ResultType

        return MeasurementRecord(
            measurement_id=UUID(entry["measurement_id"]),
            customer_id=UUID(entry["customer_id"]),
            artifact_id=UUID(entry["artifact_id"]),
            artifact_digest=entry["artifact_digest"],
            application_version=entry["application_version"],
            ccp_version=entry["ccp_version"],
            timestamp=datetime.fromisoformat(entry["timestamp"]),
            baseline_bytes=entry["baseline_bytes"],
            ccp_bytes=entry["ccp_bytes"],
            delta_bytes=entry["delta_bytes"],
            result_type=ResultType(entry["result_type"]),
            savings_bytes=entry["savings_bytes"],
            overage_bytes=entry["overage_bytes"],
            savings_ratio=Decimal(entry["savings_ratio"]),
            overage_ratio=Decimal(entry["overage_ratio"]),
            pricing_model_version=entry["pricing_model_version"],
            calculated_credit=Decimal(entry["calculated_credit"]),
            calculated_charge=Decimal(entry["calculated_charge"]),
            currency=entry["currency"],
            status=BillingStatus(entry["status"]),
            payment_transaction_id=entry.get("payment_transaction_id"),
            agreement_id=UUID(entry["agreement_id"]) if entry.get("agreement_id") else None,
        )

    def total_credits_for_customer(self, customer_id: UUID) -> Decimal:
        """Sum all credits issued to a customer."""
        records = self.read_for_customer(customer_id)
        return sum(
            (r.calculated_credit for r in records if r.status in (BillingStatus.CREDITED, BillingStatus.SETTLED)),
            Decimal(0),
        )

    def total_charges_for_customer(self, customer_id: UUID) -> Decimal:
        """Sum all charges applied to a customer."""
        records = self.read_for_customer(customer_id)
        return sum(
            (r.calculated_charge for r in records if r.status in (BillingStatus.SETTLED, BillingStatus.AUTHORIZED)),
            Decimal(0),
        )

    def balance_for_customer(self, customer_id: UUID) -> Decimal:
        """Calculate the account balance (credits - charges)."""
        credits = self.total_credits_for_customer(customer_id)
        charges = self.total_charges_for_customer(customer_id)
        return credits - charges
