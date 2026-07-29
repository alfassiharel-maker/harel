"""Request and tenant context.

Held in a `ContextVar` so log records and audit writes can pick it up without
threading it through every call signature. It is *not* the authorisation
mechanism: database access is scoped by explicitly passing the principal into the
session (see `backend/database/session.py`), because an implicit ambient value is
exactly the kind of thing that silently carries over between requests.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = ["Principal", "RequestContext", "current_context", "reset_context", "set_context"]


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated identity, derived from a verified access token."""

    user_id: uuid.UUID
    role: str
    org_id: uuid.UUID | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in ("admin", "support")


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    principal: Principal | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    route: str | None = None

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.principal.user_id if self.principal else None

    @property
    def role(self) -> str | None:
        return self.principal.role if self.principal else None


_context: ContextVar[RequestContext | None] = ContextVar("request_context", default=None)


def current_context() -> RequestContext | None:
    return _context.get()


def set_context(context: RequestContext) -> object:
    """Set the context, returning a token for `reset_context`.

    Always reset in a `finally`: under an async server the same task may be reused,
    and a leaked context would attribute one request's log lines to another
    request's user.
    """
    return _context.set(context)


def reset_context(token: object) -> None:
    _context.reset(token)  # type: ignore[arg-type]
