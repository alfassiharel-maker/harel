# Database migrations

## Execution policy — read this first

**Nothing in this directory is ever applied automatically.** Not by the
application, not by the test suite, not by CI, not by a deploy pipeline. Every
file here is a change proposal to be **reviewed by a developer and executed
manually**. This applies to structural changes and to data changes alike.

The files in this directory have **not been executed against any database**. They
are written for review. Expect to run them first against a scratch database and
to correct anything the review finds.

## How to review and apply

```bash
# 1. Read the file. Every migration is written to be readable top to bottom.

# 2. Dry-run inside a transaction against a scratch database and roll back.
psql "$SCRATCH_DATABASE_URL" --single-transaction --set ON_ERROR_STOP=on \
     -c 'BEGIN;' -f database/migrations/0002_identity_and_consent.sql -c 'ROLLBACK;'

# 3. Apply to the scratch database for real, then run the test suite against it.

# 4. Apply to staging. Re-run the suite. Exercise the affected endpoints.

# 5. Apply to production, in a maintenance window if the file says so.
psql "$DATABASE_URL" --single-transaction --set ON_ERROR_STOP=on \
     -f database/migrations/0002_identity_and_consent.sql
```

`--single-transaction` plus `ON_ERROR_STOP=on` means a failure leaves the
database untouched rather than half-migrated. Where a statement cannot run inside
a transaction (`CREATE INDEX CONCURRENTLY`, for example) the file says so in its
header and is split accordingly.

## Order

| File | Contents | Reversible |
|---|---|---|
| `0001_extensions_and_roles.sql` | extensions, database roles, shared helpers | yes |
| `0002_identity_and_consent.sql` | organizations, users, tokens, consents, grants, audit | yes |
| `0003_training_data.sql` | profiles, goals, PBs, provider links, activities, wellness | yes |
| `0004_analytics_and_governance.sql` | `daily_metrics`, data-quality flags, algorithm versioning, predictions, experiments | yes |
| `0005_coaching_ai.sql` | digital twin, conversations, messages, usage counters, plans, adaptations | yes |
| `0006_billing_entitlements.sql` | plans, subscriptions, payments, provider webhooks, entitlements | yes |
| `0007_rewards_wallet_partners.sql` | wallets, double-entry ledger, reward policies, redemptions, payouts, partners | yes |
| `0008_community_notifications.sql` | groups, challenges, achievements, devices, notification log | yes |
| `0009_row_level_security.sql` | RLS enablement and policies on every athlete-scoped table | yes |

Apply in numeric order. `0009` depends on every table existing.

## Conventions used throughout

* **Identifiers:** `snake_case`, plural table names, singular column names.
* **Primary keys:** `id UUID`, supplied by the application as UUIDv7.
  `DEFAULT gen_random_uuid()` is a safety net, not the intended path.
* **Enumerations:** `TEXT` + `CHECK` constraint, not native `ENUM`. Adding a value
  is then an ordinary constraint change instead of `ALTER TYPE`, and removing one
  is possible at all.
* **Money:** `BIGINT` minor units plus a `currency CHAR(3)` column. Never float.
* **Time:** `TIMESTAMPTZ` for instants; `DATE` for an athlete's local calendar day.
* **Immutable tables** are marked in a comment and have `UPDATE`/`DELETE` revoked
  from `app_rw` in `0001`.
* Every table and non-obvious column carries a `COMMENT`, so the schema is
  self-documenting in `psql` and in generated docs.

## Rollback

Each file ends with a commented-out `-- ROLLBACK:` block containing the inverse
statements. It is commented out deliberately: a rollback of a migration that has
been live is a data-loss decision that must be made consciously, not by running a
script.
