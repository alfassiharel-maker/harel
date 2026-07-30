# 16 — Mobile Application Architecture

**Status:** awaiting review · **Scope:** `apps/mobile` (iOS + Android) · **Extends:**
`apps/README.md`, `docs/03-api-design.md` §2, `docs/06-security-privacy.md` §2/§5,
`docs/07-roadmap.md` 1.14 / 1.22 / 2.4 / 2.8 / 2.10

The mobile app is the athlete's primary surface and it computes **nothing**. It
renders decisions the server has already made and recorded, captures subjective
input, and survives having no signal. Everything below follows from those three
sentences.

---

## 1. Stack and the Expo-vs-bare decision

**Verdict: the `apps/README.md` recommendation is CONFIRMED — Expo SDK with a
development client and EAS Build.** Not Expo Go: we need native modules from week
one, and Expo Go cannot load them.

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Runtime | React Native (New Architecture: Fabric + TurboModules), Hermes | Fixed by the brief; shares TypeScript and `packages/shared` with web. Flutter would fork the domain layer; a WebView shell cannot hold a 60 fps chart or a HealthKit entitlement. |
| Tooling | Expo SDK + development client + EAS Build/Submit/Update | One mobile engineer (roadmap §Team). Expo owns the build matrix, signing, and the OTA channel — work we would otherwise pay for in weeks we do not have. |
| Navigation | React Navigation (native stack + bottom tabs), one typed linking config | Deep-link validation needs a single parse point (§7). |
| Server state | TanStack Query v5 | §3 |
| Client state | Redux Toolkit | §3 |
| Local durability | `expo-sqlite` (outbox), encrypted MMKV (query cache) | §4 |
| Charts | Skia-backed (Victory Native XL or a thin custom Skia renderer) | SVG-per-point charts miss the §9 budget by an order of magnitude on a 90-day, 3-series chart. |
| i18n | `i18next` + `expo-localization`, `he-IL` as the **development** locale | §5 |
| Secrets | `expo-secure-store` | §7 |

The two native requirements named in `apps/README.md` are both config-plugin
territory, and both are worth restating because they are the whole decision:

* **Garmin OAuth needs a custom scheme + a verified callback.** `app.config.ts`
  declares `scheme`, `ios.associatedDomains` and Android `intentFilters`; the flow
  itself runs in `ASWebAuthenticationSession` / Custom Tabs (§7). Plugin-shaped.
* **Health-data background sync needs native modules.** HealthKit / Health Connect
  bridges plus `expo-task-manager` + `expo-background-task` (BGAppRefreshTask /
  WorkManager). Entitlements, `Info.plist` strings and manifest permissions are all
  declarative. Plugin-shaped.

`expo prebuild` is the escape hatch, which is what makes this reversible and
therefore not worth agonising over — but "reversible" needs a trigger, or it
becomes a decision nobody ever revisits:

> **Reversal trigger.** We commit `ios/` and `android/` (prebuild, bare workflow)
> the first time a required native capability cannot be expressed as a config
> plugin *and* patching the plugin is not viable inside one sprint. Concretely:
> (a) a sensor SDK that ships as a binary framework needing a hand-written Podspec
> or a Gradle variant; (b) an `AppDelegate`/`Application` lifecycle hook no plugin
> exposes (HealthKit background delivery is the likely candidate); or (c) two
> consecutive releases blocked on a patched plugin breaking against an Expo SDK
> upgrade. Any one of those, and we prebuild — it is a tooling migration, not a
> rewrite, and nothing in §2–§12 changes.

---

## 2. Project structure

Extends the tree in `apps/README.md`; nothing there is renamed.

```
apps/mobile/
├── app.config.ts             # the entire native surface: scheme, plugins,
│                             # infoPlist strings, permissions, pins. Reviewed as code.
├── eas.json                  # channels: development · preview · production
├── src/
│   ├── navigation/           # root switch, auth stack, tabs, typed linking config
│   ├── screens/              # one folder per screen (§5)
│   ├── components/
│   │   ├── primitives/       # RTL-aware Text/Stack/Card — logical props only
│   │   ├── charts/           # thin wrappers over packages/shared/charts
│   │   └── metric/           # MetricTile, DriverList, ProvenanceBadge, UnvalidatedTag
│   ├── services/
│   │   ├── api/              # generated client + fetch wrapper (auth, X-Request-Id, retry)
│   │   ├── auth/             # token store, single-flight refresh, biometric gate
│   │   ├── outbox/           # durable mutation queue (§4)
│   │   ├── health/           # HealthKit / Health Connect delta reads
│   │   ├── push/             # token registration, channels, handlers
│   │   └── telemetry/        # crash + product analytics, PII deny-list
│   ├── store/
│   │   ├── query/            # QueryClient, key factory, persister
│   │   └── slices/           # session · ui · density · drafts · outboxStatus
│   ├── i18n/                 # he (default), en
│   └── domain/               # re-exports packages/shared/domain — nothing local
└── e2e/                      # Maestro flows + Detox specs
```

```
packages/shared/
├── api-types/    # GENERATED from OpenAPI 3.1 in CI — never hand-written
├── domain/       # sport/zone/band/load_source enums + ALL unit formatting
├── charts/       # axis, zone palette, band colours
└── mocks/        # GENERATED from the same schema (§12)
```

**Why unit formatting lives in exactly one place.** The wire format is SI and
suffixed (`docs/03` §1); turning `pace_s_per_km: 271.4` into text is a chain of
*lossy* decisions — rounding, unit system, and what to print when the value is
`null`. Two implementations disagree at the rounding boundary and the athlete sees
`4:31` on the phone and `4:30` on the web for the same run, which is
indistinguishable from a data bug and costs the same support time. The `null` case
is worse: if "unknown → `—`" is implemented twice, one copy will eventually print
`0`, which is the precise failure the analytics engine is built to refuse
(`docs/04`, CLAUDE.md). So formatting is a set of pure functions in
`packages/shared/domain` with their own tests, and an ESLint
`no-restricted-syntax` rule forbids arithmetic on any `_m` / `_s` / `_kg` /
`_per_km` field outside that package. Same argument, same package, for the
bidi-isolation wrapping in §5.

---

## 3. State and data layer

The line is drawn by ownership, not by convenience:

* **TanStack Query owns everything that has a `/v1` URL.** Summary, readiness,
  load, activities, plan, zones, entitlements, quota, wallet, groups. It is a
  *cache*: it has a fetch timestamp, it can be stale, it can be invalidated.
* **Redux Toolkit owns only what has no server representation.** Auth status and
  biometric-lock state, theme/locale/density override, half-filled form drafts,
  outbox status counters. Nothing in Redux is a health value.

**Why mixing them produces stale-metric bugs.** Copy a server number into Redux
and it acquires a second lifetime with no invalidation path. Query knows readiness
was fetched at 06:12 and can mark it stale; the Redux copy cannot, so after an
activity lands and the analytics worker recomputes, the screen keeps rendering
yesterday's score under today's date with no staleness cue. This product makes it
worse than usual: readiness ships as `{score, band, drivers[], data_quality}` and
the drivers must reconcile to the score — `docs/09` §2 tests exactly that on the
server. A score cached in Redux and drivers fetched fresh from Query produce an
explanation that does not add up to the number, which is the one failure mode that
destroys trust in an explainable-metrics product. **Rule: no server-derived number
is ever written into a Redux slice.** Reviewed, and lint-checked on `slices/`.

| Query key | Endpoint | `staleTime` | Invalidated by |
|---|---|---|---|
| `['summary']` | `GET /v1/metrics/summary` | 5 min | ingest push · wellness write · profile write |
| `['readiness','today']` | `GET /v1/metrics/readiness/today` | 5 min | same |
| `['metrics','load',{from,to}]` | `GET /v1/metrics/training-load` | 30 min | ingest push |
| `['metrics','acwr']` · `['metrics','injury-risk']` | as `docs/03` §6 | 30 min | ingest push |
| `['activities',{filters}]` (infinite) | `GET /v1/activities` | 2 min | manual create · ingest push |
| `['activity',id]` | `GET /v1/activities/{id}` | 1 h | `PATCH` of that id |
| `['plan','today']` | `GET /v1/plans/today` | 15 min | complete/skip · adaptation push · local midnight |
| `['me','zones']` · `['me','profile']` | `GET /v1/me/zones`, `/profile` | 24 h | `PUT /v1/me/profile` |
| `['entitlements']` | `GET /v1/entitlements` | 60 s, always on foreground | receipt validated · billing push |
| `['coach','quota']` | `GET /v1/coach/quota` | 60 s | every stream `done` event |
| `['coach','messages',id]` | conversation history | `Infinity` | own send (append-only) |

**Invalidation on new data.** One helper, `invalidateAfterIngest()`, invalidates
summary, readiness, load, ACWR, injury risk, plan/today and the activities list —
a single list so a new metric screen cannot be forgotten. Recompute is
asynchronous (`docs/01` §4.2), so the client must **not** assume the numbers are
ready when the push arrives: it refetches summary, reads `computed_at`, and if the
recompute is still behind renders an explicit *updating* state for at most three
bounded retries. It never renders a stale number as if it were fresh.

**Background refetch.** `refetchOnReconnect: true` (NetInfo) — the same event that
flushes the outbox. Focus refetch is wired to `AppState → active` but throttled by
`staleTime`, so pocket-checking the phone does not hammer the API. No polling
anywhere except the bounded post-ingest loop. Retries are exponential with jitter
on network/5xx/429 (honouring `Retry-After`) — and mutations are retried *only*
with their original `Idempotency-Key` (§4). The query cache is persisted to
encrypted MMKV with a 7-day `maxAge`, **excluding `['entitlements']` and
`['coach','quota']`**: a persisted entitlement is a client-side entitlement claim,
and `docs/06` §6 forbids trusting one.

---

## 4. Offline-first behaviour

The design case is an athlete standing in a field at 06:10 with one bar and no
data. Offline is the normal path, not an error state.

### Readable offline (from the persisted cache, always with provenance)

| Available | Staleness rule |
|---|---|
| Last dashboard summary | Rendered with an "as of HH:mm" label. **A readiness score older than 36 h is not shown as a score at all** — it collapses to "no reading for today". |
| Today's and this week's plan | Sessions are safe to show stale; the *adaptation* is not. If the cached `plans/today` predates the current local date, show the planned session and suppress the adaptation banner. |
| Zones, thresholds, profile, goals | Change rarely; safe. Anchor source shown as always. |
| Activities list pages and any activity detail already opened | Safe — they are historical facts. |
| Coach conversation history | Append-only; safe. |
| **Not offline:** new coach answers, quota, entitlements, injury-risk recompute | Explicit offline state. Never a guess. |

The 36-hour rule is the client-side mirror of the server rule in `docs/03` §6
(below `data_quality` 0.35, return `422` rather than a confident-looking score).
Yesterday's readiness shown without a date is how an athlete trains hard on a day
the system would have told them to rest.

### Queued for later — the outbox

```ts
type OutboxRow = {
  id: string;                 // UUIDv7, also the row's ordering key
  kind: 'rpe' | 'wellness' | 'manual_activity' | 'session_result' | 'injury_report';
  method: 'POST' | 'PATCH' | 'PUT'; path: string; body: unknown;
  idempotency_key: string;    // generated ONCE, on save. Never regenerated.
  if_match?: string;          // ETag captured when the athlete started editing
  attempts: number; next_attempt_at: string;
  state: 'pending' | 'inflight' | 'conflict' | 'needs_attention';
};
```

Stored in `expo-sqlite`, not MMKV, because a flush interrupted by an OS kill must
leave the queue transactionally consistent.

| Mutation | Endpoint | Idempotency key | Conflict rule |
|---|---|---|---|
| RPE / notes | `PATCH /v1/activities/{id}` | UUIDv7 | `If-Match`; on `412` keep the server version, show both, athlete decides |
| Subjective wellness | `PUT /v1/wellness/{local_date}` *(API delta, §11)* | natural: `wellness:{local_date}` — lets the queue coalesce edits of the same day | last-write-wins is correct: the athlete is the only author |
| Manual activity | `POST /v1/activities` | UUIDv7, minted when the form opens | server dedupes on the key; a replay returns the original `201` |
| Session complete / skip | `POST /v1/plans/sessions/{id}/complete|skip` | natural: session id + action | idempotent by construction |
| Injury / pain report | `POST /v1/injuries` *(PLANNED Phase 3, roadmap 3.4)* | UUIDv7 | append-only, never coalesced |
| **Consent change** | `PUT /v1/me/consents/{purpose}` | — | **never queued.** `identity.consents` is an append-only ledger recording timestamp, IP and `document_version`; a consent replayed six hours later records an act we cannot vouch for. Blocked offline, with the reason shown. |
| **AI message** | `POST /v1/coach/conversations/{id}/messages` | — | **never queued.** It is billed, it needs the athlete present to read a streamed answer, and by flush time the state it asked about has changed. |

Flush rules: single-flight; strict FIFO *per resource* (an RPE edit cannot land
before the activity it edits) and parallel across resources. Retry only on network
error, `5xx`, `408` and `429` (respecting `Retry-After`), exponential with jitter,
capped around 6 h. **Any other `4xx` is terminal** — retrying a rejected body
forever burns battery and hides the failure; the row moves to `needs_attention`
and appears in a visible "not synced" list. Nothing is ever silently dropped.
`409` from the idempotency store counts as success. `412` is a conflict, not a
failure: fetch, present both, let the athlete choose — health data is never
auto-merged. Triggers: app foreground, NetInfo reconnect, successful token
refresh, and a periodic `expo-background-task`. Bounded at 500 rows / 5 MB; on
overflow new offline writes are **refused with a message** rather than evicting an
unsent injury report.

Offline must not become a backdating channel: a queued row carries `local_date`,
the device capture time, and `captured_offline: true`; the server records its own
receipt time alongside the claim, so the ingest data-quality checks (roadmap 1.10)
can flag implausible backfills instead of trusting the phone's clock.

### Never trusted from the device

| Never trusted | Where truth lives |
|---|---|
| Tier / entitlement / "I am premium" | `GET /v1/entitlements`, server-resolved from validated receipts (`docs/06` §6). The cached copy drives UI affordances only; every gated action is authorised server-side and a wrong client gets a `402`/`403` — the correct outcome. |
| Reward eligibility, balance, payout eligibility | Computed from the append-only ledger (`docs/06` §7). The app never sums local events into a balance. |
| Trust score / whether an activity counts in a challenge | Ingest-side scoring, roadmap 1.10 / 4.2. |
| A manual activity's plausibility | Server: manual entries earn at a reduced rate and carry `load_source='rpe'` with lower `load_confidence` (`docs/03` §5). |
| AI quota remaining | `GET /v1/coach/quota`. A locally decremented counter is a bypass surface. |
| **`user_id`, on any request** | The verified access token. No `/v1` route accepts a client-supplied user id — the same rule that protects the AI tool surface (ADR-011). |

---

## 5. Screen inventory and navigation

Root switch: `Bootstrapping → Auth stack | Onboarding stack | App tabs`. Tabs:
**היום (Today) · אימונים (Activities) · מאמן (Coach) · מועדון (Club) · אני (Me)**.

| # | Screen | Endpoints | Phase |
|---|---|---|---|
| 1 | Welcome / locale / force-update gate | `GET /v1/meta` (`min_supported_client_version`) | 1 |
| 2 | Register · Login · Forgot · MFA | `POST /v1/auth/register|login|refresh|password/forgot|password/reset`; `POST /v1/auth/mfa/enroll|verify` | 1 (MFA 2) |
| 3 | **Consent capture** — ToS, privacy, `health_data_processing`, `ai_training_improvement` | `GET /v1/me/consents`, `PUT /v1/me/consents/{purpose}` | 1 |
| 4 | Profile basics + thresholds | `PUT /v1/me/profile` (`If-Match`), `GET /v1/me/zones` | 1 |
| 5 | Goals | `GET|POST /v1/me/goals`, `PATCH|DELETE /v1/me/goals/{id}` | 1 |
| 6 | **Health-data permission screen** — what we read, why, and what we never read | none (pre-empts the one-shot OS dialog) | 1 |
| 7 | Connect Garmin | `POST /v1/integrations/garmin/authorize`, `POST /v1/integrations/garmin/callback` | 1 |
| 8 | **Today / dashboard** | `GET /v1/metrics/summary` (one request, `docs/03` §6), `GET /v1/plans/today` | 1 |
| 9 | Readiness detail (drivers) | `GET /v1/metrics/readiness/today`, `GET /v1/metrics/readiness?from=&to=` | 1 |
| 10 | Load & form | `GET /v1/metrics/training-load`, `GET /v1/metrics/acwr` | 1 |
| 11 | Injury risk | `GET /v1/metrics/injury-risk` — always with the `is_clinically_validated:false` badge | 1 |
| 12 | Coach chat (SSE streaming, `Idempotency-Key`) | `GET|POST /v1/coach/conversations`, `GET|POST /v1/coach/conversations/{id}/messages`, `POST /v1/coach/messages/{id}/feedback`, `GET /v1/coach/quota` | 1 |
| 13 | Weekly review · digital twin | `GET /v1/coach/weekly-review`, `GET /v1/coach/twin` | 2 / 3 |
| 14 | Activities list (cursor → infinite query) | `GET /v1/activities?from=&to=&sport=&cursor=&limit=` | 1 |
| 15 | Activity detail | `GET /v1/activities/{id}`, `/laps`, `/efficiency`, downsampled series (`docs/03` §13 Q2), `PATCH /v1/activities/{id}` | 1 |
| 16 | Manual activity · wellness entry | `POST /v1/activities`; `PUT /v1/wellness/{local_date}` *(delta)* | 1 / 2 |
| 17 | Plan / today · compliance | `GET /v1/plans/current|today|{id}`, `POST /v1/plans`, `/{id}/activate`, `/sessions/{id}/complete|skip`, `GET /v1/plans/{id}/compliance`, `/adaptations` | 1 |
| 18 | Sensors & connections | `GET /v1/integrations`, `POST /v1/integrations/{p}/sync|backfill`, `DELETE /v1/integrations/{p}`, `GET /v1/integrations/jobs/{id}` | 1 |
| 19 | Subscription & entitlements | `GET /v1/subscription`, `/plans`, `POST /v1/subscription/receipts/apple|google`, `POST /v1/subscription/cancel`, `GET /v1/entitlements` | 2 |
| 20 | Club / community | `GET|POST /v1/groups`, `/groups/{id}/members`, `/challenges`, `/challenges/{id}/join`, `/leaderboard`, `GET /v1/achievements`, `POST /v1/activities/{id}/share` | 4 |
| 21 | Wallet & partner offers | `GET /v1/wallet`, `/transactions`, `/rewards`; `GET /v1/partners`, `/{slug}/offers`, `POST /v1/partners/offers/{id}/redeem` | 4 |
| 22 | Settings / privacy | `GET|PATCH /v1/me`, `GET /v1/me/consents`, `GET|POST|DELETE /v1/me/data-access`, `POST /v1/me/export`, `GET /v1/me/export/{id}`, `DELETE /v1/me`, `GET /v1/me/deletion`, `GET /v1/auth/sessions`, `DELETE /v1/auth/sessions/{id}`, `POST /v1/auth/logout-all`, `GET|PUT /v1/notifications/preferences` *(delta)* | 1–2 |

### Hebrew-first and RTL as a first-class concern

* **`he-IL` is the development locale**, not a translation target. The app is built,
  demoed and reviewed in Hebrew; English is the locale that gets translated. Build
  in LTR and RTL bugs are found by users.
* Layout uses **logical properties only** — `start`/`end`, `marginStart`,
  `paddingEnd`, `textAlign: 'start'`. An ESLint rule bans `left`/`right` in styles.
  That one rule is the difference between RTL-native and an RTL retrofit.
  `I18nManager` direction is set from the locale at launch.
* **Not everything mirrors, and getting this wrong inverts meaning.** A time axis
  stays left→right (time does not reverse in Hebrew). Numbers, paces, durations and
  clock times stay LTR inside RTL text, wrapped in Unicode bidi isolates
  (`U+2068`/`U+2069`) by the shared formatter — otherwise `4:31 דק׳/ק״מ` reorders
  around the colon. Navigational chevrons mirror; a trend arrow does **not** —
  a mirrored "improving" arrow reads as "declining".
* Component snapshots run in both directions (§12), plus a manual RTL pass in the
  release checklist.

---

## 6. Adaptive UI by athlete level

`training.athlete_profiles.level` (`beginner|intermediate|advanced`, default
`intermediate`) already exists — **BUILT**, migration `0003`. No new column is
needed for the level itself. Density is `useDensity()` → a
`density: 'basic' | 'standard' | 'full'` **prop on every metric component from day
one** (roadmap 2.8), resolved once from level plus an athlete override. Components
never read a global, so all three densities are cheap to snapshot.

| Surface | `basic` (beginner) | `standard` (intermediate) | `full` (advanced) |
|---|---|---|---|
| Readiness | band + one sentence + the top driver | score, band, top 3 drivers | score, all drivers with contributions, `data_quality`, 28-day trend |
| Training load | "this week vs last week", in words | CTL / ATL / TSB chart | + ramp %, monotony, strain, per-sport split |
| **ACWR** | **not shown** | ratio + band + `is_reliable` | rolling *and* EWMA forms, reliability flag |
| Injury risk | not shown — expressed only as the plan's decision | band + drivers + unvalidated badge | probability, drivers, `model_version` |
| Zones | names + colours | + boundaries + anchor used | + anchor source, `thresholds_updated_at` |
| Predictions | goal-race estimate | + interval | both models, interval, fitted Riegel exponent |
| Activity detail | duration, distance, effort in words | + load with `load_source` / `load_confidence` | + NP, IF, efficiency, laps, streams |

**Why showing a beginner ACWR is harmful, not merely noisy.** Three reasons, in
increasing order of damage. (1) The ratio needs 28 days of history to mean
anything — `is_reliable` is false below that (`docs/04` §2) — and a beginner is by
definition inside that window, so the first number they would ever see is the one
we already know is wrong. (2) A new athlete's chronic load is genuinely low, so
*any* sensible progression pushes ACWR above 1.30, the "danger" band. The true
reading is "you are new and ramping"; the number says "you are about to get
injured." (3) The athlete who believes it undertrains and churns — and the athlete
who learns to ignore it has learned to ignore our warnings, which is the worse
outcome, because ACWR is a driver we do want heeded once it is reliable. It is
also contested in the literature (Impellizzeri et al., `docs/04` §2); handing a
contested ratio as a bare number to someone with no framework to discount it is
the opposite of "honesty as a feature".

For a beginner, ramp safety is therefore expressed the way it is actually used —
as the plan's decision and reason from `GET /v1/plans/today`. Note the invariant:
**density changes what is rendered, never what is requested or computed.** The
gating rules run server-side either way (`docs/03` §8), so no density can produce
a different training decision.

---

## 7. Auth and security on device

**Access token — memory only.** A module-scoped variable in `services/auth`. Not
Redux (devtools-inspectable, and it ends up in state dumps), not AsyncStorage, not
MMKV. It is lost on cold start by design and recovered by one refresh call.

**Refresh token — hardware-backed, device-bound.**

```ts
SecureStore.setItemAsync('refresh_token', value, {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY, // kSecAttrAccessibleWhenUnlockedThisDeviceOnly
  requireAuthentication: biometricLockEnabled,                     // opt-in, see below
});
```

iOS: Keychain, `WhenUnlockedThisDeviceOnly` — **no iCloud Keychain sync and no
migration through an encrypted backup.** Android: EncryptedSharedPreferences with
a non-exportable AES-256-GCM Keystore key, plus `allowBackup="false"` and
auto-backup exclusion. The reason is our own auth model: refresh rotation with
reuse detection (`docs/06` §2) revokes the whole family when a token is presented
twice, so a *synced* refresh token turns "restore my backup on a new phone" into a
silent session transfer that logs the athlete out of their real device. A storage
flag prevents a support incident of our own making.

**Single-flight refresh.** Exactly one `POST /v1/auth/refresh` in flight;
concurrent `401`s await the same promise and then replay. Two parallel refreshes
present the same rotating token twice, reuse detection fires, the family is
revoked, and the athlete is logged out *for using the app normally*. This is the
single most consequential client-side detail in the auth model, and §12 tests it
with ten simultaneous `401`s. On a refresh `401`: wipe SecureStore, wipe the
persisted query cache (it holds health values), route to login with an explanation.

**Biometric gate — offered, not imposed.** An opt-in "lock the app" setting gates
cold start and foreground-after-N-minutes via
`LocalAuthentication.authenticateAsync()`, and additionally sets
`requireAuthentication: true` on the stored token. Not default-on: a hard
biometric requirement locks out an athlete whose Face ID fails mid-race. On
Android, enrolling a new fingerprint permanently invalidates the key — that path
must fall back to re-login, not crash.

**Transport.** TLS 1.3 only; ATS on iOS with no exceptions;
`cleartextTrafficPermitted="false"` on Android. Certificate pinning per `docs/06`
§5, pinned to the **SPKI hash of the intermediate CA, with a backup pin for a key
held offline** — never the leaf, which rotates every 60–90 days. The rotation plan
that cannot brick the app:

1. **Always ship ≥ 2 pins**, one of them a not-yet-deployed backup key.
2. Pins ship **in the binary**, never fetched remotely — a remotely updatable pin
   set is attacker-controllable in exactly the scenario pinning defends against,
   and it also cannot ship via Expo Updates (§10).
3. Each pin set carries a hard `expires_at` (proposed 180 days). After it, the app
   **falls back to standard system-trust TLS 1.3 rather than refusing to connect.**
   This is a deliberate, named trade-off: a fleet of bricked apps is a worse
   outcome than a still-encrypted but unpinned connection for the days it takes to
   ship a build. Compensating control: the server reports the share of unpinned
   traffic, and any rise alerts.
4. Pinning applies to our API host only — never to store, push or provider hosts we
   do not control.
5. A new pin must have been live in a shipped build for **at least one full release
   cycle** before the old key is retired. Release-checklist item, not a hope.

**Jailbreak / root: detect and report, never block.** Blocking is trivially
bypassed by the population able to root a phone and locks out legitimate power
users; the value is signal. The flag rides on the session and
`notifications.device_tokens` row and feeds `rewards.fraud_signals` (`docs/06`
§7) — it belongs on the money path, not the login path. App Attest / Play Integrity
as a server-verified signal on receipt validation, payout requests and manual
activity creation: PLANNED Phase 4.

**No health values in logs or crash reports** (`docs/06` §5). Concretely: the crash
reporter's `beforeSend` drops request bodies and breadcrumb payloads wholesale and
strips any key matching a deny-list (`hrv`, `readiness`, `hr_*`, `weight_kg`,
`sleep_*`, `rpe`, `email`, `display_name`, `token`, `authorization`). Network
breadcrumbs record method, **path template**, status and `X-Request-Id` — never a
resolved URL with a query string (`?from=&to=` is itself information), never a
body. Screens are tagged by route name. `X-Request-Id` is what ties a client crash
to a server log without carrying any data across.

**Screenshot and backgrounding privacy.** iOS snapshots the app on backgrounding
for the switcher, so health screens install a privacy overlay on `AppState →
inactive` (a native view from the config plugin; JS alone cannot intercept the
snapshot). Android sets `FLAG_SECURE` on those screens, suppressing both the
recents thumbnail and screenshots. Applied **per screen** — readiness detail,
injury risk, wellness, thresholds, coach chat — not app-wide, because an athlete
screenshotting their own workout to share is a feature.

**Deep-link validation for the OAuth callback** — the highest-risk link in the app:

* The provider flow runs in `WebBrowser.openAuthSessionAsync`
  (`ASWebAuthenticationSession` / Custom Tabs). **Never an in-app WebView** — that
  would put the athlete's Garmin credentials inside our process.
* PKCE with a per-attempt verifier; `state` is **server-generated** by
  `POST /v1/integrations/garmin/authorize`, single-use, short-TTL, held in memory
  for the attempt. A callback whose `state` does not match the pending attempt is
  discarded and never forwarded.
* The app forwards `code` + `state` to
  `POST /v1/integrations/garmin/callback`; the **server** exchanges the code. The
  client never holds a provider token, and no provider client secret ships in the
  binary.
* Prefer **Universal Links / App Links** (domain-verified) for the callback; the
  custom scheme is a fallback only, because any installed app can claim a scheme.
* One typed linking config parses every link against a route allowlist with
  validated params. A deep link arriving while unauthenticated is *held* until
  after login — never dropped, never acted on.

---

## 8. Push notifications

**Registration.** `getDevicePushTokenAsync()` (the native APNs/FCM token, not an
Expo push token — we send via our own workers) →
`POST /v1/notifications/device-tokens` *(API delta, §11)*, writing
`notifications.device_tokens` — which already exists in migration `0008` with
`UNIQUE (platform, token)`, `app_version`, `device_model`, `locale`,
`last_seen_at`, `invalidated_at`. Registered on every cold start (tokens rotate)
and deleted on logout, so a sold or shared device stops receiving. A provider
"unregistered" response sets `invalidated_at` server-side.

**Permission timing: after value, never on first launch.** The prompt appears once
the athlete has seen their first readiness score — i.e. after Garmin connect and
first sync — on a screen that states what the daily push contains and when it
arrives. iOS grants exactly one system prompt; spending it on launch is how an
install becomes permanently muted. For athletes who decline, iOS **provisional
authorisation** still delivers the daily readiness quietly to Notification Centre.

**Quiet hours are evaluated server-side.**
`notification_preferences.quiet_hours_start/end` are local times, compared against
the athlete's timezone in the sending worker — not suppressed on the device, where
suppression happens *after* the lock screen has already displayed it. `security`
and `billing` kinds ignore quiet hours by policy, as the `0008` comment states.
Android notification channels and iOS `interruption-level` are set per `kind`, so
`social` can be muted without muting `security`. Channel names are Hebrew.

**The daily readiness push is the retention lever** (roadmap 2.4). One per local
day, at a per-athlete time derived from their usual wake or first-activity hour,
deduped via `notification_deliveries.dedupe_key =
'daily_readiness:{user_id}:{local_date}'` — the unique partial index already
exists. When `data_quality < 0.35` it is **suppressed or reworded, never faked**:
"we need one more night of data", consistent with `docs/03` §6.

**Payload rules — a notification body NEVER contains a health value.** A lock
screen is readable by anyone holding the phone and is mirrored to a watch, a car
display and a shared Mac.

| Allowed | Forbidden |
|---|---|
| "הבדיקה של הבוקר מוכנה" / "Today's session was adjusted — tap to see why" | "Readiness 42 — HRV 28% below baseline" |
| Data payload of **identifiers only**: `{kind, entity_type, entity_id, local_date, dedupe_key}` | any score, HRV, HR, sleep, weight, RPE, or a name |
| An action that *opens* the wellness form (which then queues offline, §4) | an action that mutates state straight from the payload |

`notification_deliveries.body` is retained for support, so the stored string must
be the same value-free text — the table comment already requires it. The app
fetches the actual number over TLS after unlock.

---

## 9. Performance budgets

Measured on a release build, p95, iPhone 12 and Pixel 6a. A regression against any
of these blocks the staged rollout (§10).

| Budget | Target | Why this number |
|---|---|---|
| Cold start to interactive | **< 2.0 s** | Below this the app feels like a watch face; above it athletes check Garmin Connect instead. |
| Time to dashboard content | **< 1.0 s** from persisted cache; **< 2.5 s** fresh on 4G | The server budget is p95 < 150 ms (Phase 1 exit); the rest is network and render — which is exactly why `GET /v1/metrics/summary` is one request and not seven (`docs/03` §6). |
| Warm foreground to interactive | < 400 ms | |
| Chart first paint (90 days × 3 series) | **< 250 ms**, no dropped frames while panning (60 fps; 120 on ProMotion) | An SVG node per point misses this by an order of magnitude — the reason for Skia. |
| Activity history list | Virtualised (`FlashList`, stable keys, fixed row height); flat memory across 2,000 rows; < 16 ms per row commit | Cursor pagination (`docs/03` §1) + infinite query, 30-row pages. A three-season history is thousands of rows and must not be a memory graph. |
| Series payload | Server-downsampled to ≤ 1,000 points per channel | A 3-hour ride at 1 Hz is ~11k points per channel; downsampling on the phone means transferring and parsing it first. This is mobile's position on `docs/03` §13 Q2: **resolve it as a server endpoint.** |
| JS bundle (Hermes bytecode, per platform) | < 4 MB; download < 60 MB iOS / < 40 MB Android | Gated in CI with a bundle visualiser. |
| Background sync | ≤ 2 wake-ups/day, ≤ 5 s CPU each, ≤ 200 KB; **no background location, ever** | Garmin data arrives via *our* webhook, so the phone never polls for it — background work is only outbox flush plus a HealthKit/Health Connect delta read. Battery complaints are uninstalls, and iOS quietly stops granting background time to an app that wastes it. |
| Peak memory | < 250 MB | |

No network call precedes first paint except the token refresh, which runs in
parallel with rendering the cached dashboard. The `GET /v1/meta` version gate runs
*after* first paint unless the cached `min_supported_client_version` already fails.

---

## 10. Release and store compliance

**iOS disclosures.** `NSHealthShareUsageDescription` and
`NSHealthUpdateUsageDescription` in Hebrew and English, purpose-specific — "כדי
לחשב את מדד המוכנות היומי שלך", not "to improve your experience", which is itself
a rejection reason. `NSFaceIDUsageDescription` for the biometric gate. Deliberately
**no** location strings: we do not need background location, and not requesting it
removes an entire review conversation. All strings live in `app.config.ts`
`ios.infoPlist`, so they are reviewed as code alongside the behaviour they
describe.

**Privacy manifest** (`PrivacyInfo.xcprivacy`): declared data types — health &
fitness, identifiers, usage, diagnostics — each with purpose and linkage, plus
required-reason API declarations. Third-party SDKs must ship their own signed
manifests (the crash reporter is the one to verify). The App Store Connect privacy
answers, the manifest and the published privacy policy must agree; a mismatch is
the most common health-app rejection after usage strings. HealthKit rules that are
easy to fail: health data must never be used for advertising, sold, or written to
third-party analytics — which §7's logging deny-list enforces mechanically rather
than by policy.

**Android.** Health Connect permission declarations, the Play sensitive-permission
declaration form, the Data Safety form, and an in-app privacy-policy link
reachable **before** any permission grant.

**Budget a rejection round.** Roadmap 2.10 and sequencing rule 4 already say store
submission is a phase, not a step. Ship a **review account seeded with 60 days of
activities** — a reviewer who cannot see a readiness score cannot approve a
readiness app.

**OTA policy** — the line is where review obligations begin:

| May ship via Expo Updates (JS + assets) | Must go through store review |
|---|---|
| Copy, translations, RTL and layout fixes | Anything in the `app.config.ts` native surface: permissions, entitlements, plugins, URL schemes |
| Formatting, chart and density tuning | **Certificate pins** (§7) |
| New screens built from already-shipped native modules | A new native module or an Expo SDK upgrade |
| Feature flags and rollout gating | Anything the privacy manifest or a usage string describes |
| Bug fixes | Anything that changes which health data is read |

Updates are code-signed (`expo-updates` code signing) so a compromised channel
cannot push JS to our athletes, and pinned to a `runtimeVersion` matching the
native build so a bundle can never load against a binary missing its native
module. Rollback is a re-publish of the previous bundle and is rehearsed, not
assumed. One hard rule: **an OTA update must never change what the app tells an
athlete about their health without the review an equivalent server change would
get.** Changing analytics semantics is gated server-side by
`min_supported_client_version` (`docs/03` §12), not by hoping every client updated.

**Staged rollout.** EAS channels `development → preview` (TestFlight / internal
track) `→ production`. Production goes 5% → 25% → 100% with 24 h at each step,
gated on crash-free sessions ≥ 99.5% and no regression in the §9 budgets. Expo
Updates rollouts use the same percentages. `min_supported_client_version` is the
emergency brake for a build that must not be used at all.

**Crash and error reporting** per §7: PII stripped in `beforeSend`, sourcemaps
uploaded per build and not shipped in the bundle, `X-Request-Id` on every
breadcrumb.

---

## 11. Module contract — mobile client

The app is a **client** of the backend modules, not a module. The rubric applies
with that framing.

**Purpose.** Present decisions and their drivers to the athlete; capture
subjective input reliably, including offline.

**Responsibilities.** Render derived metrics with provenance (`load_source`,
`load_confidence`, anchor used, `is_clinically_validated`, `data_quality`); hold a
durable outbox; adapt density; keep health data off lock screens, logs and
switcher snapshots.

**Explicit non-responsibilities.** **No analytics computation, ever** — no
client-side CTL, ACWR, zone, load or plan-gating maths. Two implementations of the
same rules diverge, and the decision must be recorded server-side for
explainability and evaluation (`docs/03` §8). Enforced by the same lint rule as
§2: `packages/shared` holds formatting and no physiology, and `apps/mobile` may not
import anything that does arithmetic on a metric value.

**Database changes.** None owned by the client. Existing tables it activates:
`notifications.device_tokens` and `notifications.notification_preferences`
(migration `0008`, empty), `training.daily_wellness` (`0003`, write path). New
work it implies: a preference for the density override, and an injury/pain report
table for roadmap 3.4. **Every one is a reviewed SQL file in
`database/migrations/` applied by a human, never auto-applied (ADR-012), and
sequenced in `docs/19` (Database Evolution).**

**APIs required.** Consumed: the full §5 table. Deltas `docs/03` must add:
`PUT /v1/wellness/{local_date}`; `POST|DELETE /v1/notifications/device-tokens`;
`GET|PUT /v1/notifications/preferences`; `GET /v1/notifications` (in-app inbox);
`POST /v1/injuries` (Phase 3); and a server-downsampled series endpoint replacing
raw stream URLs (`docs/03` §13 Q2).

**Dependencies.** Backend modules over `/v1` only — identity, training, coaching,
billing, notifications, community, rewards/partners (Phase 4). No module
internals, no database access. External: APNs, FCM, App Store / Play Billing,
HealthKit / Health Connect, Garmin (system browser only), crash reporting, EAS.

**Security considerations.** §7 in full, plus the §4 untrusted list. The client is
deliberately **not** a security boundary: RLS plus repository scoping (`docs/06`
§3) mean a fully compromised device still cannot read another athlete's data, and
no `/v1` route accepts a user id. What the client *is* responsible for is privacy
hygiene — storage flags, transport, logs, snapshots, notification payloads.
Mobile-OWASP items addressed: insecure storage (SecureStore flags; the persisted
cache is encrypted and wiped on logout), insecure communication (TLS 1.3 +
pinning), client-side authorisation (none relied upon), reverse engineering
(accepted — no secret ships in the binary, which is why the OAuth code exchange is
server-side).

**Testing strategy.** §12. Note the tie-in to the backend gate: the app adds no
rows to the tenant-isolation matrix because it has no endpoints, but **every new
screen must name the endpoint it reads, and that endpoint must already have a
matrix row** (`docs/06` §11.1). A screen calling an unmatrixed endpoint fails
review.

---

## 12. Testing strategy

| Layer | Tooling | Scope | Gate |
|---|---|---|---|
| Unit | Jest | formatters and bidi isolation, outbox reducer, backoff, query-key factory, density selector | ≥ 90% on `packages/shared` — it decides what every number looks like |
| Component | React Native Testing Library | every metric component × 3 densities × 2 directions; null rendering (`—`, never `0`) | a new metric component without all six cases fails review |
| Integration | MSW backed by a mock **generated from the same OpenAPI 3.1 schema** as `api-types` | screen ↔ query ↔ outbox; `401`→single-flight refresh; `412` conflict; `429` quota; `422` insufficient-data; SSE stream incl. a truncated stream | schema regeneration must not break the mocks |
| E2E | **Maestro** primary, Detox where native automation is required | onboarding → consent → health permission → connect (mock provider) → dashboard → coach → wellness. The critical path only (`docs/09` §1) | on real devices per release candidate |
| Offline | Maestro + network conditioning | the matrix below | every row green per release |
| Accessibility | manual + `eslint-plugin-react-native-a11y` | dynamic type to 200%, VoiceOver and TalkBack on Today and Coach, contrast ≥ 4.5:1, 44×44 targets, colour never the sole carrier of a readiness band | manual pass in the release checklist, alongside the manual RTL pass |
| Performance | startup and render trace on fixed devices | §9 budgets | a regression blocks the rollout |

**Why the mock is generated, not written.** A hand-written mock encodes what its
author believed the API returns, so an integration suite can be entirely green
against an API that no longer exists — the same drift argument `docs/03` §1 makes
about hand-written clients, one layer up. The step that emits
`packages/shared/api-types` emits `packages/shared/mocks`, so contract drift
surfaces as a compile error rather than as a wrong number on an athlete's phone.

### Offline test matrix

| Scenario | Required outcome |
|---|---|
| Airplane mode → dashboard | Cached summary with "as of HH:mm"; nothing older than 36 h rendered as a score |
| Airplane mode → log wellness → kill app → reopen → reconnect | Exactly one `PUT`; row leaves the outbox; no duplicate day |
| Airplane mode → manual activity → reconnect over a flaky link (2 attempts) | Exactly one activity — same `Idempotency-Key`; the second attempt returns the original `201` |
| Offline RPE edit on an activity edited on web meanwhile | `412` → conflict UI; athlete chooses; no silent overwrite |
| Offline → coach chat | Explicit offline state; **no queued AI message** |
| Offline → change a consent | Blocked with the reason shown (§4) |
| Access token expired **and** offline | Cached reads still render; **no logout** — a refresh that fails for lack of network must not end the session |
| Refresh rejected (reuse detection fired) | SecureStore wiped, persisted cache wiped, routed to login with an explanation |
| 10 parallel `401`s | Exactly one `POST /v1/auth/refresh` |
| Outbox at capacity | New offline writes refused with a message; nothing evicted |
| Push arrives while offline | Tap opens the screen with cached data and a "not up to date" banner; never a value taken from the payload |
| Entitlement cached as Premium, server says Free | Server wins immediately on foreground; gated action returns `403` and the UI corrects itself |

---

## 13. Open questions

1. **Charting library commitment** — Victory Native XL versus a thin custom Skia
   renderer. Decide against the §9 budget on a Pixel 6a before roadmap 1.14, not
   after.
2. **Apple Health as a source for the first submission.** Reading HealthKit
   enlarges the privacy manifest and the review conversation. Recommendation:
   Garmin only for submission one (roadmap 2.10), Apple Health in 5.2, so the first
   health review is the smallest possible.
3. **Diagnostics consent gating** for the crash reporter — opt-in versus disclosed
   legitimate interest. Counsel, alongside `docs/06` §10.
4. **Density override: server preference or device-local?** Recommendation: server,
   so phone and web agree — which needs a preferences column, sequenced in
   `docs/19` (Database Evolution).
5. **Pin expiry window** (180 days proposed) and whether soft-fail after expiry is
   acceptable. Must be signed off with the `docs/06` security review, not decided
   unilaterally here.
