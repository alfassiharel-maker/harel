"""Identity ORM models, mirroring `database/migrations/0002` and `0010`.

The migrations are the source of truth (ADR-012). These are the application's
typed view; a CI drift check compares the two.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.base import Base, created_at_column, updated_at_column, uuid_pk

__all__ = [
    "CONSENT_PURPOSES",
    "GRANT_SCOPES",
    "USER_ROLES",
    "USER_STATUSES",
    "AuditEvent",
    "Consent",
    "DataAccessGrant",
    "OrgMembership",
    "Organization",
    "RefreshToken",
    "User",
]

SCHEMA = "identity"

USER_ROLES = ("athlete", "coach", "partner", "admin", "support")
USER_STATUSES = ("pending_verification", "active", "suspended", "closed")
CONSENT_PURPOSES = (
    "terms_of_service",
    "privacy_policy",
    "health_data_processing",
    "marketing_email",
    "partner_data_sharing",
    "ai_training_improvement",
)
GRANT_SCOPES = ("activities", "metrics", "wellness", "plans", "goals", "conversations")


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="club")
    country_code: Mapped[str | None] = mapped_column(String(2))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = updated_at_column()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.organizations.id", ondelete="SET NULL")
    )
    # CITEXT: case-insensitivity is enforced by the column type, not by the
    # application remembering to lower() consistently.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    # NULL is valid for a federated-only account (Apple/Google sign-in).
    password_hash: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(Text, nullable=False, default="he-IL")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="Asia/Jerusalem")
    role: Mapped[str] = mapped_column(Text, nullable=False, default="athlete")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    # Throttling state lives on the row, not only in Redis, so a cache flush
    # cannot reset a lockout (docs/06 §2).
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = created_at_column()
    updated_at: Mapped[dt.datetime] = updated_at_column()
    deleted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = created_at_column()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    # Groups a rotation chain. Presenting an already-rotated token means the
    # credential leaked, and the whole family is revoked.
    family_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # SHA-256 of the token. The token itself is never stored.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    device_id: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    issued_at: Mapped[dt.datetime] = created_at_column()
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    rotated_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text)

    @property
    def is_live(self) -> bool:
        return self.rotated_at is None and self.revoked_at is None


class Consent(Base):
    """Append-only. Withdrawal is a new row, never an update (docs/06 §10)."""

    __tablename__ = "consents"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    document_version: Mapped[str] = mapped_column(Text, nullable=False)
    granted: Mapped[bool] = mapped_column(nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[dt.datetime] = created_at_column()


class DataAccessGrant(Base):
    """The only mechanism by which one person reads another's health data."""

    __tablename__ = "data_access_grants"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    grantor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), nullable=False
    )
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    # Expiry is mandatory in the schema — an open-ended grant is unrepresentable.
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    created_at: Mapped[dt.datetime] = created_at_column()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class AuditEvent(Base):
    """Append-only. Retained 7 years, survives erasure in redacted form."""

    __tablename__ = "audit_events"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL")
    )
    actor_role: Mapped[str | None] = mapped_column(Text)
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL")
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.organizations.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text, nullable=False, default="success")
    ip_address: Mapped[str | None] = mapped_column(INET)
    request_id: Mapped[str | None] = mapped_column(Text)
    # Identifiers and metadata only — never health values or credentials.
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    occurred_at: Mapped[dt.datetime] = created_at_column()
