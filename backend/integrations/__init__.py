"""Provider adapters.

Imports nothing from `backend.modules` — the dependency rule is
`modules → integrations`, never the reverse (`docs/01` §3). A normalised activity is
this layer's output and the training module's input.

Adapters are registered explicitly at import. An adapter that is importable but
unregistered is unreachable, which is the correct default for a half-finished
provider on a feature branch.
"""

from backend.integrations.base import (
    ProviderAdapter,
    ProviderError,
    ProviderRateLimited,
    ProviderRegistry,
    ProviderTokenExpired,
    ProviderUnavailable,
    registry,
)
from backend.integrations.mock.adapter import MockAdapter
from backend.integrations.types import (
    NormalisedActivity,
    NormalisedWellnessDay,
    ProviderTokens,
    RawPayload,
    StreamBlob,
)

# The mock provider is always available: it is what the ingest, analytics and AI
# layers are developed and tested against while Garmin approval is pending, and it
# is the fixture behind the integration suite. Registering it unconditionally is
# safe because reaching it still requires an athlete to hold a `mock`
# provider_connection, which only ever happens in local, CI and seeded demo data.
registry.register(MockAdapter())

# Garmin is registered by the API/worker entrypoint rather than here, because
# constructing it needs credentials and an HTTP client from settings. Importing this
# package must never require secrets to be present.

__all__ = [
    "MockAdapter",
    "NormalisedActivity",
    "NormalisedWellnessDay",
    "ProviderAdapter",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRegistry",
    "ProviderTokenExpired",
    "ProviderTokens",
    "ProviderUnavailable",
    "RawPayload",
    "StreamBlob",
    "registry",
]
