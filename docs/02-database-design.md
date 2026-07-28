# 02 — Database Design

**Status:** awaiting review · **Engine:** PostgreSQL 16

Companion to the SQL in `database/migrations/`. **No migration is ever applied
automatically** — every file is reviewed and executed by a developer. See
`database/README.md`.

---

## 1. Principles

| # | Principle | Consequence |
|---|---|---|
| 1 | **Every athlete-scoped row carries `user_id`** | It is the shard key. Even where it is derivable via a join, it is denormalised onto the row so RLS is a single-column predicate and a future shard split needs no schema change. |
| 2 | **UUIDv7 primary keys, application-generated** | Time-ordered, so B-tree inserts stay at the right edge of the index (unlike UUIDv4, which fragments); no sequence to coordinate across shards; ids are safe to expose. `gen_random_uuid()` is the DEFAULT only as a safety net. |
| 3 | **Money is `BIGINT` minor units + explicit currency** | Never `FLOAT`, never `NUMERIC` without a currency. `1550` + `ILS` is 15.50 ₪. |
| 4 | **Timestamps are `TIMESTAMPTZ`, always UTC** | Plus a separate `local_date` where the athlete's calendar day matters (a 23:30 run belongs to that day for the athlete even if it is tomorrow in UTC). |
| 5 | **Immutable tables for anything auditable** | Ledger entries, audit events, provider events, prediction records. `UPDATE`/`DELETE` are revoked from the application role. |
| 6 | **Derived metrics are materialised** | `daily_metrics` is written by the worker. Reads never recompute. |
| 7 | **Soft delete for user-owned aggregates, hard delete for GDPR erasure** | `deleted_at` for normal lifecycle; a separate, audited erasure path genuinely removes rows and redacts immutable ones. |
| 8 | **High-volume time-series tables are partition-ready** | Declarative range partitioning on the time column, monthly. Created partitioned from the start so growth is a `CREATE TABLE … PARTITION OF`, not a migration of billions of rows. |

---

## 2. Table inventory

Grouped by owning module (§3 of `01-architecture.md`). Schema per module, so
extraction later is a `pg_dump --schema` away.

### identity
| Table | Purpose | Notes |
|---|---|---|
| `organizations` | clubs, teams, coaching businesses | The second tenancy axis. B2C athletes have `org_id IS NULL`. |
| `users` | login identity, role, status | `email` is `CITEXT UNIQUE` — case-insensitive at the database, not at the application. |
| `org_memberships` | user ↔ organization with a role | Many-to-many so a coach can serve several clubs. |
| `refresh_tokens` | rotating refresh tokens | Stores a SHA-256 hash, never the token. `family_id` enables reuse detection. |
| `mfa_credentials` | TOTP secrets (encrypted) | Ships in Phase 2; the table exists now so enabling it is not a migration under pressure. |
| `consents` | versioned consent records | Required for GDPR lawful basis. Append-only: withdrawal is a new row, not an update. |
| `data_access_grants` | athlete → coach/partner access | Scoped, expiring, athlete-revocable. The only mechanism by which one person reads another's health data. |
| `audit_events` | append-only audit trail | Every cross-user health-data read, admin action, entitlement and wallet change. |

### training
| Table | Purpose | Notes |
|---|---|---|
| `athlete_profiles` | physiology and thresholds | 1:1 with `users`. Feeds `AthleteProfile` in the analytics engine verbatim. |
| `athlete_goals` | goals with target dates | Drives plan generation. |
| `personal_bests` | PBs per sport and distance | Feeds the personalised Riegel exponent. |
| `provider_connections` | OAuth link per provider | Tokens **encrypted at column level**; `key_id` recorded for rotation. |
| `provider_events` | raw inbound webhook payloads | **Immutable.** Written before parsing so a parser bug is replayable. Unique on `(provider, provider_event_id)` for idempotency. |
| `activities` | one session, normalised | **Partitioned monthly on `start_time`.** Unique on `(provider, provider_activity_id)`. |
| `activity_laps` | per-lap splits | Partitioned alongside activities. |
| `activity_streams` | pointer to the sample stream | Samples live in object storage; the row holds the key, sample count, and checksum. Keeping multi-megabyte streams out of Postgres is the difference between a 50 GB and a 5 TB database at 100k athletes. |
| `daily_wellness` | overnight + subjective inputs | One row per athlete-day. Feeds `DailyWellness`. |
| `daily_metrics` | materialised analytics output | One row per athlete-day: load, CTL/ATL/TSB, ACWR, monotony, readiness (+ drivers as JSONB), injury risk, data quality, engine version. **This is what the dashboard reads.** |
| `data_quality_flags` | detected anomalies per record | The Data Quality System: what was rejected or down-weighted and why. |

### coaching
| Table | Purpose | Notes |
|---|---|---|
| `athlete_twin_snapshots` | the Athlete Digital Twin over time | Versioned JSONB snapshot + typed columns for the fields we query. Keeping history is what lets us show "your aerobic capacity trend" and evaluate whether the twin improved. |
| `ai_conversations` | chat threads | |
| `ai_messages` | every turn, with full cost and provenance | Model, provider, prompt/completion/cached tokens, cost in micro-USD, context-packet hash, latency, finish reason, safety flags. Without this, AI cost control and the eval harness are both impossible. |
| `ai_message_feedback` | thumbs + comment | The Continuous Improvement System's raw signal. |
| `ai_usage_counters` | per-user per-period quota counters | Enforces tier limits without scanning `ai_messages`. |
| `training_plans` | generated plans | Records `generator_version` and the input params, so any plan is reproducible. |
| `plan_sessions` | scheduled sessions | Links to the activity that completed it, for compliance measurement. |
| `plan_adaptations` | every daily gating decision | Input readiness/risk, action taken, reason text. Explainability and evaluation both need this. |

### governance (algorithm science)
| Table | Purpose |
|---|---|
| `algorithm_versions` | name, semver, parameters JSONB, active window, notes — the version registry |
| `prediction_records` | every prediction we made, with its interval and horizon |
| `prediction_outcomes` | what actually happened, and the error — this is how "did the algorithm improve?" gets answered with data |
| `experiment_assignments` | user → variant, for A/B comparison of algorithm versions |
| `eval_runs` | AI/algorithm eval executions with scores, tied to a version |

### billing
| Table | Purpose | Notes |
|---|---|---|
| `subscription_plans` | catalogue (Free, Premium) | Price in minor units per currency. |
| `subscriptions` | current state per user | Mirrors the provider; reconciled nightly. Provider is source of truth. |
| `payments` | settled transactions | Unique on `(provider, provider_transaction_id)`. |
| `payment_webhook_events` | raw provider webhooks | Immutable, signature-verification result recorded. Unique on `(provider, provider_event_id)`. |
| `entitlements` | what this user may access right now | Tier features **and** individually unlocked sensors. Read path for every gated feature. |

### rewards
| Table | Purpose | Notes |
|---|---|---|
| `wallets` | one per user per currency | Holds no balance. |
| `wallet_transactions` | groups ledger entries | Every transaction must balance to zero. |
| `wallet_ledger_entries` | **immutable double-entry lines** | Balance is `SUM(credit) - SUM(debit)`. Records the `reward_policy_version` that applied. |
| `reward_policies` | versioned rules and split percentages | The spec requires the percentages to be changeable; versioning them means a change never rewrites history. |
| `reward_events` | earn events, before they become ledger entries | Deduplicated so one workout cannot be claimed twice. |
| `redemptions` | spending at a partner | |
| `payouts` | cash-out requests | Carries an `eligibility_snapshot` and an explicit human decision record. |
| `fraud_signals` | per-user risk signals | Includes workout-spoofing detections (§6 of the security doc). |

### partners
| Table | Purpose |
|---|---|
| `partners` | partner company, status, commission rate in basis points |
| `partner_offers` | offers with validity windows and inventory |
| `partner_conversions` | attributed conversions with gross and commission amounts |

### community
| Table | Purpose |
|---|---|
| `groups`, `group_members` | training groups with roles |
| `challenges`, `challenge_participants` | challenges and progress |
| `achievements` | earned achievements, linked to the activity that earned them |
| `activity_shares` | what an athlete chose to share, and with whom |

### notifications
| Table | Purpose |
|---|---|
| `device_tokens` | APNs/FCM tokens per device |
| `notification_preferences` | per-kind, per-channel opt-in + quiet hours |
| `notification_deliveries` | delivery log with provider reference |

### marketplace (Phase 5 — schema only, no code)
| Table | Purpose |
|---|---|
| `coach_profiles`, `plan_products`, `plan_purchases` | coach marketplace, defined now so the rewards/billing model does not need reshaping later |

---

## 3. Indexing strategy

The access patterns that actually matter, and the index that serves each:

| Query | Index |
|---|---|
| Athlete's activity feed, newest first | `activities (user_id, start_time DESC)` |
| Activity by provider id (ingest idempotency) | `activities (provider, provider_activity_id)` UNIQUE |
| Dashboard: today's metrics | `daily_metrics (user_id, day DESC)` — PK, covers it |
| Metrics range for a chart | same PK, range scan |
| Wellness for the 28-day baseline | `daily_wellness (user_id, day DESC)` — PK |
| Wallet balance | `wallet_ledger_entries (wallet_id, account)` — plus a periodic snapshot row to bound the scan |
| Unprocessed provider events | partial index `provider_events (received_at) WHERE processed_at IS NULL` |
| Conversation history | `ai_messages (conversation_id, created_at)` |
| AI monthly cost per user | `ai_messages (user_id, created_at)` |
| Active subscriptions expiring soon | partial index `subscriptions (current_period_end) WHERE status = 'active'` |
| Group leaderboard | `challenge_participants (challenge_id, progress DESC)` |

Deliberate omissions: no index is created for a query we do not yet make. Each
index costs write throughput on the hottest tables in the system.

**Partial indexes over full ones** wherever a status column makes most rows
irrelevant — the "unprocessed events" and "expiring subscriptions" cases above
are the two that would otherwise scan the largest tables.

---

## 4. Partitioning

Partitioning is **deferred to its own migration**, but the tables that will need
it are *shaped* for it today. That shaping is the part that is expensive to
retrofit:

* each carries a single monotonic time column suitable as the partition key
  (`activities.start_time`, `ai_messages.created_at`, `audit_events.occurred_at`,
  `notification_deliveries.created_at`, `wallet_ledger_entries.created_at`);
* none of them carries a business `UNIQUE` constraint that excludes that time
  column — a partitioned table's unique constraints must include the partition
  key, so a `UNIQUE (provider, provider_activity_id)` on `activities` would block
  partitioning entirely. Ingest idempotency is therefore enforced on
  `provider_activity_map`, a small lookup table that stays unpartitioned.

Trigger to execute the partitioning migration: any of these tables passing ~50M
rows, or index maintenance windows becoming disruptive. At that point a scheduled
job creates partitions three months ahead and archives those past the retention
window.

`activity_streams` never needs partitioning because it holds object-storage
pointers, not samples.

---

## 5. Row-level security

Two enforcement layers, because either one alone has a realistic failure mode:
application code forgets a `WHERE`, and database policies can be bypassed by a
mis-provisioned role.

```sql
-- Set per request, inside the transaction, from the verified JWT
SET LOCAL app.current_user_id = '018f…';
SET LOCAL app.current_org_id  = '018e…';   -- or NULL
SET LOCAL app.current_role    = 'athlete';
```

Every athlete-scoped table gets:

1. **Owner policy** — `user_id = current_setting('app.current_user_id', true)::uuid`
2. **Grant policy** — visible if an unexpired, unrevoked `data_access_grants` row
   covers this `(grantor, grantee, scope)`
3. **Org policy** — for org-scoped tables, `org_id = current_setting('app.current_org_id', true)::uuid`

`current_setting(…, true)` returns `NULL` when unset, and `user_id = NULL`
evaluates to `NULL` → the row is filtered. **Isolation fails closed:** a request
that forgets to set the context sees nothing rather than everything.

The application connects as `app_rw`, which has neither `SUPERUSER` nor
`BYPASSRLS`. Migrations run as a separate `app_migrator` role. Analytics reads use
`app_ro`. This separation is what makes the RLS layer meaningful.

---

## 6. The wallet ledger, concretely

```
wallet_transactions
  id, wallet_id, kind, reason_code, reward_policy_version,
  external_reference, created_at, created_by

wallet_ledger_entries                      -- IMMUTABLE
  id, transaction_id, wallet_id, account, direction, amount_minor,
  currency, created_at
```

Accounts: `earned`, `promotional`, `redeemable`, `withdrawable`, `spent`,
`expired`, `reversed`, `house`.

Earning 100 ₪ of reward under a policy that splits 50/50 between club credit and
withdrawable value is one transaction with balanced entries:

| account | direction | amount |
|---|---|---|
| `house` | debit | 10000 |
| `redeemable` | credit | 5000 |
| `withdrawable` | credit | 5000 |

Invariant, checked nightly and in tests: **for every transaction,
`SUM(credits) = SUM(debits)`**. A reversal is a new mirrored transaction, never
an update. This is why the split percentages can change safely — history records
which version applied.

---

## 7. Data retention

| Data | Retention | Rationale |
|---|---|---|
| Activities, wellness, metrics | Life of account + 30 days after deletion request | The product's value is longitudinal. |
| `activity_streams` (raw samples) | 24 months hot, then archived to cold storage | Rarely read after the first analysis; dominates storage. |
| `provider_events` | 90 days | Replay window for parser bugs. |
| `ai_messages` | 24 months | Needed for evals and for the athlete's own history. |
| `audit_events` | 7 years | Accountability. Survives account deletion in redacted form. |
| `wallet_ledger_entries` | 7 years | Financial record; legally cannot be deleted on request. |
| `payments` / `payouts` | 7 years | Tax and audit. |

**GDPR erasure** deletes or anonymises athlete data but retains financial and
audit records in redacted form (subject id replaced by a tombstone), which is the
standard lawful-basis carve-out. The erasure path is a reviewed SQL procedure with
an audit record, never an ad-hoc `DELETE`.

---

## 8. Open questions

1. Multi-currency wallets at launch, or ILS only? (Schema supports both; ILS-only
   removes an FX problem from Phase 4.)
2. Retention for `activity_streams` — 24 months is a guess; it should be set by
   what the analytics actually re-read.
3. Do we need `pgvector` for semantic search over past coach conversations? Not
   in MVP; it is an extension, so adding it later is cheap.
