"""The check that keeps tenant isolation from degrading quietly as tables are added.

`docs/18` §4 commits to this rule: *every new athlete-scoped table gets `user_id`
plus an RLS policy in the same reviewed migration, or the build fails.* Until now
that was a review convention — a human noticing. This makes it mechanical.

The rule has two halves, and both are enforced here:

1. A table without RLS must have a row in `app.rls_exemptions` explaining why. A
   forgotten policy is then a build failure rather than a silent data leak waiting
   for the first query that omits a `WHERE`.
2. An exemption for a table that *now has* RLS is stale and must be deleted.
   Otherwise the registry accumulates rows nobody re-reads and stops meaning
   anything — which is how a rubber-stamp control is born.

**Why these tests skip rather than fail when the registry is absent.** Migration
0011 creates `app.rls_exemptions`, and per ADR-012 and the organisation's policy
migrations are applied by a human, never automatically. A red suite on a developer's
machine because a reviewed migration has not been applied yet would train people to
ignore red, which is worse than the gap being open for a day. The skip message says
exactly what to apply, and the check arms itself the moment 0011 lands.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.database.session import SYSTEM_PRINCIPAL, Database

pytestmark = [pytest.mark.security, pytest.mark.integration]

# The schemas that hold athlete data. `app` and `public` are infrastructure and
# hold no tenant rows, so they are deliberately outside the check.
ATHLETE_SCHEMAS = (
    "identity",
    "training",
    "analytics",
    "coaching",
    "billing",
    "rewards",
    "partners",
    "community",
    "notifications",
)

_REGISTRY_MISSING = (
    "app.rls_exemptions does not exist. Apply the reviewed migration "
    "database/migrations/0011_rls_exemption_registry.sql (by hand, per ADR-012), "
    "then re-run. These checks arm themselves once it is applied."
)


async def _registry_exists(db: Database) -> bool:
    async with db.session(SYSTEM_PRINCIPAL) as session:
        found = await session.execute(
            text(
                "SELECT 1 FROM pg_tables WHERE schemaname = 'app' AND tablename = 'rls_exemptions'"
            )
        )
        return found.scalar_one_or_none() is not None


async def test_every_table_without_rls_has_a_registered_exemption(db: Database) -> None:
    """A forgotten RLS policy must fail the build, not leak health data."""
    if not await _registry_exists(db):
        pytest.skip(_REGISTRY_MISSING)

    async with db.session(SYSTEM_PRINCIPAL) as session:
        result = await session.execute(
            text(
                """
                SELECT t.schemaname || '.' || t.tablename AS table_ref
                  FROM pg_tables t
                  LEFT JOIN app.rls_exemptions e
                    ON e.schema_name = t.schemaname
                   AND e.table_name  = t.tablename
                 WHERE t.schemaname = ANY(:schemas)
                   AND NOT t.rowsecurity
                   AND e.schema_name IS NULL
                 ORDER BY 1
                """
            ),
            {"schemas": list(ATHLETE_SCHEMAS)},
        )
        unregistered = [row[0] for row in result]

    assert not unregistered, (
        "These tables have no row-level security and no registered exemption:\n  "
        + "\n  ".join(unregistered)
        + "\n\nEither add an RLS policy in the migration that created the table, or "
        "add a justified row to app.rls_exemptions. Do not add an exemption to make "
        "this test pass without deciding which is correct."
    )


async def test_no_stale_exemptions(db: Database) -> None:
    """An exemption for a table that now has RLS must be removed.

    A registry full of rows that no longer apply is a control nobody reads.
    """
    if not await _registry_exists(db):
        pytest.skip(_REGISTRY_MISSING)

    async with db.session(SYSTEM_PRINCIPAL) as session:
        result = await session.execute(
            text(
                """
                SELECT e.schema_name || '.' || e.table_name AS table_ref
                  FROM app.rls_exemptions e
                  JOIN pg_tables t
                    ON t.schemaname = e.schema_name
                   AND t.tablename  = e.table_name
                 WHERE t.rowsecurity
                 ORDER BY 1
                """
            )
        )
        stale = [row[0] for row in result]

    assert not stale, (
        "These tables now have RLS but are still registered as exempt:\n  "
        + "\n  ".join(stale)
        + "\n\nDelete the exemption rows; the policy supersedes them."
    )


async def test_exemptions_reference_tables_that_exist(db: Database) -> None:
    """A row for a dropped table is dead weight that hides a real gap."""
    if not await _registry_exists(db):
        pytest.skip(_REGISTRY_MISSING)

    async with db.session(SYSTEM_PRINCIPAL) as session:
        result = await session.execute(
            text(
                """
                SELECT e.schema_name || '.' || e.table_name AS table_ref
                  FROM app.rls_exemptions e
                  LEFT JOIN pg_tables t
                    ON t.schemaname = e.schema_name
                   AND t.tablename  = e.table_name
                 WHERE t.tablename IS NULL
                 ORDER BY 1
                """
            )
        )
        orphans = [row[0] for row in result]

    assert not orphans, "Exemptions reference tables that do not exist: " + ", ".join(orphans)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, closed by database/migrations/0011_rls_exemption_registry.sql, "
        "which is written and awaiting manual application (ADR-012). app_rw "
        "currently holds DELETE on training.provider_events and "
        "billing.payment_webhook_events, and UPDATE+DELETE on "
        "rewards.wallet_transactions — the money ledger's parent table. All three "
        "are trigger-guarded, so this is a defence-in-depth gap rather than an open "
        "door, but `docs/17` §2.4 requires both layers. "
        "strict=True is deliberate: once 0011 is applied this test XPASSes, which "
        "fails the build and forces this marker to be removed. The gap therefore "
        "cannot be forgotten, and the suite stays green in the meantime instead of "
        "training everyone to ignore a red run."
    ),
)
async def test_append_only_tables_do_not_grant_mutation_to_the_app_role(db: Database) -> None:
    """Immutability is enforced by revoked privilege as well as by trigger.

    A trigger can be disabled by the table owner in one statement. A revoked
    privilege cannot be restored by the application role at all, which is why
    `docs/17` §2.4 requires both. Migration 0011 closes the three gaps this asserts.
    """
    expected_revoked = {
        ("training", "provider_events", "DELETE"),
        ("billing", "payment_webhook_events", "DELETE"),
        ("rewards", "wallet_transactions", "UPDATE"),
        ("rewards", "wallet_transactions", "DELETE"),
        # Already revoked before 0011 — asserted so a future migration cannot
        # quietly re-grant them.
        ("rewards", "wallet_ledger_entries", "UPDATE"),
        ("rewards", "wallet_ledger_entries", "DELETE"),
        ("identity", "audit_events", "UPDATE"),
        ("identity", "audit_events", "DELETE"),
    }

    async with db.session(SYSTEM_PRINCIPAL) as session:
        result = await session.execute(
            text(
                """
                SELECT table_schema, table_name, privilege_type
                  FROM information_schema.table_privileges
                 WHERE grantee = 'app_rw'
                   AND privilege_type IN ('UPDATE', 'DELETE')
                """
            )
        )
        granted = {(row[0], row[1], row[2]) for row in result}

    violations = sorted(expected_revoked & granted)
    if violations:
        # provider_events / payment_webhook_events / wallet_transactions are the
        # three 0011 closes; name the migration so the fix is obvious.
        pytest.fail(
            "app_rw still holds mutation privileges on append-only tables:\n  "
            + "\n  ".join(f"{s}.{t}: {p}" for s, t, p in violations)
            + "\n\nApply database/migrations/0011_rls_exemption_registry.sql, which "
            "revokes these. Immutability must be enforced by privilege as well as "
            "by trigger."
        )
