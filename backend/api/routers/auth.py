"""Authentication endpoints (docs/03 §2).

Transport only. Every rule that matters — timing-equalised login, refresh
rotation, reuse detection, lockout — lives in `IdentityService`; this router
validates the body, calls the service, and shapes the response. The Phase-2
endpoints (`/password/forgot`, `/email/verify`, `/mfa/*`) are intentionally
absent: they need an email/SMS delivery path that does not exist yet, and a
stub that returns 200 would be a security lie.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel

from backend.api.deps import CurrentPrincipal, get_identity_service
from backend.core.context import RequestContext
from backend.modules.identity import (
    IdentityService,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
    TokenPair,
    UserOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/auth", tags=["auth"])

IdentityDep = Annotated[IdentityService, Depends(get_identity_service)]


class AuthResponse(BaseModel):
    """The `{user, tokens}` envelope returned by register and login."""

    user: UserOut
    tokens: TokenPair


def _context(request: Request) -> RequestContext | None:
    return getattr(request.state, "context", None)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, service: IdentityDep) -> AuthResponse:
    outcome = await service.register(payload, _context(request))
    return AuthResponse(user=outcome.user, tokens=outcome.tokens)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, service: IdentityDep) -> AuthResponse:
    outcome = await service.login(payload, _context(request))
    return AuthResponse(user=outcome.user, tokens=outcome.tokens)


@router.post("/refresh")
async def refresh(payload: RefreshRequest, request: Request, service: IdentityDep) -> TokenPair:
    return await service.refresh(payload.refresh_token, _context(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, request: Request, service: IdentityDep) -> Response:
    # Idempotent by design: an unknown or already-revoked token returns 204. A
    # logout that could fail encourages clients to ignore the result.
    await service.logout(payload.refresh_token, _context(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(principal: CurrentPrincipal, request: Request, service: IdentityDep) -> Response:
    await service.logout_everywhere(principal, _context(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions")
async def list_sessions(principal: CurrentPrincipal, service: IdentityDep) -> list[SessionOut]:
    return await service.list_sessions(principal)
