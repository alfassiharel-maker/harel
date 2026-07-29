"""All SQL for the identity module.

Every method takes an `AsyncSession` whose RLS context is already established. The
repository never sets that context itself — the caller owns the transaction and its
footing, so a reviewer can see at the call site which principal a query runs as.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.identity.models import (
    AuditEvent,
    Consent,
    DataAccessGrant,
    RefreshToken,
    User,
)

__all__ = ["AuthLookup", "IdentityRepository", "TokenLookup"]


@dataclass(frozen=True, slots=True)
class AuthLookup:
    """Result of the pre-authentication lookup (migration 0010).

    Carries only what the credential check needs. Deliberately not a `User`: it
    must be impossible to accidentally return profile data from an unauthenticated
    code path.
    """

    id: uuid.UUID
    password_hash: str | None
    status: str
    role: str
    org_id: uuid.UUID | None
    email_verified_at: dt.datetime | None
    failed_logins: int
    locked_until: dt.datetime | None


@dataclass(frozen=True, slots=True)
class TokenLookup:
    id: uuid.UUID
    user_id: uuid.UUID
    family_id: uuid.UUID
    issued_at: dt.datetime
    expires_at: dt.datetime
    rotated_at: dt.datetime | None
    revoked_at: dt.datetime | None
    user_status: str
    user_role: str
    user_org_id: uuid.UUID | None


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Pre-authentication lookups (SECURITY DEFINER, migration 0010) --------
    # These are the only two RLS holes in the schema. They exist because both
    # lookups happen before any user is identified, so an RLS predicate comparing
    # against `app.current_user_id()` would match nothing and make login and
    # refresh impossible. See the header of 0010 for the alternatives considered.

    async def lookup_for_authentication(self, email: str) -> AuthLookup | None:
        result = await self._session.execute(
            text(
                "SELECT id, password_hash, status, role, org_id, email_verified_at,"
                "       failed_logins, locked_until"
                "  FROM identity.lookup_user_for_authentication(:email)"
            ),
            {"email": email},
        )
        row = result.mappings().one_or_none()
        return AuthLookup(**row) if row else None

    async def lookup_refresh_token(self, token_hash: bytes) -> TokenLookup | None:
        result = await self._session.execute(
            text(
                "SELECT id, user_id, family_id, issued_at, expires_at, rotated_at,"
                "       revoked_at, user_status, user_role, user_org_id"
                "  FROM identity.lookup_refresh_token(:token_hash)"
            ),
            {"token_hash": token_hash},
        )
        row = result.mappings().one_or_none()
        return TokenLookup(**row) if row else None

    async def revoke_token_family(self, family_id: uuid.UUID, reason: str) -> int:
        """Revoke every live token in a rotation family.

        Uses the definer function so revocation works even when the caller has no
        user context — which is the case during reuse detection, where the
        presented credential is untrusted.
        """
        result = await self._session.execute(
            text("SELECT identity.revoke_token_family(:family_id, :reason)"),
            {"family_id": family_id, "reason": reason},
        )
        return int(result.scalar_one())

    # -- Users ----------------------------------------------------------------

    async def insert_user(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        """Fetch by id. RLS restricts this to the caller's own row."""
        result = await self._session.execute(
            select(User).where(User.id == user_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def update_user_fields(self, user_id: uuid.UUID, **fields: object) -> None:
        if not fields:
            return
        await self._session.execute(
            update(User).where(User.id == user_id, User.deleted_at.is_(None)).values(**fields)
        )

    async def record_login_success(self, user_id: uuid.UUID, when: dt.datetime) -> None:
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_login_at=when, failed_logins=0, locked_until=None)
        )

    async def record_login_failure(
        self, user_id: uuid.UUID, *, max_failures: int, lockout_until: dt.datetime
    ) -> int:
        """Increment the failed-attempt counter, locking the account at the limit.

        Increments in the database (`failed_logins + 1`) rather than reading then
        writing: two concurrent wrong-password attempts must both count, and a
        read-modify-write would lose one.

        Returns the new failure count.
        """
        result = await self._session.execute(
            text(
                """
                UPDATE identity.users
                   SET failed_logins = failed_logins + 1,
                       locked_until = CASE
                           WHEN failed_logins + 1 >= :max_failures THEN :lockout_until
                           ELSE locked_until
                       END
                 WHERE id = :user_id
                RETURNING failed_logins
                """
            ),
            {"user_id": user_id, "max_failures": max_failures, "lockout_until": lockout_until},
        )
        row = result.scalar_one_or_none()
        return int(row) if row is not None else 0

    # -- Refresh tokens -------------------------------------------------------

    async def insert_refresh_token(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        await self._session.flush()
        return token

    async def mark_token_rotated(self, token_id: uuid.UUID, when: dt.datetime) -> bool:
        """Mark a token as rotated, but only if it is still live.

        The `rotated_at IS NULL` predicate makes this the atomic step that
        serialises concurrent refreshes: two simultaneous requests presenting the
        same token both reach here, exactly one updates a row, and the loser is
        treated as reuse. Without the predicate both would succeed and issue two
        valid token chains from one credential.
        """
        result = await self._session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == token_id,
                RefreshToken.rotated_at.is_(None),
                RefreshToken.revoked_at.is_(None),
            )
            .values(rotated_at=when, revoked_reason="rotation")
        )
        return (result.rowcount or 0) == 1

    async def revoke_all_user_tokens(self, user_id: uuid.UUID, reason: str) -> int:
        """Revoke every live refresh token for one user ("log out everywhere").

        Runs under the user's own RLS context, so it can only ever affect that
        user's tokens even if the predicate below were wrong.
        """
        result = await self._session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=dt.datetime.now(dt.UTC), revoked_reason=reason)
        )
        return result.rowcount or 0

    async def list_live_token_families(self, user_id: uuid.UUID) -> list[RefreshToken]:
        result = await self._session.execute(
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.rotated_at.is_(None),
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > dt.datetime.now(dt.UTC),
            )
            .order_by(RefreshToken.issued_at.desc())
        )
        return list(result.scalars().all())

    # -- Consent --------------------------------------------------------------

    async def insert_consent(self, consent: Consent) -> Consent:
        self._session.add(consent)
        await self._session.flush()
        return consent

    async def latest_consents(self, user_id: uuid.UUID) -> list[Consent]:
        """Current consent state: the newest row per purpose.

        `DISTINCT ON` is the direct way to express "latest per group" in Postgres;
        a window function or a correlated subquery would both be slower and less
        obvious.
        """
        result = await self._session.execute(
            select(Consent)
            .where(Consent.user_id == user_id)
            .distinct(Consent.purpose)
            .order_by(Consent.purpose, Consent.recorded_at.desc())
        )
        return list(result.scalars().all())

    # -- Data access grants ---------------------------------------------------

    async def insert_grant(self, grant: DataAccessGrant) -> DataAccessGrant:
        self._session.add(grant)
        await self._session.flush()
        return grant

    async def list_grants_issued(self, grantor_user_id: uuid.UUID) -> list[DataAccessGrant]:
        result = await self._session.execute(
            select(DataAccessGrant)
            .where(DataAccessGrant.grantor_user_id == grantor_user_id)
            .order_by(DataAccessGrant.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke_grant(self, grant_id: uuid.UUID, grantor_user_id: uuid.UUID) -> bool:
        """Revoke a grant. Scoped to the grantor as well as covered by RLS.

        Belt and braces: the RLS policy already restricts updates to the grantor,
        and this predicate means the same is true if the policy is ever loosened.
        """
        result = await self._session.execute(
            update(DataAccessGrant)
            .where(
                DataAccessGrant.id == grant_id,
                DataAccessGrant.grantor_user_id == grantor_user_id,
                DataAccessGrant.revoked_at.is_(None),
            )
            .values(revoked_at=dt.datetime.now(dt.UTC))
        )
        return (result.rowcount or 0) == 1

    # -- Audit ----------------------------------------------------------------

    async def insert_audit_event(self, event: AuditEvent) -> None:
        """Append an audit row.

        `identity.audit_events` has no RLS policy and `UPDATE`/`DELETE` are revoked
        from `app_rw`, so this insert succeeds from any footing — including the
        anonymous one, which is required to record failed logins.
        """
        self._session.add(event)
        await self._session.flush()
