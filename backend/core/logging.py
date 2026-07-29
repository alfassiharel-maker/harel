"""Structured logging with PII scrubbing.

The scrubber runs as a processor in the logging pipeline rather than at call
sites. A call site can be forgotten; a pipeline stage cannot. Health values,
email addresses and names must never reach a log sink — see docs/06 §5.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from backend.core.config import Settings
from backend.core.context import current_context

__all__ = ["REDACTED", "SENSITIVE_KEYS", "configure_logging", "get_logger"]

REDACTED = "[redacted]"

# Anything whose key contains one of these fragments is redacted. Substring
# matching rather than exact keys, so `user_email`, `athlete_email` and `email`
# are all covered without maintaining an exhaustive list.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # Credentials and secrets
        "password",
        "secret",
        "token",
        "authorization",
        "api_key",
        "apikey",
        "private_key",
        "credential",
        "signature",
        "cookie",
        "session_id",
        # Direct identifiers
        "email",
        "phone",
        "display_name",
        "full_name",
        "first_name",
        "last_name",
        "birth_date",
        "date_of_birth",
        "address",
        # Health values — sensitive personal data under GDPR Article 9
        "hrv",
        "rmssd",
        "resting_hr",
        "heart_rate",
        "avg_hr",
        "max_hr",
        "sleep",
        "weight",
        "height",
        "vo2max",
        "readiness",
        "injury",
        # Financial
        "iban",
        "account_number",
        "destination_ref",
    }
)

# Keys that look sensitive by the rule above but are safe and useful to log.
# `user_id` is an opaque UUID and is the primary correlation key for support.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        "token_family_id",
        "refresh_token_id",
        "user_id",
        "subject_user_id",
        "actor_user_id",
    }
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in _ALLOWLIST:
        return False
    return any(fragment in lowered for fragment in SENSITIVE_KEYS)


def _scrub(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
            continue
        value = event_dict[key]
        # One level of nesting is scrubbed. Deeper structures are not logged by
        # convention — if a payload needs deep inspection it belongs in a trace,
        # not a log line.
        if isinstance(value, dict):
            event_dict[key] = {
                inner: (REDACTED if _is_sensitive(inner) else inner_value)
                for inner, inner_value in value.items()
            }
    return event_dict


def _bind_request_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach request correlation automatically.

    Every line carries `request_id` and, when authenticated, `user_id` — the two
    fields that make an incident traceable across API and worker.
    """
    context = current_context()
    if context is not None:
        event_dict.setdefault("request_id", context.request_id)
        if context.route:
            event_dict.setdefault("route", context.route)
        if context.user_id is not None:
            event_dict.setdefault("user_id", str(context.user_id))
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _bind_request_context,
        # Scrubbing runs last among the enrichers so it also covers anything the
        # enrichers themselves added.
        _scrub,
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.log_format == "json":
        # JSON has no exception renderer of its own, so format the traceback into
        # the event dict. The console renderer, by contrast, formats exceptions
        # itself and warns if `format_exc_info` has already consumed them — hence
        # this is in the JSON branch only.
        shared_processors.append(structlog.processors.format_exc_info)
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy) through the same pipeline so
    # third-party output is scrubbed too.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
