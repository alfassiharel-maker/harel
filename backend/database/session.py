"""Async database engine and RLS-scoped sessions.

The single most security-critical module in the backend. Every athlete-scoped
query runs inside a transaction that has first set the row-level-security context
from the authenticated principal:

    SET LOCAL app.current_user_id = '<uuid>';

`SET LOCAL` is transaction-scoped, so the setting cannot leak to the next request
that reuses the same pooled connection — which a plain `SET` would. If a code path
forgets to establish context, `app.current_user_id()` returns NULL in the
database and every RLS predicate filters every row: isolation fails closed
(docs/06 §3).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.sql import text

from backend.core.config import Settings
from backend.core.context import Principal

__all__ = ["SYSTEM_PRINCIPAL", "Database"]

# Sentinel meaning "this operation legitimately runs with no athlete context":
# the pre-authentication lookups (which use SECURITY DEFINER functions), and
# worker jobs that operate across athletes through their own audited paths. Making
# it an explicit, named choice means an unscoped session is never accidental — a
# reviewer sees `SYSTEM_PRINCIPAL` and asks why.
SYSTEM_PRINCIPAL = "__system__"


class Database:
    """Owns the engine and hands out scoped sessions.

    One instance per process, created at startup and disposed at shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_pre_ping=True,  # detect connections killed by the DB or a proxy
            # A statement timeout is a blunt but effective backstop against a
            # runaway query holding a connection and cascading into pool
            # exhaustion.
            connect_args={
                "server_settings": {
                    "statement_timeout": str(settings.database_statement_timeout_ms),
                    "application_name": "aisportscoach-api",
                }
            },
            echo=False,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def dispose(self) -> None:
        await self._engine.dispose()

    @contextlib.asynccontextmanager
    async def session(self, principal: Principal | str) -> AsyncIterator[AsyncSession]:
        """A transaction with the RLS context established for `principal`.

        `principal` is required, not optional. Passing `SYSTEM_PRINCIPAL` is the
        explicit, greppable way to run without athlete scoping; there is no
        implicit unscoped mode.

        The whole body runs in one transaction: it commits on clean exit and rolls
        back on any exception, so a half-applied multi-statement change can never
        persist.
        """
        # One transaction for the whole body: `session.begin()` commits on a clean
        # exit and rolls back if the body raises, so a half-applied multi-statement
        # change can never persist. No explicit try/except is needed — the context
        # manager already re-raises after rolling back.
        async with self._sessionmaker() as session, session.begin():
            await self._apply_context(session, principal)
            yield session

    async def apply_principal(self, session: AsyncSession, principal: Principal) -> None:
        """Establish (or re-establish) the RLS context inside an open transaction.

        Public because two flows only learn who the principal is *after* the
        transaction has started, and both must run scoped from that point on:

        * **Registration** generates the new user's id before inserting, and the
          `WITH CHECK (id = app.current_user_id())` policy on `identity.users`
          requires the context to already name that id or the insert is rejected.
        * **Login and token refresh** identify the user through the SECURITY
          DEFINER lookups in migration 0010, after which every subsequent write
          (failed-attempt counter, token rotation) must be normally RLS-scoped
          rather than continuing to run as the system.

        Calling this is how a session moves from `SYSTEM_PRINCIPAL` footing to a
        real athlete's footing without opening a second transaction.
        """
        await self._apply_context(session, principal)

    async def _apply_context(self, session: AsyncSession, principal: Principal | str) -> None:
        if principal is SYSTEM_PRINCIPAL:
            # Leave app.current_user_id unset. RLS then filters every athlete
            # table to zero rows; only SECURITY DEFINER functions and
            # non-RLS tables are reachable. This is the correct footing for the
            # pre-auth lookups.
            return
        if not isinstance(principal, Principal):
            raise TypeError(
                "session(principal=...) requires a Principal or SYSTEM_PRINCIPAL; " f"got {type(principal)!r}"
            )

        # set_config(key, value, is_local=true) is the parameterised form of
        # `SET LOCAL`. It is used rather than an f-string into `SET LOCAL` because
        # SET does not accept bind parameters, and interpolating a value into a
        # statement is how injection happens even when the value is 'only a UUID'.
        #
        # All three settings go in one statement: binding costs a single round
        # trip per transaction instead of three, on the hot path of every request.
        await session.execute(
            text(
                "SELECT set_config('app.current_user_id', :uid,  true),"
                "       set_config('app.current_role',    :role, true),"
                "       set_config('app.current_org_id',  :org,  true)"
            ),
            {
                "uid": str(principal.user_id),
                "role": principal.role,
                "org": str(principal.org_id) if principal.org_id else "",
            },
        )

    async def healthcheck(self) -> bool:
        """Cheap liveness probe for /readyz. Never raises."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
