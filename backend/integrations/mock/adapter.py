"""A deterministic synthetic provider. The thing that unblocks everything else.

Garmin Developer Program approval has a lead time measured in weeks and is outside
our control (`docs/12` §5, risk R1). Without this adapter, ingest, analytics
integration and the AI coach all sit idle waiting for a third party. With it, every
downstream layer can be built and tested against a realistic athlete today, and
switching to Garmin is a configuration change.

It is a **real adapter**, not a stub: it satisfies the same Protocol, produces the
same normalised DTOs, and goes through the same pipeline. That is what makes it a
contract test rather than a mock that lies. If the pipeline works against this
adapter and the Garmin adapter's pure methods pass their fixture tests, the two
compose.

**Determinism is the whole point.** `CLAUDE.md` forbids randomness in fixtures, and
for good reason: a flaky physiology test is worse than no test. Every value here is
derived from a seeded hash of `(athlete, day)`, so the same athlete on the same date
always produces byte-identical data — across runs, machines and CI. The synthetic
athlete also has *deliberate structure*: a build/recovery cycle, a bad-sleep week,
and one missing HRV day, so the analytics engine's edge cases (ramp caps, readiness
refusal on thin data, `None` handling) are exercised by the default fixture rather
than needing hand-built ones.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import struct
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from backend.integrations.normalisation import checksum, local_date_of
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

__all__ = ["MockAdapter", "synthesise_activity", "synthesise_wellness"]

# A fixed epoch so a synthetic athlete's history is stable regardless of when the
# test runs. Tests that need "today" pass an explicit date.
SYNTHETIC_EPOCH = dt.date(2026, 1, 1)

# The weekly pattern of a real age-group triathlete: three build weeks then a
# recovery week. Multiplies the day's base load, so CTL/ATL/TSB and the ramp cap all
# see a realistic shape instead of a flat line that hides bugs.
_WEEK_CYCLE = (1.0, 1.08, 1.16, 0.55)

# Day-of-week session plan. Sunday is the long session; Friday is rest.
_WEEK_PLAN: tuple[tuple[str, int, str] | None, ...] = (
    ("run", 3600, "easy"),  # Monday
    ("bike", 4500, "threshold"),  # Tuesday
    ("swim", 2700, "technique"),  # Wednesday
    ("run", 3000, "intervals"),  # Thursday
    None,  # Friday — rest, so the engine sees gaps
    ("strength", 2400, "strength"),  # Saturday
    ("bike", 9000, "long"),  # Sunday
)


def _seed(*parts: object) -> int:
    """A stable 64-bit seed from any key. Same inputs, same number, forever."""
    joined = "|".join(str(p) for p in parts).encode()
    (value,) = struct.unpack("!Q", hashlib.sha256(joined).digest()[:8])
    return int(value)


def _jitter(seed: int, spread: float) -> float:
    """Deterministic pseudo-variation in `[1 - spread, 1 + spread]`.

    Not `random`: a seeded PRNG would still depend on call order, so two tests that
    request the same day in a different sequence would disagree. A pure function of
    the seed cannot.
    """
    unit = (seed % 10_000) / 10_000.0
    return 1.0 + (unit * 2.0 - 1.0) * spread


def synthesise_activity(
    provider_user_id: str, day: dt.date, *, provider: str = "mock"
) -> dict[str, Any] | None:
    """One day's session as a provider-shaped payload, or `None` on a rest day."""
    plan = _WEEK_PLAN[day.weekday()]
    if plan is None:
        return None

    sport, base_duration, flavour = plan
    week_index = ((day - SYNTHETIC_EPOCH).days // 7) % len(_WEEK_CYCLE)
    load_factor = _WEEK_CYCLE[week_index]
    seed = _seed(provider_user_id, day.isoformat(), sport)

    duration = round(base_duration * load_factor * _jitter(seed, 0.08))
    start = dt.datetime.combine(day, dt.time(hour=6, minute=30), tzinfo=dt.UTC)

    payload: dict[str, Any] = {
        "summaryId": f"{provider}-{provider_user_id}-{day.isoformat()}-{sport}",
        "userId": provider_user_id,
        "activityType": {"run": "RUNNING", "bike": "CYCLING", "swim": "LAP_SWIMMING", "strength": "STRENGTH_TRAINING"}[sport],
        "startTimeInSeconds": int(start.timestamp()),
        "startTimeOffsetInSeconds": 7200,  # Asia/Jerusalem summer offset
        "durationInSeconds": duration,
        "activityName": f"{flavour.title()} {sport}",
        "manual": False,
        "deviceName": "synthetic-watch",
    }

    intensity = {"easy": 0.72, "technique": 0.75, "strength": 0.70, "long": 0.78, "threshold": 0.91, "intervals": 0.94}[flavour]

    # Heart rate on every session including strength: an athlete lifting while
    # wearing a watch records HR, so a strength activity with no channels at all is
    # not something a real provider would send. Getting this wrong made the fixture
    # trip the `no_sensor_channels` quality flag, which would have degraded every
    # downstream test that used a Saturday.
    payload["averageHeartRateInBeatsPerMinute"] = round(165 * intensity * _jitter(seed + 2, 0.03))
    payload["maxHeartRateInBeatsPerMinute"] = round(178 * intensity * _jitter(seed + 3, 0.02))

    distance = 0.0
    if sport in ("run", "bike", "swim"):
        speed = {"run": 3.2, "bike": 8.4, "swim": 1.15}[sport] * intensity * _jitter(seed + 1, 0.05)
        distance = speed * duration
        payload["distanceInMeters"] = round(distance, 1)
        payload["averageSpeedInMetersPerSecond"] = round(speed, 3)
        payload["maxSpeedInMetersPerSecond"] = round(speed * 1.25, 3)

    if sport == "bike":
        # Only the bike has a power meter — deliberately, so the per-sport load
        # precedence table is exercised: bike uses power, run falls back to pace.
        power = 235.0 * intensity * _jitter(seed + 4, 0.06)
        payload["averagePowerInWatts"] = round(power, 1)
        payload["normalizedPowerInWatts"] = round(power * 1.04, 1)
        payload["maxPowerInWatts"] = round(power * 2.1, 1)
        payload["averageBikeCadenceInRoundsPerMinute"] = round(88 * _jitter(seed + 5, 0.04))
    elif sport == "run":
        payload["averageRunCadenceInStepsPerMinute"] = round(168 * _jitter(seed + 5, 0.03))
        payload["totalElevationGainInMeters"] = round(distance * 0.012, 1)

    if flavour == "intervals":
        payload["laps"] = [
            {
                "durationInSeconds": round(duration / 6),
                "distanceInMeters": round(distance / 6, 1) if sport != "strength" else None,
                "averageHeartRateInBeatsPerMinute": round(150 + index * 4),
            }
            for index in range(6)
        ]
    return payload


def synthesise_wellness(provider_user_id: str, day: dt.date) -> dict[str, Any] | None:
    """One day's overnight data, or `None` for a deliberate gap.

    Two structural absences, both intentional so the engine's honest-refusal paths
    are covered by the default fixture:
    * every 23rd day has no HRV at all — the watch was not worn;
    * days 40-46 have poor sleep, so readiness drops for a reason the drivers can
      explain rather than by an injected constant.
    """
    offset = (day - SYNTHETIC_EPOCH).days
    if offset % 23 == 0:
        return None

    seed = _seed(provider_user_id, day.isoformat(), "wellness")
    bad_sleep_week = 40 <= offset <= 46

    sleep_hours = (5.4 if bad_sleep_week else 7.6) * _jitter(seed, 0.06)
    hrv = (44.0 if bad_sleep_week else 58.0) * _jitter(seed + 1, 0.09)

    return {
        "userId": provider_user_id,
        "calendarDate": day.isoformat(),
        "lastNightAvg": round(hrv, 1),
        "restingHeartRateInBeatsPerMinute": round((54 if not bad_sleep_week else 59) * _jitter(seed + 2, 0.03)),
        "sleepTimeInSeconds": round(sleep_hours * 3600),
        "sleepScore": round((58 if bad_sleep_week else 82) * _jitter(seed + 3, 0.05)),
        "deepSleepDurationInSeconds": round(sleep_hours * 3600 * 0.18),
        "remSleepInSeconds": round(sleep_hours * 3600 * 0.22),
        "averageStressLevel": round((42 if bad_sleep_week else 26) * _jitter(seed + 4, 0.08)),
        "bodyBatteryLowestValue": round((12 if bad_sleep_week else 24) * _jitter(seed + 5, 0.1)),
        "bodyBatteryHighestValue": round((61 if bad_sleep_week else 88) * _jitter(seed + 6, 0.05)),
        "averageSpo2": round(96 * _jitter(seed + 7, 0.01)),
        "steps": round(9500 * _jitter(seed + 8, 0.2)),
    }


class MockAdapter:
    """A fully-functional synthetic provider. Satisfies `ProviderAdapter`.

    Reuses the Garmin payload vocabulary on purpose: the synthetic payloads go
    through Garmin-shaped normalisation, so this adapter also exercises that parsing
    rather than bypassing it.
    """

    provider: ClassVar[str] = "mock"
    auth_style: ClassVar[AuthStyle] = "oauth2_code"
    delivery: ClassVar[Delivery] = "webhook_push"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {"activity_summary", "activity_laps", "activity_samples", "daily_summary", "sleep", "hrv"}
    )
    rate_limit: ClassVar[RateLimitPolicy] = RateLimitPolicy(requests_per_minute=10_000, max_concurrent=16)
    max_backfill_window_days: ClassVar[int] = 3650

    def __init__(self, *, webhook_secret: str = "mock-secret") -> None:  # noqa: S107
        # Signed, unlike Garmin — so the pipeline's `signed_valid` branch has a
        # provider to exercise it, and a signature-verification regression is caught
        # by a test rather than at a partner integration.
        self._webhook_secret = webhook_secret

    # ------------------------------------------------------------ authorisation

    def authorize(self, *, redirect_uri: str, scopes: Sequence[str]) -> AuthorizeChallenge:
        state = f"mock-state-{_seed(redirect_uri, tuple(scopes)) % 10**12}"
        return AuthorizeChallenge(
            authorize_url=f"https://mock.invalid/authorize?state={state}",
            state=state,
            verifier="mock-verifier",
        )

    async def exchange(
        self, *, params: Mapping[str, str], challenge: AuthorizeChallenge
    ) -> ProviderTokens:
        if params.get("state") != challenge.state:
            raise ValueError("state mismatch")
        athlete = params.get("code", "athlete-1")
        return ProviderTokens(
            access_token=f"mock-access-{athlete}",
            refresh_token=f"mock-refresh-{athlete}",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=24),
            scopes=frozenset(scopes_of(challenge)),
            provider_user_id=athlete,
        )

    async def refresh(self, *, tokens: ProviderTokens) -> ProviderTokens | None:
        return ProviderTokens(
            access_token=f"{tokens.access_token}-refreshed",
            refresh_token=tokens.refresh_token,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=24),
            scopes=tokens.scopes,
            provider_user_id=tokens.provider_user_id,
        )

    async def revoke(self, *, tokens: ProviderTokens) -> None:
        return None

    async def granted_scopes(self, *, tokens: ProviderTokens) -> frozenset[str]:
        return tokens.scopes

    # ---------------------------------------------------------- inbound (pure)

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> WebhookVerdict:
        supplied = headers.get("x-mock-signature")
        if not supplied:
            return "signature_invalid"
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return "signed_valid" if hmac.compare_digest(supplied, expected) else "signature_invalid"

    def sign(self, body: bytes) -> str:
        """Test helper: produce the signature this adapter will accept."""
        return hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    def event_key(self, *, headers: Mapping[str, str], body: bytes) -> str:
        return f"mock:sha256:{hashlib.sha256(body).hexdigest()}"

    def subjects(self, *, body: bytes) -> Sequence[EventSubject]:
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            return ()
        if not isinstance(document, dict):
            return ()

        found: list[EventSubject] = []
        for key, kind in (("activities", "activity"), ("dailies", "dailies")):
            for item in document.get(key, []) or []:
                if not isinstance(item, dict) or not item.get("userId"):
                    continue
                start = item.get("startTimeInSeconds")
                found.append(
                    EventSubject(
                        provider_user_id=str(item["userId"]),
                        kind=kind,  # type: ignore[arg-type]
                        provider_ref=str(item.get("summaryId")) if item.get("summaryId") else None,
                        occurred_at=dt.datetime.fromtimestamp(start, tz=dt.UTC)
                        if isinstance(start, int | float)
                        else None,
                    )
                )
        return tuple(found)

    def plan_fetches(self, *, subject: EventSubject) -> Sequence[FetchTask]:
        kind = "activity_summary" if subject.kind == "activity" else "dailies"
        return (
            FetchTask(
                kind=kind,  # type: ignore[arg-type]
                provider_user_id=subject.provider_user_id,
                provider_ref=subject.provider_ref,
                window_start=subject.occurred_at,
            ),
        )

    def backfill_tasks(self, *, since: dt.date, until: dt.date) -> Sequence[FetchTask]:
        if until < since:
            raise ValueError("backfill window ends before it starts")
        tasks: list[FetchTask] = []
        day = since
        while day < until:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC)
            tasks.append(
                FetchTask(
                    kind="activity_summary",
                    provider_user_id="",
                    window_start=start,
                    window_end=start + dt.timedelta(days=1),
                    priority="backfill",
                )
            )
            day += dt.timedelta(days=1)
        return tuple(tasks)

    # ------------------------------------------------------ outbound (no network)

    async def fetch(self, *, task: FetchTask, tokens: ProviderTokens) -> RawPayload:
        """Synthesise instead of calling out. Same return type, same pipeline."""
        athlete = task.provider_user_id or tokens.provider_user_id or "athlete-1"
        day = (task.window_start or dt.datetime.now(dt.UTC)).date()

        if task.kind in ("activity_summary", "activity_details"):
            body = synthesise_activity(athlete, day) or {}
        else:
            body = synthesise_wellness(athlete, day) or {}

        return RawPayload(
            provider=self.provider,
            kind=task.kind,
            body=json.dumps(body, sort_keys=True).encode(),
            provider_user_id=athlete,
            provider_ref=task.provider_ref,
            fetched_at=dt.datetime.now(dt.UTC),
        )

    async def commit(self, *, cursor: SyncCursor | None, tokens: ProviderTokens) -> None:
        return None

    # ------------------------------------------------------- normalisation (pure)

    def normalise_activity(self, payload: RawPayload) -> NormalisedActivity | None:
        document = self._object(payload.body)
        if not document or not document.get("summaryId"):
            return None

        start = dt.datetime.fromtimestamp(float(document["startTimeInSeconds"]), tz=dt.UTC)
        offset_s = int(document.get("startTimeOffsetInSeconds") or 0)
        sport_map = {
            "RUNNING": "run",
            "CYCLING": "bike",
            "LAP_SWIMMING": "swim",
            "STRENGTH_TRAINING": "strength",
        }
        laps = tuple(
            NormalisedLap(
                lap_index=index + 1,
                duration_s=float(lap["durationInSeconds"]),
                distance_m=float(lap["distanceInMeters"]) if lap.get("distanceInMeters") else None,
                avg_hr=float(lap["averageHeartRateInBeatsPerMinute"])
                if lap.get("averageHeartRateInBeatsPerMinute")
                else None,
            )
            for index, lap in enumerate(document.get("laps", []) or [])
        )

        return NormalisedActivity(
            provider=self.provider,
            provider_activity_id=str(document["summaryId"]),
            provider_user_id=str(document["userId"]),
            sport=sport_map.get(document.get("activityType", ""), "other"),  # type: ignore[arg-type]
            sub_sport=document.get("activityType"),
            start_time=start,
            local_date=local_date_of(start, offset_s),
            timezone_offset_s=offset_s,
            duration_s=float(document["durationInSeconds"]),
            distance_m=_opt_float(document.get("distanceInMeters")),
            elevation_gain_m=_opt_float(document.get("totalElevationGainInMeters")),
            avg_hr=_opt_float(document.get("averageHeartRateInBeatsPerMinute")),
            max_hr=_opt_float(document.get("maxHeartRateInBeatsPerMinute")),
            avg_power_w=_opt_float(document.get("averagePowerInWatts")),
            normalised_power_w=_opt_float(document.get("normalizedPowerInWatts")),
            max_power_w=_opt_float(document.get("maxPowerInWatts")),
            avg_speed_m_s=_opt_float(document.get("averageSpeedInMetersPerSecond")),
            max_speed_m_s=_opt_float(document.get("maxSpeedInMetersPerSecond")),
            avg_cadence_rpm=_opt_float(
                document.get("averageBikeCadenceInRoundsPerMinute")
                or document.get("averageRunCadenceInStepsPerMinute")
            ),
            device_name=document.get("deviceName"),
            is_manual=bool(document.get("manual", False)),
            title=document.get("activityName"),
            laps=laps,
        )

    def normalise_wellness(self, payload: RawPayload) -> Sequence[NormalisedWellnessDay]:
        document = self._object(payload.body)
        if not document or not document.get("calendarDate"):
            return ()
        return (
            NormalisedWellnessDay(
                provider=self.provider,
                provider_user_id=str(document["userId"]),
                local_date=dt.date.fromisoformat(document["calendarDate"]),
                hrv_rmssd_ms=_opt_float(document.get("lastNightAvg")),
                resting_hr=_opt_float(document.get("restingHeartRateInBeatsPerMinute")),
                sleep_duration_s=_opt_float(document.get("sleepTimeInSeconds")),
                sleep_score=_opt_float(document.get("sleepScore")),
                deep_sleep_s=_opt_float(document.get("deepSleepDurationInSeconds")),
                rem_sleep_s=_opt_float(document.get("remSleepInSeconds")),
                spo2_pct=_opt_float(document.get("averageSpo2")),
                stress_avg=_opt_float(document.get("averageStressLevel")),
                body_battery_min=_opt_float(document.get("bodyBatteryLowestValue")),
                body_battery_max=_opt_float(document.get("bodyBatteryHighestValue")),
                steps=int(document["steps"]) if document.get("steps") else None,
            ),
        )

    def extract_stream(self, payload: RawPayload) -> StreamBlob | None:
        document = self._object(payload.body)
        if not document or not document.get("summaryId"):
            return None
        # A short deterministic HR trace, enough to prove the storage path works.
        duration = int(document.get("durationInSeconds", 0))
        base = int(document.get("averageHeartRateInBeatsPerMinute") or 140)
        samples = [
            {"offset_s": s, "hr": base + (_seed(document["summaryId"], s) % 11) - 5}
            for s in range(0, min(duration, 3600), 60)
        ]
        if not samples:
            return None
        content = json.dumps(samples, separators=(",", ":"), sort_keys=True).encode()
        return StreamBlob(
            provider_ref=str(document["summaryId"]),
            content=content,
            content_type="application/json",
            sample_count=len(samples),
            channels=("hr",),
            checksum_sha256=checksum(content),
        )

    @staticmethod
    def _object(body: bytes) -> dict[str, Any] | None:
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) and document else None


def _opt_float(value: Any) -> float | None:
    """`None` for absent or non-positive. Mirrors `normalisation.coerce_positive`."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def scopes_of(challenge: AuthorizeChallenge) -> tuple[str, ...]:
    """Recover requested scopes from a mock authorize URL."""
    if "scope=" not in challenge.authorize_url:
        return ("activity", "wellness")
    query = urllib.parse.urlparse(challenge.authorize_url).query
    return tuple(urllib.parse.parse_qs(query).get("scope", ["activity wellness"])[0].split())
