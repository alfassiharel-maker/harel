"""Application settings.

Loaded from the environment (and `.env` locally). Validation here is deliberately
strict: a misconfiguration that reaches production is a security incident, and the
cheapest place to catch it is process startup.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "ci", "staging", "production"]

__all__ = ["Environment", "Settings", "get_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Unknown variables are ignored rather than fatal: the deployment
        # environment legitimately carries variables this process does not use.
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---------------------------------------------------------
    app_env: Environment = "local"
    app_debug: bool = False
    app_base_url: str = "http://localhost:8000"
    log_level: str = "info"
    log_format: Literal["console", "json"] = "console"

    # --- Database -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://app_rw:localdev@localhost:5432/aisportscoach"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=5, ge=0, le=100)
    database_statement_timeout_ms: int = Field(default=10_000, ge=100)

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_url: str = "redis://localhost:6379/1"

    # --- Auth ---------------------------------------------------------------
    jwt_private_key_path: Path | None = None
    jwt_public_key_path: Path | None = None
    jwt_issuer: str = "aisportscoach"
    jwt_audience: str = "aisportscoach-api"
    access_token_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)

    # Failed-login throttling. Counters live on the user row so a Redis flush
    # cannot reset a lockout (docs/06 §2).
    max_failed_logins: int = Field(default=8, ge=3)
    lockout_minutes: int = Field(default=15, ge=1)

    # --- AI -----------------------------------------------------------------
    anthropic_api_key: str | None = None
    ai_default_model: str = "claude-opus-5"
    ai_chat_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    ai_deep_review_model: str = "claude-opus-5"
    ai_deep_review_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    ai_free_messages_per_month: int = Field(default=5, ge=0)
    ai_premium_messages_per_month: int = Field(default=100, ge=0)

    # --- Integrations -------------------------------------------------------
    garmin_adapter: Literal["mock", "live"] = "mock"

    # --- Feature flags ------------------------------------------------------
    feature_rewards_enabled: bool = False
    feature_payouts_enabled: bool = False
    feature_community_enabled: bool = False
    feature_coach_portal_enabled: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "ci"

    @property
    def allows_ephemeral_jwt_keys(self) -> bool:
        """Generating a throwaway signing key is acceptable locally and in CI.

        It is never acceptable in a deployed environment: an ephemeral key means
        every restart silently invalidates every session, and the key is not
        recoverable for incident analysis.
        """
        return self.app_env in ("local", "ci")

    @model_validator(mode="after")
    def _validate_deployment_safety(self) -> Settings:
        if self.is_production:
            problems: list[str] = []
            if self.app_debug:
                problems.append("app_debug must be false in production")
            if self.log_format != "json":
                problems.append("log_format must be json in production")
            if not (self.jwt_private_key_path and self.jwt_public_key_path):
                problems.append("JWT key paths are required in production")
            if "localhost" in self.database_url or "127.0.0.1" in self.database_url:
                problems.append("database_url points at localhost in production")
            # A production app connecting as a superuser or migrator role would
            # bypass row-level security and silently void tenant isolation.
            if "app_rw" not in self.database_url:
                problems.append(
                    "database_url must connect as app_rw in production so " "row-level security applies"
                )
            if problems:
                raise ValueError("unsafe production configuration: " + "; ".join(problems))
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so validation runs once at startup.

    Tests clear the cache via `get_settings.cache_clear()` rather than mutating a
    global, so each test builds settings from its own environment.
    """
    return Settings()
