"""Per-request context middleware.

Establishes the `RequestContext` (request id, client IP, user agent) at the top
of every request and tears it down in a `finally`. Two things depend on getting
this exactly right:

* **Correlation.** Every log line and audit row for the request carries the same
  `request_id`, which is what makes an incident traceable across the API and the
  workers.
* **Isolation of the ambient context.** The `ContextVar` is reset unconditionally
  at the end of the request. Under an async server the same worker task is reused
  across requests, and a leaked context would attribute one athlete's log lines —
  and worse, one athlete's `user_id` on an audit row — to the next request.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from backend.core.context import RequestContext, reset_context, set_context

__all__ = ["REQUEST_ID_HEADER", "request_context_middleware"]

REQUEST_ID_HEADER = "X-Request-Id"

# A client-supplied request id is echoed for correlation, but only if it looks
# like an id we would issue. An unbounded, unvalidated header value flows into
# every log line and the error body — a log-injection and log-forging vector if
# taken verbatim (OWASP A09).
_MAX_REQUEST_ID_LEN = 128


def _clean_request_id(raw: str | None) -> str:
    if raw:
        candidate = raw.strip()
        if 0 < len(candidate) <= _MAX_REQUEST_ID_LEN and candidate.isprintable():
            return candidate
    return uuid.uuid4().hex


def _client_ip(request: Request) -> str | None:
    # Trust the direct peer only. `X-Forwarded-For` is set by the load balancer in
    # front of the app and is validated there; parsing it here, unvalidated, would
    # let a client spoof its own IP in the audit trail.
    return request.client.host if request.client else None


async def request_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = _clean_request_id(request.headers.get(REQUEST_ID_HEADER))
    context = RequestContext(
        request_id=request_id,
        principal=None,  # filled in by require_principal once the token is verified
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        route=request.url.path,
    )
    request.state.context = context
    token = set_context(context)
    try:
        response = await call_next(request)
    finally:
        reset_context(token)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response
