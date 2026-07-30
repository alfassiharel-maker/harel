"""The `ProviderAdapter` contract, and the registry that resolves one by name.

Adding a provider means writing one class satisfying this Protocol and registering
it. It changes **zero lines** of `modules/training/service.py` — that is the
property the whole layer exists to provide (`docs/13` §5.5), and it is what makes
Polar, Suunto and Samsung a day of work each rather than a rewrite.

The method groups are separated by an execution constraint, not by taste:

* **pure, no I/O** — `verify_webhook`, `event_key`, `subjects`, `plan_fetches`,
  `normalise_*`, `extract_stream`, `backfill_tasks`. These run inside the sub-100 ms
  webhook budget or inside a worker's parse step. No network, no clock, no database.
  Consequence: the entire provider surface is testable from recorded fixtures, which
  is also the mock that unblocks development while Garmin approval is pending.
* **async, network permitted** — `exchange`, `refresh`, `revoke`,
  `granted_scopes`, `fetch`, `commit`. The only places provider HTTP may happen.

A method that reaches the network from the pure group will pass its unit test and
then blow the webhook budget in production, so the split is enforced by review and
by the fact that the pure tests run with no HTTP transport installed at all.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import ClassVar, Protocol, runtime_checkable

from backend.core.errors import AppError
from backend.integrations.types import (
    AuthorizeChallenge,
    AuthStyle,
    Capability,
    Delivery,
    EventSubject,
    FetchTask,
    NormalisedActivity,
    NormalisedWellnessDay,
    ProviderTokens,
    RateLimitPolicy,
    RawPayload,
    StreamBlob,
    SyncCursor,
    WebhookVerdict,
)

__all__ = [
    "ProviderAdapter",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRegistry",
    "ProviderTokenExpired",
    "ProviderUnavailable",
    "registry",
]


class ProviderError(AppError):
    """A provider misbehaved in a way the caller cannot fix by retrying."""

    status = 502
    code = "provider_error"
    title = "Provider request failed"


class ProviderUnavailable(ProviderError):
    """Transient: the provider is down or timed out. Retry with backoff."""

    status = 503
    code = "provider_unavailable"
    title = "Provider temporarily unavailable"


class ProviderRateLimited(ProviderError):
    """The provider throttled us. Carries the wait so the worker honours it."""

    status = 503
    code = "provider_rate_limited"
    title = "Provider rate limit reached"

    def __init__(self, detail: str | None = None, *, retry_after_s: float = 60.0, **kwargs: object) -> None:
        super().__init__(detail, **kwargs)  # type: ignore[arg-type]
        self.retry_after_s = retry_after_s


class ProviderTokenExpired(ProviderError):
    """The stored credential is dead and refresh did not recover it.

    Distinct from `ProviderUnavailable` because the remedy is different and
    user-visible: the athlete must reconnect, and retrying forever instead just
    burns rate limit and hides a broken connection behind a stale last-sync time.
    """

    status = 401
    code = "provider_token_expired"
    title = "Provider authorisation expired"


@runtime_checkable
class ProviderAdapter(Protocol):
    """Everything the platform needs to know about one data provider."""

    provider: ClassVar[str]  # matches training.provider_connections.provider
    auth_style: ClassVar[AuthStyle]
    delivery: ClassVar[Delivery]
    capabilities: ClassVar[frozenset[Capability]]
    rate_limit: ClassVar[RateLimitPolicy]
    max_backfill_window_days: ClassVar[int]

    # --- authorisation -------------------------------------------------------

    def authorize(self, *, redirect_uri: str, scopes: Sequence[str]) -> AuthorizeChallenge:
        """Build the URL to send the athlete to, plus the state to verify the return."""
        ...

    async def exchange(
        self, *, params: Mapping[str, str], challenge: AuthorizeChallenge
    ) -> ProviderTokens:
        """Trade the callback parameters for tokens. Must verify `state` itself."""
        ...

    async def refresh(self, *, tokens: ProviderTokens) -> ProviderTokens | None:
        """Refresh, or return `None` when this provider's tokens do not expire."""
        ...

    async def revoke(self, *, tokens: ProviderTokens) -> None:
        """Revoke provider-side. Best-effort: local deletion proceeds regardless."""
        ...

    async def granted_scopes(self, *, tokens: ProviderTokens) -> frozenset[str]:
        """What the athlete actually granted, which may be less than we asked for."""
        ...

    # --- inbound: pure, runs inside the <100 ms webhook budget ---------------

    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> WebhookVerdict:
        """Check the provider's signature over the raw body.

        Must be constant-time on the comparison and must not parse the body first:
        a forged payload gets to influence nothing, including our parser.
        """
        ...

    def event_key(self, *, headers: Mapping[str, str], body: bytes) -> str:
        """The provider's own event identifier — the idempotency key. Providers retry."""
        ...

    def subjects(self, *, body: bytes) -> Sequence[EventSubject]:
        """Which athletes and which data types this event refers to."""
        ...

    def plan_fetches(self, *, subject: EventSubject) -> Sequence[FetchTask]:
        """What to fetch for a subject. Never trusts values carried in the event."""
        ...

    def backfill_tasks(self, *, since: dt.date, until: dt.date) -> Sequence[FetchTask]:
        """Chunk a history import into provider-sized windows."""
        ...

    # --- outbound: the only place provider HTTP is allowed to happen ---------

    async def fetch(self, *, task: FetchTask, tokens: ProviderTokens) -> RawPayload:
        """Perform one provider call and return the bytes, uninterpreted."""
        ...

    async def commit(self, *, cursor: SyncCursor | None, tokens: ProviderTokens) -> None:
        """Acknowledge consumption. No-op for Garmin; load-bearing for Polar."""
        ...

    # --- normalisation: pure. No I/O, no clock, no network. ------------------

    def normalise_activity(self, payload: RawPayload) -> NormalisedActivity | None:
        """Map a payload to our shape, or `None` if it is not a meaningful session.

        `None` rather than a zero-filled row: a 4-second GPS artefact is not a
        training session, and inventing one corrupts every load average it enters.
        """
        ...

    def normalise_wellness(self, payload: RawPayload) -> Sequence[NormalisedWellnessDay]:
        """Map a payload to zero or more athlete-days."""
        ...

    def extract_stream(self, payload: RawPayload) -> StreamBlob | None:
        """Pull sample-level data out for object storage, if the payload carries any."""
        ...


class ProviderRegistry:
    """Name → adapter. The one place a provider string becomes an implementation.

    Registration is explicit rather than by module scanning: an adapter that is
    importable but not registered is unreachable, which is the correct default for a
    half-finished provider on a feature branch.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> ProviderAdapter:
        name = adapter.provider
        existing = self._adapters.get(name)
        if existing is not None and type(existing) is not type(adapter):
            raise ProviderError(
                f"provider {name!r} is already registered to {type(existing).__name__}"
            )
        self._adapters[name] = adapter
        return adapter

    def get(self, provider: str) -> ProviderAdapter:
        adapter = self._adapters.get(provider)
        if adapter is None:
            # Not a 404: the caller asked for a provider this build does not have,
            # which is a configuration fault, not a missing athlete resource.
            raise ProviderError(f"no adapter registered for provider {provider!r}")
        return adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def supporting(self, capability: Capability) -> tuple[str, ...]:
        return tuple(sorted(n for n, a in self._adapters.items() if capability in a.capabilities))

    def clear(self) -> None:
        """Test-only. Keeps one test's registrations out of the next one's registry."""
        self._adapters.clear()


# Process-wide registry. Populated at import of `backend.integrations`.
registry = ProviderRegistry()
