"""Identity module — authentication, users, consent, audit.

Owns the `identity` Postgres schema. Other modules and the API layer import from
here and nowhere deeper: `models` and `repository` are private, and the
import-linter contract in `pyproject.toml` fails the build on a direct import of
either.

The DTOs are re-exported alongside the service because a caller cannot use the
service without them — they *are* the boundary contract.
"""

# The valid consent purposes are part of the boundary contract, not an internal
# detail: the API needs them to validate a path segment. Re-exported here so the
# transport layer never has to reach into `models` (which the architecture
# contract forbids).
from backend.modules.identity.models import CONSENT_PURPOSES, GRANT_SCOPES, USER_ROLES
from backend.modules.identity.schemas import (
    MIN_PASSWORD_LENGTH,
    ConsentIn,
    ConsentOut,
    GrantOut,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
    TokenPair,
    UserOut,
    UserUpdate,
)
from backend.modules.identity.service import AuthOutcome, IdentityService

__all__ = [
    "CONSENT_PURPOSES",
    "GRANT_SCOPES",
    "MIN_PASSWORD_LENGTH",
    "USER_ROLES",
    "AuthOutcome",
    "ConsentIn",
    "ConsentOut",
    "GrantOut",
    "IdentityService",
    "LoginRequest",
    "RefreshRequest",
    "RegisterRequest",
    "SessionOut",
    "TokenPair",
    "UserOut",
    "UserUpdate",
]
