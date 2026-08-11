# Commercial Model: Billing and Payment

CCP Forge's commercial layer provides consumer-focused billing for artifact measurement, storage savings, and any overage. This layer sits above the Product Engine and runs entirely on the client, with optional server-side settlement and billing.

## Overview

The commercial layer consists of:

- **Models** (`ccp.commercial.models`): Customer, Agreement, Contract, MeasurementRecord, PaymentStatus
- **Accounting** (`ccp.commercial.accounting`): CommerceCalculation with deterministic savings/overage formulas
- **Ledger** (`ccp.commercial.ledger`): Append-only JSONL-based measurement and transaction ledger
- **Contracts** (`ccp.commercial.contracts`): Versioned contract terms and terms acceptance tracking
- **Payments** (`ccp.commercial.payments`): PaymentProvider interface and sandbox implementation
- **Service** (`ccp.commercial.service`): CommerceService orchestrating all commercial operations
- **UI Integration** (`ccp.ui.commerce_controller`): Application-level commercial state and measurement

## Measurement and Billing

### Result Types

Every artifact measurement produces one of three results:

1. **SAVINGS**: CCP container is smaller than original files
   - Credit issued to customer account
   - Formula: `(baseline_bytes - ccp_bytes) / baseline_bytes * unit_price`

2. **NO_CHANGE**: CCP container is approximately the same size
   - No financial transaction
   - Formula: neither credit nor charge applies

3. **OVERAGE**: CCP container is larger than original files
   - Charge applied to customer account
   - Formula: `(ccp_bytes - baseline_bytes) / 1024 * unit_price_per_kb`

### Measurement Record

Each measurement creates a `MeasurementRecord` with:

```
measurement_id: UUID (unique identifier)
customer_id: UUID (customer account)
artifact_id: UUID (content hash)
artifact_digest: str (for reproducibility)
application_version: str (CCP Forge version)
ccp_version: str (CCP Core version)
timestamp: datetime (when measured)

baseline_bytes: int (sum of original file sizes)
ccp_bytes: int (CCP container size)
delta_bytes: int (ccp_bytes - baseline_bytes)
result_type: ResultType (SAVINGS | NO_CHANGE | OVERAGE)
savings_bytes: int (only if SAVINGS)
overage_bytes: int (only if OVERAGE)
savings_ratio: Decimal (percentage)
overage_ratio: Decimal (percentage)

pricing_model_version: int (contract version used)
calculated_credit: Decimal (in minor units, e.g., cents)
calculated_charge: Decimal (in minor units, e.g., cents)
currency: str (ISO 4217, e.g., "USD")
status: BillingStatus (MEASURED | PENDING | AUTHORIZED | SETTLED | CREDITED | FAILED | DISPUTED | CANCELLED)
payment_transaction_id: Optional[str] (if settled)
agreement_id: Optional[UUID] (customer agreement)
```

## Deterministic Calculations

All financial calculations are **deterministic**: the same artifact measured against the same contract always produces the identical credit or charge, down to the last cent.

This is achieved through:

1. **Decimal Arithmetic**: All calculations use Python's `Decimal` type, never binary floating-point. This ensures exact arithmetic without rounding errors.

2. **No Randomness**: No random values in financial fields. The ledger contains only measurement data and deterministic derivations.

3. **Versioned Contracts**: Each measurement records which contract version was used. A contract's content is immutable once published.

4. **Reproducible Digest**: Measurements include `artifact_digest` (the content hash), so the same artifact always has the same measurement.

Example:
```python
contract = CommercialContractVersioned(contract_version=1, ...)
calc = CommerceCalculation(contract.to_commercial_contract())

# Same inputs always produce identical output
record1 = calc.calculate(customer_id, ..., baseline_bytes=10000, ccp_bytes=5000)
record2 = calc.calculate(customer_id, ..., baseline_bytes=10000, ccp_bytes=5000)

assert record1.calculated_credit == record2.calculated_credit  # Always true
```

## Append-Only Ledger

The measurement ledger is **append-only**: records are written once and never modified. This is enforced at the interface level.

```
MeasurementLedger(ledger_path).append(record)  # Writes to JSONL
```

The ledger file is newline-delimited JSON, one `MeasurementRecord` per line. To correct an error (e.g., an overpayment), a **compensating entry** is appended with opposite sign, never modifying the original.

Queries:
- `read_all()`: all measurements
- `read_for_customer(customer_id)`: customer's measurements
- `read_for_customer_and_status(customer_id, status)`: filtered by billing status
- `total_credits_for_customer(customer_id)`: sum of issued credits
- `total_charges_for_customer(customer_id)`: sum of applied charges
- `balance_for_customer(customer_id)`: credits minus charges

## Billing States

A measurement flows through a lifecycle:

1. **MEASURED**: initial state, awaiting authorization
2. **PENDING**: authorization request in progress
3. **AUTHORIZED**: payment processor approved the amount
4. **SETTLED**: charge captured or credit issued (terminal for successful payment)
5. **CREDITED**: credit applied to account (terminal)
6. **FAILED**: payment attempt failed (terminal)
7. **DISPUTED**: customer disputed the charge (terminal)
8. **CANCELLED**: transaction cancelled (terminal)

## Contract Versioning

Contracts are versioned and immutable. A new contract becomes effective on a specified date.

```python
CommercialContractVersioned(
    contract_version=1,
    effective_from=datetime(2026, 1, 1),
    baseline_definition="Original artifact file sizes",
    measurement_definition="CCP container size",
    savings_formula="(baseline - ccp) / baseline * $0.01_per_unit",
    overage_formula="(ccp - baseline) / 1024 * $0.02_per_kb",
    price_model="per_kilobyte",
    currency="USD",
    maximum_credit=Decimal(10000),  # $100
    maximum_charge=Decimal(50000),  # $500
    billing_period="per_artifact",
    dispute_policy_reference="policy_v1_disputes",
    terms_version=1,
)
```

Fields:
- `contract_version`: unique version identifier
- `effective_from`: when terms take effect
- `baseline_definition`: what counts as "original size"
- `measurement_definition`: how artifact size is computed
- `savings_formula`: how credits are calculated
- `overage_formula`: how charges are calculated
- `price_model`: e.g., "per_kilobyte"
- `currency`: ISO 4217 code
- `maximum_credit`: largest credit per artifact
- `maximum_charge`: largest charge per artifact
- `billing_period`: e.g., "per_artifact" or "monthly"
- `dispute_policy_reference`: link to dispute resolution policy
- `terms_version`: version of customer-facing terms

## Terms Acceptance

Before measurement can begin, a customer must accept commercial terms.

```python
commerce_controller.accept_terms(terms_version=1)
```

This records:
- `customer_id`: who accepted
- `terms_version`: which terms version
- `accepted_at`: when (ISO 8601)
- `ip_address`: for compliance audit trail
- `user_agent`: for compliance audit trail

Terms acceptance is versioned. If a new contract requires new terms, the customer must accept again before artifacts are measured under the new contract.

## Payment Provider Interface

The `PaymentProvider` abstract base class defines the payment processing interface:

```python
class PaymentProvider(ABC):
    def create_customer(self, email: str, name: str, customer_id: UUID) -> PaymentStatus
    def create_payment_method_reference(self, customer_id: UUID, token: str) -> PaymentStatus
    def authorize_charge(self, customer_id, amount, currency, description, idempotency_key) -> PaymentStatus
    def capture_charge(self, transaction_id: str, amount: Decimal) -> PaymentStatus
    def issue_credit(self, customer_id, amount, currency, description, idempotency_key) -> PaymentStatus
    def refund(self, transaction_id: str, amount: Decimal) -> PaymentStatus
    def get_transaction(self, transaction_id: str) -> PaymentStatus
```

This interface is **provider-independent**: implementations can use Stripe, Square, PayPal, or any other processor without changing the core accounting.

### Sandbox Implementation

`SandboxPaymentProvider` is a test-only implementation that simulates all operations without touching real money. Used for:
- Local development
- Testing and QA
- Demonstration
- Integration validation

```python
provider = SandboxPaymentProvider()
provider.create_customer(email="test@example.com", name="Test", customer_id=uuid4())

# Simulate failure
provider.set_failure_mode("INSUFFICIENT_FUNDS")
status = provider.authorize_charge(...)
assert not status.success
```

No real processor credentials or live payment flows are involved.

## CommerceService Orchestration

`CommerceService` combines accounting, ledger, and payment integration:

```python
ledger = MeasurementLedger(ledger_path)
payment = SandboxPaymentProvider()
commerce = CommerceService(ledger, payment)

# Register contract
commerce.register_contract(contract_v1)

# Measure and record
record = commerce.measure_artifact(
    customer_id=customer_id,
    artifact_id=artifact_id,
    artifact_digest=digest,
    application_version="1.0.0",
    ccp_version="1.0.0",
    baseline_bytes=10000,
    ccp_bytes=5000,
)
# record.status = MEASURED
# record.calculated_credit > 0

# Authorize payment
auth_record = commerce.authorize_measurement(measurement_id, customer_id, agreement_id, payment_method)
# auth_record.status = AUTHORIZED

# Settle (capture or credit)
settled = commerce.settle_measurement(measurement_id, customer_id)
# settled.status = SETTLED
```

## UI Integration

The application controller includes a `CommerceController` that provides customer account management and measurement for the UI.

```python
commerce = CommerceController(app_controller, ledger_path=path)

# Set customer
state = commerce.set_customer(customer_id, name, email)

# Accept terms
commerce.accept_terms(terms_version=1)

# Measure an artifact
measurement = commerce.measure_artifact(
    artifact_digest=digest,
    baseline_bytes=10000,
    ccp_bytes=5000,
)

# Query state
state = commerce.get_commercial_state()
measurements = commerce.get_customer_measurements()
```

HTTP endpoints:
- `GET /api/commerce/state`: customer's commercial state
- `POST /api/commerce/accept-terms`: record terms acceptance
- `GET /api/commerce/measurements`: measurement history
- `GET /api/commerce/dashboard`: summary for dashboard view
- `GET /api/support`: support information

## Security Architecture

### Client-Side Limitations

The client (browser + controller) can:
- Display measurements
- Show account balance
- Record customer intention to accept terms
- Calculate financial values for display

The client **cannot**:
- Authoritatively modify the ledger
- Change a customer's balance
- Reverse or dispute transactions
- Issue refunds
- Access payment processor credentials

### Server-Side Authority

A server-side billing system would:
- Receive measurement requests from the client
- **Recalculate** the exact measurement (never trust client calculation)
- Persist the authoritative ledger
- Manage payment processor integration
- Issue refunds and reversals
- Validate terms acceptance

The client is **untrusted for financial values**. A customer could edit their browser's memory and claim `account_balance = $1,000,000`, but without server-side authority, that has no effect on real charges.

## Limitations in This Version

1. **Ledger is local**: measurements are stored in a file on the user's machine. A production system would persist to a backend database.

2. **Payment is sandbox-only**: the `SandboxPaymentProvider` never charges real credit cards. A production system would integrate with a real payment processor.

3. **No account sync**: a customer who runs CCP Forge on a second machine has a separate ledger. A production system would sync across devices.

4. **Single artifact at a time**: the application measures one artifact per build. Production workflows might batch multiple artifacts.

5. **No dispute resolution UI**: the ledger records disputed status, but there is no user interface to initiate disputes.

## Example: Measuring and Billing

```python
from ccp.ui.commerce_controller import CommerceController
from ccp.ui.controller import AppController

# Setup
app = AppController()
commerce = CommerceController(app, ledger_path="/tmp/ledger.jsonl")

# Customer setup
commerce.set_customer(
    customer_id=uuid4(),
    customer_name="Alice",
    email="alice@example.com"
)

# Terms acceptance
commerce.accept_terms(terms_version=1)

# Build an artifact and measure it
# (The app builds, the commercial layer records)
measurement = commerce.measure_artifact(
    artifact_digest="sha256_of_artifact",
    baseline_bytes=1_000_000,  # 1 MB of original files
    ccp_bytes=600_000,          # 600 KB in CCP container
)

# Result: SAVINGS with $0.39 credit
print(f"Result: {measurement.result_type}")
print(f"Credit: ${float(measurement.calculated_credit) / 100:.2f}")

# Check balance
state = commerce.get_commercial_state()
print(f"Account balance: ${float(state.account_balance) / 100:.2f}")
```

## Design Principles

1. **Determinism First**: Identical inputs produce identical outputs, always. This is load-bearing for customer trust.

2. **Ledger as Truth**: The append-only ledger is the source of truth. Nothing rewrites history.

3. **Client Untrusted for Money**: The client can calculate and display, but cannot authoritatively affect financial state.

4. **Contract Versioning**: Terms are immutable once published. Changes require new versions.

5. **Terms Explicit**: No implicit agreement. Customers must actively accept terms before measurement begins.

6. **No Hidden Overage**: Overage (larger CCP container) is visible and quantified, never hidden.

7. **Sandbox for Safety**: The payment provider is sandbox-only by default, preventing accidental real charges.
