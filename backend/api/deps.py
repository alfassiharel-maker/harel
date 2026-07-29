"""Dependency wiring and request-scoped authentication.

The long-lived objects — the database engine, the token and password services,
each module's service — are built once in `lifespan` (see `main.py`) and stashed
on `app.state`. The dependencies here read them from there. Nothing process-wide
is constructed at import time, so a test can build its own `AppServices` with a
throwaway database and never touch a global.

`require_principal` is the single choke point where a bearer token becomes a
`Principal`. Every athlete-scoped route depends on it, so there is exactly one
place to audit for "can an unauthenticated request reach this data".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.core.config import Settings
from backend.core.context import Principal
from backend.core.errors import Unauthenticated
from backend.core.security import PasswordService, TokenService
from backend.database.session import Database
from backend.modules.identity import IdentityService
from backend.modules.training import TrainingService

__all__ = [
    "AppServices",
    "CurrentPrincipal",
    "get_identity_service",
    "get_services",
    "get_settings_dep",
    "get_training_service",
    "require_principal",
]


@dataclass(frozen=True, slots=True)
class AppServices:
    """Everything the routers need, assembled once at startup.

    A single container rather than a scatter of `app.state.x` reads: the type is
    checked, and a test constructs the whole graph in one place.
    """

    settings: Settings
    database: Database
    passwords: PasswordService
    tokens: TokenService
    identity: IdentityService
    training: TrainingService


def get_services(request: Request) -> AppServices:
    services = getattr(request.app.state, "services", None)
    # `app.state` is untyped, so `isinstance` both narrows the type for the type
    # checker and guards against the only way this is None: the lifespan not
    # having run, which is a programming error, not a client one.
    if not isinstance(services, AppServices):  # pragma: no cover - lifespan invariant
        raise RuntimeError("AppServices not initialised; lifespan did not run")
    return services


def get_settings_dep(services: Annotated[AppServices, Depends(get_services)]) -> Settings:
    return services.settings


def get_identity_service(
    services: Annotated[AppServices, Depends(get_services)],
) -> IdentityService:
    return services.identity


def get_training_service(
    services: Annotated[AppServices, Depends(get_services)],
) -> TrainingService:
    return services.training


# auto_error=False: an absent or malformed Authorization header must surface as
# our own RFC 9457 `Unauthenticated` (with the WWW-Authenticate challenge), not as
# FastAPI's bare `{"detail": "Not authenticated"}`, which is off-contract.
_bearer = HTTPBearer(auto_error=False)


def require_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    services: Annotated[AppServices, Depends(get_services)],
) -> Principal:
    """Verify the access token and return the caller's `Principal`.

    The token signature, expiry, issuer and audience are all checked in
    `TokenService.decode_access_token`; anything wrong there raises
    `Unauthenticated` with a message that deliberately does not say which check
    failed. The resulting principal is also attached to the request's
    `RequestContext` so log lines and audit rows pick up the `user_id`.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthenticated("Authentication required.")

    principal = services.tokens.decode_access_token(credentials.credentials)

    # Enrich the ambient request context (set by middleware) with the now-known
    # identity, so structured logs for the rest of the request carry user_id.
    context = getattr(request.state, "context", None)
    if context is not None:
        from backend.core.context import RequestContext, set_context

        enriched = RequestContext(
            request_id=context.request_id,
            principal=principal,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            route=context.route,
        )
        request.state.context = enriched
        set_context(enriched)

    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
