# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

An AI coaching platform for endurance and gym athletes: sensor data in,
**explained decisions** out. Python 3.11, FastAPI, PostgreSQL 16 with row-level
security, Redis/arq, Anthropic `claude-opus-5`.

Read `docs/01-architecture.md` before writing code here. `docs/22-specification-index.md`
is the map of the other 23 documents.

## Process

Per the project's engineering rule: **no service implementation before the
architecture, database design, API design and security review are approved.** The
approval artefacts are `docs/01`, `docs/02`, `docs/03` and `docs/06`.

Two things are implemented and fully tested ahead of that gate, both because they
are pure and verifiable with no infrastructure: the analytics engine
(`backend/algorithms/`) and the footprint policy language (`backend/squeeze/`).

## Commands

Two suites run on a bare Python with nothing installed. Keep it that way — they
are the fastest signal on the most correctness-critical code in the product.

```bash
python3 -m unittest discover -s tests/algorithms -t . -v   # analytics engine, 209 tests
python3 -m unittest discover -s tests/squeeze    -t . -v   # footprint language, 98 tests
make test-nodeps                                           # both of the above

# one module, one class, one test
python3 -m unittest tests.algorithms.test_recovery -v
python3 -m unittest tests.squeeze.test_planner.TestAnchor.test_hand_computed_footprint
```

Discover per directory, not `-s tests`: the integration and security suites
import pytest, so discovering the whole tree fails at import time with no
dependencies installed.

Everything else needs the virtualenv:

```bash
make venv                    # create .venv and install requirements + dev requirements
make test                    # pytest, whole suite
make test-security           # the security suite — tenant isolation, auth, webhooks, ledger
make lint                    # ruff check + ruff format --check
make fmt                     # auto-fix
make types                   # mypy --strict on algorithms, squeeze, modules
make lint-arch               # import-linter: the layering contract below
make cov                     # coverage; algorithms gated at 90%, overall at 80%
make ci                      # everything CI runs
make up / down / run / worker # docker services, uvicorn, arq worker

.venv/bin/pytest tests/integrations/test_garmin_adapter.py::test_name -q   # a single test
.venv/bin/pytest -q -m "not integration"                                   # skip DB-backed tests
```

Footprint policy (`docs/23`), also dependency-free:

```bash
make sqz-check      # python3 -m backend.squeeze check  policies/footprint.sqz
make sqz-plan       # the plan, with its drivers
make sqz-verify     # measured codec ratios vs declared; fails on optimistic drift
make sqz-gate       # plan --strict: fails if a limit is exceeded *or* unprovable
```

Database: `make db-migrations` lists files in apply order; `make db-dry-run
FILE=…` runs one in a transaction and rolls back. There is deliberately no
`make db-migrate`.

## Layering — enforced by `make lint-arch`

```
api ──▶ modules ──▶ algorithms
 │         └──────▶ integrations, core, database
workers ──▶ modules

backend/squeeze  ──▶ (nothing)
backend/algorithms ──▶ (nothing)
```

* `backend/algorithms/` and `backend/squeeze/` import **nothing** from this
  project and no third-party package. Standard library only. Both must stay
  runnable and testable with zero dependencies installed.
* Modules talk to each other only through `modules.<other>.service`. Never import
  another module's `models.py` or `repository.py`.
* `api/` is transport only: validate, call a service, serialise. No SQL, no business
  rules.
* Adding `backend/modules/<name>/` without adding it to the `independence`
  contract in `pyproject.toml` is a contract regression, not an omission.

## Architecture in one screen

* **`backend/algorithms/`** — the deterministic analytics engine and the core IP.
  Pure functions over plain dataclasses: training load and CTL/ATL/TSB, readiness,
  session efficiency, injury risk, race predictions, plan generation and daily
  adaptation. Every result carries its drivers and a `data_quality` figure.
  Formulas, sources and evidence limits are in `docs/04`.
* **`backend/modules/<name>/`** — one schema namespace in Postgres each, with a
  fixed internal shape: `models.py` (this module's tables only), `schemas.py`
  (DTOs at the boundary), `repository.py` (all SQL, enforces tenant scoping),
  `service.py` (business rules; the only thing other modules may call),
  `events.py`. Built: `identity`, `training`. Specified: `coaching`, `billing`,
  `rewards`, `partners`, `community`, `notifications` (`docs/21`).
* **`backend/integrations/`** — provider adapters (Garmin built) behind one
  contract in `base.py`, normalisation in `normalisation.py`, confidence scoring
  in `quality.py`. Tokens are envelope-encrypted (`core/crypto.py`).
* **`backend/squeeze/`** — the footprint policy language: lexer → parser →
  checker → planner → report, plus real stdlib codecs and a drift verifier. See
  below and `docs/23`.
* **`backend/api/`**, **`backend/core/`**, **`backend/database/`** — transport,
  primitives (config, security, ids, etag, tenancy context, logging), and
  session/engine wiring.
* **`database/migrations/`** — reviewed SQL, applied by a human, never by code.

## The footprint language (`backend/squeeze/`, `docs/23`)

`policies/footprint.sqz` declares, in a typed language, how much room the
product's data may occupy — per class of data, per storage tier, per retention
window — and the compiler emits a **plan**: an estimate with ranked drivers, the
limits it was checked against, and an explicit list of what it could not compute.
CI fails when the plan exceeds a limit, when it cannot *prove* it fits, or when a
declared compression ratio has drifted above what the codec actually achieves.

When changing storage, retention, a codec or a budget, change the policy and let
the gate check it:

* Units are typed — `30 days` and `30 GiB` cannot be added. Magnitudes are
  `Fraction`, never `float`, so two runs over one source produce byte-identical
  totals and a plan is diffable in review.
* Retention windows are cumulative and may not decrease; residency in a tier is
  the difference between consecutive windows, clipped to the policy horizon.
* `sensitivity clinical` forbids lossy codecs on that class. A latency budget
  with an unmeasured CPU cost fails **closed** — unprovable is not satisfied.
* An unmeasured class is `unknown`, `coverage` drops below 1, and every limit
  check becomes `None`. Never `True`, never `0 bytes`.
* Drivers sum to the total saving exactly, the same contract the readiness score
  holds to.
* A declared `ratio` needs an `impl` naming a real codec so `verify` can measure
  it. Understating a codec is fine; overstating it fails the build.

Adding a setting to the language: token → atom in `parser.py` only if the shape is
new, meaning in `checker.py`, model field in `model.py` (`X | None` if the source
may omit it), behaviour in `planner.py`, a line in `report.py`, a stable
diagnostic code, a test, and a row in `docs/23`. A change that would make an
existing source mean something different bumps `CURRENT_VERSION`.

## Database

* Every schema or data change is a reviewed SQL file in `database/migrations/`,
  applied by a human. Never auto-applied. See `database/README.md`.
* Every athlete-scoped table carries `user_id` and has an RLS policy.
* Money is `BIGINT` minor units plus a currency column. Never a float.
* Enumerations are `TEXT` + `CHECK` (ADR-008). Ids are UUIDv7 (ADR-009).
* Append-only tables get `app.forbid_mutation()` **and** a `REVOKE`.

## Code

* Type hints everywhere; `mypy --strict` on `modules/`, `algorithms/` and
  `squeeze/`.
* Comments explain **why**, never what. If a formula has a source, name it. If a
  constant was chosen, say what it was chosen against.
* Missing data is `None`, never `0`. A function that cannot compute a meaningful
  answer returns `None` rather than a plausible fake — this is load-bearing
  throughout the analytics *and* the footprint compiler.
* Every composite score returns its drivers, and the contributions sum exactly to
  the total.
* No `except Exception: pass`. No bare `except`.
* Ruff bans `print`; write to `sys.stdout` in CLIs. Timestamps are tz-aware.

## Tests

* New endpoint → a row in the tenant-isolation matrix, or the build fails.
* New algorithm → tests for the anchor case, the undefined case, and one
  hand-computed value.
* New money path → a property test.
* New language rule → a test naming its diagnostic code, and a hand-computed
  footprint if it changes the arithmetic.
* The dependency-free suites must keep running with nothing installed:
  `python3 -m unittest discover -s tests/algorithms -t .` and
  `… -s tests/squeeze -t .`
* No randomness in fixtures or samples. A flaky physiology test is worse than no
  test, and a footprint number that moves between runs gets ignored.

## AI

* The model never receives raw data and never computes. Derived metrics only.
* Tools execute server-side under the caller's identity and take no user id
  parameter.
* Every number in an answer must be grounded in the context packet.
* Default model `claude-opus-5`; adaptive thinking is on by default and thinking
  tokens bill as output, so size `max_tokens` accordingly. Depth is
  `output_config.effort`, not `budget_tokens`. `temperature`/`top_p` are rejected.
  Check `stop_reason` before reading content.
* Record provider, model, tokens and cost on every message row.

## Security

* Never log health values, emails or names. `user_id` only.
* Never trust a client claim about entitlement.
* Never act on an unverified webhook.
* Never add an RLS exemption for admin or support.
