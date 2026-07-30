"""Garmin endpoints, scopes and payload keys.

> **VERIFY BEFORE BUILD.** Every URL, scope name and payload key in this file must
> be checked against the Garmin Developer Program documentation once access is
> granted (`docs/13` §8). They are written from Garmin's published Health and
> Activity API shapes, but Garmin has migrated auth styles and renamed fields
> between API generations, and a stale constant here fails at integration time
> rather than at import time.
>
> The reason this is isolated in its own module: when the real documentation
> arrives, exactly one file changes. No parsing logic, no adapter behaviour, and no
> test needs touching — the fixtures assert on our normalised shape, not Garmin's.

Garmin's auth generation is the one open question that changes a method signature
rather than a constant. Older Health API integrations use OAuth 1.0a; current
onboarding uses OAuth 2.0 with PKCE. The adapter implements PKCE and declares
`auth_style = "oauth2_pkce"`; `AuthorizeChallenge` already carries
`request_token_secret` so an OAuth 1.0a variant needs no DTO change.
"""

from __future__ import annotations

from backend.integrations.types import Capability, RateLimitPolicy

__all__ = [
    "ACTIVITY_KEYS",
    "API_BASE",
    "AUTHORIZE_URL",
    "BACKFILL_CHUNK_DAYS",
    "CAPABILITIES",
    "DEFAULT_SCOPES",
    "MAX_BACKFILL_WINDOW_DAYS",
    "RATE_LIMIT",
    "REVOKE_URL",
    "TOKEN_URL",
    "WELLNESS_KEYS",
]

# --- endpoints (VERIFY) ------------------------------------------------------
AUTHORIZE_URL = "https://connect.garmin.com/oauth2Confirm"
# S105 is suppressed below because ruff matches "TOKEN" in the *variable name*. The
# value is a public OAuth endpoint URL, not a credential. No secret is ever
# hardcoded in this repository; Garmin's client id and secret come from settings.
TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"  # noqa: S105
REVOKE_URL = "https://apis.garmin.com/wellness-api/rest/user/registration"
API_BASE = "https://apis.garmin.com/wellness-api/rest"

# --- scopes (VERIFY) --------------------------------------------------------
# Least privilege: request only what the product actually computes on. Every extra
# scope is data we must protect, justify in the privacy policy, and explain on the
# permission screen — and an unused scope is pure liability (`docs/13` §3).
DEFAULT_SCOPES: tuple[str, ...] = (
    "ACTIVITY_EXPORT",  # activity summaries and details
    "HEALTH_EXPORT",  # dailies, sleep, HRV, stress
)

CAPABILITIES: frozenset[Capability] = frozenset(
    {
        "activity_summary",
        "activity_laps",
        "activity_samples",
        "activity_file",
        "daily_summary",
        "sleep",
        "hrv",
        "device_stress",
        "body_composition",
        "spo2",
        "respiration",
        "vo2max",
    }
)

# Garmin's published limits are per-application, not per-user, which is why the
# ingest worker budgets concurrency per provider rather than per athlete
# (`docs/20` §5). Conservative until measured against the real quota.
RATE_LIMIT = RateLimitPolicy(
    requests_per_minute=100,
    requests_per_day=None,
    max_concurrent=4,
    default_backoff_s=2.0,
)

# Garmin caps a single backfill request's window; longer histories must be chunked.
# 90 days is a safe chunk under the documented limit, and chunking also bounds the
# blast radius of one failed request during a two-year import.
MAX_BACKFILL_WINDOW_DAYS = 730
BACKFILL_CHUNK_DAYS = 90

# --- payload keys (VERIFY) --------------------------------------------------
# Top-level webhook keys → the subject kind each represents. Garmin batches several
# types into one POST, so a single event can name activities *and* dailies.
ACTIVITY_KEYS: dict[str, str] = {
    "activities": "activity",
    "activityDetails": "activity",
    "manuallyUpdatedActivities": "activity",
}

WELLNESS_KEYS: dict[str, str] = {
    "dailies": "dailies",
    "sleeps": "sleep",
    "hrv": "hrv",
    "hrvSummaries": "hrv",
    "stressDetails": "dailies",
    "bodyComps": "body_composition",
    "userMetrics": "body_composition",
    "respiration": "dailies",
    "pulseOx": "dailies",
}
