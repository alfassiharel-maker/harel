# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

An AI personal coach for triathletes, runners, cyclists, swimmers and gym athletes.
Watch and sensor data goes in; **decisions** come out. Hebrew-first launch, Israeli
market, 15 ₪/month price point (`docs/10`).

Read `docs/01-architecture.md` before writing code. `docs/22-specification-index.md`
is the map of the other 22 documents and of which one owns which decision.

---

## Orientation — what is built and what is only specified

```
backend/algorithms/     ✅ BUILT   analytics engine, stdlib only, 209 tests
backend/core/           ✅ BUILT   config, crypto, security, errors, logging, tenancy context
backend/database/       ✅ BUILT   engine + RLS-scoped session
backend/modules/identity/ ✅ BUILT auth, users, consent, sessions, audit
backend/modules/training/ 🟡 PARTIAL profile, goals, PBs, zones (ingest = Phase 1 wk 3–4)
backend/integrations/   🟡 PARTIAL adapter protocol + registry, mock + garmin adapters
backend/api/            🟡 PARTIAL app factory, deps, RFC 9457 errors, health/auth/me/training
backend/workers/        ⏳ not created — `make worker` target exists, package does not
backend/modules/{coaching,billing,rewards,partners,community,notifications}/ ⏳
apps/{mobile,web}/, ml/ ⏳ README only
database/migrations/    ✅ 0001–0011, 0014, 0014b written — NONE applied anywhere
docs/00–22              ✅ ~65k words of specification
```

The engine was built first on purpose: it is the core IP, pure functions with no
I/O, and the one layer that could be fully verified before any infrastructure
existed. Protect that property.

---

## Commands

### Zero-dependency loop (works on a bare Python 3.11, ~45 ms)

```bash
python3 -m unittest discover -s tests/algorithms -t .   # 209 tests, OK
python3 -c "import backend.algorithms as a; print(a.__version__)"
python3 -m unittest tests.algorithms.test_recovery                       # one module
python3 -m unittest tests.algorithms.test_recovery.ReadinessTests.test_x  # one test
```

Note: `make test-algorithms` and the CI `analytics` job still run
`discover -s tests -t .`, which now walks the whole `tests/` tree and **fails with
9 import errors** — `tests/{integration,security,unit}` and
`tests/integrations/test_garmin_adapter.py` import `structlog`, `httpx`, `asyncpg`
and `cryptography`. Either narrow those two commands to `tests/algorithms` (plus the
stdlib-clean `tests/integrations` modules) or the zero-dep signal stays red.

### Full environment

```bash
make venv                      # .venv + requirements.txt + requirements-dev.txt
make up                        # docker compose postgres + redis
./scripts/dev_db_bootstrap.sh  # apply migrations to a LOCAL db only (there is no `make db-bootstrap`,
                               # despite tests/conftest.py's docstring)
make run                       # uvicorn backend.api.main:app --reload
make ci                        # lint types lint-arch test-algorithms test test-security audit
```

Individual gates: `make lint` / `fmt` (ruff), `make types` (mypy --strict on
`algorithms` + `modules`), `make lint-arch` (import-linter), `make test`,
`make test-security`, `make cov` (80% overall, 90% on `algorithms`), `make audit`.

Single pytest test: `.venv/bin/pytest tests/integration/test_auth.py::test_login_locks_after_five_failures -q`.
Markers (`--strict-markers`): `security` (never skippable), `integration` (needs PG+Redis),
`slow`, `ai_eval` (costs money; nightly, excluded per-commit).

The integration and security suites connect as `app_rw` — not the table owner, no
`SUPERUSER`, no `BYPASSRLS` — so RLS actually bites. Cleanup runs on a separate
superuser connection because `app_rw` legitimately cannot truncate athlete data.

### Migrations

```bash
make db-migrations                                   # list in apply order
make db-dry-run FILE=database/migrations/0011_....sql # BEGIN … ROLLBACK on SCRATCH_DATABASE_URL
make db-verify-rls                                   # post-apply role/ownership checks
```

There is deliberately **no** `make db-migrate`. See `database/README.md` for the
review-and-apply procedure.

---

## Architecture — the load-bearing shapes

### Layering, enforced by `make lint-arch` (contracts live in `pyproject.toml`)

```
api ──▶ modules ──▶ algorithms
 │         └──────▶ integrations, core, database
workers ──▶ modules
```

* `backend/algorithms/` imports **nothing** from this project and no third-party
  package. Standard library only. Adding `numpy` or `sqlalchemy` there fails CI.
* Modules talk to each other only through `modules.<other>.service`. Never import
  another module's `models.py` or `repository.py`.
* `api/` is transport only: validate, call a service, serialise. No SQL, no business
  rules. It may sequence two service calls (`/v1/me` does); it may never hold a
  business rule or a cross-module transaction.
* `integrations/` may import `core` only — an adapter never touches our schema.
* Every module has the same five files: `models.py`, `schemas.py`, `repository.py`,
  `service.py`, `events.py`, with `__init__.py` re-exporting the service **and its
  DTOs** (the DTOs *are* the boundary contract).
* Adding `backend/modules/<name>/` without adding it to the `independence` and
  `forbidden` contracts is a contract regression. The `tests/architecture/` drift
  check that `pyproject.toml` promises does not exist yet — write it.

Phase 2 will replace the flat `independence` contract with a module **DAG**
(`identity → billing → training → coaching → rewards → partners → community →
notifications`; import downward only) plus one `forbidden` contract per module —
`docs/11` §3. Three sanctioned cross-module patterns: downward `service` call for
synchronous reads, API-layer sequencing, domain event for fan-out or upward
direction. `notifications` sits at the top precisely so nothing may import it.

### Tenant isolation — the thing to get right

`backend/database/session.py` is the most security-critical file. `Database.session(principal)`
opens one transaction and issues `set_config('app.current_user_id', …, true)` —
`SET LOCAL`, transaction-scoped, so it cannot leak to the next request on a pooled
connection. A path that forgets context gets NULL, every RLS predicate filters every
row, and isolation **fails closed**. `principal` is required; `SYSTEM_PRINCIPAL` is
the explicit, greppable way to run unscoped (pre-auth `SECURITY DEFINER` lookups from
migration 0010, worker jobs). `apply_principal()` promotes an open transaction from
system to athlete footing — needed by register (the `WITH CHECK (id = app.current_user_id())`
policy) and by login/refresh.

Isolation is enforced **twice**: repository-level scoping *and* Postgres RLS.
`api/deps.py::require_principal` is the single choke point where a bearer token
becomes a `Principal` — one place to audit.

### Data flow

* No analytics computation in a request. Derived metrics are written to
  `analytics.daily_metrics` by the worker; a dashboard read is one indexed row
  (Redis 15-min cache in front). Missing row → return the last one with
  `stale_as_of` and enqueue a recompute.
* Provider ingest: verify webhook → persist the **raw** payload to `provider_events`
  with an idempotency key → return 200 in <100 ms → worker fetches detail, upserts
  on `(provider, provider_activity_id)`, enqueues analytics + twin refresh. Raw-first
  means a parser bug is replayable, not data loss.
* Redis is cache, rate limits and the arq queue — never the system of record.
  Everything in it must be reconstructible from Postgres.
* Money is an append-only double-entry ledger. Balance is `SUM(credits) - SUM(debits)`,
  derived. No mutable balance column exists anywhere.

### API conventions (`docs/03`)

`/v1` prefix; kebab-case paths, `snake_case` JSON; RFC 9457 `application/problem+json`
with a stable machine `code`; opaque cursor pagination (never offset);
`Idempotency-Key` required on money/AI writes; `ETag`+`If-Match` on mutable resources;
SI units always suffixed (`distance_m`, `pace_s_per_km`) — an unsuffixed numeric field
fails review; money as `{amount_minor, currency}`; `null` means unknown, never zero.
`/docs` and `/openapi.json` are off in production.

---

## Database

* Every schema or data change is a reviewed SQL file in `database/migrations/`,
  applied by a human. Never auto-applied — not by the app, tests, CI or a deploy.
  CI builds an *ephemeral* schema by replaying the same files, which is how it proves
  the schema matches what a developer would apply.
* Migration numbers are assigned **only** in `docs/19` §3 (the ledger, 0011–0036).
  Every other document defers to it. 0012 and 0013 are specified but not yet written;
  0014/0014b exist. Do not invent a number.
* Every athlete-scoped table carries `user_id` and has an RLS policy. The 12
  intentionally non-RLS tables are registered in `app.rls_exemptions` (`docs/19` §1.1).
* Money is `BIGINT` minor units plus a currency column. Never a float.
* Enumerations are `TEXT` + `CHECK` (ADR-008). Ids are UUIDv7, app-supplied (ADR-009).
* Append-only tables get `app.forbid_mutation()` **and** a `REVOKE`.
* `TIMESTAMPTZ` for instants, `DATE` for an athlete's local day. Every table and
  non-obvious column carries a `COMMENT`.
* Known defect to respect: `DELETE /v1/me` cannot work until migration 0020 —
  cascading deletes fire `forbid_mutation()` through `consents`, `prediction_records`
  and `audit_events`.

---

## Code

* Type hints everywhere; `mypy --strict` on `modules/` and `algorithms/`.
* Comments explain **why**, never what. If a formula has a source, name it. If a
  constant was chosen, say what it was chosen against. The existing codebase sets a
  high bar here — match it, including in `pyproject.toml` and migration headers.
* Missing data is `None`, never `0`. A function that cannot compute a meaningful
  answer returns `None` rather than a plausible fake — this is load-bearing
  throughout the analytics.
* Every composite score returns its drivers. Readiness drivers' contributions sum
  exactly to `score − 50`. Below `data_quality` 0.35 the API returns 422 rather than
  a confident-looking number.
* Every load score carries its `LoadSource` (power > heart_rate > pace > rpe), so the
  UI and the AI layer know how much to lean on it.
* No `except Exception: pass`. No bare `except`. Ruff runs with `S` (bandit), `DTZ`
  (every datetime tz-aware) and `T20` (no `print`) enabled.
* Errors in `core/errors.py` are HTTP-shaped nouns (`NotFound`, `Conflict`) by
  deliberate convention — `N818` is ignored there, not accidentally.

---

## Tests

* New endpoint → a row in the tenant-isolation matrix, or the build fails.
* New algorithm → tests for the anchor case, the undefined case, and one
  hand-computed value.
* New money path → a property test.
* Analytics tests must keep running with no dependencies installed.
* No randomness in fixtures, and no `Date.now`-style non-determinism. A flaky
  physiology test is worse than no test.
* Fixture caveat worth knowing: the algorithm fixtures are calendar-dense, which hides
  the ACWR reliability bug in `docs/22` §4.1 (`is_reliable` counts mapping keys, so a
  resting athlete silently drops the ACWR term out of readiness and injury risk).

---

## AI

* The model never receives raw data and never computes. Derived metrics only.
* Tools execute server-side under the caller's identity and take no user id
  parameter. The AI provider layer never reads the database, so a prompt injection
  has no data path.
* Every number in an answer must be grounded in the context packet.
* Default model `claude-opus-5`; adaptive thinking is on by default and thinking
  tokens bill as output, so size `max_tokens` accordingly. Depth is
  `output_config.effort`, not `budget_tokens`. `temperature`/`top_p` are rejected.
  `thinking: {"type": "disabled"}` returns 400 above effort `high`. Check
  `stop_reason` before reading content. Verify claims against the `claude-api` skill,
  not memory — `docs/22` §4.2 records what that check already corrected.
* Record provider, model, tokens and cost on every message row.
* Injury risk stays `is_clinically_validated: false` until a model trained on real
  labels beats `heuristic-v0` on held-out data. Labels precede models (0022 before 0024).

---

## Security

* Never log health values, emails or names. `user_id` only. The PII scrubber lives in
  the logging pipeline, not at call sites — a call site can be forgotten.
* Never trust a client claim about entitlement. Entitlements cache ≤ 5 min so a
  cancellation loses access promptly.
* Never act on an unverified webhook.
* Never add an RLS exemption for admin or support.
* No card data, ever — provider-hosted flows only; the schema has no column capable
  of holding a PAN.
* Provider OAuth tokens and payout identifiers are column-encrypted (AES-256-GCM,
  envelope-encrypted, key id stored beside the ciphertext for rotation).
* Auth errors never say which check failed; `/password/forgot` always returns 202 and
  `/register` returns a generic conflict — both are account-enumeration defences.
* Payouts are single-attempt with a provider idempotency key; ambiguous results park
  in manual review and are **never** auto-retried.

---

## Process

Per the project's engineering rule: **no service implementation before the
architecture, database design, API design and security review are approved.** The
approval artefacts are `docs/01`, `docs/02`, `docs/03` and `docs/06`; `docs/11`–`21`
are the Phase 2+ specification awaiting approval.

Where a doc and the code disagree, the code is ground truth for algorithm formulas
and the doc is authority for boundaries, endpoints, migration numbers and threat
model (`docs/22` §3). Record a disagreement in `docs/22` §4 rather than silently
editing an approved artefact.

The maintainer writes in Hebrew; reply in Hebrew when they do. Repository artefacts
— code, comments, docs, commit messages — stay in English.
