# 03 — API Design

**Status:** awaiting review · **Style:** REST/JSON over HTTPS · **Base:** `/v1`

REST first, as specified. The conventions below exist so that adding capability
later — more sports, a partner API, a coach marketplace — needs no breaking
change and no second API style.

---

## 1. Conventions

| Concern | Decision |
|---|---|
| **Versioning** | Path prefix `/v1`. A breaking change means `/v2` alongside, with an announced sunset for `/v1`. Additive fields are not breaking; clients must ignore unknown fields (enforced in the generated clients). |
| **Naming** | Plural, lower-kebab resource paths (`/v1/training-plans`); `snake_case` JSON bodies, matching Python and Postgres so nothing is renamed in three places. |
| **Errors** | RFC 9457 `application/problem+json`, always with a stable machine `code`. |
| **Pagination** | Opaque cursor (`?cursor=…&limit=…`), returning `{data, next_cursor}`. Never offset: an athlete syncing while new activities land would otherwise see duplicates and gaps. |
| **Filtering** | Explicit named params only (`?from=&to=&sport=`). No generic query DSL — it becomes an unauditable injection surface. |
| **Idempotency** | `Idempotency-Key` header **required** on every non-GET that creates money movement, a redemption, a payout, or an AI message. Stored 24 h; a replay returns the original response. |
| **Concurrency** | `ETag` + `If-Match` on mutable resources (profile, plan, preferences). A lost-update on threshold values silently corrupts every derived metric. |
| **Time** | ISO 8601 with offset on input; UTC `Z` on output. `local_date` fields are plain `YYYY-MM-DD`. |
| **Units** | SI in the wire format, always suffixed: `distance_m`, `duration_s`, `pace_s_per_km`, `weight_kg`. Display conversion is the client's job. Unsuffixed numeric fields are forbidden by review. |
| **Money** | `{"amount_minor": 1550, "currency": "ILS"}`. Never a decimal string, never a float. |
| **Nulls** | `null` means "unknown/not measured". It never means zero. This distinction is load-bearing throughout the analytics. |
| **Rate limits** | Per user and per route class; `RateLimit-*` response headers; `429` with `Retry-After`. |
| **Correlation** | `X-Request-Id` accepted and echoed; generated when absent; present in every log line and error body. |
| **Contract** | OpenAPI 3.1 generated from the FastAPI app; TS clients generated for web and mobile in CI. Hand-written API clients drift. |

### Error shape

```json
{
  "type": "https://api.aisportscoach.app/problems/insufficient-data",
  "title": "Not enough data to compute readiness",
  "status": 422,
  "code": "readiness_insufficient_data",
  "detail": "Readiness needs at least 7 days of HRV history; 3 days are available.",
  "instance": "/v1/metrics/readiness/today",
  "request_id": "01J9Z…",
  "meta": { "data_quality": 0.18, "required": 0.35 }
}
```

`code` is the contract; `title` and `detail` are human text and may be localised
or reworded without a version bump.

### Rate limit classes

| Class | Limit | Rationale |
|---|---|---|
| `auth` | 5 / min / IP + 10 / hour / account | Credential stuffing defence. Counted on failures, per account **and** per IP, so neither a distributed attack nor a single-IP one slips through. |
| `read` | 120 / min / user | Generous; dashboards poll. |
| `write` | 60 / min / user | |
| `ai` | tier quota (Free 5/month, Premium 100/month) + 10 / min burst | The quota is the cost control; the burst limit stops one client looping. |
| `webhook` | 1000 / min / provider | Providers batch; being too strict here loses data. |

---

## 2. Authentication

```
POST /v1/auth/register              → 201 {user, tokens}
POST /v1/auth/login                 → 200 {user, tokens} | 401 | 423 locked
POST /v1/auth/refresh               → 200 {tokens}       (rotates; reuse revokes family)
POST /v1/auth/logout                → 204                (revokes this family)
POST /v1/auth/logout-all            → 204                (revokes every family)
POST /v1/auth/password/forgot       → 202                (always 202 — see below)
POST /v1/auth/password/reset        → 204
POST /v1/auth/email/verify          → 204
POST /v1/auth/mfa/enroll            → 200 {otpauth_uri}  (Phase 2)
POST /v1/auth/mfa/verify            → 204
GET  /v1/auth/sessions              → 200 [{device, ip, last_seen}]
DELETE /v1/auth/sessions/{id}       → 204
```

`/password/forgot` always returns `202` whether or not the address exists —
differentiating turns the endpoint into an account-enumeration oracle. Same
reason `/register` returns a generic conflict without confirming the address.

Token shapes: access JWT (ES256, 10 min, `sub`/`role`/`org`/`entitlements_hash`),
refresh opaque (32 random bytes, 30 days, rotating). Mobile stores the refresh
token in Keychain/Keystore; web receives it as an `HttpOnly; Secure;
SameSite=Strict` cookie and must send `X-CSRF-Token` matching the cookie's paired
value on every non-GET.

---

## 3. Profile and consent

```
GET   /v1/me                        → user, tier, entitlements, flags
PATCH /v1/me                        → display name, locale, timezone
GET   /v1/me/profile                → athlete profile incl. thresholds + sources
PUT   /v1/me/profile                → If-Match required
GET   /v1/me/zones                  → computed HR/power/pace zones + anchor used
GET   /v1/me/goals                  |  POST /v1/me/goals
PATCH /v1/me/goals/{id}             |  DELETE /v1/me/goals/{id}
GET   /v1/me/personal-bests         |  POST /v1/me/personal-bests
GET   /v1/me/consents               |  PUT /v1/me/consents/{purpose}
GET   /v1/me/data-access            → grants this athlete has issued
POST  /v1/me/data-access            → issue a scoped, expiring grant
DELETE /v1/me/data-access/{id}      → revoke immediately
POST  /v1/me/export                 → 202 {job_id}  (GDPR portability)
GET   /v1/me/export/{job_id}        → 200 {status, download_url}
DELETE /v1/me                       → 202  (GDPR erasure; see §11)
```

`GET /v1/me/zones` returns the **anchor** used (`lthr`, `hr_max`, `ftp`,
`threshold_pace`) alongside the bands. An athlete shown zones derived from an
age-estimated HRmax must be able to see that, or they will train to a number that
is a guess.

---

## 4. Integrations

```
GET    /v1/integrations                          → connected providers + last sync
POST   /v1/integrations/{provider}/authorize     → 200 {authorize_url, state}
POST   /v1/integrations/{provider}/callback      → 201 {connection}
POST   /v1/integrations/{provider}/sync          → 202 {job_id}
POST   /v1/integrations/{provider}/backfill      → 202 {job_id, window}
DELETE /v1/integrations/{provider}               → 204 (revokes tokens both sides)
GET    /v1/integrations/jobs/{job_id}            → job status
```

Webhooks — unauthenticated by session, verified by provider signature, always
answered within 100 ms (see `01-architecture.md` §4.2):

```
POST /v1/webhooks/garmin/dailies
POST /v1/webhooks/garmin/activities
POST /v1/webhooks/garmin/deregistration
POST /v1/webhooks/apple-iap
POST /v1/webhooks/google-play
POST /v1/webhooks/paypal
```

Every webhook: verify signature → persist raw → enqueue → `200`. An unverified
signature is persisted and **never** acted upon.

---

## 5. Activities

```
GET    /v1/activities?from=&to=&sport=&cursor=&limit=   → cursor page
GET    /v1/activities/{id}                              → full summary + metrics
GET    /v1/activities/{id}/streams?channels=hr,power    → signed object URL
GET    /v1/activities/{id}/laps
POST   /v1/activities                                   → manual entry
PATCH  /v1/activities/{id}                              → rpe, notes, sport
DELETE /v1/activities/{id}                              → soft delete
GET    /v1/activities/{id}/efficiency                   → metrics + baseline deltas
```

Each activity carries its `training_load` **with `load_source` and
`load_confidence`**. A client rendering a load number without showing that it came
from RPE rather than power is misrepresenting it.

---

## 6. Metrics

```
GET /v1/metrics/readiness/today          → score, band, ordered drivers, data_quality
GET /v1/metrics/readiness?from=&to=
GET /v1/metrics/training-load?from=&to=  → daily load, CTL, ATL, TSB
GET /v1/metrics/acwr                     → ratio, zone, reliability flag
GET /v1/metrics/injury-risk              → probability, band, drivers, model_version
GET /v1/metrics/efficiency?sport=&from=&to=
GET /v1/metrics/predictions?distance=    → equivalent times, both models, interval
GET /v1/metrics/summary                  → the dashboard payload, one request
```

Two hard rules on this router, both enforced in tests:

1. **Every score ships with its explanation.** `readiness` returns the ordered
   driver array; `injury-risk` returns drivers plus `model_version` and
   `is_clinically_validated: false`. There is no endpoint that returns a bare
   number.
2. **Below `data_quality` 0.35 the endpoint returns `422
   readiness_insufficient_data`** rather than a confident-looking score. Advice
   built on one night of data is worse than no advice.

`GET /v1/metrics/summary` exists because the mobile dashboard needs seven things
at once and seven round trips on a phone network is the difference between a
fast app and a slow one.

---

## 7. AI coach

```
GET  /v1/coach/conversations                        → list
POST /v1/coach/conversations                        → create
GET  /v1/coach/conversations/{id}/messages          → history
POST /v1/coach/conversations/{id}/messages          → SSE stream (Idempotency-Key)
POST /v1/coach/messages/{id}/feedback               → thumbs + reason
GET  /v1/coach/quota                                → used, limit, resets_at
GET  /v1/coach/weekly-review                        → latest deep review
GET  /v1/coach/twin                                 → digital twin + confidence
```

The message endpoint streams `text/event-stream`:

```
event: routing   data: {"route":"llm_chat","model":"claude-opus-5"}
event: delta     data: {"text":"Your readiness is 48 this morning"}
event: citation  data: {"metric":"hrv","value":42.1,"baseline":58.4,"day":"2026-07-28"}
event: done      data: {"message_id":"…","tokens":{"in":5480,"out":612},"cost_micro_usd":31000}
```

`citation` events are what make the answer checkable: every number the model
states is emitted with the metric and day it came from, so the client can link it
to the chart. A `429` with `Retry-After` and a `quota` problem code is returned
when the tier limit is spent — the request never silently degrades to a worse
model without telling the client which model answered.

---

## 8. Training plans

```
GET   /v1/plans/current                     → active plan
POST  /v1/plans                             → generate (goal, weeks, availability)
GET   /v1/plans/{id}                        → weeks + sessions
POST  /v1/plans/{id}/activate               → supersedes any active plan
GET   /v1/plans/today                       → today's session AFTER adaptation
POST  /v1/plans/sessions/{id}/complete      → link an activity
POST  /v1/plans/sessions/{id}/skip
GET   /v1/plans/{id}/compliance             → planned vs actual load per week
GET   /v1/plans/adaptations?from=&to=       → the audit trail of daily decisions
```

`GET /v1/plans/today` returns the **adapted** session plus `action` and `reason`.
The client never applies the gating rules itself: the decision has to be recorded
server-side for explainability and evaluation, and two implementations of the same
rules would diverge.

---

## 9. Subscription, wallet, partners

```
GET  /v1/subscription                         → plan, status, period end
GET  /v1/subscription/plans                   → catalogue
POST /v1/subscription/receipts/apple          → validate server-side, grant
POST /v1/subscription/receipts/google         → validate server-side, grant
POST /v1/subscription/paypal/agreement        → start PayPal flow
POST /v1/subscription/cancel                  → cancel at period end
GET  /v1/entitlements                         → resolved feature access

GET  /v1/wallet                               → balances by account
GET  /v1/wallet/transactions?cursor=          → ledger history, human-readable
GET  /v1/wallet/rewards                       → earn rules and current policy
POST /v1/wallet/payouts                       → request (Idempotency-Key)
GET  /v1/wallet/payouts                       → history + status

GET  /v1/partners                             → active partners
GET  /v1/partners/{slug}/offers               → offers available to this athlete
POST /v1/partners/offers/{id}/redeem          → voucher (Idempotency-Key)
```

Receipt endpoints validate against the store server-side and return the resolved
entitlements. **A client-supplied "I am premium" claim is never trusted** — that
is the single most common IAP bypass.

`GET /v1/wallet/transactions` returns the ledger in athlete-readable form: what
happened, why, and which policy version applied. A balance the athlete cannot
explain is a support ticket.

---

## 10. Community, coach, partner, admin

```
GET/POST     /v1/groups            · /v1/groups/{id}/members
GET/POST     /v1/challenges        · /v1/challenges/{id}/join · /leaderboard
GET          /v1/achievements
POST         /v1/activities/{id}/share

# Coach — every route requires a grant and writes an audit event
GET /v1/coach-portal/athletes            → athletes who granted access
GET /v1/coach-portal/athletes/{id}/...   → only within granted scopes

# Partner — separate scope, no athlete health data, ever
GET  /v1/partner-portal/offers  · POST /v1/partner-portal/conversions
GET  /v1/partner-portal/reports

# Admin — separate audience, MFA required, every action audited
GET  /v1/admin/users · /v1/admin/subscriptions · /v1/admin/revenue
GET  /v1/admin/payouts/queue · POST /v1/admin/payouts/{id}/decide
GET  /v1/admin/data-quality/review · /v1/admin/fraud/signals
GET  /v1/admin/algorithms · POST /v1/admin/algorithms/{name}/activate
```

The partner surface returns **aggregate and conversion data only**. There is no
partner route that can return an athlete's health data — enforced by keeping
partner routes on a router with no dependency that can resolve a health resource,
and tested explicitly.

---

## 11. GDPR endpoints

```
POST   /v1/me/export     → 202, async; delivers a signed archive of everything
DELETE /v1/me            → 202, async; 30-day grace, then erasure
GET    /v1/me/deletion   → status, scheduled_at, what is retained and why
```

`GET /v1/me/deletion` tells the athlete plainly which records survive erasure
(financial ledger, audit trail) and the legal basis. Silently retaining data
after a deletion request is the failure mode this endpoint prevents.

---

## 12. Health and meta

```
GET /healthz     → 200, no dependencies (liveness)
GET /readyz      → 200/503, checks Postgres + Redis (readiness)
GET /v1/meta     → API version, engine version, min supported client version
```

`min_supported_client_version` lets the server tell an old mobile build to force
an update — necessary once analytics semantics change, because a stale client
would render new fields wrongly.

---

## 13. Open questions

1. **SSE vs WebSocket for coach chat.** SSE is simpler, works through more
   proxies, and is sufficient for one-way streaming. WebSocket only becomes
   necessary if we add live coaching sessions. Recommendation: SSE.
2. **Do we expose raw streams to clients at all in MVP?** Signed object URLs are
   cheap, but chart rendering from a 3-hour ride stream on a phone is not.
   Recommendation: server-side downsampled series endpoint instead.
3. **Public partner API** (Phase 5) — separate OAuth2 client-credentials surface
   at `/partner/v1`, deliberately not the same API with a different token.
