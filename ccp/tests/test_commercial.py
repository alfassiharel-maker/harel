"""Tests for commercial accounting, ledger, and payment functionality."""

import json
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from ccp.commercial import (
    Agreement,
    BillingStatus,
    CommerceCalculation,
    CommercialContract,
    CommercialContractVersioned,
    CommerceService,
    Customer,
    Entitlement,
    MeasurementLedger,
    MeasurementRecord,
    ResultType,
    SandboxPaymentProvider,
    TermsAcceptance,
)


class TestCommerceCalculationDeterminism(unittest.TestCase):
    """Test that calculations are deterministic and reproducible."""

    def setUp(self):
        self.contract = CommercialContract(
            contract_version=1,
            effective_from=datetime.utcnow(),
            baseline_definition="original file size",
            measurement_definition="artifact size including overhead",
            savings_formula="(baseline - ccp) / baseline * price",
            overage_formula="(ccp - baseline) / 1024 * price",
            price_model="per_kilobyte",
            currency="USD",
            maximum_credit=Decimal(10000),  # $100.00
            maximum_charge=Decimal(50000),  # $500.00
            billing_period="per_artifact",
            dispute_policy_reference="policy_v1",
            terms_version=1,
        )
        self.calc = CommerceCalculation(self.contract)

    def test_savings_result_type_classification(self):
        """CCP smaller than baseline produces SAVINGS result."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=10000,
            ccp_bytes=6000,
        )

        self.assertEqual(record.result_type, ResultType.SAVINGS)
        self.assertEqual(record.savings_bytes, 4000)
        self.assertEqual(record.overage_bytes, 0)
        self.assertGreater(record.calculated_credit, 0)
        self.assertEqual(record.calculated_charge, 0)

    def test_overage_result_type_classification(self):
        """CCP larger than baseline produces OVERAGE result."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=10000,
            ccp_bytes=15000,
        )

        self.assertEqual(record.result_type, ResultType.OVERAGE)
        self.assertEqual(record.savings_bytes, 0)
        self.assertEqual(record.overage_bytes, 5000)
        self.assertEqual(record.calculated_credit, 0)
        self.assertGreater(record.calculated_charge, 0)

    def test_no_change_result_type_classification(self):
        """CCP same size as baseline produces NO_CHANGE result."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=10000,
            ccp_bytes=10000,
        )

        self.assertEqual(record.result_type, ResultType.NO_CHANGE)
        self.assertEqual(record.savings_bytes, 0)
        self.assertEqual(record.overage_bytes, 0)
        self.assertEqual(record.calculated_credit, 0)
        self.assertEqual(record.calculated_charge, 0)

    def test_deterministic_savings_calculation(self):
        """Same input always produces identical credit calculation."""
        inputs = {
            "customer_id": UUID("12345678-1234-5678-1234-567812345678"),
            "artifact_id": UUID("87654321-4321-8765-4321-876543218765"),
            "artifact_digest": "fixed_digest_value",
            "application_version": "1.0.0",
            "ccp_version": "1.0.0",
            "baseline_bytes": 102400,  # 100 KB
            "ccp_bytes": 51200,  # 50 KB = 50% savings
        }

        record1 = self.calc.calculate(**inputs)
        record2 = self.calc.calculate(**inputs)

        # Key values must be identical
        self.assertEqual(record1.result_type, record2.result_type)
        self.assertEqual(record1.savings_bytes, record2.savings_bytes)
        self.assertEqual(record1.savings_ratio, record2.savings_ratio)
        self.assertEqual(record1.calculated_credit, record2.calculated_credit)
        self.assertEqual(record1.currency, record2.currency)

    def test_decimal_arithmetic_precision(self):
        """Financial calculations use Decimal for exact precision."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=1024,
            ccp_bytes=512,
        )

        # 512 bytes saved / 1024 base = 50% savings
        # Credit = 512 / 1024 cents = 0.5 cents -> rounds to 1 cent
        self.assertIsInstance(record.calculated_credit, Decimal)
        self.assertGreater(record.calculated_credit, 0)

    def test_ratio_calculation_with_zero_baseline(self):
        """Ratios are zero when there is no baseline."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=0,
            ccp_bytes=1000,
        )

        self.assertEqual(record.savings_ratio, Decimal(0))
        self.assertEqual(record.overage_ratio, Decimal(0))

    def test_maximum_credit_enforced(self):
        """Credits are capped at maximum_credit."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=100_000_000,  # 100 MB
            ccp_bytes=1_000_000,  # 1 MB - massive savings
        )

        # Credit should be capped at maximum_credit (10000 = $100.00)
        self.assertLessEqual(record.calculated_credit, self.contract.maximum_credit)

    def test_maximum_charge_enforced(self):
        """Charges are capped at maximum_charge."""
        record = self.calc.calculate(
            customer_id=uuid4(),
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=1000,
            ccp_bytes=500_000_000,  # 500 MB - massive overage
        )

        # Charge should be capped at maximum_charge (50000 = $500.00)
        self.assertLessEqual(record.calculated_charge, self.contract.maximum_charge)

    def test_negative_bytes_rejected(self):
        """Negative byte counts are rejected."""
        with self.assertRaises(ValueError):
            self.calc.calculate(
                customer_id=uuid4(),
                artifact_id=uuid4(),
                artifact_digest="abc123",
                application_version="1.0.0",
                ccp_version="1.0.0",
                baseline_bytes=-1000,
                ccp_bytes=5000,
            )


class TestMeasurementLedger(unittest.TestCase):
    """Test append-only ledger functionality."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ledger_path = Path(self.temp_dir) / "ledger.jsonl"
        self.ledger = MeasurementLedger(str(self.ledger_path))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_append_and_read_single_record(self):
        """Records can be appended and read back."""
        customer_id = uuid4()
        record = MeasurementRecord(
            measurement_id=uuid4(),
            customer_id=customer_id,
            artifact_id=uuid4(),
            artifact_digest="abc123",
            application_version="1.0.0",
            ccp_version="1.0.0",
            timestamp=datetime.utcnow(),
            baseline_bytes=10000,
            ccp_bytes=5000,
            delta_bytes=-5000,
            result_type=ResultType.SAVINGS,
            savings_bytes=5000,
            overage_bytes=0,
            savings_ratio=Decimal("0.5"),
            overage_ratio=Decimal("0"),
            pricing_model_version=1,
            calculated_credit=Decimal("5"),
            calculated_charge=Decimal("0"),
            currency="USD",
            status=BillingStatus.MEASURED,
        )

        self.ledger.append(record)
        records = self.ledger.read_all()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].measurement_id, record.measurement_id)
        self.assertEqual(records[0].customer_id, customer_id)
        self.assertEqual(records[0].result_type, ResultType.SAVINGS)

    def test_append_multiple_records(self):
        """Multiple records can be appended and all are preserved."""
        customer_id = uuid4()
        records_to_append = []

        for i in range(5):
            record = MeasurementRecord(
                measurement_id=uuid4(),
                customer_id=customer_id,
                artifact_id=uuid4(),
                artifact_digest=f"digest_{i}",
                application_version="1.0.0",
                ccp_version="1.0.0",
                timestamp=datetime.utcnow(),
                baseline_bytes=1000 * (i + 1),
                ccp_bytes=500 * (i + 1),
                delta_bytes=-500 * (i + 1),
                result_type=ResultType.SAVINGS,
                savings_bytes=500 * (i + 1),
                overage_bytes=0,
                savings_ratio=Decimal("0.5"),
                overage_ratio=Decimal("0"),
                pricing_model_version=1,
                calculated_credit=Decimal(i + 1),
                calculated_charge=Decimal("0"),
                currency="USD",
                status=BillingStatus.MEASURED,
            )
            records_to_append.append(record)
            self.ledger.append(record)

        all_records = self.ledger.read_all()
        self.assertEqual(len(all_records), 5)

    def test_read_for_customer(self):
        """Filtering by customer ID works correctly."""
        customer1 = uuid4()
        customer2 = uuid4()

        for i in range(3):
            record = MeasurementRecord(
                measurement_id=uuid4(),
                customer_id=customer1 if i % 2 == 0 else customer2,
                artifact_id=uuid4(),
                artifact_digest=f"digest_{i}",
                application_version="1.0.0",
                ccp_version="1.0.0",
                timestamp=datetime.utcnow(),
                baseline_bytes=1000,
                ccp_bytes=500,
                delta_bytes=-500,
                result_type=ResultType.SAVINGS,
                savings_bytes=500,
                overage_bytes=0,
                savings_ratio=Decimal("0.5"),
                overage_ratio=Decimal("0"),
                pricing_model_version=1,
                calculated_credit=Decimal("1"),
                calculated_charge=Decimal("0"),
                currency="USD",
                status=BillingStatus.MEASURED,
            )
            self.ledger.append(record)

        customer1_records = self.ledger.read_for_customer(customer1)
        customer2_records = self.ledger.read_for_customer(customer2)

        self.assertEqual(len(customer1_records), 2)
        self.assertEqual(len(customer2_records), 1)

    def test_total_credits_for_customer(self):
        """Summing customer credits works correctly."""
        customer_id = uuid4()

        for amount in [Decimal("10"), Decimal("20"), Decimal("30")]:
            record = MeasurementRecord(
                measurement_id=uuid4(),
                customer_id=customer_id,
                artifact_id=uuid4(),
                artifact_digest="digest",
                application_version="1.0.0",
                ccp_version="1.0.0",
                timestamp=datetime.utcnow(),
                baseline_bytes=1000,
                ccp_bytes=500,
                delta_bytes=-500,
                result_type=ResultType.SAVINGS,
                savings_bytes=500,
                overage_bytes=0,
                savings_ratio=Decimal("0.5"),
                overage_ratio=Decimal("0"),
                pricing_model_version=1,
                calculated_credit=amount,
                calculated_charge=Decimal("0"),
                currency="USD",
                status=BillingStatus.CREDITED,
            )
            self.ledger.append(record)

        total = self.ledger.total_credits_for_customer(customer_id)
        self.assertEqual(total, Decimal("60"))

    def test_balance_calculation(self):
        """Customer balance (credits - charges) is calculated correctly."""
        customer_id = uuid4()

        # Add credits
        for amount in [Decimal("100"), Decimal("50")]:
            record = MeasurementRecord(
                measurement_id=uuid4(),
                customer_id=customer_id,
                artifact_id=uuid4(),
                artifact_digest="digest",
                application_version="1.0.0",
                ccp_version="1.0.0",
                timestamp=datetime.utcnow(),
                baseline_bytes=10000,
                ccp_bytes=5000,
                delta_bytes=-5000,
                result_type=ResultType.SAVINGS,
                savings_bytes=5000,
                overage_bytes=0,
                savings_ratio=Decimal("0.5"),
                overage_ratio=Decimal("0"),
                pricing_model_version=1,
                calculated_credit=amount,
                calculated_charge=Decimal("0"),
                currency="USD",
                status=BillingStatus.CREDITED,
            )
            self.ledger.append(record)

        # Add charges
        for amount in [Decimal("30"), Decimal("20")]:
            record = MeasurementRecord(
                measurement_id=uuid4(),
                customer_id=customer_id,
                artifact_id=uuid4(),
                artifact_digest="digest",
                application_version="1.0.0",
                ccp_version="1.0.0",
                timestamp=datetime.utcnow(),
                baseline_bytes=1000,
                ccp_bytes=2000,
                delta_bytes=1000,
                result_type=ResultType.OVERAGE,
                savings_bytes=0,
                overage_bytes=1000,
                savings_ratio=Decimal("0"),
                overage_ratio=Decimal("1.0"),
                pricing_model_version=1,
                calculated_credit=Decimal("0"),
                calculated_charge=amount,
                currency="USD",
                status=BillingStatus.SETTLED,
            )
            self.ledger.append(record)

        balance = self.ledger.balance_for_customer(customer_id)
        # 150 credits - 50 charges = 100 balance
        self.assertEqual(balance, Decimal("100"))

    def test_ledger_persistence(self):
        """Records persist across ledger instances."""
        customer_id = uuid4()
        original_record = MeasurementRecord(
            measurement_id=uuid4(),
            customer_id=customer_id,
            artifact_id=uuid4(),
            artifact_digest="persistent_digest",
            application_version="1.0.0",
            ccp_version="1.0.0",
            timestamp=datetime.utcnow(),
            baseline_bytes=5000,
            ccp_bytes=2500,
            delta_bytes=-2500,
            result_type=ResultType.SAVINGS,
            savings_bytes=2500,
            overage_bytes=0,
            savings_ratio=Decimal("0.5"),
            overage_ratio=Decimal("0"),
            pricing_model_version=1,
            calculated_credit=Decimal("2.44"),
            calculated_charge=Decimal("0"),
            currency="USD",
            status=BillingStatus.MEASURED,
        )

        ledger1 = MeasurementLedger(str(self.ledger_path))
        ledger1.append(original_record)

        # Create new ledger instance pointing to same file
        ledger2 = MeasurementLedger(str(self.ledger_path))
        records = ledger2.read_all()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].artifact_digest, "persistent_digest")
        self.assertEqual(records[0].calculated_credit, Decimal("2.44"))


class TestSandboxPaymentProvider(unittest.TestCase):
    """Test sandbox payment provider."""

    def setUp(self):
        self.provider = SandboxPaymentProvider()
        self.customer_id = uuid4()

    def test_create_customer_success(self):
        """Creating a customer succeeds."""
        status = self.provider.create_customer(
            email="test@example.com",
            name="Test Customer",
            customer_id=self.customer_id,
        )

        self.assertTrue(status.success)
        self.assertIsNotNone(status.transaction_id)

    def test_authorize_charge_success(self):
        """Authorizing a charge succeeds."""
        self.provider.create_customer(
            email="test@example.com",
            name="Test Customer",
            customer_id=self.customer_id,
        )

        status = self.provider.authorize_charge(
            customer_id=self.customer_id,
            amount_minor_units=Decimal("1000"),
            currency="USD",
            description="Test charge",
            idempotency_key="test_key_1",
        )

        self.assertTrue(status.success)
        self.assertIsNotNone(status.transaction_id)

    def test_capture_charge_success(self):
        """Capturing a previously authorized charge succeeds."""
        self.provider.create_customer(
            email="test@example.com",
            name="Test Customer",
            customer_id=self.customer_id,
        )

        auth_status = self.provider.authorize_charge(
            customer_id=self.customer_id,
            amount_minor_units=Decimal("1000"),
            currency="USD",
            description="Test charge",
            idempotency_key="test_key_1",
        )

        capture_status = self.provider.capture_charge(
            transaction_id=auth_status.transaction_id,
            amount_minor_units=Decimal("1000"),
        )

        self.assertTrue(capture_status.success)

    def test_issue_credit_success(self):
        """Issuing a credit succeeds."""
        self.provider.create_customer(
            email="test@example.com",
            name="Test Customer",
            customer_id=self.customer_id,
        )

        status = self.provider.issue_credit(
            customer_id=self.customer_id,
            amount_minor_units=Decimal("500"),
            currency="USD",
            description="Test credit",
            idempotency_key="test_key_2",
        )

        self.assertTrue(status.success)

    def test_failure_mode(self):
        """Setting failure mode makes operations fail."""
        self.provider.set_failure_mode("INSUFFICIENT_FUNDS")

        status = self.provider.authorize_charge(
            customer_id=self.customer_id,
            amount_minor_units=Decimal("1000"),
            currency="USD",
            description="Test charge",
            idempotency_key="test_key_1",
        )

        self.assertFalse(status.success)
        self.assertEqual(status.error_code, "INSUFFICIENT_FUNDS")


class TestCommerceService(unittest.TestCase):
    """Test the CommerceService orchestration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.ledger_path = Path(self.temp_dir) / "ledger.jsonl"
        self.ledger = MeasurementLedger(str(self.ledger_path))
        self.payment_provider = SandboxPaymentProvider()
        self.service = CommerceService(self.ledger, self.payment_provider)

        # Register a contract
        self.contract = CommercialContractVersioned(
            contract_version=1,
            effective_from=datetime.utcnow(),
            content_hash="contract_hash_v1",
            baseline_definition="original file size",
            measurement_definition="artifact size",
            savings_formula="(baseline - ccp) / baseline",
            overage_formula="(ccp - baseline) / 1024",
            price_model="per_kilobyte",
            currency="USD",
            maximum_credit=Decimal(10000),
            maximum_charge=Decimal(50000),
            billing_period="per_artifact",
            dispute_policy_reference="policy_v1",
            terms_version=1,
        )
        self.service.register_contract(self.contract)
        self.customer_id = uuid4()
        self.payment_provider.create_customer(
            email="customer@example.com",
            name="Test Customer",
            customer_id=self.customer_id,
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_measure_artifact_savings(self):
        """Measuring an artifact with savings creates a record."""
        record = self.service.measure_artifact(
            customer_id=self.customer_id,
            artifact_id=uuid4(),
            artifact_digest="digest1",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=10000,
            ccp_bytes=5000,
        )

        self.assertEqual(record.result_type, ResultType.SAVINGS)
        self.assertEqual(record.status, BillingStatus.MEASURED)
        self.assertGreater(record.calculated_credit, 0)

    def test_measure_artifact_overage(self):
        """Measuring an artifact with overage creates a record."""
        record = self.service.measure_artifact(
            customer_id=self.customer_id,
            artifact_id=uuid4(),
            artifact_digest="digest2",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=5000,
            ccp_bytes=10000,
        )

        self.assertEqual(record.result_type, ResultType.OVERAGE)
        self.assertEqual(record.status, BillingStatus.MEASURED)
        self.assertGreater(record.calculated_charge, 0)

    def test_customer_balance(self):
        """Customer balance is calculated correctly."""
        # Record a savings measurement
        self.service.measure_artifact(
            customer_id=self.customer_id,
            artifact_id=uuid4(),
            artifact_digest="digest1",
            application_version="1.0.0",
            ccp_version="1.0.0",
            baseline_bytes=10000,
            ccp_bytes=5000,
        )

        # Manually set status to CREDITED to include in balance
        all_records = self.ledger.read_for_customer(self.customer_id)
        if all_records:
            record = all_records[0]
            credited = MeasurementRecord(
                measurement_id=record.measurement_id,
                customer_id=record.customer_id,
                artifact_id=record.artifact_id,
                artifact_digest=record.artifact_digest,
                application_version=record.application_version,
                ccp_version=record.ccp_version,
                timestamp=record.timestamp,
                baseline_bytes=record.baseline_bytes,
                ccp_bytes=record.ccp_bytes,
                delta_bytes=record.delta_bytes,
                result_type=record.result_type,
                savings_bytes=record.savings_bytes,
                overage_bytes=record.overage_bytes,
                savings_ratio=record.savings_ratio,
                overage_ratio=record.overage_ratio,
                pricing_model_version=record.pricing_model_version,
                calculated_credit=record.calculated_credit,
                calculated_charge=record.calculated_charge,
                currency=record.currency,
                status=BillingStatus.CREDITED,
            )
            self.ledger.append(credited)

        balance = self.service.customer_balance(self.customer_id)
        self.assertGreater(balance, 0)


if __name__ == "__main__":
    unittest.main()
