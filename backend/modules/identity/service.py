"""Identity service — the module's public interface.

Owns authentication policy: what counts as a valid credential, when an account
locks, how refresh tokens rotate, and what gets audited. The repository owns SQL;
this owns the rules.

Transaction discipline worth reading before changing anything here: several flows
must **commit a side effect and then fail the request**. A failed login has to
persist the incremented attempt counter and still return 401. Raising inside the
transaction would roll the counter back and make lockout unenforceable, so those
flows record an outcome, exit the transaction cleanly, and raise afterwards.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from backend.core.config import Settings
from backend.core.context import Principal, RequestContext
from backend.core.errors import (
    AccountLocked,
    AccountNotActive,
    Conflict,
    InvalidCredentials,
    NotFound,
    Unauthenticated,
    ValidationFailed,
)
from backend.core.ids import uuid7
from backend.core.logging import get_logger
from backend.core.security import PasswordService, TokenService, generate_refresh_token, hash_refresh_token
from backend.database.session import SYSTEM_PRINCIPAL, Database
from backend.modules.identity.models import (
    CONSENT_PURPOSES,
    AuditEvent,
    Consent,
    RefreshToken,
    User,
)
from backend.modules.identity.repository import IdentityRepository
from backend.modules.identity.schemas import (
    ConsentIn,
    ConsentOut,
    LoginRequest,
    RegisterRequest,
    SessionOut,
    TokenPair,
    UserOut,
    UserUpdate,
)

__all__ = ["AuthOutcome", "IdentityService"]

logger = get_logger(__name__)

# Purposes captured at registration, mapped from the request flags.
_REGISTRATION_CONSENTS = {
    "terms_of_service": "accept_terms",
    "privacy_policy": "accept_privacy_policy",
    "health_data_processing": "consent_health_data",
    "marketing_email": "consent_marketing",
}


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    user: UserOut
    tokens: TokenPair


class IdentityService:
    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        passwords: PasswordService,
        tokens: TokenService,
    ) -> None:
        self._db = database
        self._settings = settings
        self._passwords = passwords
        self._tokens = tokens

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def _principal_of(self, user_id: uuid.UUID, role: str, org_id: uuid.UUID | None) -> Principal:
        return Principal(user_id=user_id, role=role, org_id=org_id)

    async def _issue_tokens(
        self,
        repo: IdentityRepository,
        principal: Principal,
        *,
        family_id: uuid.UUID | None,
        context: RequestContext | None,
        device_id: str | None = None,
    ) -> TokenPair:
        """Mint an access token and persist a new refresh token.

        A new `family_id` starts a fresh rotation chain (login); passing an existing
        one continues it (refresh), which is what makes reuse detection able to kill
        every descendant of a leaked credential.
        """
        access = self._tokens.create_access_token(principal)
        raw_refresh, refresh_hash = generate_refresh_token()
        now = self._now()

        await repo.insert_refresh_token(
            RefreshToken(
                id=uuid7(),
                user_id=principal.user_id,
                family_id=family_id or uuid7(),
                token_hash=refresh_hash,
                device_id=device_id,
                user_agent=context.user_agent if context else None,
                ip_address=context.ip_address if context else None,
                expires_at=now + dt.timedelta(days=self._settings.refresh_token_ttl_days),
            )
        )

        return TokenPair(
            access_token=access.token,
            refresh_token=raw_refresh,
            expires_in=self._settings.access_token_ttl_seconds,
            expires_at=access.expires_at,
        )

    @staticmethod
    def _audit(
        repo: IdentityRepository,
        *,
        action: str,
        resource_type: str,
        outcome: str = "success",
        actor_user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
        subject_user_id: uuid.UUID | None = None,
        resource_id: str | None = None,
        context: RequestContext | None = None,
        detail: dict[str, object] | None = None,
    ) -> Awaitable[None]:
        # Returns the coroutine rather than awaiting it, so a caller writes
        # `await self._audit(...)` at the call site where the transaction lives.
        return repo.insert_audit_event(
            AuditEvent(
                id=uuid7(),
                actor_user_id=actor_user_id,
                actor_role=actor_role,
                subject_user_id=subject_user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                ip_address=context.ip_address if context else None,
                request_id=context.request_id if context else None,
                # Identifiers and metadata only — never credentials or health values.
                detail=detail or {},
            )
        )

    # ----------------------------------------------------------------- register

    async def register(self, request: RegisterRequest, context: RequestContext | None = None) -> AuthOutcome:
        """Create an account, record consent, and issue a first token pair.

        The user's id is generated up front because the RLS policy on
        `identity.users` is `WITH CHECK (id = app.current_user_id())` — the insert is
        only permitted once the transaction's context already names the row being
        created. So the flow is: mint the id, bind it as the principal, then insert.
        """
        user_id = uuid7()
        principal = self._principal_of(user_id, "athlete", None)
        password_hash = self._passwords.hash(request.password)

        try:
            async with self._db.session(principal) as session:
                repo = IdentityRepository(session)
                await repo.insert_user(
                    User(
                        id=user_id,
                        org_id=None,
                        email=request.email,
                        password_hash=password_hash,
                        display_name=request.display_name.strip(),
                        locale=request.locale,
                        timezone=request.timezone,
                        role="athlete",
                        # Phase 1 has no email-delivery path yet, so accounts start
                        # active. Email verification lands in Phase 2 (roadmap 2.x)
                        # and will flip this to 'pending_verification'.
                        status="active",
                    )
                )

                for purpose, field in _REGISTRATION_CONSENTS.items():
                    await repo.insert_consent(
                        Consent(
                            id=uuid7(),
                            user_id=user_id,
                            purpose=purpose,
                            document_version="2026-07-01",
                            granted=bool(getattr(request, field)),
                            ip_address=context.ip_address if context else None,
                            user_agent=context.user_agent if context else None,
                        )
                    )

                tokens = await self._issue_tokens(repo, principal, family_id=None, context=context)
                await self._audit(
                    repo,
                    action="user.registered",
                    resource_type="user",
                    resource_id=str(user_id),
                    actor_user_id=user_id,
                    actor_role="athlete",
                    subject_user_id=user_id,
                    context=context,
                )
                user = await repo.get_user(user_id)
                assert user is not None  # just inserted in this transaction
                out = self._to_user_out(user)

        except IntegrityError as exc:
            # The partial unique index on (email) WHERE deleted_at IS NULL.
            #
            # Honest limitation: returning 409 here does reveal that an address is
            # registered. Full enumeration resistance needs a verification-email
            # flow that always returns 202, which requires mail delivery — Phase 2.
            # Until then the message is kept generic and the event is audited.
            raise Conflict(
                "This account could not be created. If you already have an account, "
                "try signing in or resetting your password.",
                meta={"reason": "registration_conflict"},
            ) from exc

        logger.info("user_registered", user_id=str(user_id))
        return AuthOutcome(user=out, tokens=tokens)

    # -------------------------------------------------------------------- login

    async def login(self, request: LoginRequest, context: RequestContext | None = None) -> AuthOutcome:
        """Authenticate and issue a token pair.

        Every failure mode returns the same `InvalidCredentials` error, and the
        password hash is computed even when no account matches, so neither the
        response body nor its timing reveals whether an address is registered
        (docs/06 §2, threat T9).
        """
        # Starts on system footing: the lookup happens before any user is known and
        # goes through the SECURITY DEFINER function in migration 0010.
        failure: Exception | None = None
        outcome: AuthOutcome | None = None

        async with self._db.session(SYSTEM_PRINCIPAL) as session:
            repo = IdentityRepository(session)
            found = await repo.lookup_for_authentication(request.email)

            if found is None:
                # Burn equivalent CPU so a missing account is not measurably faster.
                self._passwords.verify_dummy(request.password)
                await self._audit(
                    repo,
                    action="auth.login_failed",
                    resource_type="user",
                    outcome="denied",
                    context=context,
                    detail={"reason": "no_such_account"},
                )
                failure = InvalidCredentials()
            else:
                principal = self._principal_of(found.id, found.role, found.org_id)
                now = self._now()

                if found.locked_until is not None and found.locked_until > now:
                    await self._audit(
                        repo,
                        action="auth.login_blocked",
                        resource_type="user",
                        resource_id=str(found.id),
                        outcome="denied",
                        subject_user_id=found.id,
                        context=context,
                        detail={"reason": "locked"},
                    )
                    failure = AccountLocked(
                        "Too many failed attempts. Try again later.",
                        meta={"locked_until": found.locked_until.isoformat()},
                    )
                elif found.password_hash is None or not self._passwords.verify(
                    found.password_hash, request.password
                ):
                    if found.password_hash is None:
                        # Federated-only account: still spend the time.
                        self._passwords.verify_dummy(request.password)
                    # Elevate so the counter update passes the RLS policy on
                    # identity.users, then commit it. The 401 is raised after the
                    # transaction closes — raising here would roll the counter back
                    # and make lockout unenforceable.
                    await self._db.apply_principal(session, principal)
                    attempts = await repo.record_login_failure(
                        found.id,
                        max_failures=self._settings.max_failed_logins,
                        lockout_until=now + dt.timedelta(minutes=self._settings.lockout_minutes),
                    )
                    await self._audit(
                        repo,
                        action="auth.login_failed",
                        resource_type="user",
                        resource_id=str(found.id),
                        outcome="denied",
                        subject_user_id=found.id,
                        actor_user_id=found.id,
                        context=context,
                        detail={"reason": "bad_password", "failed_logins": attempts},
                    )
                    failure = InvalidCredentials()
                elif found.status != "active":
                    await self._audit(
                        repo,
                        action="auth.login_blocked",
                        resource_type="user",
                        resource_id=str(found.id),
                        outcome="denied",
                        subject_user_id=found.id,
                        context=context,
                        detail={"reason": f"status_{found.status}"},
                    )
                    failure = AccountNotActive(
                        "This account is not active. Contact support if you believe " "this is a mistake."
                    )
                else:
                    await self._db.apply_principal(session, principal)
                    await repo.record_login_success(found.id, now)

                    # Opportunistic hash upgrade: a successful login is the only
                    # moment the plaintext is available.
                    if self._passwords.needs_rehash(found.password_hash):
                        await repo.update_user_fields(
                            found.id, password_hash=self._passwords.hash(request.password)
                        )

                    tokens = await self._issue_tokens(
                        repo,
                        principal,
                        family_id=None,
                        context=context,
                        device_id=request.device_id,
                    )
                    await self._audit(
                        repo,
                        action="auth.login",
                        resource_type="user",
                        resource_id=str(found.id),
                        actor_user_id=found.id,
                        actor_role=found.role,
                        subject_user_id=found.id,
                        context=context,
                    )
                    user = await repo.get_user(found.id)
                    if user is None:
                        # RLS returned nothing for the user we just authenticated,
                        # which means the context is wrong. Fail closed rather than
                        # issuing tokens for an identity we cannot read.
                        raise Unauthenticated("Authentication could not be completed.")
                    outcome = AuthOutcome(user=self._to_user_out(user), tokens=tokens)

        if failure is not None:
            raise failure
        assert outcome is not None
        logger.info("user_logged_in", user_id=str(outcome.user.id))
        return outcome

    # ------------------------------------------------------------------ refresh

    async def refresh(self, raw_token: str, context: RequestContext | None = None) -> TokenPair:
        """Rotate a refresh token, detecting reuse.

        Presenting a token that has already been rotated is the signature of a
        stolen credential: the legitimate client would be holding the newest token.
        The whole family is revoked, which logs the attacker *and* the victim out
        and forces a fresh login.
        """
        token_hash = hash_refresh_token(raw_token)
        reuse_detected = False
        failure: Exception | None = None
        pair: TokenPair | None = None

        async with self._db.session(SYSTEM_PRINCIPAL) as session:
            repo = IdentityRepository(session)
            found = await repo.lookup_refresh_token(token_hash)
            now = self._now()

            if found is None or found.revoked_at is not None:
                failure = Unauthenticated("Refresh token is not valid.")
            elif found.rotated_at is not None:
                reuse_detected = True
            elif found.expires_at <= now:
                failure = Unauthenticated("Refresh token has expired.")
            elif found.user_status != "active":
                failure = AccountNotActive("This account is not active.")

            if reuse_detected and found is not None:
                revoked = await repo.revoke_token_family(found.family_id, "reuse_detected")
                await self._audit(
                    repo,
                    action="auth.refresh_reuse_detected",
                    resource_type="refresh_token_family",
                    resource_id=str(found.family_id),
                    outcome="denied",
                    subject_user_id=found.user_id,
                    context=context,
                    detail={"revoked_tokens": revoked},
                )
                logger.warning(
                    "refresh_token_reuse_detected",
                    user_id=str(found.user_id),
                    token_family_id=str(found.family_id),
                    revoked_tokens=revoked,
                )
                failure = Unauthenticated(
                    "This session has been ended for security reasons. Please sign in again."
                )

            elif failure is None and found is not None:
                principal = self._principal_of(found.user_id, found.user_role, found.user_org_id)
                await self._db.apply_principal(session, principal)

                # Atomic guard: exactly one concurrent refresh can win this update.
                # The loser is a genuine double-spend of one token and is treated as
                # reuse rather than being quietly allowed to mint a second chain.
                if not await repo.mark_token_rotated(found.id, now):
                    revoked = await repo.revoke_token_family(found.family_id, "reuse_detected")
                    await self._audit(
                        repo,
                        action="auth.refresh_race_detected",
                        resource_type="refresh_token_family",
                        resource_id=str(found.family_id),
                        outcome="denied",
                        subject_user_id=found.user_id,
                        context=context,
                        detail={"revoked_tokens": revoked},
                    )
                    failure = Unauthenticated(
                        "This session has been ended for security reasons. Please sign in again."
                    )
                else:
                    pair = await self._issue_tokens(
                        repo, principal, family_id=found.family_id, context=context
                    )
                    await self._audit(
                        repo,
                        action="auth.token_refreshed",
                        resource_type="refresh_token_family",
                        resource_id=str(found.family_id),
                        actor_user_id=found.user_id,
                        actor_role=found.user_role,
                        subject_user_id=found.user_id,
                        context=context,
                    )

        if failure is not None:
            raise failure
        assert pair is not None
        return pair

    # ------------------------------------------------------------------- logout

    async def logout(self, raw_token: str, context: RequestContext | None = None) -> None:
        """End the session this refresh token belongs to.

        Revokes the whole family rather than the single token: the client is being
        logged out, and leaving earlier links in the chain live would defeat the
        point.

        Idempotent — an unknown or already-revoked token returns quietly. A logout
        that errors encourages clients to retry or, worse, to ignore failures.
        """
        token_hash = hash_refresh_token(raw_token)
        async with self._db.session(SYSTEM_PRINCIPAL) as session:
            repo = IdentityRepository(session)
            found = await repo.lookup_refresh_token(token_hash)
            if found is None:
                return
            revoked = await repo.revoke_token_family(found.family_id, "logout")
            await self._audit(
                repo,
                action="auth.logout",
                resource_type="refresh_token_family",
                resource_id=str(found.family_id),
                actor_user_id=found.user_id,
                subject_user_id=found.user_id,
                context=context,
                detail={"revoked_tokens": revoked},
            )

    async def logout_everywhere(self, principal: Principal, context: RequestContext | None = None) -> int:
        """Revoke every live refresh token for the authenticated user."""
        async with self._db.session(principal) as session:
            repo = IdentityRepository(session)
            revoked = await repo.revoke_all_user_tokens(principal.user_id, "logout")
            await self._audit(
                repo,
                action="auth.logout_all",
                resource_type="user",
                resource_id=str(principal.user_id),
                actor_user_id=principal.user_id,
                actor_role=principal.role,
                subject_user_id=principal.user_id,
                context=context,
                detail={"revoked_tokens": revoked},
            )
            return revoked

    # -------------------------------------------------------------- user access

    async def get_user(self, principal: Principal) -> UserOut:
        async with self._db.session(principal) as session:
            user = await IdentityRepository(session).get_user(principal.user_id)
            if user is None:
                # RLS filtered it, or the account was deleted mid-session.
                raise NotFound("Account not found.")
            return self._to_user_out(user)

    async def update_user(
        self, principal: Principal, update: UserUpdate, context: RequestContext | None = None
    ) -> UserOut:
        fields = update.model_dump(exclude_none=True)
        if not fields:
            raise ValidationFailed("No fields to update.")
        async with self._db.session(principal) as session:
            repo = IdentityRepository(session)
            await repo.update_user_fields(principal.user_id, **fields)
            await self._audit(
                repo,
                action="user.updated",
                resource_type="user",
                resource_id=str(principal.user_id),
                actor_user_id=principal.user_id,
                actor_role=principal.role,
                subject_user_id=principal.user_id,
                context=context,
                # Field names only, never the values: display_name is personal data.
                detail={"fields": sorted(fields)},
            )
            user = await repo.get_user(principal.user_id)
            if user is None:
                raise NotFound("Account not found.")
            return self._to_user_out(user)

    async def list_sessions(self, principal: Principal) -> list[SessionOut]:
        async with self._db.session(principal) as session:
            tokens = await IdentityRepository(session).list_live_token_families(principal.user_id)
            return [SessionOut.model_validate(token) for token in tokens]

    # ------------------------------------------------------------------ consent

    async def get_consents(self, principal: Principal) -> list[ConsentOut]:
        async with self._db.session(principal) as session:
            rows = await IdentityRepository(session).latest_consents(principal.user_id)
            return [ConsentOut.model_validate(row) for row in rows]

    async def set_consent(
        self,
        principal: Principal,
        purpose: str,
        payload: ConsentIn,
        context: RequestContext | None = None,
    ) -> ConsentOut:
        """Record a consent decision as a new append-only row.

        Withdrawal is a row with `granted = false`, never an update — GDPR requires
        demonstrating what was consented to and when, which an overwritten row
        cannot do.
        """
        if purpose not in CONSENT_PURPOSES:
            raise ValidationFailed(f"Unknown consent purpose: {purpose}")
        # Withdrawing consent to health-data processing is the athlete's right, but
        # it removes the lawful basis for the analytics — the account has to be
        # closed or exported instead, which is a Phase 2 flow (account deletion).
        if purpose == "health_data_processing" and not payload.granted:
            raise ValidationFailed(
                "Withdrawing health-data consent stops all analysis. Use account "
                "deletion or data export instead, so your data is handled properly."
            )

        async with self._db.session(principal) as session:
            repo = IdentityRepository(session)
            consent = Consent(
                id=uuid7(),
                user_id=principal.user_id,
                purpose=purpose,
                document_version=payload.document_version,
                granted=payload.granted,
                ip_address=context.ip_address if context else None,
                user_agent=context.user_agent if context else None,
            )
            await repo.insert_consent(consent)
            await self._audit(
                repo,
                action="consent.recorded",
                resource_type="consent",
                resource_id=purpose,
                actor_user_id=principal.user_id,
                actor_role=principal.role,
                subject_user_id=principal.user_id,
                context=context,
                detail={"purpose": purpose, "granted": payload.granted},
            )
            return ConsentOut.model_validate(consent)

    # -------------------------------------------------------------- serialising

    @staticmethod
    def _to_user_out(user: User) -> UserOut:
        return UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            status=user.status,
            locale=user.locale,
            timezone=user.timezone,
            org_id=user.org_id,
            email_verified=user.email_verified_at is not None,
            created_at=user.created_at,
        )
