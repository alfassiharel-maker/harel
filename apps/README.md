# Client applications

Blocked on architecture approval. Structure is fixed now so that shared code has a
home from the first commit rather than being extracted later.

```
apps/
├── mobile/            ⏳ React Native + TypeScript (iOS, Android)
│   ├── src/screens/         onboarding, dashboard, coach chat, activities, sensors, club
│   ├── src/components/
│   ├── src/services/        generated API client, provider connect flows
│   ├── src/store/           Redux Toolkit + React Query
│   └── src/navigation/
└── web/               ⏳ React + TypeScript (Vite)
    ├── src/pages/           deep analytics, charts, group management, coach and partner portals
    ├── src/components/
    ├── src/services/
    └── src/store/

packages/
└── shared/            ⏳ TypeScript shared between mobile and web
    ├── api-types/           GENERATED from the backend OpenAPI schema — never hand-written
    ├── domain/              sport, zone, band enums and formatting
    └── charts/              shared chart configuration
```

## Decisions already made

**API types are generated, never hand-written.** The backend emits OpenAPI 3.1 and
CI generates the TypeScript client. A hand-maintained client drifts from the server,
and the first symptom is a wrong number on an athlete's dashboard.

**Unit formatting lives in `packages/shared`, once.** The wire format is SI with
explicit suffixes (`distance_m`, `pace_s_per_km`). Converting to km, min/km or
miles is a client concern, implemented in one place — two implementations will
disagree, and a pace shown differently on phone and web is a support ticket.

**Adaptive information density is a first-class concern.** A beginner and an
advanced athlete see different screens (`docs/07` task 2.8). Components take a
density prop from the start rather than being retrofitted.

**Access tokens live in memory only.** Mobile keeps the refresh token in
Keychain/Keystore; web receives it as an `HttpOnly` cookie and never sees it in JS.
See `docs/06-security-privacy.md` §2.

## Mobile: Expo or bare?

Recommendation: **Expo with a development client**. Garmin OAuth needs a custom
scheme and health-data background sync needs native modules, both of which Expo
config plugins handle. Bare React Native is available via prebuild if a plugin
ever proves insufficient — this decision is reversible, which is why it is not
worth agonising over now.
