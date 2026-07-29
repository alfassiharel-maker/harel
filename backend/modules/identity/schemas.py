"""Identity DTOs — the module's boundary contract.

Wire format is `snake_case` and SI-suffixed, matching Python and Postgres so
nothing is renamed in three places (docs/03 §1).
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "ConsentIn",
    "ConsentOut",
    "GrantIn",
    "GrantOut",
    "LoginRequest",
    "RefreshRequest",
    "RegisterRequest",
    "SessionOut",
    "TokenPair",
    "UserOut",
    "UserUpdate",
]

# NIST guidance: length is what matters. No composition rules and no forced
# rotation — both measurably reduce real-world password strength (docs/06 §2).
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256

Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)]


class _Strict(BaseModel):
    # Reject unknown fields on input: a client sending `is_admin: true` should get
    # a 422, not have it silently ignored while the client author believes it worked.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _normalise_password(value: str) -> str:
    """Unicode-normalise to NFKC before hashing.

    Without this, a password typed with a composed accent on one device and a
    decomposed one on another produces different bytes and fails to verify — a
    genuine and hard-to-diagnose lockout for non-ASCII passwords, which matters
    for a Hebrew-first product.
    """
    return unicodedata.normalize("NFKC", value)


class RegisterRequest(_Strict):
    email: EmailStr
    password: Password
    display_name: str = Field(min_length=1, max_length=120)
    locale: str = Field(default="he-IL", max_length=16)
    timezone: str = Field(default="Asia/Jerusalem", max_length=64)
    # Registration must capture consent: health-data processing has no lawful
    # basis without it, and the flow cannot proceed on a default.
    accept_terms: bool
    accept_privacy_policy: bool
    consent_health_data: bool
    consent_marketing: bool = False

    @field_validator("password")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_password(value)

    @field_validator("accept_terms", "accept_privacy_policy", "consent_health_data")
    @classmethod
    def _must_be_accepted(cls, value: bool) -> bool:
        if not value:
            raise ValueError("must be accepted to create an account")
        return value

    @field_validator("display_name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class LoginRequest(_Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    device_id: str | None = Field(default=None, max_length=200)

    @field_validator("password")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return _normalise_password(value)


class RefreshRequest(_Strict):
    refresh_token: str = Field(min_length=16, max_length=512)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 — a scheme name, not a secret
    expires_in: int
    expires_at: dt.datetime


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: str
    status: str
    locale: str
    timezone: str
    org_id: uuid.UUID | None
    email_verified: bool
    created_at: dt.datetime


class UserUpdate(_Strict):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


class ConsentIn(_Strict):
    granted: bool
    document_version: str = Field(min_length=1, max_length=64)


class ConsentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purpose: str
    granted: bool
    document_version: str
    recorded_at: dt.datetime


class GrantIn(_Strict):
    grantee_email: EmailStr
    scopes: list[str] = Field(min_length=1, max_length=6)
    # No default: the athlete chooses how long, and the schema cannot express
    # "forever" (docs/06 §4).
    expires_at: dt.datetime

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str]) -> list[str]:
        allowed = {"activities", "metrics", "wellness", "plans", "goals", "conversations"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown scopes: {', '.join(unknown)}")
        # Deduplicate but keep it deterministic, so the stored array is stable.
        return sorted(set(value))


class GrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    grantee_user_id: uuid.UUID
    scopes: list[str]
    expires_at: dt.datetime
    created_at: dt.datetime
    revoked_at: dt.datetime | None


class SessionOut(BaseModel):
    """A live refresh-token family, for the "where am I logged in" screen."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    device_id: str | None
    user_agent: str | None
    issued_at: dt.datetime
    expires_at: dt.datetime
