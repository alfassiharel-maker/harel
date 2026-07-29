"""Row-level security, tested at the database boundary (roadmap 1.5).

The tenant-isolation matrix proves the *application* scopes every query. This
file proves the *backstop* underneath it: that even a query with no `WHERE
user_id` clause, run on the real connection role, cannot cross tenants. The two
together are the whole argument that a forgotten predicate is contained rather
than catastrophic.

These tests talk to Postgres directly through the `Database` session helper,
using the `app_rw` role exactly as the application does. If they passed while
connected as the table owner they would prove nothing, so the fixture's use of
`app_rw` is load-bearing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.core.context import Principal
from backend.core.ids import uuid7
from backend.database.session import SYSTEM_PRINCIPAL, Database

pytestmark = [pytest.mark.integration, pytest.mark.security]


def _principal(user_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, role="athlete", org_id=None)


async def _insert_user(db: Database, user_id: uuid.UUID, email: str) -> None:
    async with db.session(_principal(user_id)) as session:
        await session.execute(
            text("INSERT INTO identity.users (id, email, display_name) " "VALUES (:id, :email, :name)"),
            {"id": str(user_id), "email": email, "name": "RLS Test"},
        )


async def test_with_check_forbids_inserting_a_row_for_another_user(db: Database) -> None:
    """You cannot create a row that names someone else as its owner.

    The `WITH CHECK (id = app.current_user_id())` policy on `identity.users` is
    what makes registration safe: the id is generated, bound as the principal, and
    only then inserted. Trying to insert a *different* id under that context must
    be rejected by the database, not merely by application code.
    """
    a, b = uuid7(), uuid7()
    with pytest.raises(Exception) as excinfo:
        async with db.session(_principal(a)) as session:
            await session.execute(
                text("INSERT INTO identity.users (id, email, display_name) " "VALUES (:id, :email, :name)"),
                {"id": str(b), "email": "wrong-owner@example.com", "name": "Nope"},
            )
    # A row-level-security violation, specifically.
    assert "row-level security" in str(excinfo.value).lower()


async def test_a_user_sees_only_their_own_row(db: Database) -> None:
    a, b = uuid7(), uuid7()
    await _insert_user(db, a, "rls-a@example.com")
    await _insert_user(db, b, "rls-b@example.com")

    async with db.session(_principal(a)) as session:
        # An unqualified SELECT — no WHERE clause at all — must still return only
        # the caller's row. This is the forgotten-predicate scenario made safe.
        rows = (await session.execute(text("SELECT id FROM identity.users"))).scalars().all()
    assert [str(r) for r in rows] == [str(a)]


async def test_explicitly_naming_another_users_id_returns_nothing(db: Database) -> None:
    a, b = uuid7(), uuid7()
    await _insert_user(db, a, "rls-a@example.com")
    await _insert_user(db, b, "rls-b@example.com")

    async with db.session(_principal(a)) as session:
        # Even asking for B's row by primary key, as user A, returns empty. The
        # policy filters it before the equality is ever considered.
        result = await session.execute(text("SELECT id FROM identity.users WHERE id = :bid"), {"bid": str(b)})
        assert result.scalar_one_or_none() is None


async def test_isolation_fails_closed_with_no_context(db: Database) -> None:
    a = uuid7()
    await _insert_user(db, a, "rls-a@example.com")

    # SYSTEM_PRINCIPAL leaves app.current_user_id unset. `user_id = NULL` is NULL,
    # which filters every row: an unscoped session sees nothing, not everything.
    async with db.session(SYSTEM_PRINCIPAL) as session:
        count = (await session.execute(text("SELECT count(*) FROM identity.users"))).scalar_one()
    assert count == 0


async def test_athlete_scoped_tables_are_all_isolated(db: Database) -> None:
    """Spot-check the profile tables, not just users, so the policy loop in
    migration 0009 is exercised and not merely assumed."""
    a, b = uuid7(), uuid7()
    await _insert_user(db, a, "rls-a@example.com")
    await _insert_user(db, b, "rls-b@example.com")

    async with db.session(_principal(a)) as session:
        await session.execute(
            text(
                "INSERT INTO training.athlete_goals "
                "(id, user_id, goal_type, primary_sport) "
                "VALUES (:id, :uid, 'endurance', 'run')"
            ),
            {"id": str(uuid7()), "uid": str(a)},
        )

    # B cannot see A's goal even with an unqualified scan.
    async with db.session(_principal(b)) as session:
        count = (await session.execute(text("SELECT count(*) FROM training.athlete_goals"))).scalar_one()
    assert count == 0


async def test_connection_role_cannot_bypass_rls(db: Database) -> None:
    """The whole scheme rests on `app_rw` not being able to bypass RLS.

    If `app_rw` were SUPERUSER or had BYPASSRLS the policies above would be
    decorative. Assert the property directly rather than trusting the migration.
    """
    async with db.session(SYSTEM_PRINCIPAL) as session:
        row = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles " "WHERE rolname = current_user")
            )
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False
