# AI Sports Coach Platform

An AI personal coach for triathletes, runners, cyclists, swimmers and gym
athletes. Watch and sensor data goes in; **decisions** come out — what to train
today, how much to rest, why yesterday felt flat, what to improve.

> **Project state: architecture review.**
> The analysis, architecture, database design, API design, security review and
> roadmap are complete and awaiting approval. The deterministic analytics engine
> is implemented and tested (209 tests). Service implementation has **not**
> started, per the engineering rule that no code is written before the
> architecture is approved.

---

## Read these first, in order

| # | Document | What it answers |
|---|---|---|
| 0 | [`docs/00-product-brief.md`](docs/00-product-brief.md) | What we are building and for whom |
| 1 | [`docs/01-architecture.md`](docs/01-architecture.md) | **Architecture Plan** — modules, data flow, cache, queue, scaling, observability |
| 2 | [`docs/02-database-design.md`](docs/02-database-design.md) | **Database Design** — tables, indexes, partitioning, RLS, ledger |
| 3 | [`docs/03-api-design.md`](docs/03-api-design.md) | **API Design** — every endpoint and every convention |
| 4 | [`docs/04-analytics-algorithms.md`](docs/04-analytics-algorithms.md) | Every formula, its source, and its evidence limits |
| 5 | [`docs/05-ai-architecture.md`](docs/05-ai-architecture.md) | Context packet, routing, tools, guardrails, digital twin |
| 6 | [`docs/06-security-privacy.md`](docs/06-security-privacy.md) | **Security Review** — threat model, OWASP coverage, GDPR |
| 7 | [`docs/07-roadmap.md`](docs/07-roadmap.md) | **MVP Roadmap** — phases, tasks, estimates, risks |
| 8 | [`docs/08-tech-decisions.md`](docs/08-tech-decisions.md) | ADRs: every choice, what was rejected, when to revisit |
| 9 | [`docs/09-testing-and-model-governance.md`](docs/09-testing-and-model-governance.md) | Test strategy, algorithm versioning, accuracy measurement |
| 10 | [`docs/10-cost-model-and-risks.md`](docs/10-cost-model-and-risks.md) | Unit economics at 15 ₪, AI cost model, risk register |
| 23 | [`docs/23-squeeze-language.md`](docs/23-squeeze-language.md) | **Squeeze** — the footprint policy language that governs storage, memory and model-weight budgets |

---

## What exists today

```
backend/algorithms/        ✅ analytics engine — stdlib only, 209 tests passing
backend/squeeze/           ✅ footprint policy language — stdlib only, 98 tests passing
policies/footprint.sqz     ✅ the platform's footprint policy, gated in CI
tests/algorithms/          ✅ the test suite
database/migrations/       ✅ 0001–0009, reviewable SQL (NOT applied — see below)
docs/                      ✅ architecture, DB, API, security, roadmap, ADRs
backend/{api,core,modules,integrations,workers,database}/   ⏳ after approval
apps/{mobile,web}/         ⏳ after approval
ml/                        ⏳ Phase 3
```

The analytics engine was built first on purpose: it is the core IP, it is pure
functions with no dependencies, and it is the one layer that could be **fully
verified** before any infrastructure existed.

## Run the tests right now

No dependencies, no database, no setup:

```bash
python3 -m unittest discover -s tests/algorithms -t .   # Ran 209 tests — OK
python3 -m unittest discover -s tests/squeeze    -t .   # Ran  98 tests — OK
```

That the most correctness-critical code in the product tests in tens of
milliseconds on a bare Python 3.11 is a deliberate property, not an accident.
(The suites are discovered per directory because the integration and security
suites import pytest; discovering `tests/` as a whole fails at import time with
no dependencies installed.)

## See the footprint budget

Also dependency-free — the language that governs how much room the product's data
is allowed to occupy (`docs/23`):

```bash
python3 -m backend.squeeze plan   policies/footprint.sqz    # the plan, with drivers
python3 -m backend.squeeze verify policies/footprint.sqz    # declared ratios vs measured
```

---

## Database migrations

**Nothing in `database/migrations/` is ever applied automatically** — not by the
app, not by the tests, not by CI, not by a deploy. Every file is a change proposal
to be reviewed and executed by a developer. The files have **not** been executed
against any database.

See [`database/README.md`](database/README.md) for the review and apply procedure.

---

## Stack

| Layer | Choice | ADR |
|---|---|---|
| Mobile | React Native + TypeScript (iOS, Android) | — |
| Web | React + TypeScript | — |
| Backend | Python 3.11 + FastAPI | ADR-001 |
| Analytics | Python standard library only | ADR-003 |
| Footprint policy | Squeeze (`.sqz`), compiler in `backend/squeeze/`, stdlib only | docs/23 |
| ML (Phase 3) | scikit-learn; PyTorch when justified | ADR-004 |
| Database | PostgreSQL 16 with row-level security | ADR-002, ADR-005 |
| Cache / queue | Redis + arq | ADR-006 |
| AI | Anthropic `claude-opus-5`, provider-agnostic client | ADR-011 |
| Infra | Docker, managed Postgres/Redis/object storage | — |

---

## Non-negotiables

These are enforced by tests and review, not by intention:

1. **Every score ships with its explanation.** No endpoint returns a bare number.
   Readiness returns ordered drivers whose contributions sum exactly to
   `score − 50`.
2. **Missing data is never zero.** An absent input lowers `data_quality`; below
   0.35 the API returns 422 instead of a confident-looking score.
3. **Injury risk is labelled unvalidated** (`is_clinically_validated: false`) until
   a model trained on real labels beats the heuristic baseline.
4. **Tenant isolation fails closed** — application scoping *and* Postgres RLS. A
   request with no context sees nothing, not everything.
5. **Money is an append-only double-entry ledger.** No mutable balance column
   exists anywhere.
6. **No card data, ever.** Provider-hosted flows only; the schema has no column
   capable of holding a PAN.
7. **The AI never receives raw data and never computes.** It reasons over derived,
   explained metrics, and every number it states is grounded in them.
8. **The storage budget is compiled, not estimated.** `policies/footprint.sqz`
   declares every retention window, codec and byte limit; CI fails when the plan
   exceeds a limit, when it *cannot prove* it fits, or when a declared
   compression ratio has drifted above what the codec actually achieves.

---

## Contributing

Read [`CLAUDE.md`](CLAUDE.md) for the conventions this repository is held to.
