# 06 — Security Review and Privacy Design

**Status:** awaiting review · **Scope:** OWASP Top 10 (2021), multi-tenant
isolation, health-data privacy, payment and payout integrity

Health and training data is **sensitive personal data** under GDPR Article 9 and
"מידע רגיש" under the Israeli Privacy Protection Law. The controls below are
treated as requirements, not aspirations.

---

## 1. Threat model

| # | Threat | Impact | Primary control |
|---|---|---|---|
| T1 | One athlete reads another's health data | Sensitive-data breach; regulatory exposure | Dual-layer isolation: repository scoping **and** Postgres RLS (§3) |
| T2 | A coach or partner retains access after the relationship ends | Same, quietly and indefinitely | Mandatory-expiry, athlete-revocable grants (§4) |
| T3 | Stolen refresh token gives lasting access | Account takeover | Rotation with reuse detection + family revocation (§2) |
| T4 | Forged store receipt or unsigned webhook grants Premium | Revenue loss | Server-side receipt validation; unverified webhooks stored but never acted on (§6) |
| T5 | Fabricated workouts farm rewards, then cash out | Direct financial loss | Trust scoring on ingest + reward hold + human payout approval (§7) |
| T6 | Prompt injection through activity names or notes | Data exfiltration via the coach | Untrusted-content delimiting; tools scoped to the caller server-side (§8) |
| T7 | Provider OAuth tokens leak from the database | Access to the athlete's whole Garmin account | Column-level envelope encryption, KMS-held keys (§5) |
| T8 | Insider or support access to health records | Privacy violation | No blanket admin RLS exemption; separate audited role; every read logged (§3, §9) |
| T9 | Enumeration of accounts via auth endpoints | Targeted attack, privacy leak | Uniform responses and timing on auth flows (§2) |
| T10 | Duplicate payout via retry | Unrecoverable financial loss | Provider idempotency key + single attempt + manual reconciliation state (§7) |

---

## 2. Authentication

**Passwords.** Argon2id (memory-hard; bcrypt acceptable if a platform constraint
forces it), minimum 10 characters, checked against a breached-password list.
No composition rules and no forced rotation — both are known to reduce real
strength.

**Tokens.**

| | Access | Refresh |
|---|---|---|
| Type | JWT, ES256 (asymmetric, so verifiers need no signing key) | 32 random bytes, opaque |
| Lifetime | 10 minutes | 30 days, rotating on every use |
| Storage (mobile) | memory only | Keychain / Keystore |
| Storage (web) | memory only | `HttpOnly; Secure; SameSite=Strict` cookie |
| Revocation | short expiry | immediate, per token or per family |

**Reuse detection.** Refresh tokens rotate; each rotation records
`rotated_at`. Presenting an already-rotated token means the token was captured, so
the entire `family_id` is revoked and the athlete is notified. Without this, a
stolen refresh token is valid for a month.

**Enumeration resistance.** `/auth/register`, `/auth/login` and
`/auth/password/forgot` return uniform responses and are padded to a constant
minimum duration. A password hash is computed even for a non-existent account, so
response time does not reveal existence.

**Lockout.** Progressive delay then temporary lock after repeated failures,
counted **per account and per IP**. Counters live on the user row, not only in
Redis — a Redis flush must not reset a lockout.

**MFA.** TOTP, Phase 2 for athletes, **mandatory for `admin` and `support`** from
the day those roles exist.

---

## 3. Authorisation and multi-tenant isolation

Isolation is enforced twice, on the assumption that either layer will eventually
fail.

**Layer 1 — application.** Every repository method takes the tenant context
explicitly; there is no repository method that can query an athlete-scoped table
without a `user_id`. Enforced by review and by an import-linter contract that
forbids the API layer from touching repositories or models directly.

**Layer 2 — database.** Postgres RLS on every athlete-scoped table
(`database/migrations/0009_row_level_security.sql`), keyed on transaction-local
settings established from the verified JWT:

```sql
SET LOCAL app.current_user_id = '…';
```

`current_setting(…, true)` returns `NULL` when unset, and `user_id = NULL` filters
every row. **Isolation fails closed:** a code path that forgets to set context
returns nothing rather than everything.

Three properties make this real, and all three are verified after every
migration by the queries at the end of `0009`:

1. `app_rw` has neither `SUPERUSER` nor `BYPASSRLS`.
2. `app_rw` **owns no table** — table owners bypass RLS by default.
3. Every athlete-scoped table has RLS enabled and at least one policy.

**No admin exemption in the policies.** Support and admin access uses a distinct
connection role with a recorded purpose, so every access to another person's
health record produces an `audit_events` row. A blanket admin policy would make
insider access invisible, which is precisely the access most worth logging.

**RBAC** roles: `athlete`, `coach`, `partner`, `admin`, `support`. The role grants
*route* access; it never grants access to a specific athlete's data. That comes
only from §4.

---

## 4. Health-data permissions (athlete-controlled)

```
data_access_grants(grantor, grantee, scopes[], expires_at, revoked_at)
scopes ⊆ {activities, metrics, wellness, plans, goals, conversations}
```

* **The athlete is always the grantor.** No admin, coach or partner can create a
  grant over someone else's data.
* **Expiry is mandatory** — the schema has no way to express an open-ended grant.
  A coach relationship that quietly ended two years ago must not still be reading
  HRV.
* **Scopes are least-privilege.** A coach with `activities` cannot read
  `conversations`; a running-form analysis grant does not include sleep.
* **Revocation is immediate** and takes effect at the database layer, not just in
  the UI.
* **Group membership grants nothing.** Joining a training group exposes only what
  the athlete explicitly shares through `activity_shares`, whose constraints
  forbid heart-rate or readiness data in a public share.
* Every cross-athlete read writes an `audit_events` row naming actor, subject,
  scope and request id.

---

## 5. Data protection

| Data | At rest | Notes |
|---|---|---|
| Provider OAuth tokens | AES-256-GCM, envelope-encrypted, KMS-held key; `kms_key_id` per row | These are the highest-value secrets in the database — they open the athlete's entire Garmin account. Per-row key id makes rotation possible without downtime. |
| Payout destination refs | Same | |
| MFA secrets | Same | |
| Passwords | Argon2id hash | Not encryption; not reversible. |
| Refresh tokens | SHA-256 hash | A database dump must not yield live sessions. |
| Health metrics | Full-disk + managed-Postgres encryption | Column encryption here would defeat the indexing and range queries the whole product depends on; the compensating controls are RLS, audit and least-privilege roles. This trade-off is explicit rather than accidental. |
| Backups | Encrypted, separate key, restore rehearsed quarterly | |
| In transit | TLS 1.3, HSTS, certificate pinning on mobile | |

**Secrets** live in the platform secrets manager; nothing sensitive in the repo,
the image, or an environment file that is committed. `.env.example` contains
placeholders only. A pre-commit secret scanner runs in CI.

**Logs** carry `user_id` but never email, name, or health values. Scrubbing
happens in the logging pipeline, not at call sites — a call site can be
forgotten.

**AI provider boundary.** The context packet sent to the model contains derived
metrics and an **age band rather than a date of birth**, no name, no email, no GPS
coordinates, and no raw streams. Data residency and retention for the AI provider
are recorded in the DPA and surfaced in the privacy policy.

---

## 6. Payment integrity

* **No card data, ever.** Every flow is provider-hosted. There is deliberately no
  column in the schema capable of holding a PAN, CVV or bank password, which
  keeps us out of PCI-DSS scope by construction rather than by policy.
* **Server-side receipt validation.** Apple and Google receipts are validated
  against the store from the server. A client claim of Premium is never trusted.
* **Webhook signature verification** on Apple, Google, PayPal and later Stripe.
  Unverified events are persisted with `signature_verified = false` and **must
  not** change entitlement state — accepting an unsigned "subscription active"
  callback is a free-subscription vulnerability.
* **Replay protection** via `UNIQUE (provider, provider_event_id)`.
* **Reconciliation.** A nightly job re-verifies every active subscription against
  its provider. Refunds, cancellations and expiries that arrive as a webhook we
  failed to process are caught here. A stale `last_verified_at` alerts rather than
  continuing to grant access.
* **Entitlements expire.** Every subscription-sourced entitlement carries
  `expires_at` = period end + grace, so a failed reconciliation degrades to loss
  of access rather than to permanent free access.

---

## 7. Reward, wallet and payout integrity

This is where a bug costs real money, so the controls are deliberately heavier
than elsewhere.

**Workout spoofing (T5).** Rewards are the incentive to fake training. Ingest
computes a `trust_score` per activity from checks including: physiologically
impossible speed/power for the athlete's history; GPS displacement inconsistent
with elapsed time; heart-rate flatline during claimed hard effort; distance/GPS
mismatch; the same activity appearing under multiple accounts; bursts of manual
entries; a device shared across accounts. Findings land in
`analytics.data_quality_flags` and `rewards.fraud_signals`.

Consequences, in order:

1. Low trust → `reward_events.status = 'held_for_review'`; **no ledger entry is
   written**, so no balance ever reflects a suspect session.
2. Manual activities earn at a reduced rate and cannot win sponsored challenges.
3. Confirmed spoofing → wallet frozen (earning continues to accrue, redemption and
   payout are blocked) pending review.

**Ledger integrity.** Append-only double entry (`0007`). `UPDATE`/`DELETE` are
revoked from `app_rw` *and* blocked by trigger. A nightly trial balance asserts
every transaction nets to zero via the `wallet_unbalanced_transactions` view; any
row returned is a paging incident.

**Earning idempotency.** `UNIQUE (user_id, rule_code, reference_type,
reference_id)` on `reward_events` and a matching dedupe index on
`wallet_transactions`. One workout cannot be paid twice even if the job runs twice.

**Payout safety.**

* Gates evaluated and **frozen into `eligibility_snapshot`**: minimum balance,
  account age, cooling-off period, KYC state, open fraud signals, payout velocity.
* **A human approves.** `CHECK (status <> 'approved' OR decided_by IS NOT NULL)`
  makes an unapproved payout unrepresentable.
* One attempt, with an idempotency key held against the provider.
* An ambiguous provider response parks in `needs_manual_reconciliation` and is
  **never auto-retried**. A delayed payout is recoverable; a duplicated one is not.

---

## 8. AI-specific security

**Prompt injection (T6).** Activity names, notes and group text are attacker-
controlled. They are wrapped in explicitly delimited untrusted blocks with a
standing instruction that content inside is data, never instructions.

The control that actually matters: **tools execute server-side under the
authenticated caller's identity and never accept a user id from the model.** Even
a fully successful injection cannot make a tool read another athlete's data,
because there is no parameter through which to ask.

**Output safety.** A medical red-flag classifier (chest pain, syncope, severe
unexplained symptoms) routes to "seek medical advice" and suppresses training
recommendations. The coach never diagnoses, never names conditions, and never
contradicts medical advice. Injury risk is always presented with
`is_clinically_validated: false`.

**Numeric grounding.** Every number in an answer is checked against the context
packet; an ungrounded number sets `grounded = false`, is flagged, and fails the
eval suite. This is the hallucination control.

**Cost and abuse.** Per-tier monthly quota plus a per-minute burst limit; a
maximum context size; a maximum tool-call depth. Cost per message is recorded on
every row, so an anomaly alerts.

---

## 9. OWASP Top 10 (2021) coverage

| | Risk | Controls |
|---|---|---|
| A01 | Broken access control | Dual-layer isolation (§3); grants with mandatory expiry (§4); RBAC on routes; no IDOR possible because every read is RLS-filtered by owner; automated per-endpoint isolation tests in CI |
| A02 | Cryptographic failures | TLS 1.3 + HSTS + pinning; envelope encryption for tokens and payout refs; Argon2id passwords; hashed refresh tokens; no secrets in the repo |
| A03 | Injection | SQLAlchemy parameter binding only, no string-built SQL (grep-enforced in CI); Pydantic validation on every input; no generic query DSL; prompt-injection handling in §8 |
| A04 | Insecure design | This document; threat model above; append-only money design; fail-closed isolation; explicit refusal to compute advice on thin data |
| A05 | Security misconfiguration | Non-root containers; no debug in production; strict CORS allowlist; security headers (CSP, HSTS, `X-Content-Type-Options`, `Referrer-Policy`); `/readyz` separate from `/healthz`; the RLS verification queries after every migration |
| A06 | Vulnerable components | Pinned dependencies with hashes; Dependabot; `pip-audit` and `npm audit` gate the build; base images rebuilt weekly |
| A07 | Auth failures | §2 in full — rotation, reuse detection, lockout, MFA for staff, enumeration resistance |
| A08 | Integrity failures | Signed webhooks; server-side receipt validation; append-only ledger and audit; migrations reviewed and applied by a human; artefact provenance in CI |
| A09 | Logging and monitoring failures | Structured logs with request correlation; append-only audit for every cross-user health read and every admin action; alerts on auth-failure spikes, unverified webhooks and payout failures |
| A10 | SSRF | No user-supplied URL is ever fetched server-side; provider endpoints are a fixed allowlist; the object-storage client cannot be pointed at an arbitrary host |

---

## 10. GDPR and Israeli Privacy Protection Law

| Obligation | Implementation |
|---|---|
| Lawful basis for Article 9 data | Explicit, versioned consent (`identity.consents`), separately for health processing and for AI improvement. Health processing consent is required before any provider connection. |
| Purpose limitation | Consent purposes are enumerated in the schema; a new purpose needs a new consent row, not a reinterpretation of an old one. |
| Data minimisation | The AI layer receives derived metrics and an age band, never raw identity. Public shares cannot include health fields. |
| Right of access / portability | `POST /v1/me/export` — machine-readable archive of everything held. |
| Right to erasure | `DELETE /v1/me` — 30-day grace, then health data deleted and financial/audit records retained in redacted form under the legal-obligation carve-out. `GET /v1/me/deletion` states plainly what is retained and why. |
| Right to rectification | Profile and activity edits; corrections trigger a metrics recompute so derived values follow. |
| Storage limitation | Retention table in `02-database-design.md` §7, enforced by scheduled jobs, not by intention. |
| Records of processing | This document plus the schema comments. |
| Breach notification | Runbook with a 72-hour clock; the audit log is what makes scope assessment possible. |
| Transfers | AI and cloud provider DPAs; regions recorded in the privacy policy. |
| **Amendment 13 (Israel)** | **Open question for counsel:** health data is sensitive information; confirm whether database registration and a DPO appointment are required at our scale. Flagged in `01-architecture.md` §14. |

---

## 11. Security testing (detail in `09-testing-and-model-governance.md`)

Non-negotiable in CI:

1. **Tenant isolation matrix** — parametrised over *every* athlete-scoped
   endpoint: athlete A's credentials against athlete B's resource id must return
   404 or 403, never 200. New endpoint without a row in this matrix fails the
   build.
2. **RLS enforcement at the database layer** — with no session context set, every
   protected table returns zero rows.
3. **Grant lifecycle** — a coach can read within scope, cannot read outside it,
   and loses access the instant a grant is revoked or expires.
4. **Auth** — JWT tampering and algorithm confusion rejected; expired tokens
   rejected; refresh reuse revokes the family.
5. **Webhooks** — an invalid signature never mutates entitlement state.
6. **Ledger** — property test: no sequence of reward, redemption, reversal and
   payout operations can produce a negative withdrawable balance or an unbalanced
   transaction.
7. **Prompt injection** — a corpus of malicious activity names must not cause a
   cross-tenant tool call or an instruction leak.

---

## 12. Open security questions

1. **Do we hold Garmin refresh tokens long-term, or re-authorise?** Holding them
   is a permanent high-value liability; re-authorising costs the athlete friction.
   Recommendation: hold, encrypted, with a documented rotation and revocation
   path.
2. **Payout KYC threshold** — above what cumulative amount do we require identity
   verification? Needs counsel; the schema already carries the gate.
3. **Penetration test** before public launch — recommend an external test focused
   on tenant isolation and the payment/payout paths specifically.
4. **Bug bounty** — worthwhile once there is real user data; not before.
