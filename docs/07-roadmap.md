# 07 — Development Roadmap

**Status:** awaiting review

Sequenced so that each phase ends with something releasable and something learned.
Estimates assume **two engineers** (one backend/AI, one mobile/web) plus design
support, and are in calendar weeks including test and review time.

**Phase 0 is done.** Everything from Phase 1 onward is blocked on approval of
`01-architecture.md`, `02-database-design.md`, `03-api-design.md` and
`06-security-privacy.md`.

---

## Phase 0 — Foundations ✅ complete

| # | Task | State |
|---|---|---|
| 0.1 | Product analysis and architecture plan | ✅ `docs/01-architecture.md` |
| 0.2 | Database design + reviewable SQL for every module | ✅ `docs/02-database-design.md`, `database/migrations/0001–0009` |
| 0.3 | API design | ✅ `docs/03-api-design.md` |
| 0.4 | Security review | ✅ `docs/06-security-privacy.md` |
| 0.5 | **Analytics engine** — training load, readiness, efficiency, injury risk, performance prediction, plan generation and adaptation | ✅ `backend/algorithms/`, **209 tests passing** |
| 0.6 | Repository scaffold, folder structure, CI, tooling | ✅ |

The analytics engine was built first deliberately: it is the product's core IP, it
is pure functions with no dependencies, and it is the layer least likely to change
once correct. It is also the only layer that could be **verified** before the
architecture review — which is why the code that exists is exactly the code that
is already tested.

---

## Phase 1 — MVP (weeks 1–9)

**Goal:** an athlete connects Garmin, sees a readiness score they trust, asks the
coach why, and gets a plan that adapts. Nothing else.

### Week 1–2 · Skeleton and identity
| # | Task | Est. | Depends on |
|---|---|---|---|
| 1.1 | FastAPI app, settings, structured logging, error handling, `/healthz` `/readyz` | 2d | approval |
| 1.2 | SQLAlchemy models + repositories mirroring `0002`–`0003`; RLS session context wiring | 3d | 1.1 |
| 1.3 | Registration, login, refresh rotation with reuse detection, logout | 3d | 1.2 |
| 1.4 | Consent capture; athlete profile CRUD; goals | 2d | 1.3 |
| 1.5 | **Tenant isolation test matrix** + RLS enforcement tests | 2d | 1.2 |

> 1.5 is in Phase 1 week 2, not later. Retrofitting isolation tests after twenty
> endpoints exist means auditing twenty endpoints instead of two.

### Week 3–4 · Garmin ingest
| # | Task | Est. | Notes |
|---|---|---|---|
| 1.6 | Provider adapter interface + Garmin implementation behind it | 3d | Core code must not import Garmin directly |
| 1.7 | OAuth connect/disconnect; encrypted token storage with KMS envelope | 3d | |
| 1.8 | Webhook receivers → raw persist → enqueue; <100 ms response | 2d | |
| 1.9 | Ingest worker: fetch, normalise, upsert, idempotency; backfill chunking | 4d | |
| 1.10 | **Data-quality checks** on ingest; `data_quality_flags`; trust scoring | 3d | Must precede rewards, not follow |

> **Dependency risk:** Garmin Developer Program approval has a multi-week lead
> time. Apply in week 1. A mock provider fixture (recorded payloads) unblocks
> 1.9–1.10 in the meantime, and doubles as the contract test.

### Week 5–6 · Analytics service and dashboard
| # | Task | Est. |
|---|---|---|
| 1.11 | Analytics worker: wire `backend/algorithms` to `daily_metrics`; coalesced recompute | 4d |
| 1.12 | Metrics API with drivers, `data_quality` gating, `422` below threshold | 3d |
| 1.13 | Redis caching + invalidation | 2d |
| 1.14 | Mobile: onboarding, profile, Garmin connect, dashboard | 6d |
| 1.15 | Web: same, plus deeper charts | 4d |

### Week 7–8 · AI coach and plans
| # | Task | Est. |
|---|---|---|
| 1.16 | Context packet builder (derived metrics only) + prompt-cache layout | 3d |
| 1.17 | Provider-agnostic LLM client; Anthropic implementation; streaming | 3d |
| 1.18 | Router: deterministic answers vs model; read-only tool surface | 3d |
| 1.19 | Guardrails: injection delimiting, medical red flags, numeric grounding | 2d |
| 1.20 | Quota + cost accounting per message | 2d |
| 1.21 | Plan generation + daily adaptation endpoints (engine already built) | 2d |
| 1.22 | Coach chat UI (mobile + web) | 5d |
| 1.23 | **AI eval suite** — 40 golden cases, grounding + safety checks | 3d |

### Week 9 · Hardening and closed beta
| # | Task | Est. |
|---|---|---|
| 1.24 | Load test the ingest path; queue backpressure | 2d |
| 1.25 | Security test suite complete; dependency audit | 2d |
| 1.26 | Backup restore rehearsal | 1d |
| 1.27 | Observability: dashboards, alerts, runbooks | 2d |
| 1.28 | Closed beta with 20–50 athletes | — |

**Phase 1 exit criteria** — all must hold:
- [ ] An athlete completes connect → activity appears → readiness with drivers → coach answers with cited numbers → plan adapts, with no manual intervention.
- [ ] Isolation matrix green for every endpoint; RLS verification queries clean.
- [ ] p95 dashboard read < 150 ms; ingest webhook p99 < 100 ms.
- [ ] AI cost per active athlete measured against the §10 budget.
- [ ] Eval suite passing; zero ungrounded numbers in the golden set.
- [ ] Backup restored successfully in a rehearsal, with the time recorded.

---

## Phase 2 — Monetisation and retention (weeks 10–15)

| # | Task | Est. | Notes |
|---|---|---|---|
| 2.1 | Subscription plans, entitlements resolution, feature gating | 4d | |
| 2.2 | Apple IAP + Google Play: server-side receipt validation, webhooks, reconciliation | 6d | Store review is itself a lead time |
| 2.3 | PayPal subscriptions (web) | 4d | |
| 2.4 | Notifications: device tokens, preferences, quiet hours, daily readiness push | 4d | Retention lever |
| 2.5 | Advanced sensor analyzers as gated features (engine already built) | 4d | |
| 2.6 | Weekly deep review via Batch API (50% cheaper, overnight) | 3d | |
| 2.7 | MFA for athletes; mandatory for staff | 2d | |
| 2.8 | Adaptive UI: beginner vs advanced information density | 4d | |
| 2.9 | Product analytics: activation, retention, AI usage, conversion | 3d | |
| 2.10 | **Store submission**: privacy manifests, health-data disclosures, review prep | 5d | Apple health-data review is strict; budget for a rejection round |

**Exit:** paying subscribers; both stores live; conversion measurable.

---

## Phase 3 — The learning coach (weeks 16–23)

This is where the product becomes defensible.

| # | Task | Est. | Notes |
|---|---|---|---|
| 3.1 | Athlete Digital Twin v1: personalised recovery half-life, load tolerance, fitted fatigue exponent | 6d | Per-athlete parameters replace population defaults |
| 3.2 | Algorithm version registry + experiment assignment wired end to end | 4d | `algorithm_versions`, `experiment_assignments` |
| 3.3 | Prediction accuracy loop: record every prediction, score at horizon, report calibration | 4d | Turns "our predictions are good" into a number |
| 3.4 | Injury-label collection: in-app injury and pain reporting | 3d | **Without labels there is no model to train.** Must ship before 3.5. |
| 3.5 | Injury risk v1: scikit-learn model trained on collected labels, versus the heuristic baseline | 6d | Ships only if it beats `heuristic-v0` on held-out data |
| 3.6 | Performance prediction refinement using per-athlete history | 4d | |
| 3.7 | Continuous improvement: feedback → prompt/algorithm iteration with eval gates | 4d | |
| 3.8 | Efficiency trend analytics and weakness detection | 4d | |

**Exit:** measured evidence that athletes using the coach improve — the metric the
whole product is judged on (`docs/09` §6).

---

## Phase 4 — Club, rewards and community (weeks 24–33)

| # | Task | Est. | Notes |
|---|---|---|---|
| 4.1 | Wallet + double-entry ledger + reward policy engine | 6d | Ledger correctness tests first |
| 4.2 | Reward earning rules; fraud gating on trust score | 4d | Depends on 1.10 |
| 4.3 | Groups, memberships, activity sharing with privacy constraints | 5d | |
| 4.4 | Challenges and leaderboards, incl. sponsored | 5d | |
| 4.5 | Partner accounts, offers, redemption vouchers | 6d | |
| 4.6 | Partner portal (web) with aggregate reporting only | 5d | No health data reachable |
| 4.7 | Conversion attribution and commission accounting | 4d | |
| 4.8 | Payout: eligibility gates, admin approval queue, PayPal Payouts | 6d | **Legal sign-off required before enabling** |
| 4.9 | Admin panel: users, revenue, payouts, fraud, data-quality review | 6d | |

**Blocking dependency:** points-to-cash conversion has consumer-protection and tax
implications in Israel. Counsel must clear 4.8 before it is enabled; the ledger is
designed so partner-credit-only can ship first and cash payout later.

---

## Phase 5 — Platform (weeks 34+)

| # | Task | Notes |
|---|---|---|
| 5.1 | Coach portal and coach marketplace (plan products, purchases, commission) | Schema already exists |
| 5.2 | Additional providers: Apple Health, Coros, Polar, Suunto | Adapter interface already exists |
| 5.3 | Public partner API (`/partner/v1`, OAuth2 client credentials) | Separate surface, not the athlete API |
| 5.4 | Service extraction: `training`, then `coaching` | Trigger is measured, not scheduled (`01` §9) |
| 5.5 | Table partitioning migration | Trigger: ~50M rows |
| 5.6 | Desktop app (Tauri) if web is insufficient | Deferred deliberately |

---

## Sequencing rules

1. **Data quality precedes rewards.** Paying for workouts before trust scoring
   exists is paying for fabricated workouts.
2. **Labels precede models.** Injury-report collection (3.4) ships before the
   trained model (3.5). Everything before that is an honestly-labelled heuristic.
3. **Isolation tests precede endpoint volume.** Week 2, not week 20.
4. **Store submission is a phase, not a step.** Health-data review is strict;
   assume one rejection round.
5. **Garmin approval starts week 1** regardless of what is being built.

## Team

| Phase | Backend/AI | Mobile/Web | Other |
|---|---|---|---|
| 1 | 1.0 | 1.0 | design 0.3 |
| 2 | 1.0 | 1.0 | design 0.3 |
| 3 | 1.5 (add data science) | 0.5 | — |
| 4 | 1.5 | 1.0 | +legal, +partnerships (non-engineering) |

## Top risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Garmin approval delayed | medium | blocks Phase 1 | Apply week 1; mock adapter with recorded payloads unblocks development |
| AI cost exceeds the margin at 15 ₪ | **high** | breaks unit economics | Measured from day one; routing, caching, quotas and Batch API all designed in (`docs/10`) |
| Store rejection over health data | medium | delays launch | Privacy manifests and disclosures treated as a task, not an afterthought |
| Injury model never beats the heuristic | medium | a headline feature stays a heuristic | Acceptable and honest — it is already labelled unvalidated; do not ship a model that is not better |
| Reward economics abused | medium | direct loss | Trust scoring, holds, human payout approval |
| Points-to-cash blocked by law | medium | Phase 4 scope cut | Partner-credit-only path ships independently |
