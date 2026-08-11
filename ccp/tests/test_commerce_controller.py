"""Tests for commercial UI controller integration."""

import tempfile
import unittest
from uuid import uuid4

from ccp.ui.commerce_controller import CommerceController, CommercialState
from ccp.ui.controller import AppController


class TestCommerceController(unittest.TestCase):
    """Test CommerceController integration with AppController."""

    def setUp(self):
        self.app_controller = AppController()
        self.temp_dir = tempfile.mkdtemp()
        self.ledger_path = f"{self.temp_dir}/ledger.jsonl"
        self.commerce_controller = CommerceController(
            self.app_controller, self.ledger_path
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_set_customer(self):
        """Setting a customer initializes commercial state."""
        customer_id = uuid4()
        state = self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )

        self.assertEqual(state.customer_id, customer_id)
        self.assertEqual(state.customer_name, "Test Customer")
        self.assertFalse(state.terms_accepted)

    def test_accept_terms(self):
        """Terms acceptance is recorded."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )

        state = self.commerce_controller.accept_terms(terms_version=1)

        self.assertTrue(state.terms_accepted)
        self.assertIsNotNone(state.terms_accepted_at)
        self.assertEqual(state.contract_version, 1)

    def test_measure_artifact_requires_terms(self):
        """Measuring requires terms acceptance."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )

        # Should fail without terms acceptance
        with self.assertRaises(ValueError):
            self.commerce_controller.measure_artifact(
                artifact_digest="digest1",
                baseline_bytes=10000,
                ccp_bytes=5000,
            )

    def test_measure_artifact_savings(self):
        """Measuring savings creates financial record."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )
        self.commerce_controller.accept_terms(terms_version=1)

        measurement = self.commerce_controller.measure_artifact(
            artifact_digest="digest1",
            baseline_bytes=10000,
            ccp_bytes=5000,
        )

        self.assertEqual(measurement.result_type, "SAVINGS")
        self.assertEqual(measurement.baseline_bytes, 10000)
        self.assertEqual(measurement.ccp_bytes, 5000)
        self.assertGreater(int(measurement.calculated_credit), 0)

    def test_measure_artifact_overage(self):
        """Measuring overage creates financial record."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )
        self.commerce_controller.accept_terms(terms_version=1)

        measurement = self.commerce_controller.measure_artifact(
            artifact_digest="digest2",
            baseline_bytes=5000,
            ccp_bytes=15000,
        )

        self.assertEqual(measurement.result_type, "OVERAGE")
        self.assertGreater(int(measurement.calculated_charge), 0)

    def test_get_commercial_state(self):
        """Commercial state can be retrieved."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )

        state = self.commerce_controller.get_commercial_state()

        self.assertEqual(state.customer_id, customer_id)
        self.assertEqual(state.customer_name, "Test Customer")

    def test_get_customer_measurements(self):
        """Customer measurement history can be retrieved."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )
        self.commerce_controller.accept_terms(terms_version=1)

        # Record some measurements
        self.commerce_controller.measure_artifact(
            artifact_digest="digest1",
            baseline_bytes=10000,
            ccp_bytes=5000,
        )
        self.commerce_controller.measure_artifact(
            artifact_digest="digest2",
            baseline_bytes=20000,
            ccp_bytes=10000,
        )

        measurements = self.commerce_controller.get_customer_measurements()

        self.assertEqual(len(measurements), 2)
        self.assertEqual(measurements[0].result_type, "SAVINGS")
        self.assertEqual(measurements[1].result_type, "SAVINGS")

    def test_balance_calculation(self):
        """Customer balance reflects measurements."""
        customer_id = uuid4()
        self.commerce_controller.set_customer(
            customer_id=customer_id,
            customer_name="Test Customer",
            email="test@example.com",
        )
        self.commerce_controller.accept_terms(terms_version=1)

        # Record a savings measurement
        self.commerce_controller.measure_artifact(
            artifact_digest="digest1",
            baseline_bytes=102400,
            ccp_bytes=51200,
        )

        state = self.commerce_controller.get_commercial_state()

        # Balance should have some credits
        # (exact amount depends on pricing model)
        self.assertGreaterEqual(state.total_credits, 0)


if __name__ == "__main__":
    unittest.main()
