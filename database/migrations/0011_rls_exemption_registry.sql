-- =============================================================================
-- 0011 — RLS exemption registry + append-only privilege hardening
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0010.
-- Reversible: yes (drop the table, re-GRANT the privileges).
-- Lock impact: online-safe. The REVOKEs take a brief ACCESS EXCLUSIVE lock on
--   each named table — milliseconds, but not zero, so prefer a quiet moment.
--
-- WHY THIS MIGRATION EXISTS
--
-- Two gaps found while auditing the schema for the Phase 2 surfaces, both of the
-- same shape: a rule the project relies on is documented in a comment rather
-- than enforced by the database.
--
-- GAP 1 — the non-RLS table list is a comment, not data.
--
--   0009 enables RLS on every athlete-scoped table. A handful of tables are
--   deliberately NOT RLS-protected, each for a stated reason: they are
--   provider-scoped or staff-scoped and carry no `user_id` at write time. That
--   list currently exists only in the prose of 0009 and 0010.
--
--   The consequence is that "is this table intentionally exempt, or did someone
--   forget?" cannot be answered by a query — which means CI cannot answer it
--   either, and the isolation guarantee degrades quietly as tables are added.
--   This migration turns the list into a table so the check becomes mechanical.
--
-- GAP 2 — three append-only tables are trigger-guarded but not privilege-guarded.
--
--   `app.forbid_mutation()` is attached to the append-only tables, which stops
--   ordinary UPDATE and DELETE. But defence in depth means the application role
--   should not hold the privilege in the first place: a trigger can be disabled
--   by a table owner, and `ALTER TABLE ... DISABLE TRIGGER` is a single statement.
--
--   Specifically missing today:
--     * training.provider_events      — has the trigger, retains DELETE
--     * billing.payment_webhook_events— has the trigger, retains DELETE
--     * rewards.wallet_transactions   — has the trigger, retains UPDATE + DELETE
--
--   `wallet_transactions` is the one that matters most: it is the parent of the
--   money ledger, and `docs/17` §2.4 commits to immutability being enforced by
--   trigger AND revoked privilege. Right now only the trigger is there.
--
-- NOTE ON provider_events AND RETENTION
--   Revoking DELETE here makes the 90-day pruning in `docs/02` §7 impossible by
--   design. That is intentional and is resolved in migration 0021, which
--   range-partitions the table so pruning becomes `DROP TABLE <partition>` — DDL,
--   which fires no row trigger and takes no long lock. Do not re-grant DELETE to
--   work around it; that would reopen the hole this migration closes.
-- =============================================================================

SET search_path = app, public;


-- -----------------------------------------------------------------------------
-- app.rls_exemptions — the registry
-- -----------------------------------------------------------------------------
-- One row per table that is deliberately not covered by row-level security.
-- Adding a row is a decision with an owner and a date, which is the point: an
-- exemption that nobody signed off on should not exist.
CREATE TABLE app.rls_exemptions (
    schema_name  TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    -- Why this table does not need (or cannot have) an owner policy. Free text on
    -- purpose: the reasoning is what a future reviewer needs, and it does not
    -- enumerate cleanly.
    reason       TEXT NOT NULL CHECK (length(trim(reason)) > 20),
    -- Who accepted the risk. NULL is not permitted: an anonymous exemption is
    -- indistinguishable from an oversight, which is the failure mode here.
    approved_by  TEXT NOT NULL,
    approved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (schema_name, table_name)
);

COMMENT ON TABLE app.rls_exemptions IS
    'Tables deliberately not covered by RLS, with the reason and the approver. '
    'The CI check in tests/security compares this against pg_tables: a table '
    'without RLS and without a row here fails the build.';

-- Read-only to the application. It never needs to write this, and a compromised
-- app role must not be able to legitimise an exemption by inserting a row.
GRANT SELECT ON app.rls_exemptions TO app_rw, app_ro;


-- -----------------------------------------------------------------------------
-- Seed the registry with the current, intentional exemptions
-- -----------------------------------------------------------------------------
-- This is a data change as well as a structural one, and it is the reviewable
-- artefact: if any line below looks wrong, the schema is wrong, not the seed.
INSERT INTO app.rls_exemptions (schema_name, table_name, reason, approved_by) VALUES
    ('identity', 'audit_events',
     'Append-only audit trail. Has no single owning user: a row names an actor and '
     'a subject, and failed-login rows are written with no authenticated user at '
     'all. UPDATE and DELETE are revoked from app_rw, and no read endpoint exists '
     'for athletes. A subject-scoped read path, if ever added, must add a policy.',
     'phase-1-security-review'),

    ('identity', 'organizations',
     'Org-scoped rather than athlete-scoped, and readable by prospective members '
     'during onboarding before any membership row exists. Access is mediated by '
     'org_memberships at the service layer.',
     'phase-1-security-review'),

    ('training', 'provider_events',
     'Raw inbound provider webhooks, persisted before parsing. user_id is NULL at '
     'write time because the provider account has not yet been resolved to one of '
     'our athletes, so an owner policy would reject the insert that keeps the '
     'event from being lost. Immutable; DELETE revoked below.',
     'phase-1-security-review'),

    ('billing', 'subscription_plans',
     'Public product catalogue. Every athlete must be able to read every plan in '
     'order to choose one; there is no tenant dimension to scope by.',
     'phase-1-security-review'),

    ('billing', 'payment_webhook_events',
     'Raw provider payment webhooks, persisted before verification and before the '
     'subscriber is resolved. Signature-verification outcome is recorded on the '
     'row. Immutable; DELETE revoked below.',
     'phase-1-security-review'),

    ('rewards', 'reward_policies',
     'Versioned reward economics. Global configuration, not athlete data. Every '
     'athlete reads the active policy to see the earn rules that apply to them.',
     'phase-1-security-review'),

    ('rewards', 'fraud_signals',
     'Deliberately unreadable by its subject. An athlete must not be able to see '
     'their own fraud assessment: it would tell someone probing the reward system '
     'exactly which checks fired and how to avoid them. Staff-only, via the admin '
     'surface, and every read is audited.',
     'phase-1-security-review'),

    ('partners', 'partners',
     'Partner records, not athlete data. Athletes read partner names and '
     'categories to browse offers. NOTE: partner STAFF isolation is enforced at '
     'the route layer only and has no database backstop — the one tenant boundary '
     'in the schema without one. Migration 0027 adds app.current_partner_id() and '
     'real policies here; until then every partner route carries an explicit row '
     'in the tenant-isolation matrix.',
     'phase-1-security-review'),

    ('partners', 'partner_offers',
     'Offer catalogue, browsable by every athlete. Partner-staff write access is '
     'route-scoped until 0027. See the partners note above.',
     'phase-1-security-review'),

    ('partners', 'partner_conversions',
     'Attributed commercial outcomes. Carries a nullable user_id but is read '
     'per-partner for settlement, not per-athlete, so an owner policy would break '
     'the partner portal. Route-scoped until 0027 keys a policy on the partner.',
     'phase-1-security-review'),

    ('analytics', 'algorithm_versions',
     'Global algorithm registry. Not athlete data. Read by every request that '
     'renders a metric so the engine version can be reported alongside it.',
     'phase-1-security-review'),

    ('analytics', 'eval_runs',
     'Model and algorithm evaluation results. Global governance data with no '
     'tenant dimension.',
     'phase-1-security-review');


-- -----------------------------------------------------------------------------
-- Append-only privilege hardening
-- -----------------------------------------------------------------------------
-- Defence in depth behind app.forbid_mutation(). The trigger stops the statement;
-- these revokes mean the application role never held the right to attempt it.
-- A trigger can be disabled by the table owner in one statement; a revoked
-- privilege cannot be restored by the application role at all.

-- Raw provider events: replayable history. See the retention note in the header —
-- pruning arrives via partitioning in 0021, not via DELETE.
REVOKE DELETE ON training.provider_events FROM app_rw;

-- Raw payment webhooks: the evidence trail for every entitlement decision.
REVOKE DELETE ON billing.payment_webhook_events FROM app_rw;

-- The money ledger's parent table. `docs/17` §2.4 commits to immutability being
-- enforced by trigger AND privilege; this is the privilege half.
REVOKE UPDATE, DELETE ON rewards.wallet_transactions FROM app_rw;

COMMENT ON TABLE rewards.wallet_transactions IS
    'Groups ledger entries; every transaction must net to zero. Immutable: '
    'UPDATE and DELETE are revoked from app_rw (0011) and blocked by trigger. '
    'Corrections are compensating transactions, never edits.';


-- -----------------------------------------------------------------------------
-- Verification — run these after applying
-- -----------------------------------------------------------------------------
-- 1. Every table without RLS must have an exemption row. Zero rows expected.
--
-- SELECT t.schemaname, t.tablename
--   FROM pg_tables t
--   LEFT JOIN app.rls_exemptions e
--     ON e.schema_name = t.schemaname AND e.table_name = t.tablename
--  WHERE t.schemaname IN ('identity','training','analytics','coaching',
--                         'billing','rewards','partners','community',
--                         'notifications')
--    AND NOT t.rowsecurity
--    AND e.schema_name IS NULL;
--
-- 2. And the converse: an exemption for a table that now HAS RLS is stale and
--    should be removed. Zero rows expected.
--
-- SELECT e.schema_name, e.table_name
--   FROM app.rls_exemptions e
--   JOIN pg_tables t
--     ON t.schemaname = e.schema_name AND t.tablename = e.table_name
--  WHERE t.rowsecurity;
--
-- 3. Confirm the revokes took effect. Zero rows expected.
--
-- SELECT table_schema, table_name, privilege_type
--   FROM information_schema.table_privileges
--  WHERE grantee = 'app_rw'
--    AND ((table_schema = 'training'  AND table_name = 'provider_events'
--          AND privilege_type = 'DELETE')
--      OR (table_schema = 'billing'   AND table_name = 'payment_webhook_events'
--          AND privilege_type = 'DELETE')
--      OR (table_schema = 'rewards'   AND table_name = 'wallet_transactions'
--          AND privilege_type IN ('UPDATE','DELETE')));
-- =============================================================================
