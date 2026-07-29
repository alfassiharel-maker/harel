"""Application error hierarchy, rendered as RFC 9457 problem+json.

Every error carries a stable machine-readable `code`. That code is the API
contract; `title` and `detail` are human text and may be reworded or localised
without a version bump (docs/03 §1).

Rule enforced by review: an error raised in a service never contains a value the
caller is not already entitled to see. "User 018f… not found" leaks existence;
"not found" does not.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AccountLocked",
    "AccountNotActive",
    "AppError",
    "ConfigurationError",
    "Conflict",
    "InsufficientData",
    "InvalidCredentials",
    "NotFound",
    "PermissionDenied",
    "PreconditionFailed",
    "RateLimited",
    "Unauthenticated",
    "UpstreamUnavailable",
    "ValidationFailed",
]

_PROBLEM_BASE = "https://api.aisportscoach.app/problems/"


class AppError(Exception):
    """Base class for every error the API deliberately returns.

    Anything that is not an AppError escaping to the transport layer is a bug,
    and is rendered as a generic 500 with the detail withheld — an unexpected
    exception's message is as likely to contain a connection string as anything
    useful to a client.
    """

    status: int = 500
    code: str = "internal_error"
    title: str = "Internal error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        meta: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.meta = meta or {}
        self.headers = headers or {}
        super().__init__(self.detail)

    @property
    def type_uri(self) -> str:
        return _PROBLEM_BASE + self.code.replace("_", "-")

    def to_problem(self, *, instance: str | None = None, request_id: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": self.type_uri,
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        if request_id:
            problem["request_id"] = request_id
        if self.meta:
            problem["meta"] = self.meta
        return problem


class ValidationFailed(AppError):
    status = 422
    code = "validation_failed"
    title = "Request validation failed"


class Unauthenticated(AppError):
    status = 401
    code = "unauthenticated"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        # RFC 9110 requires a challenge on a 401.
        self.headers.setdefault("WWW-Authenticate", 'Bearer realm="api"')


class InvalidCredentials(Unauthenticated):
    code = "invalid_credentials"
    title = "Invalid credentials"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        # Deliberately identical whether the account is absent or the password is
        # wrong. Distinguishing them turns login into an account-existence oracle
        # (docs/06 §2, threat T9).
        super().__init__(detail or "Email or password is incorrect.", **kwargs)


class AccountLocked(AppError):
    status = 423
    code = "account_locked"
    title = "Account temporarily locked"


class AccountNotActive(AppError):
    status = 403
    code = "account_not_active"
    title = "Account is not active"


class PermissionDenied(AppError):
    status = 403
    code = "permission_denied"
    title = "Permission denied"


class NotFound(AppError):
    status = 404
    code = "not_found"
    title = "Resource not found"


class Conflict(AppError):
    status = 409
    code = "conflict"
    title = "Conflicting request"


class PreconditionFailed(AppError):
    status = 412
    code = "precondition_failed"
    title = "Precondition failed"


class RateLimited(AppError):
    status = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(self, detail: str | None = None, *, retry_after_seconds: int = 60, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        self.headers.setdefault("Retry-After", str(retry_after_seconds))


class InsufficientData(AppError):
    """Not enough data to compute a meaningful answer.

    A distinct error rather than a low-confidence result, because the product rule
    is to refuse rather than to produce a confident-looking number from noise
    (docs/04 §0, docs/03 §6).
    """

    status = 422
    code = "insufficient_data"
    title = "Not enough data"


class UpstreamUnavailable(AppError):
    status = 503
    code = "upstream_unavailable"
    title = "A dependency is unavailable"


class ConfigurationError(AppError):
    """Raised at startup only. Never reaches a client."""

    status = 500
    code = "configuration_error"
    title = "Server misconfigured"
