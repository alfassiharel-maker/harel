"""Translation of exceptions into RFC 9457 problem+json responses.

One place decides what a client sees when something goes wrong, so the rule "an
error never leaks a value the caller is not entitled to" (see
`backend/core/errors.py`) is enforced in exactly one place rather than at every
raise site.

Three cases:

* `AppError` — a deliberate, contract-bearing error. Rendered with its stable
  `code`, status and any headers it carries (e.g. `Retry-After`, the 401
  challenge).
* `RequestValidationError` — Pydantic rejected the body. Mapped to our own
  `validation_failed` shape rather than FastAPI's default so the whole API speaks
  one error dialect, with field errors sanitised.
* Anything else — a bug. Logged with a stack trace, but rendered as a bare 500
  whose detail is withheld: an unexpected exception's message is as likely to hold
  a connection string as anything useful.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from backend.core.errors import AppError, ValidationFailed
from backend.core.logging import get_logger

__all__ = ["PROBLEM_CONTENT_TYPE", "install_exception_handlers"]

PROBLEM_CONTENT_TYPE = "application/problem+json"

logger = get_logger(__name__)


def _request_id(request: Request) -> str | None:
    context = getattr(request.state, "context", None)
    return context.request_id if context is not None else None


def _problem_response(
    problem: dict[str, Any], *, status: int, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        problem,
        status_code=status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers or None,
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        problem = exc.to_problem(instance=request.url.path, request_id=_request_id(request))
        # 5xx AppErrors are our own faults; log them. 4xx are expected client
        # outcomes and would only add noise.
        if exc.status >= 500:
            logger.error("app_error", code=exc.code, status=exc.status)
        return _problem_response(problem, status=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = ValidationFailed("The request did not pass validation.").to_problem(
            instance=request.url.path, request_id=_request_id(request)
        )
        # Location and message, never the rejected value: it can itself be
        # sensitive (a password that was too short, a token), and echoing it back
        # in the error body would defeat the point of not logging it.
        problem["errors"] = [
            {"loc": list(err.get("loc", ())), "type": err.get("type", ""), "msg": err.get("msg", "")}
            for err in exc.errors()
        ]
        return _problem_response(problem, status=422)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The message is deliberately discarded. `exc_info=True` records the full
        # traceback to the server log (scrubbed by the logging pipeline), while the
        # client gets nothing that could describe internal structure.
        logger.error("unhandled_exception", exc_info=True)
        problem = {
            "type": "https://api.aisportscoach.app/problems/internal-error",
            "title": "Internal error",
            "status": 500,
            "code": "internal_error",
            "detail": "An unexpected error occurred.",
            "instance": request.url.path,
        }
        request_id = _request_id(request)
        if request_id:
            problem["request_id"] = request_id
        return _problem_response(problem, status=500)
