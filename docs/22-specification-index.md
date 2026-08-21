# 22 — Specification Index

**Status:** awaiting approval · **Date:** 2026-07-29 · **Covers:** `docs/00`–`docs/23`

The reading order and ownership map for the whole specification. Phase 1 weeks 1–2
are built and pushed; everything from here is specified and **awaiting approval
before implementation**.

---

## 1. The document set

### Approved — Phase 1 design (`00`–`10`)

| # | Document | Role |
|---|---|---|
| 00 | Product Brief | The problem, the market, what makes it defensible, how we know it works |
| 01 | Architecture Plan | Modules, request paths, cache, queue, ledger, scaling path |
| 02 | Database Design | Principles, table inventory, indexing, partitioning, RLS, retention |
| 03 | API Design | Conventions, error shape, the full `/v1` surface |
| 04 | Analytics Algorithms | The physiology and maths, per engine module |
| 05 | AI Architecture | Context packet, routing, tools, guardrails, twin |
| 06 | Security & Privacy | Threat model T1–T10, OWASP, GDPR — the approved Phase 1 review |
| 07 | Roadmap | The original 5-phase plan |
| 08 | Technology Decisions | ADR-001…ADR-012 |
| 09 | Testing & Model Governance | Test strategy, eval suite, promotion gates, outcome measurement |
| 10 | Cost Model & Risks | The 15 ₪ economics and the risk register |

### New — Phase 2+ specification (`11`–`21`)

| # | Document | Topic | Words |
|---|---|---|---|
| 11 | Architecture Update After Phase 1 | 1 | 3,630 |
| 12 | Roadmap: MVP to Production | 2 | 5,552 |
| 13 | Provider Integration (Garmin + adapter contract) | 3 | 5,378 |
| 14 | AI Coaching Architecture | 4 | 4,800 |
| 15 | Training Engine Specification | 5 | 5,811 |
| 16 | Mobile Application Architecture | 6 | 6,722 |
| 17 | Payments, Subscriptions, Rewards, Partners | 7 | 5,000 |
| 18 | Security & Privacy Requirements (Phase 2+) | 8 | 7,163 |
| 19 | Database Evolution Plan | 9 | 5,612 |
| 20 | Scaling Plan: First Users to Millions | 10 | 7,901 |
| 21 | Module Contracts (all modules, uniform rubric) | — | 7,777 |

### Implemented — tooling (`23`)

| # | Document | Topic |
|---|---|---|
| 23 | [Squeeze: the footprint policy language](23-squeeze-language.md) | The `.sqz` language, its compiler in `backend/squeeze/`, the storage/memory/model-weight footprint gate in CI |

~65,000 words. Each was written against the real Phase 1 code and migrations
rather than against the earlier design docs, which is why several of them report
corrections *to* those docs (§4).

---

## 2. Reading order by audience

| If you are… | Read |
|---|---|
| **Approving this specification** | `22` (this) → `11` → `12` → `19` §3 → `18` §2 |
| **Implementing Phase 1 wk 3–4 (ingest)** | `13` → `21` `training` → `19` 0014 |
| **Implementing Phase 2 (monetisation)** | `17` → `12` Phase 2 → `19` 0018 |
| **Implementing the AI coach** | `14` → `05` → `10` → `19` 0016 |
| **Building the mobile app** | `16` → `03` → `21` |
| **Reviewing a migration** | `19` §2 checklist → `19` §3 ledger row |
| **Changing retention, a codec or a storage budget** | `23` → `policies/footprint.sqz` → `19` §3 |
| **Reviewing security** | `18` → `06` → `21` per-module security blocks |
| **Planning capacity** | `20` → `10` |
| **New engineer, day one** | `00` → `01` → `11` → `21` |

---

## 3. Authority map — which document owns which decision

Ambiguity about ownership is how two documents come to disagree. These are
exclusive:

| Decision | Sole authority |
|---|---|
| **Migration numbers and sequencing** | **`19` §3.** Assigned nowhere else. Every other doc defers with "sequenced in `docs/19`". |
| Module boundaries and the dependency rule | `21`, refined by `11` §3 |
| Endpoint paths and conventions | `03`, extended per-module by `21` |
| Algorithm formulas and constants | The code in `backend/algorithms/`; `04` and `15` describe it |
| Threat model and controls | `06` (Phase 1) + `18` (Phase 2+) |
| Task sequencing and dates | `12` |
| Scaling triggers and thresholds | `20` |
| AI model parameters and pricing | `14`, checked against the `claude-api` skill |
| Money invariants | `17` §4 |
| Technology choices | `08` (ADRs) |

---

## 4. Corrections to approved documents

The authors were instructed to read the code as ground truth and to **record**
disagreements rather than silently fix approved artefacts. They found real ones.
Each needs a decision at approval time.

### 4.1 `docs/04` vs `backend/algorithms/` — nine findings (`15` §9)

The two that are behavioural, not editorial:

- **ACWR reliability is caller-dependent.** `is_reliable` counts keys in the loads
  mapping, and `daily_load_series` emits only training days — so 14 sessions across
  28 days yields `days_of_history = 14` and `is_reliable = False`. **The ACWR term
  therefore silently drops out of readiness and injury risk for any athlete who
  rests.** The test suite passes because its fixtures are calendar-dense. Needs the
  service-layer contract in `15` §1.2.
- **`generate_plan(request, profile)` ignores `profile` entirely**, and the goal
  does not currently influence the plan, despite the docstring claiming both.

Plus: `triathlon_prediction` returns no interval and hardcodes `confidence = 0.6`
(the one place the engine breaks its own "never a bare point estimate" rule);
`TrainingPhase.RACE` is unreachable; two taper edge cases; SWOLF never gets a
baseline; Coggan band boundaries rounded in `04` vs the code; two functions missing
from `__all__`.

### 4.2 `docs/05` and `docs/10` vs the Claude API (`14` §0)

Verified against the `claude-api` skill:

- **`effort` reduces *thinking* tokens, not visible prose.** `10` §4.2's "−30–50%
  output tokens" holds for thinking only; shortening answers needs an explicit
  conciseness instruction. Both bill as output, so keep both levers.
- **Disabling thinking is not the cheap route.** `thinking: {"type": "disabled"}`
  returns **400 above effort `high`** on Opus 5, and when disabled the model may
  emit a tool call as plain text that never runs. The cheap route is thinking **on**
  at `low`/`medium`.
- Opus 5 has a separate rate-limit bucket; `effort` errors on Haiku 4.5 (which
  still uses `budget_tokens` and a 4096-token cache minimum); the `speed: "fast"`
  variant at $10/$50 per MTok is rejected on cost.

### 4.3 `docs/02` §2 claims schema that does not exist

`coach_profiles`, `plan_products`, `plan_purchases` are listed as "schema only" but
are **absent** from migrations 0001–0010. `19` sequences them as **0029**; `02` §2
should be amended.

### 4.4 `pyproject.toml` vs `docs/01` §3

The import-linter `independence` contract forbids **all** imports between modules —
stricter than the prose "cross-module access goes through service only". A flow
spanning modules therefore has nowhere to live. `11` §3 recommends a resolution;
`21` §15 documents the gap. **This needs a decision before Phase 2 code exists.**

### 4.5 A discovered defect: `DELETE /v1/me` cannot work today

Two independent findings converge:

- Cascade referential actions fire `app.forbid_mutation()` on append-only tables,
  so hard-deleting a `users` row **fails** (`19` 0020).
- `wallets.user_id` is `ON DELETE RESTRICT`, so erasing an athlete who ever had a
  wallet fails on the foreign key (`17` §2.8).

Both must be fixed in the same reviewed erasure procedure. GDPR erasure is a
launch-blocking gate in `12`.

---

## 5. What is built, and what is specified

| Layer | Status |
|---|---|
| `backend/algorithms/` | **BUILT** — 209 tests, zero dependencies |
| `database/migrations/0001`–`0010` | **APPLIED** locally — 46 RLS tables, roles verified non-bypassing |
| `backend/core/`, `backend/database/` | **BUILT** |
| `backend/modules/identity/` | **BUILT** — register, login, refresh rotation with reuse detection, logout, consents, sessions |
| `backend/modules/training/` | **PARTIAL** — profile, goals, personal bests, zones |
| `backend/api/` | **PARTIAL** — health, auth, me |
| Everything else | **SPECIFIED, NOT BUILT** |

---

## 6. The approval decision

Approving this set means agreeing to:

1. **The modular monolith stands.** No working part is redesigned. Extraction is
   triggered by measurement (`20` §7), not by schedule.
2. **`19` is the migration authority.** 26 migrations planned, 0011–0036, each with
   a stated risk and reversibility. Reviewed SQL, applied by a human — never by the
   app, CI, or the deploy pipeline (ADR-012).
3. **The sequencing rules are binding** (`12` §5): data quality precedes rewards;
   labels precede models; isolation tests precede endpoint volume; Garmin approval
   starts immediately; store submission is a phase, not a step.
4. **The launch gate is a gate** (`12` §4), including the legal items: Garmin
   Developer Program approval, the Amendment 13 / DPO question, and points-to-cash
   counsel clearance before cash payout is enabled.
5. **Honesty stays a product feature.** Injury risk labelled unvalidated; a refusal
   to score readiness on thin data; predictions with intervals; `null` meaning
   unknown and never zero.

### Decisions needed at approval

| # | Decision | Doc | Why it cannot wait |
|---|---|---|---|
| 1 | Cross-module orchestration: relax the contract, or event bus? | `11` §3 | Phase 2 code shape depends on it |
| 2 | Fix or accept the nine `docs/04` findings | `15` §9 | Two are behavioural and affect live metrics |
| 3 | Cloud provider | `01` §14 | Managed-service choices follow |
| 4 | Amendment 13: DPO and database registration? | `18` §7 | Needs counsel; long lead time |
| 5 | Points-to-cash legality | `17` §5 | Gates payout; ledger already designed for either answer |
| 6 | ILS-only at launch (recommended) | `17` §4 | Removes an FX problem from Phase 4 |
| 7 | Store small-business programme enrolment (15% vs 30%) | `17` §7 | Materially changes margin in `10` |
| 8 | Attribution window per partner | `17` §7 | Before first partner onboarding |

---

## 7. Immediate next actions on approval

In order, with the first three parallelisable:

1. **Apply for the Garmin Developer Program.** Multi-week lead time; blocks Phase 1
   weeks 3–4. `12` §5 says week 1 regardless of what is being built — it is already
   later than that.
2. **Engage counsel** on Amendment 13 and points-to-cash.
3. **Decide cross-module orchestration** (`11` §3) — it shapes the first Phase 2
   commit.
4. **Write migrations 0011 and 0012** for review: the RLS exemption registry and
   append-only privilege hardening, then email verification. 0011 closes a real
   privilege gap and unblocks the CI check that keeps it closed.
5. **Finish Phase 1 weeks 3–4** per `13`: the arq worker process, the provider
   adapter interface, the mock Garmin adapter from recorded payloads (which
   unblocks ingest development while approval is pending), then webhook receivers.
