# 08 — Technology Decisions (ADRs)

Each record states the decision, the reasoning, what was rejected and why, and
what would make us revisit. A decision without a revisit trigger is a belief, not
a decision.

---

## ADR-001 — Modular monolith first, with hard module boundaries

**Decision.** One deployable containing eight modules, each owning a Postgres
schema and exposing a service interface. Extract to separate services when a
module's scaling profile actually diverges.

**Why.** The requirement is a system that grows to millions of users without a
rewrite. That requirement is satisfied by *boundaries*, not by *deployments*.
Eight network services for a pre-launch product buys distributed transactions,
eventual consistency, cross-service tracing and eight deploy pipelines — and pays
for them in the velocity needed to reach the users that would justify them.

Boundaries are enforced mechanically, not by good intentions:
* one schema per module;
* cross-module calls go through `modules.<other>.service` only;
* an import-linter contract in CI fails the build on a violation.

**Rejected.** Microservices from day one — cost without present benefit, and the
boundaries would be guesses drawn before we know the load profile. A single
undifferentiated app — becomes untangleable exactly when extraction is needed.

**Revisit when.** A module's write volume, latency profile or failure isolation
needs diverge measurably. `training` (ingest volume) and `coaching` (slow bursty
LLM calls) are the expected first two.

---

## ADR-002 — PostgreSQL as the single system of record

**Decision.** Postgres 16 for everything transactional. Redis for cache, queue and
rate limits, never as a source of truth. Object storage for sample streams.

**Why.** The domain is relational and the invariants matter: a wallet ledger needs
real transactions, RLS gives tenant isolation the application cannot bypass,
partial and expression indexes cover our access patterns, and JSONB handles the
genuinely schemaless parts (driver arrays, twin attributes) without a second
datastore.

**Rejected.** A time-series database for activities — the volume does not warrant
it and it would split the athlete's data across two stores that must then be
joined in application code. MongoDB — we would lose the ledger's transactional
guarantees, which is the one place we cannot afford to be wrong.

**Revisit when.** Analytics reads outgrow the primary (Stage 4 in `01` §9) — at
which point a columnar store fed by CDC serves reads, and Postgres remains the
system of record.

---

## ADR-003 — Analytics engine as a dependency-free library

**Decision.** `backend/algorithms/` is pure Python standard library. No I/O, no
framework, no third-party packages. It may not import anything else in the
project.

**Why.** It is the product's core IP and the thing most in need of provable
correctness. As a library it is callable from the API, the worker and the ML
scripts with no network hop; it is testable with no database, no fixtures and no
installed dependencies (all 209 tests run on a bare Python 3.11); and it is
portable if a hot path ever needs reimplementing.

The practical proof: this layer was written and fully tested before any
infrastructure existed.

**Rejected.** An analytics microservice — a network hop for pure arithmetic.
NumPy inside the engine — see ADR-004.

**Revisit when.** A per-athlete computation becomes CPU-bound in a request path.
The batch layer, not the engine, is where vectorisation belongs.

---

## ADR-004 — NumPy/Pandas in the batch layer only

**Decision.** The deterministic per-athlete core is stdlib. NumPy and Pandas appear
in `backend/workers/analytics/batch.py` for bulk recompute and in `ml/` for
training. PyTorch only when a neural model is actually justified.

**Why.** Per-athlete series are hundreds of points; NumPy's advantage does not
appear at that size, and the import cost and API surface are real. Bulk recompute
across 100k athletes is a genuinely different problem where vectorisation wins.
Keeping them separate means the correctness-critical code has no dependency that
can change under it.

PyTorch is listed in the specification and will earn its place once there is
labelled data and a model class that beats gradient boosting — not before.

**Revisit when.** A batch recompute exceeds its window, or a sequence model
demonstrably beats the tabular baseline on the injury or performance task.

---

## ADR-005 — Row-level security in addition to application scoping

**Decision.** RLS on every athlete-scoped table, plus explicit tenant scoping in
every repository method.

**Why.** The organisational requirement is strict multi-tenant isolation. Both
layers have a realistic failure mode: application code eventually forgets a
`WHERE`, and database policies can be defeated by a mis-provisioned role. Together
they fail closed — an unscoped query returns zero rows because
`current_setting(…, true)` is NULL and `user_id = NULL` filters everything.

The cost is a small planner overhead on some policies and the discipline of
setting session context per request. Given the data is health data, that is not a
close call.

**Rejected.** Application-only scoping (one forgotten clause is a breach);
schema-per-tenant (unworkable for millions of B2C athletes).

**Revisit when.** Never for correctness. If a specific policy's cost shows up in
profiling, optimise that policy — do not remove the layer.

---

## ADR-006 — arq for the job queue

**Decision.** arq (asyncio-native, Redis-backed).

**Why.** The API is async FastAPI; arq shares the event loop model, so job code and
request code look the same and share the same database session pattern. Celery's
worker model would mean maintaining two idioms. Redis is already required for cache
and rate limits, so there is no new infrastructure.

**Rejected.** Celery — more mature and richer, but sync-first and a heavier
operational surface than we need. Kafka — an event log we do not need at this
scale. Postgres-as-queue (`SKIP LOCKED`) — attractive for having no new
dependency, but ingest volume would put queue churn on the primary we most need to
protect.

**Revisit when.** We need fan-out to multiple independent consumers, replay of an
event stream, or cross-service eventing — that is when Kafka or a managed
equivalent earns its cost.

---

## ADR-007 — REST/JSON, not GraphQL or gRPC

**Decision.** REST with OpenAPI 3.1, generated clients.

**Why.** Two first-party clients with known, stable data needs. `GET
/v1/metrics/summary` solves the dashboard's over-fetching problem in one endpoint;
GraphQL would solve it too, and bring a resolver-level authorisation surface that
is materially harder to get right on health data — an under-secured field resolver
is a leak. gRPC buys nothing for browser clients.

**Rejected.** GraphQL — authorisation complexity and cache-invalidation
complexity, for flexibility two known clients do not need. gRPC — poor browser
story, no benefit at our call volume.

**Revisit when.** Third-party developers need flexible querying (Phase 5), or
internal service-to-service traffic becomes latency-sensitive after extraction.

---

## ADR-008 — TEXT + CHECK instead of native Postgres ENUM

**Decision.** Enumerations are `TEXT` columns with `CHECK` constraints.

**Why.** Adding a value to a native `ENUM` is `ALTER TYPE`, with historical
transactional restrictions; removing one is effectively impossible. This schema has
dozens of enumerations that will gain values (new sports, new providers, new reward
rules, new payment methods). `TEXT` + `CHECK` makes each of those an ordinary,
reviewable constraint change.

Cost: a few bytes per row and no automatic ordering. Neither matters here.

**Rejected.** Native `ENUM` (rigid); lookup tables with FKs (a join for every
status read, for values that change once a year); unconstrained `TEXT` (typos reach
production).

---

## ADR-009 — UUIDv7 primary keys, application-generated

**Decision.** UUIDv7 from the application. `gen_random_uuid()` remains as a DEFAULT
safety net.

**Why.** Time-ordered, so B-tree inserts stay at the right edge of the index
instead of fragmenting it the way UUIDv4 does. No sequence to coordinate when the
athlete-scoped tables are sharded by `user_id`. Ids are safe to expose without
leaking row counts. Generating client-side means an object graph can be built
before any write.

Postgres 16 has no built-in `uuidv7()` (it arrives in 18), hence
application-generated.

**Rejected.** `BIGSERIAL` (leaks volume, needs shard coordination); UUIDv4 (index
fragmentation and worse cache locality at our insert rate).

---

## ADR-010 — Double-entry ledger for the wallet

**Decision.** Wallet balances are derived from immutable double-entry ledger
entries. No mutable balance column exists.

**Why.** A balance column is corruptible by any bug in any code path that writes
it, unauditable after the fact, and impossible to reconcile — and this is real
money owed to users. The ledger makes every shekel explainable to the athlete and
auditable by us; corrections are compensating transactions, so history is never
rewritten. It is also what lets the reward split percentages change safely: each
entry records the policy version that governed it.

Cost: reads are an aggregate. Mitigation if that ever bites is a periodic snapshot
row, **not** a balance column.

**Rejected.** A mutable balance with an audit log alongside — two sources of truth
that will disagree, and the log is the one nobody checks.

---

## ADR-011 — Provider-agnostic LLM client

**Decision.** An `LLMProvider` protocol; Anthropic `claude-opus-5` as the default
implementation. Route → provider/model/effort is configuration.

**Why.** A single-provider outage would take the headline feature offline, and
model pricing and capability move fast enough that being able to switch is worth
about a day of work. Recording `provider`, `model_id` and `prompt_version` on
every message row makes a quality regression after a model change attributable
instead of mysterious.

Deliberately **not** a lowest-common-denominator abstraction: provider-specific
features (adaptive thinking, prompt caching, the Batch API) are used through the
Anthropic implementation. The protocol covers the call shape, not the feature set.

**Rejected.** Direct SDK calls throughout (a provider change becomes a
refactor); LangChain-style framework (an abstraction layer whose cost exceeds its
benefit for one primary provider).

---

## ADR-012 — Migrations are reviewed SQL, applied by a human

**Decision.** Every schema and data change is a hand-written, reviewed SQL file in
`database/migrations/`, executed by a developer. Nothing — not the app, not CI, not
the deploy pipeline — applies migrations automatically.

**Why.** Organisational policy, and independently correct for a system holding
health data and a financial ledger: an auto-applied migration is an unreviewed
production change with no rollback plan. Hand-written SQL is also more expressive
than an ORM's autogenerate for the things this schema depends on — partial indexes,
RLS policies, expression indexes, `CHECK` constraints, triggers, comments.

Cost: no autogenerate, and models plus SQL must be kept in step. Mitigated by a CI
check that diffs the ORM metadata against a scratch database built from the
migration files, failing on drift.

**Rejected.** Alembic autogenerate as the source of truth — it produces migrations
nobody reads, and it does not express RLS.
