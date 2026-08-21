# Backend

Python 3.11 + FastAPI. Structure and rationale in `docs/01-architecture.md` §3.

```
backend/
├── algorithms/         ✅ BUILT — analytics engine, stdlib only, 209 tests
├── squeeze/            ✅ BUILT — footprint policy language, stdlib only, 98 tests (docs/23)
├── core/               ✅ BUILT — config, security primitives, errors, logging, tenancy context
├── modules/
│   ├── identity/           ✅ BUILT — auth, users, consent, sessions, audit
│   ├── training/           🟡 PARTIAL — profile, goals, personal bests, zones (ingest in wk 3–4)
│   ├── coaching/           ⏳ digital twin, AI chat, plans
│   ├── billing/            ⏳ subscriptions, receipts, entitlements
│   ├── rewards/            ⏳ wallet ledger, reward policies, payouts
│   ├── partners/           ⏳ partner accounts, offers, conversions
│   ├── community/          ⏳ groups, challenges, sharing
│   └── notifications/      ⏳ push, email, preferences
├── integrations/       ⏳ provider adapters: garmin, apple_health, coros, polar
├── api/                🟡 PARTIAL — app, deps, error handlers, health/auth/me/training routers
├── workers/            ⏳ queue consumers and scheduled jobs
└── database/           ✅ BUILT — engine, RLS-scoped session, base model helpers
```

`⏳` is not yet started; `🟡` is the Phase-1 slice only. The layering contract in
`pyproject.toml` already declares the not-yet-built modules (as optional layers),
so each lands under the boundary rules automatically.

## Module template

Every module has the same shape, so any engineer can find anything:

```
modules/<name>/
├── __init__.py       exports the public service interface ONLY
├── models.py         SQLAlchemy models for this module's tables
├── schemas.py        Pydantic DTOs at the module boundary
├── repository.py     all SQL for this module; enforces tenant scoping
├── service.py        business rules; the only thing other modules may call
└── events.py         domain events this module publishes
```

## Rules enforced by `make lint-arch`

* `algorithms/` imports nothing from this project and no third-party package.
* Modules reach each other only via `modules.<other>.service` — never another
  module's `models` or `repository`.
* `api/` is transport only: validate, call a service, serialise.
* Each module owns exactly one Postgres schema, so extraction later is
  `pg_dump --schema=<name>`.

## Why `algorithms/` is already built

It is the core IP, it is pure functions with no I/O, and it is the only layer that
could be **completely verified** before any infrastructure existed. It also has
the cheapest test loop in the repository:

```bash
python3 -m unittest discover -s tests -t .   # ~32 ms, no dependencies
```

Protect that. An import of `numpy` or `sqlalchemy` into this package fails CI.
