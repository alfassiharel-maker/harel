"""Provider-agnostic DTOs at the integration boundary.

These live here rather than in `modules/training` because the dependency rule is
`modules → integrations` and never the reverse (`docs/01` §3). A normalised
activity is the *output* of the integration layer and the *input* to the training
module, so it belongs to the layer both can see.

Unit convention, enforced by naming: SI with an explicit suffix on every field
(`distance_m`, `duration_s`, `avg_power_w`). An unsuffixed numeric field is
forbidden by review, because the one time it is read as the wrong unit the error is
silent and the athlete's load score is simply wrong.

`None` means "the provider did not report this". It never means zero. This
distinction is load-bearing all the way through to the analytics engine, which
degrades to a lower-trust load source rather than treating a missing power meter
as zero watts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "AuthStyle",
    "AuthorizeChallenge",
    "Capability",
    "Delivery",
    "EventSubject",
    "FetchTask",
    "NormalisedActivity",
    "NormalisedLap",
    "NormalisedWellnessDay",
    "ProviderTokens",
    "RateLimitPolicy",
    "RawPayload",
    "StreamBlob",
    "SyncCursor",
    "WebhookVerdict",
]

Capability = Literal[
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
]

AuthStyle = Literal["oauth1a", "oauth2_pkce", "oauth2_code", "on_device"]
Delivery = Literal["webhook_ping", "webhook_push", "poll_only", "client_push"]

# Why a verdict and not a bool: "this provider does not sign its webhooks" and
# "this signature is wrong" demand opposite handling. The first is a known
# provider property and the event may be processed as a re-fetch trigger; the
# second is a forgery attempt and must never influence state (`docs/13` §5.1).
WebhookVerdict = Literal["signed_valid", "unsigned_by_design", "signature_invalid"]

# Sports normalised across providers. Mirrors `algorithms.types.Sport` plus the
# multisport container, which the engine has no concept of but ingest must.
NormalisedSport = Literal["run", "bike", "swim", "strength", "multisport", "other"]


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """A provider's published limits, used to size the ingest worker's concurrency.

    Expressed as a policy object rather than constants so the ingest scheduler can
    budget without knowing which provider it is throttling.
    """

    requests_per_minute: int
    requests_per_day: int | None = None
    max_concurrent: int = 4
    # Providers differ on whether a 429 carries Retry-After. When it does not, the
    # worker needs a floor to back off to rather than retrying immediately.
    default_backoff_s: float = 2.0


@dataclass(frozen=True, slots=True)
class AuthorizeChallenge:
    """What the client is sent to, plus the server-side state to verify the return.

    `state` is the CSRF defence and `verifier` is the PKCE secret. Neither may be
    given to the client beyond what the URL itself carries: `state` is stored
    server-side and compared on callback, so an attacker who can make the athlete's
    browser hit our callback with their own `code` cannot bind it to our session.
    """

    authorize_url: str
    state: str
    verifier: str | None = None
    # OAuth 1.0a needs the request-token secret carried across the redirect.
    request_token_secret: str | None = None
    expires_at: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderTokens:
    """Decrypted provider credentials. Never logged, never serialised to a client.

    Held in memory only for the duration of one provider call. The persisted form is
    `EnvelopeCipher` ciphertext in `training.provider_connections`.
    """

    access_token: str
    refresh_token: str | None = None
    token_secret: str | None = None  # OAuth 1.0a
    expires_at: dt.datetime | None = None
    scopes: frozenset[str] = frozenset()
    provider_user_id: str | None = None

    def __repr__(self) -> str:
        # A default dataclass repr would put a live access token into any traceback
        # or debug log that touches this object.
        return (
            f"ProviderTokens(provider_user_id={self.provider_user_id!r}, "
            f"expires_at={self.expires_at!r}, scopes={sorted(self.scopes)!r})"
        )

    def is_expired(self, *, now: dt.datetime, skew_s: int = 120) -> bool:
        """Expiry with clock skew, so a token is refreshed before it fails mid-call.

        A refresh that happens one request too late costs a retry and, on providers
        that rate-limit failures, a backoff.
        """
        if self.expires_at is None:
            return False
        return self.expires_at <= now + dt.timedelta(seconds=skew_s)


@dataclass(frozen=True, slots=True)
class EventSubject:
    """One athlete-scoped thing an inbound webhook refers to.

    A single Garmin ping can name several. Resolution from `provider_user_id` to our
    `user_id` happens in the training module, not here — the integration layer never
    learns our identifiers.
    """

    provider_user_id: str
    kind: Literal["activity", "dailies", "sleep", "hrv", "body_composition", "deregistration"]
    provider_ref: str | None = None
    occurred_at: dt.datetime | None = None
    # Present only on push (not ping) deliveries, and never trusted as data — it
    # exists so a re-fetch can be scoped, not so a value can be believed.
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class FetchTask:
    """A single outbound provider call the worker should make.

    Deterministic and hashable: `dedupe_key` collapses the many pings a provider
    sends for one activity into one job, and survives a worker restart because it is
    derived from the task rather than from queue state.
    """

    kind: Literal["activity_summary", "activity_details", "activity_file", "dailies", "sleep", "hrv"]
    provider_user_id: str
    provider_ref: str | None = None
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    # Backfill chunks are lower priority than a live webhook: an athlete watching
    # their watch sync should not queue behind a two-year history import.
    priority: Literal["live", "backfill"] = "live"

    @property
    def dedupe_key(self) -> str:
        window = ""
        if self.window_start is not None:
            window = f":{self.window_start.isoformat()}"
            if self.window_end is not None:
                window += f"/{self.window_end.isoformat()}"
        return f"{self.kind}:{self.provider_user_id}:{self.provider_ref or ''}{window}"


@dataclass(frozen=True, slots=True)
class RawPayload:
    """Exactly what the provider returned, before any interpretation.

    Persisted to `training.provider_events` *before* parsing, so a parser bug is a
    replay rather than permanent data loss and a provider changing its payload shape
    is a backfill rather than an incident (`docs/01` §4.2).
    """

    provider: str
    kind: str
    body: bytes
    provider_user_id: str | None = None
    provider_ref: str | None = None
    fetched_at: dt.datetime | None = None
    content_type: str = "application/json"


@dataclass(frozen=True, slots=True)
class StreamBlob:
    """Sample-level data, destined for object storage rather than Postgres.

    Keeping multi-megabyte streams out of the database is the difference between a
    50 GB and a 5 TB database at 100k athletes (`docs/02` §2). The row holds the
    key, the sample count and the checksum; the samples live in the bucket.
    """

    provider_ref: str
    content: bytes
    content_type: str
    sample_count: int
    channels: tuple[str, ...]
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class NormalisedLap:
    lap_index: int
    duration_s: float
    distance_m: float | None = None
    avg_hr: float | None = None
    avg_power_w: float | None = None
    avg_speed_m_s: float | None = None
    avg_cadence_rpm: float | None = None


@dataclass(frozen=True, slots=True)
class NormalisedActivity:
    """One completed session, normalised away from any provider's schema.

    Maps onto `algorithms.types.ActivitySummary` for the analytics engine and onto
    `training.activities` for storage. The engine consumes this shape, so a new
    provider changes zero lines of analytics code.
    """

    provider: str
    provider_activity_id: str
    provider_user_id: str
    sport: NormalisedSport
    start_time: dt.datetime
    # The athlete's calendar day. A 23:30 run belongs to that day for the athlete
    # even when it is already tomorrow in UTC, and every daily aggregate keys on it.
    local_date: dt.date
    duration_s: float

    sub_sport: str | None = None
    distance_m: float | None = None
    moving_duration_s: float | None = None
    elevation_gain_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_power_w: float | None = None
    normalised_power_w: float | None = None
    max_power_w: float | None = None
    avg_speed_m_s: float | None = None
    max_speed_m_s: float | None = None
    avg_cadence_rpm: float | None = None
    calories_kcal: float | None = None
    # Athlete-supplied, so it may arrive long after the activity.
    rpe: float | None = None
    device_name: str | None = None
    is_manual: bool = False
    # Attacker-controlled free text: an athlete names their own activities, and
    # those names reach the AI layer. Wrapped in a delimited untrusted block there
    # (`docs/05` §6); carried verbatim here so the raw value is never silently lost.
    title: str | None = None
    laps: tuple[NormalisedLap, ...] = ()
    timezone_offset_s: int | None = None


@dataclass(frozen=True, slots=True)
class NormalisedWellnessDay:
    """Overnight and subjective inputs for one athlete-day.

    Maps onto `algorithms.types.DailyWellness`. Every field is optional because
    every field is a separate sensor that may not have been worn.
    """

    provider: str
    provider_user_id: str
    local_date: dt.date

    hrv_rmssd_ms: float | None = None
    resting_hr: float | None = None
    sleep_duration_s: float | None = None
    sleep_score: float | None = None
    deep_sleep_s: float | None = None
    rem_sleep_s: float | None = None
    respiration_rate: float | None = None
    spo2_pct: float | None = None
    stress_avg: float | None = None
    body_battery_min: float | None = None
    body_battery_max: float | None = None
    weight_kg: float | None = None
    vo2max: float | None = None
    steps: int | None = None


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """Where a provider's incremental sync got to.

    Providers differ: some give an opaque cursor, some a high-water timestamp, some
    require an explicit acknowledgement that a batch was consumed (see
    `ProviderAdapter.commit`). All three fit here.
    """

    provider: str
    provider_user_id: str
    opaque: str | None = None
    high_water_mark: dt.datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)
