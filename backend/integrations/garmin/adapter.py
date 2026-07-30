"""The Garmin adapter.

Structured around the pure/async split the `ProviderAdapter` contract mandates. The
pure half — signature verification, event parsing, fetch planning, normalisation —
is the majority of the code and needs no network, so the whole ingest path is
testable from recorded payloads.

Two Garmin-specific behaviours worth understanding before changing anything:

**Garmin does not sign its push notifications.** Their model is a secret callback
URL plus an optional source-IP allowlist. That makes the endpoint forgeable by
anyone who learns the URL, so this adapter returns `unsigned_by_design` and the
ingest pipeline treats such an event as **a trigger to re-fetch, never as data**. A
forged body can therefore cause a redundant authenticated fetch — wasteful — but it
can never write an attacker's numbers into an athlete's history. If Garmin later
offers HMAC signing, configuring a secret upgrades the verdict to `signed_valid`
with no other change.

**The `userAccessToken` in a webhook body is ignored.** Garmin includes it, and
using it would mean trusting an unauthenticated body to tell us which credential to
present. We resolve the athlete from `userId` against our own
`provider_connections` and use the token we stored. This is the difference between
a webhook that identifies work to do and a webhook that grants access.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from backend.core.logging import get_logger
from backend.integrations.base import (
    ProviderError,
    ProviderRateLimited,
    ProviderTokenExpired,
    ProviderUnavailable,
)
from backend.integrations.garmin import constants as g
from backend.integrations.normalisation import (
    checksum,
    coerce_positive,
    local_date_of,
    map_sport,
    speed_from_distance_duration,
)
from backend.integrations.types import (
    AuthorizeChallenge,
    AuthStyle,
    Capability,
    Delivery,
    EventSubject,
    FetchTask,
    NormalisedActivity,
    NormalisedLap,
    NormalisedWellnessDay,
    ProviderTokens,
    RateLimitPolicy,
    RawPayload,
    StreamBlob,
    SyncCursor,
    WebhookVerdict,
)

__all__ = ["GarminAdapter"]

logger = get_logger(__name__)

# Garmin timestamps are Unix seconds. Anything before this is a sentinel or a
# corrupted field, not a real activity date.
_MIN_PLAUSIBLE_EPOCH = 946_684_800  # 2000-01-01T00:00:00Z


class GarminAdapter:
    """Garmin Health + Activity API. Satisfies `ProviderAdapter`."""

    provider: ClassVar[str] = "garmin"
    auth_style: ClassVar[AuthStyle] = "oauth2_pkce"
    delivery: ClassVar[Delivery] = "webhook_push"
    capabilities: ClassVar[frozenset[Capability]] = g.CAPABILITIES
    rate_limit: ClassVar[RateLimitPolicy] = g.RATE_LIMIT
    max_backfill_window_days: ClassVar[int] = g.MAX_BACKFILL_WINDOW_DAYS

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        webhook_secret: str | None = None,
        http: Any | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        # Absent for Garmin today. Present means we can upgrade to a real signature
        # check without touching the pipeline.
        self._webhook_secret = webhook_secret
        # Injected so tests never construct a real transport, and so the pure methods
        # provably cannot reach the network — there is nothing to reach it with.
        self._http = http

    # ------------------------------------------------------------ authorisation

    def authorize(self, *, redirect_uri: str, scopes: Sequence[str]) -> AuthorizeChallenge:
        """Build the consent URL with PKCE.

        `state` is generated server-side and stored; the callback is rejected unless
        it comes back identical. Without that check, an attacker can complete their
        own Garmin authorisation against a victim's session and attach *their*
        Garmin account to the victim's athlete profile — or the reverse, harvesting
        the victim's data into an account they control.
        """
        if not self._client_id:
            raise ProviderError("Garmin client id is not configured")

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)[:128]
        # S256 rather than `plain`: a `plain` challenge is the verifier, so anyone who
        # observes the authorize request can complete the exchange.
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        query = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes or g.DEFAULT_SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return AuthorizeChallenge(
            authorize_url=f"{g.AUTHORIZE_URL}?{query}",
            state=state,
            verifier=verifier,
            # Short window: an unused challenge is an open invitation, and a real
            # athlete completes consent in under a minute.
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
        )

    async def exchange(
        self, *, params: Mapping[str, str], challenge: AuthorizeChallenge
    ) -> ProviderTokens:
        """Trade the callback code for tokens, verifying `state` first."""
        returned_state = params.get("state", "")
        # Constant-time: a leaky comparison on a CSRF token is a smaller problem than
        # on a password, but it costs nothing to do correctly.
        if not hmac.compare_digest(returned_state, challenge.state):
            raise ProviderError("OAuth state did not match; the callback is not trusted")

        if error := params.get("error"):
            # The athlete declined, or Garmin refused. Not our bug — surface it as a
            # provider condition rather than a 500.
            raise ProviderError(f"Garmin authorisation failed: {error}")

        code = params.get("code")
        if not code:
            raise ProviderError("OAuth callback carried no authorization code")

        body = {
            "grant_type": "authorization_code",
            "client_id": self._client_id or "",
            "client_secret": self._client_secret or "",
            "code": code,
            "code_verifier": challenge.verifier or "",
        }
        if redirect := params.get("redirect_uri"):
            body["redirect_uri"] = redirect

        payload = await self._post_form(g.TOKEN_URL, body)
        return self._tokens_from_response(payload)

    async def refresh(self, *, tokens: ProviderTokens) -> ProviderTokens | None:
        if not tokens.refresh_token:
            # Nothing to refresh with: the athlete must reconnect. Returning None
            # would imply "no refresh needed", which is the opposite of the truth.
            raise ProviderTokenExpired("Garmin connection has no refresh token")

        payload = await self._post_form(
            g.TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id or "",
                "client_secret": self._client_secret or "",
                "refresh_token": tokens.refresh_token,
            },
        )
        refreshed = self._tokens_from_response(payload)
        # Garmin may omit the provider user id on a refresh; keep what we know rather
        # than losing the link between the credential and the account.
        if refreshed.provider_user_id is None and tokens.provider_user_id:
            return ProviderTokens(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token or tokens.refresh_token,
                expires_at=refreshed.expires_at,
                scopes=refreshed.scopes or tokens.scopes,
                provider_user_id=tokens.provider_user_id,
            )
        return refreshed

    async def revoke(self, *, tokens: ProviderTokens) -> None:
        """Delete the registration provider-side.

        Best-effort by design: `docs/13` §2.5 requires local disconnect to succeed
        even when Garmin is unreachable. An athlete asking us to disconnect must not
        be told "no" because a third party is down — we drop our tokens regardless
        and log the failure for a retry sweep.
        """
        try:
            await self._request("DELETE", g.REVOKE_URL, tokens=tokens)
        except ProviderError as exc:
            logger.warning("garmin_revoke_failed", provider=self.provider, reason=type(exc).__name__)

    async def granted_scopes(self, *, tokens: ProviderTokens) -> frozenset[str]:
        """What the athlete actually granted.

        May be narrower than requested. The product must degrade honestly — if
        `HEALTH_EXPORT` was declined there is no HRV, and readiness says so instead
        of scoring on nothing (`docs/13` §3).
        """
        return tokens.scopes

    # --------------------------------------------- inbound (pure, <100 ms path)

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> WebhookVerdict:
        """Verify the push notification.

        Returns `unsigned_by_design` for stock Garmin: they do not sign, and
        pretending otherwise by returning `signed_valid` would let the pipeline treat
        a forgeable body as authoritative data.
        """
        if not self._webhook_secret:
            return "unsigned_by_design"

        supplied = headers.get("x-garmin-signature") or headers.get("X-Garmin-Signature")
        if not supplied:
            return "signature_invalid"

        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        # Signature computed over the raw body, before any parsing: a forged payload
        # must not reach the JSON parser, let alone the database.
        return "signed_valid" if hmac.compare_digest(supplied.strip().lower(), expected) else "signature_invalid"

    def event_key(self, *, headers: Mapping[str, str], body: bytes) -> str:
        """The idempotency key for this delivery.

        Garmin sends no event id, so the key is a digest of the body. Two identical
        retries collapse; two genuinely different batches do not. Hashing the body is
        also why the raw bytes are persisted before parsing — the key must be
        derivable again during a replay.
        """
        if supplied := headers.get("x-garmin-event-id") or headers.get("X-Garmin-Event-Id"):
            return f"garmin:{supplied}"
        return f"garmin:sha256:{hashlib.sha256(body).hexdigest()}"

    def subjects(self, *, body: bytes) -> Sequence[EventSubject]:
        """Parse a batched push into per-athlete, per-type subjects."""
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            # Malformed body: the raw row is already persisted, so a parser fix plus a
            # replay recovers it. Returning empty means "nothing to enqueue".
            logger.warning("garmin_webhook_unparseable", provider=self.provider)
            return ()
        if not isinstance(document, dict):
            return ()

        found: list[EventSubject] = []
        for key, items in document.items():
            if not isinstance(items, list):
                continue
            kind = g.ACTIVITY_KEYS.get(key) or g.WELLNESS_KEYS.get(key)
            if kind is None:
                if key in ("deregistrations", "userPermissionsChange"):
                    kind = "deregistration"
                else:
                    # An unrecognised key is a Garmin API addition. Log once and skip
                    # rather than guessing what it means.
                    logger.info("garmin_webhook_unknown_key", key=key)
                    continue
            for item in items:
                if isinstance(item, dict):
                    subject = self._subject_from_item(item, kind)
                    if subject is not None:
                        found.append(subject)
        return tuple(found)

    def _subject_from_item(self, item: dict[str, Any], kind: str) -> EventSubject | None:
        provider_user_id = item.get("userId")
        if not isinstance(provider_user_id, str) or not provider_user_id:
            # Without an athlete we cannot route it. The raw event is retained so the
            # subject can be resolved later if Garmin clarifies the payload.
            return None

        ref = item.get("summaryId") or item.get("activityId") or item.get("calendarDate")
        start = item.get("startTimeInSeconds")
        occurred = self._epoch_to_utc(start) if isinstance(start, int | float) else None

        return EventSubject(
            provider_user_id=provider_user_id,
            kind=kind,  # type: ignore[arg-type]
            provider_ref=str(ref) if ref is not None else None,
            occurred_at=occurred,
        )

    def plan_fetches(self, *, subject: EventSubject) -> Sequence[FetchTask]:
        """What to fetch for a subject.

        Note what this does *not* do: it never reads a metric out of the webhook
        body. Garmin's push includes full summaries, and using them would mean an
        unsigned, forgeable payload writing an athlete's training history. We fetch
        the same data over an authenticated connection instead — the event tells us
        *that* something changed, never *what* it is.
        """
        if subject.kind == "deregistration":
            # Handled by the training service, not by fetching anything.
            return ()

        if subject.kind == "activity":
            if subject.provider_ref is None:
                return ()
            return (
                FetchTask(
                    kind="activity_summary",
                    provider_user_id=subject.provider_user_id,
                    provider_ref=subject.provider_ref,
                ),
                FetchTask(
                    kind="activity_details",
                    provider_user_id=subject.provider_user_id,
                    provider_ref=subject.provider_ref,
                ),
            )

        kind_map = {"dailies": "dailies", "sleep": "sleep", "hrv": "hrv"}
        task_kind = kind_map.get(subject.kind)
        if task_kind is None:
            return ()

        # A wellness subject names a calendar day; re-fetch that day's window.
        anchor = subject.occurred_at or dt.datetime.now(dt.UTC)
        day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            FetchTask(
                kind=task_kind,  # type: ignore[arg-type]
                provider_user_id=subject.provider_user_id,
                window_start=day_start,
                window_end=day_start + dt.timedelta(days=1),
            ),
        )

    def backfill_tasks(self, *, since: dt.date, until: dt.date) -> Sequence[FetchTask]:
        """Chunk a history import into provider-sized windows.

        Chunking bounds the damage of one failed request during a two-year import:
        a failure retries 90 days, not the whole history. Tasks are marked
        `backfill` so a live webhook is never queued behind them.
        """
        if until < since:
            raise ValueError("backfill window ends before it starts")

        horizon = dt.timedelta(days=self.max_backfill_window_days)
        earliest = until - horizon
        if since < earliest:
            since = earliest

        tasks: list[FetchTask] = []
        cursor = since
        step = dt.timedelta(days=g.BACKFILL_CHUNK_DAYS)
        while cursor < until:
            chunk_end = min(cursor + step, until)
            window_start = dt.datetime.combine(cursor, dt.time.min, tzinfo=dt.UTC)
            window_end = dt.datetime.combine(chunk_end, dt.time.min, tzinfo=dt.UTC)
            for kind in ("activity_summary", "dailies", "sleep"):
                tasks.append(
                    FetchTask(
                        kind=kind,
                        provider_user_id="",  # filled in by the caller that owns the connection
                        window_start=window_start,
                        window_end=window_end,
                        priority="backfill",
                    )
                )
            cursor = chunk_end
        return tuple(tasks)

    # ------------------------------------------------------- outbound (network)

    async def fetch(self, *, task: FetchTask, tokens: ProviderTokens) -> RawPayload:
        path, params = self._endpoint_for(task)
        payload = await self._request("GET", f"{g.API_BASE}{path}", tokens=tokens, params=params)
        return RawPayload(
            provider=self.provider,
            kind=task.kind,
            body=payload,
            provider_user_id=task.provider_user_id or tokens.provider_user_id,
            provider_ref=task.provider_ref,
            fetched_at=dt.datetime.now(dt.UTC),
        )

    async def commit(self, *, cursor: SyncCursor | None, tokens: ProviderTokens) -> None:
        """No-op: Garmin pushes and does not require batch acknowledgement.

        Present because Polar does require it (`docs/13` §5.3), and a provider whose
        adapter silently lacked the call would lose data.
        """
        return None

    def _endpoint_for(self, task: FetchTask) -> tuple[str, dict[str, str]]:
        window: dict[str, str] = {}
        if task.window_start and task.window_end:
            # Garmin filters on upload time, not activity time — an activity synced
            # today for a ride three days ago appears in today's window. Querying by
            # start time would silently miss late syncs, which is the single most
            # common cause of "my ride never appeared".
            window = {
                "uploadStartTimeInSeconds": str(int(task.window_start.timestamp())),
                "uploadEndTimeInSeconds": str(int(task.window_end.timestamp())),
            }

        match task.kind:
            case "activity_summary":
                if task.provider_ref:
                    return "/activities", {"summaryId": task.provider_ref}
                return "/activities", window
            case "activity_details":
                if task.provider_ref:
                    return "/activityDetails", {"summaryId": task.provider_ref}
                return "/activityDetails", window
            case "activity_file":
                return f"/activityFile/{task.provider_ref or ''}", {}
            case "dailies":
                return "/dailies", window
            case "sleep":
                return "/sleeps", window
            case "hrv":
                return "/hrv", window
        raise ProviderError(f"unsupported fetch kind {task.kind!r}")

    async def _post_form(self, url: str, form: Mapping[str, str]) -> dict[str, Any]:
        raw = await self._request("POST", url, form=form)
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderError("Garmin token endpoint returned a non-JSON body") from exc
        if not isinstance(document, dict):
            raise ProviderError("Garmin token endpoint returned an unexpected shape")
        return document

    async def _request(
        self,
        method: str,
        url: str,
        *,
        tokens: ProviderTokens | None = None,
        params: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> bytes:
        if self._http is None:
            raise ProviderError("Garmin adapter has no HTTP client configured")

        headers = {"Accept": "application/json"}
        if tokens is not None:
            headers["Authorization"] = f"Bearer {tokens.access_token}"

        try:
            response = await self._http.request(
                method, url, headers=headers, params=dict(params or {}), data=dict(form) if form else None
            )
        except Exception as exc:  # transport failure: timeout, DNS, connection reset
            raise ProviderUnavailable(f"Garmin request failed: {type(exc).__name__}") from exc

        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else self.rate_limit.default_backoff_s
            raise ProviderRateLimited("Garmin rate limit reached", retry_after_s=wait)
        if status in (401, 403):
            raise ProviderTokenExpired("Garmin rejected the stored credential")
        if status >= 500:
            raise ProviderUnavailable(f"Garmin returned {status}")
        if status >= 400:
            raise ProviderError(f"Garmin returned {status}")
        # `self._http` is deliberately `Any` so a test can inject a fake transport;
        # pin the type here so nothing downstream inherits that `Any`.
        content: bytes = response.content
        return content

    def _tokens_from_response(self, payload: Mapping[str, Any]) -> ProviderTokens:
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise ProviderError("Garmin token response carried no access token")

        expires_at = None
        if isinstance(expires_in := payload.get("expires_in"), int | float):
            expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=float(expires_in))

        raw_scope = payload.get("scope")
        scopes = frozenset(raw_scope.split()) if isinstance(raw_scope, str) else frozenset()

        return ProviderTokens(
            access_token=access,
            refresh_token=payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else None,
            expires_at=expires_at,
            scopes=scopes,
            provider_user_id=payload.get("user_id") if isinstance(payload.get("user_id"), str) else None,
        )

    # ------------------------------------------------- normalisation (all pure)

    def normalise_activity(self, payload: RawPayload) -> NormalisedActivity | None:
        """Map a Garmin activity summary to our shape, or `None`.

        `None` when the payload is not a meaningful session: no id, no start time, no
        positive duration. A zero-filled row would enter every load average and
        rolling baseline the athlete has, so refusing is the only safe answer.
        """
        document = self._first_object(payload.body)
        if document is None:
            return None

        provider_activity_id = document.get("summaryId") or document.get("activityId")
        provider_user_id = document.get("userId") or payload.provider_user_id
        start_epoch = document.get("startTimeInSeconds")
        duration = coerce_positive(document.get("durationInSeconds"))

        if provider_activity_id is None or not provider_user_id or duration is None:
            return None
        if not isinstance(start_epoch, int | float) or start_epoch < _MIN_PLAUSIBLE_EPOCH:
            return None

        start_time = self._epoch_to_utc(start_epoch)
        offset = document.get("startTimeOffsetInSeconds")
        offset_s = int(offset) if isinstance(offset, int | float) else None

        distance = coerce_positive(document.get("distanceInMeters"))
        avg_speed = coerce_positive(document.get("averageSpeedInMetersPerSecond"))

        return NormalisedActivity(
            provider=self.provider,
            provider_activity_id=str(provider_activity_id),
            provider_user_id=str(provider_user_id),
            sport=map_sport(document.get("activityType")),
            sub_sport=document.get("activityType"),
            start_time=start_time,
            local_date=local_date_of(start_time, offset_s),
            timezone_offset_s=offset_s,
            duration_s=duration,
            moving_duration_s=coerce_positive(document.get("movingDurationInSeconds")),
            distance_m=distance,
            elevation_gain_m=coerce_positive(document.get("totalElevationGainInMeters")),
            avg_hr=coerce_positive(document.get("averageHeartRateInBeatsPerMinute"), upper=260),
            max_hr=coerce_positive(document.get("maxHeartRateInBeatsPerMinute"), upper=260),
            avg_power_w=coerce_positive(document.get("averagePowerInWatts"), upper=3000),
            max_power_w=coerce_positive(document.get("maxPowerInWatts"), upper=4000),
            normalised_power_w=coerce_positive(document.get("normalizedPowerInWatts"), upper=3000),
            # Fall back to the derived value when Garmin omits average speed, so a
            # missing field does not cost us the load calculation.
            avg_speed_m_s=avg_speed or speed_from_distance_duration(distance, duration),
            max_speed_m_s=coerce_positive(document.get("maxSpeedInMetersPerSecond")),
            avg_cadence_rpm=coerce_positive(document.get("averageRunCadenceInStepsPerMinute"))
            or coerce_positive(document.get("averageBikeCadenceInRoundsPerMinute")),
            calories_kcal=coerce_positive(document.get("activeKilocalories")),
            device_name=document.get("deviceName") if isinstance(document.get("deviceName"), str) else None,
            is_manual=bool(document.get("manual", False)),
            title=document.get("activityName") if isinstance(document.get("activityName"), str) else None,
            laps=self._normalise_laps(document),
        )

    def _normalise_laps(self, document: Mapping[str, Any]) -> tuple[NormalisedLap, ...]:
        raw = document.get("laps")
        if not isinstance(raw, list):
            return ()
        laps: list[NormalisedLap] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            duration = coerce_positive(item.get("durationInSeconds"))
            if duration is None:
                continue
            laps.append(
                NormalisedLap(
                    lap_index=index + 1,
                    duration_s=duration,
                    distance_m=coerce_positive(item.get("distanceInMeters")),
                    avg_hr=coerce_positive(item.get("averageHeartRateInBeatsPerMinute"), upper=260),
                    avg_power_w=coerce_positive(item.get("averagePowerInWatts"), upper=3000),
                    avg_speed_m_s=coerce_positive(item.get("averageSpeedInMetersPerSecond")),
                    avg_cadence_rpm=coerce_positive(item.get("averageCadence")),
                )
            )
        return tuple(laps)

    def normalise_wellness(self, payload: RawPayload) -> Sequence[NormalisedWellnessDay]:
        """Map dailies / sleep / HRV payloads to athlete-days.

        Garmin splits one day across several endpoints, so this returns one partial
        day per payload and the training module merges them by `(user, local_date)`.
        Merging here would need a database read, which the purity contract forbids.
        """
        documents = self._all_objects(payload.body)
        days: list[NormalisedWellnessDay] = []

        for document in documents:
            provider_user_id = document.get("userId") or payload.provider_user_id
            if not provider_user_id:
                continue

            local_date = self._wellness_date(document)
            if local_date is None:
                continue

            day = NormalisedWellnessDay(
                provider=self.provider,
                provider_user_id=str(provider_user_id),
                local_date=local_date,
                hrv_rmssd_ms=coerce_positive(
                    document.get("lastNightAvg") or document.get("hrvRmssd"), upper=400
                ),
                resting_hr=coerce_positive(document.get("restingHeartRateInBeatsPerMinute"), upper=200),
                sleep_duration_s=coerce_positive(document.get("sleepTimeInSeconds")),
                sleep_score=coerce_positive(
                    (document.get("overallSleepScore") or {}).get("value")
                    if isinstance(document.get("overallSleepScore"), dict)
                    else document.get("sleepScore"),
                    upper=100,
                ),
                deep_sleep_s=coerce_positive(document.get("deepSleepDurationInSeconds")),
                rem_sleep_s=coerce_positive(document.get("remSleepInSeconds")),
                respiration_rate=coerce_positive(document.get("avgWakingRespirationValue"), upper=60),
                spo2_pct=coerce_positive(document.get("averageSpo2"), upper=100),
                stress_avg=coerce_positive(document.get("averageStressLevel"), upper=100),
                body_battery_min=coerce_positive(document.get("bodyBatteryLowestValue"), upper=100),
                body_battery_max=coerce_positive(document.get("bodyBatteryHighestValue"), upper=100),
                weight_kg=self._grams_to_kg(document.get("weightInGrams")),
                vo2max=coerce_positive(document.get("vo2Max"), upper=100),
                steps=int(document["steps"]) if isinstance(document.get("steps"), int | float) else None,
            )
            days.append(day)
        return tuple(days)

    def extract_stream(self, payload: RawPayload) -> StreamBlob | None:
        """Pull sample arrays out for object storage.

        Samples are the bulk of the data and are read rarely after the first
        analysis, so they never enter Postgres — the row keeps a key and a checksum
        (`docs/02` §2).
        """
        document = self._first_object(payload.body)
        if document is None:
            return None
        samples = document.get("samples")
        if not isinstance(samples, list) or not samples:
            return None

        channels = sorted({key for s in samples if isinstance(s, dict) for key in s})
        ref = document.get("summaryId") or document.get("activityId") or payload.provider_ref
        if ref is None:
            return None

        content = json.dumps(samples, separators=(",", ":"), sort_keys=True).encode()
        return StreamBlob(
            provider_ref=str(ref),
            content=content,
            content_type="application/json",
            sample_count=len(samples),
            channels=tuple(channels),
            checksum_sha256=checksum(content),
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _epoch_to_utc(epoch: float) -> dt.datetime:
        return dt.datetime.fromtimestamp(float(epoch), tz=dt.UTC)

    @staticmethod
    def _grams_to_kg(grams: Any) -> float | None:
        value = coerce_positive(grams)
        return value / 1000.0 if value is not None else None

    @staticmethod
    def _wellness_date(document: Mapping[str, Any]) -> dt.date | None:
        if isinstance(calendar_date := document.get("calendarDate"), str):
            try:
                return dt.date.fromisoformat(calendar_date)
            except ValueError:
                return None
        start = document.get("startTimeInSeconds")
        if isinstance(start, int | float) and start >= _MIN_PLAUSIBLE_EPOCH:
            offset = document.get("startTimeOffsetInSeconds")
            offset_s = int(offset) if isinstance(offset, int | float) else None
            return local_date_of(GarminAdapter._epoch_to_utc(start), offset_s)
        return None

    @staticmethod
    def _first_object(body: bytes) -> dict[str, Any] | None:
        objects = GarminAdapter._all_objects(body)
        return objects[0] if objects else None

    @staticmethod
    def _all_objects(body: bytes) -> tuple[dict[str, Any], ...]:
        """Garmin returns either a bare object or a list of them, depending on route."""
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            return ()
        if isinstance(document, dict):
            return (document,)
        if isinstance(document, list):
            return tuple(item for item in document if isinstance(item, dict))
        return ()
