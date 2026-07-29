"""Account, consent and data-access endpoints (docs/03 §3).

Every route here is authenticated and acts on the caller's own identity. None of
them takes a user-id parameter: the principal comes from the verified token, and
an endpoint that accepted a target user id would be an IDOR waiting to happen
(OWASP A01). The database RLS context is set from that same principal, so even a
bug in a predicate cannot cross tenants.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from backend.api.deps import CurrentPrincipal, get_identity_service
from backend.core.context import RequestContext
from backend.core.errors import ValidationFailed
from backend.modules.identity import (
    CONSENT_PURPOSES,
    ConsentIn,
    ConsentOut,
    IdentityService,
    UserOut,
    UserUpdate,
)

__all__ = ["router"]

router = APIRouter(prefix="/me", tags=["me"])

IdentityDep = Annotated[IdentityService, Depends(get_identity_service)]


def _context(request: Request) -> RequestContext | None:
    return getattr(request.state, "context", None)


@router.get("")
async def get_me(principal: CurrentPrincipal, service: IdentityDep) -> UserOut:
    return await service.get_user(principal)


@router.patch("")
async def update_me(
    payload: UserUpdate, principal: CurrentPrincipal, request: Request, service: IdentityDep
) -> UserOut:
    return await service.update_user(principal, payload, _context(request))


@router.get("/consents")
async def get_consents(principal: CurrentPrincipal, service: IdentityDep) -> list[ConsentOut]:
    return await service.get_consents(principal)


@router.put("/consents/{purpose}")
async def set_consent(
    purpose: str,
    payload: ConsentIn,
    principal: CurrentPrincipal,
    request: Request,
    service: IdentityDep,
) -> ConsentOut:
    # Validate the path segment here so an unknown purpose is a 422 naming the
    # allowed set, not a generic service error. The service re-checks it too — the
    # transport layer is not the place isolation or correctness ultimately rests.
    if purpose not in CONSENT_PURPOSES:
        raise ValidationFailed(
            f"Unknown consent purpose '{purpose}'.",
            meta={"allowed": list(CONSENT_PURPOSES)},
        )
    return await service.set_consent(principal, purpose, payload, _context(request))
