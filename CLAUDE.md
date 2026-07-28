# Repository conventions

Read `docs/01-architecture.md` before writing code in this repository.

## Process

Per the project's engineering rule: **no service implementation before the
architecture, database design, API design and security review are approved.** The
approval artefacts are `docs/01`, `docs/02`, `docs/03` and `docs/06`.

## Layering — enforced by `make lint-arch`

```
api ──▶ modules ──▶ algorithms
 │         └──────▶ integrations, core, database
workers ──▶ modules
```

* `backend/algorithms/` imports **nothing** from this project and no third-party
  package. Standard library only. It must stay runnable and testable with zero
  dependencies installed.
* Modules talk to each other only through `modules.<other>.service`. Never import
  another module's `models.py` or `repository.py`.
* `api/` is transport only: validate, call a service, serialise. No SQL, no business
  rules.

## Database

* Every schema or data change is a reviewed SQL file in `database/migrations/`,
  applied by a human. Never auto-applied. See `database/README.md`.
* Every athlete-scoped table carries `user_id` and has an RLS policy.
* Money is `BIGINT` minor units plus a currency column. Never a float.
* Enumerations are `TEXT` + `CHECK` (ADR-008). Ids are UUIDv7 (ADR-009).
* Append-only tables get `app.forbid_mutation()` **and** a `REVOKE`.

## Code

* Type hints everywhere; `mypy --strict` on `modules/` and `algorithms/`.
* Comments explain **why**, never what. If a formula has a source, name it. If a
  constant was chosen, say what it was chosen against.
* Missing data is `None`, never `0`. A function that cannot compute a meaningful
  answer returns `None` rather than a plausible fake — this is load-bearing
  throughout the analytics.
* Every composite score returns its drivers.
* No `except Exception: pass`. No bare `except`.

## Tests

* New endpoint → a row in the tenant-isolation matrix, or the build fails.
* New algorithm → tests for the anchor case, the undefined case, and one
  hand-computed value.
* New money path → a property test.
* Analytics tests must keep running with no dependencies:
  `python3 -m unittest discover -s tests -t .`
* No randomness in fixtures. A flaky physiology test is worse than no test.

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
