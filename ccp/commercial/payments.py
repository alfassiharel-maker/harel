"""Payment provider abstraction and sandbox implementation.

The PaymentProvider interface is provider-independent. The sandbox implementation
is for testing and development only — it never attempts real money transactions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from .models import PaymentStatus


class PaymentProvider(ABC):
    """Abstract interface for payment processing."""

    @abstractmethod
    def create_customer(
        self, email: str, name: str, customer_id: UUID
    ) -> PaymentStatus:
        """Create a customer record with the payment processor."""

    @abstractmethod
    def create_payment_method_reference(
        self, customer_id: UUID, payment_method_token: str
    ) -> PaymentStatus:
        """Store a payment method reference for later use."""

    @abstractmethod
    def authorize_charge(
        self,
        customer_id: UUID,
        amount_minor_units: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> PaymentStatus:
        """Authorize a charge (hold funds, do not capture yet)."""

    @abstractmethod
    def capture_charge(self, transaction_id: str, amount_minor_units: Decimal) -> PaymentStatus:
        """Capture a previously authorized charge."""

    @abstractmethod
    def issue_credit(
        self,
        customer_id: UUID,
        amount_minor_units: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> PaymentStatus:
        """Issue a credit to a customer's account."""

    @abstractmethod
    def refund(self, transaction_id: str, amount_minor_units: Decimal) -> PaymentStatus:
        """Refund a previously captured charge (full or partial)."""

    @abstractmethod
    def get_transaction(self, transaction_id: str) -> PaymentStatus:
        """Retrieve the status of a transaction."""


class SandboxPaymentProvider(PaymentProvider):
    """Test payment provider that simulates transactions without touching real money.

    All operations succeed unless explicitly configured to fail. No real funds
    are moved. This is for testing and development only.
    """

    def __init__(self) -> None:
        self._customers: dict[UUID, dict] = {}
        self._transactions: dict[str, dict] = {}
        self._payment_methods: dict[UUID, list[str]] = {}
        self._failure_mode: Optional[str] = None

    def set_failure_mode(self, mode: Optional[str]) -> None:
        """Set next operation to fail with given error code (or None for success)."""
        self._failure_mode = mode

    def create_customer(
        self, email: str, name: str, customer_id: UUID
    ) -> PaymentStatus:
        """Record a customer locally (no real processor call)."""
        if self._failure_mode:
            return PaymentStatus(
                success=False,
                error_code=self._failure_mode,
                error_message=f"Sandbox failure: {self._failure_mode}",
            )

        self._customers[customer_id] = {"email": email, "name": name}
        return PaymentStatus(success=True, transaction_id=str(customer_id))

    def create_payment_method_reference(
        self, customer_id: UUID, payment_method_token: str
    ) -> PaymentStatus:
        """Record a payment method locally (no real processor call)."""
        if self._failure_mode:
            return PaymentStatus(
                success=False,
                error_code=self._failure_mode,
                error_message=f"Sandbox failure: {self._failure_mode}",
            )

        if customer_id not in self._customers:
            return PaymentStatus(
                success=False,
                error_code="CUSTOMER_NOT_FOUND",
                error_message=f"Customer {customer_id} not found",
            )

        if customer_id not in self._payment_methods:
            self._payment_methods[customer_id] = []
        self._payment_methods[customer_id].append(payment_method_token)

        return PaymentStatus(success=True, transaction_id=payment_method_token)

    def authorize_charge(
        self,
        customer_id: UUID,
        amount_minor_units: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> PaymentStatus:
        """Simulate authorizing a charge."""
        if self._failure_mode:
            return PaymentStatus(
                success=False,
                error_code=self._failure_mode,
                error_message=f"Sandbox failure: {self._failure_mode}",
            )

        if customer_id not in self._customers:
            return PaymentStatus(
                success=False,
                error_code="CUSTOMER_NOT_FOUND",
                error_message=f"Customer {customer_id} not found",
            )

        transaction_id = f"auth_{uuid4()}"
        self._transactions[transaction_id] = {
            "customer_id": customer_id,
            "amount": amount_minor_units,
            "currency": currency,
            "description": description,
            "status": "authorized",
            "idempotency_key": idempotency_key,
        }

        return PaymentStatus(success=True, transaction_id=transaction_id)

    def capture_charge(self, transaction_id: str, amount_minor_units: Decimal) -> PaymentStatus:
        """Simulate capturing a previously authorized charge."""
        if self._failure_mode:
            return PaymentStatus(
                success=False,
                error_code=self._failure_mode,
                error_message=f"Sandbox failure: {self._failure_mode}",
            )

        if transaction_id not in self._transactions:
            return PaymentStatus(
                success=False,
                error_code="TRANSACTION_NOT_FOUND",
                error_message=f"Transaction {transaction_id} not found",
            )

        self._transactions[transaction_id]["status"] = "captured"
        self._transactions[transaction_id]["captured_amount"] = amount_minor_units

        return PaymentStatus(success=True, transaction_id=transaction_id)

    def issue_credit(
        self,
        customer_id: UUID,
        amount_minor_units: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> PaymentStatus:
        """Simulate issuing a credit."""
        if self._failure_mode:
            return PaymentStatus(
                success=False,
                error_code=self._failure_mode,
                error_message=f"Sandbox failure: {self._failure_mode}",
            )

        if customer_id not in self._customers:
            return PaymentStatus(
                success=False,
                error_code="CUSTOMER_NOT_FOUND",
                error_message=f"Customer {customer_id} not found",
            )

        transaction_id = f"credit_{uuid4()}"
        self._transactions[transaction_id] = {
            "customer_id": customer_id,
            "amount": amount_minor_units,
            "currency": currency,
            "description": description,
            "status": "credited",
            "idempotency_key": idempotency_key,
        }

        return PaymentStatus(success=True, transaction_id=transaction_id)

    def refund(self, transaction_id: str, amount_minor_units: Decimal) -> PaymentStatus:
        """Simulate refunding a charge."""
        if self._failure_mode:
            return PaymentStatus(
                success=False,
                error_code=self._failure_mode,
                error_message=f"Sandbox failure: {self._failure_mode}",
            )

        if transaction_id not in self._transactions:
            return PaymentStatus(
                success=False,
                error_code="TRANSACTION_NOT_FOUND",
                error_message=f"Transaction {transaction_id} not found",
            )

        refund_id = f"refund_{uuid4()}"
        self._transactions[refund_id] = {
            "original_transaction": transaction_id,
            "amount": amount_minor_units,
            "status": "refunded",
        }

        return PaymentStatus(success=True, transaction_id=refund_id)

    def get_transaction(self, transaction_id: str) -> PaymentStatus:
        """Retrieve the status of a transaction."""
        if transaction_id not in self._transactions:
            return PaymentStatus(
                success=False,
                error_code="TRANSACTION_NOT_FOUND",
                error_message=f"Transaction {transaction_id} not found",
            )

        txn = self._transactions[transaction_id]
        return PaymentStatus(
            success=True,
            transaction_id=transaction_id,
            metadata=txn,
        )
