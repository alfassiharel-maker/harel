"""CCP Forge — the application.

The user-facing product. `controller.py` is the whole application without a view
and is what the tests drive; `server.py` hosts it; `web.py` is the interface;
`launcher.py` starts it.

Depends on `ccp.integration` and `ccp.product`. It never reaches into
`ccp.core`.
"""

from .controller import (
    MAX_SELECTIVE_READ_BYTES,
    AppController,
    AppState,
    ArtifactSummary,
    BuildStatus,
    UserFacingError,
    describe_error,
)
from .launcher import launch, main
from .server import AppServer

__all__ = [
    "AppController",
    "AppServer",
    "AppState",
    "ArtifactSummary",
    "BuildStatus",
    "MAX_SELECTIVE_READ_BYTES",
    "UserFacingError",
    "describe_error",
    "launch",
    "main",
]
