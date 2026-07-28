-- =============================================================================
-- 0009 — Row-level security
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on every preceding migration.
--
-- THIS IS THE TENANT-ISOLATION BACKSTOP. The application already scopes every
-- query by user_id; these policies exist because that discipline will eventually
-- be broken by a mistake. With them, a forgotten WHERE clause returns an empty
-- result set instead of another athlete's health data.
--
-- Why this works:
--   * app.current_user_id() reads a transaction-local setting and returns NULL
--     when unset. `user_id = NULL` evaluates to NULL, which filters the row.
--     Isolation therefore FAILS CLOSED — a request that forgets to establish
--     context sees nothing, not everything.
--   * app_rw is NOT the table owner (app_migrator is) and has neither SUPERUSER
--     nor BYPASSRLS, so policies genuinely apply to it. Verify with the query at
--     the end of this file after applying.
-- =============================================================================

SET search_path = public;


-- -----------------------------------------------------------------------------
-- Helper: does the current user hold a live grant over this athlete's data?
-- -----------------------------------------------------------------------------
-- SECURITY DEFINER so the lookup itself is not subject to RLS on
-- data_access_grants, which would otherwise make the policy unable to see the
-- grant row it needs. The function body is minimal and takes no free-form input,
-- so the usual SECURITY DEFINER risks do not apply. search_path is pinned to
-- defeat search_path hijacking.
CREATE OR REPLACE FUNCTION app.has_grant(target_user_id UUID, required_scope TEXT)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM identity.data_access_grants g
        WHERE g.grantor_user_id = target_user_id
          AND g.grantee_user_id = app.current_user_id()
          AND g.revoked_at IS NULL
          AND g.expires_at > now()
          AND required_scope = ANY (g.scopes)
    );
$$;

REVOKE ALL ON FUNCTION app.has_grant(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.has_grant(UUID, TEXT) TO app_rw, app_ro;

COMMENT ON FUNCTION app.has_grant(UUID, TEXT) IS
    'True when the current user holds an unexpired, unrevoked grant with the '
    'given scope over target_user_id. The only mechanism by which one person '
    'reads another person''s health data.';


-- -----------------------------------------------------------------------------
-- Owner-only tables: visible to the owning athlete, full stop.
-- -----------------------------------------------------------------------------
-- Applied by loop so no table is missed and every policy is identical. Adding a
-- new athlete-scoped table means adding it to this list.
DO $$
DECLARE
    entry TEXT;
    parts TEXT[];
BEGIN
    FOREACH entry IN ARRAY ARRAY[
        -- schema.table
        'training.athlete_profiles',
        'training.athlete_goals',
        'training.personal_bests',
        'training.provider_connections',
        'training.activity_laps',
        'training.activity_streams',
        'training.provider_activity_map',
        'analytics.data_quality_flags',
        'analytics.prediction_records',
        'analytics.prediction_outcomes',
        'analytics.experiment_assignments',
        'coaching.athlete_twin_snapshots',
        'coaching.ai_conversations',
        'coaching.ai_messages',
        'coaching.ai_message_feedback',
        'coaching.ai_usage_counters',
        'coaching.plan_adaptations',
        'billing.subscriptions',
        'billing.payments',
        'billing.entitlements',
        'rewards.wallets',
        'rewards.wallet_transactions',
        'rewards.wallet_ledger_entries',
        'rewards.reward_events',
        'rewards.redemptions',
        'rewards.payouts',
        'community.achievements',
        'community.challenge_participants',
        'community.activity_shares',
        'notifications.device_tokens',
        'notifications.notification_preferences',
        'notifications.notification_deliveries'
    ]
    LOOP
        parts := string_to_array(entry, '.');
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', parts[1], parts[2]);
        EXECUTE format(
            'CREATE POLICY owner_all ON %I.%I FOR ALL '
            'USING (user_id = app.current_user_id()) '
            'WITH CHECK (user_id = app.current_user_id())',
            parts[1], parts[2]);
    END LOOP;
END
$$;


-- -----------------------------------------------------------------------------
-- Tables that are additionally readable under a scoped grant (coach access)
-- -----------------------------------------------------------------------------
-- Read-only, and only for the scope the athlete granted. A coach can never write
-- to an athlete's data through these policies.

ALTER TABLE training.activities ENABLE ROW LEVEL SECURITY;
CREATE POLICY activities_owner ON training.activities FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());
CREATE POLICY activities_granted_read ON training.activities FOR SELECT
    USING (app.has_grant(user_id, 'activities'));

ALTER TABLE training.daily_wellness ENABLE ROW LEVEL SECURITY;
CREATE POLICY wellness_owner ON training.daily_wellness FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());
CREATE POLICY wellness_granted_read ON training.daily_wellness FOR SELECT
    USING (app.has_grant(user_id, 'wellness'));

ALTER TABLE analytics.daily_metrics ENABLE ROW LEVEL SECURITY;
CREATE POLICY metrics_owner ON analytics.daily_metrics FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());
CREATE POLICY metrics_granted_read ON analytics.daily_metrics FOR SELECT
    USING (app.has_grant(user_id, 'metrics'));

ALTER TABLE coaching.training_plans ENABLE ROW LEVEL SECURITY;
CREATE POLICY plans_owner ON coaching.training_plans FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());
CREATE POLICY plans_granted_read ON coaching.training_plans FOR SELECT
    USING (app.has_grant(user_id, 'plans'));

ALTER TABLE coaching.plan_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY plan_sessions_owner ON coaching.plan_sessions FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());
CREATE POLICY plan_sessions_granted_read ON coaching.plan_sessions FOR SELECT
    USING (app.has_grant(user_id, 'plans'));


-- -----------------------------------------------------------------------------
-- Identity tables
-- -----------------------------------------------------------------------------
-- A user reads their own row. Admin and support access does NOT come from a
-- policy exception here: it goes through a separate connection role with an
-- audited purpose, so every support lookup lands in identity.audit_events.
ALTER TABLE identity.users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_self ON identity.users FOR ALL
    USING (id = app.current_user_id())
    WITH CHECK (id = app.current_user_id());

ALTER TABLE identity.refresh_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY refresh_tokens_self ON identity.refresh_tokens FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());

ALTER TABLE identity.mfa_credentials ENABLE ROW LEVEL SECURITY;
CREATE POLICY mfa_self ON identity.mfa_credentials FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());

ALTER TABLE identity.consents ENABLE ROW LEVEL SECURITY;
CREATE POLICY consents_self ON identity.consents FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());

-- Both sides of a grant may see it: the athlete to manage and revoke it, the
-- grantee to know what they hold.
ALTER TABLE identity.data_access_grants ENABLE ROW LEVEL SECURITY;
CREATE POLICY grants_visible ON identity.data_access_grants FOR SELECT
    USING (grantor_user_id = app.current_user_id()
           OR grantee_user_id = app.current_user_id());
-- Only the athlete who owns the data may create or modify a grant over it.
CREATE POLICY grants_managed_by_grantor ON identity.data_access_grants FOR INSERT
    WITH CHECK (grantor_user_id = app.current_user_id());
CREATE POLICY grants_revoked_by_grantor ON identity.data_access_grants FOR UPDATE
    USING (grantor_user_id = app.current_user_id())
    WITH CHECK (grantor_user_id = app.current_user_id());

ALTER TABLE identity.org_memberships ENABLE ROW LEVEL SECURITY;
CREATE POLICY org_memberships_visible ON identity.org_memberships FOR SELECT
    USING (user_id = app.current_user_id()
           OR org_id = app.current_org_id());


-- -----------------------------------------------------------------------------
-- Community: membership-scoped rather than owner-scoped
-- -----------------------------------------------------------------------------
ALTER TABLE community.groups ENABLE ROW LEVEL SECURITY;
CREATE POLICY groups_visible ON community.groups FOR SELECT
    USING (
        visibility = 'public'
        OR owner_user_id = app.current_user_id()
        OR (visibility = 'org_only' AND org_id = app.current_org_id())
        OR EXISTS (
            SELECT 1 FROM community.group_members m
            WHERE m.group_id = groups.id
              AND m.user_id = app.current_user_id()
              AND m.left_at IS NULL
        )
    );
CREATE POLICY groups_owner_write ON community.groups FOR UPDATE
    USING (owner_user_id = app.current_user_id())
    WITH CHECK (owner_user_id = app.current_user_id());

ALTER TABLE community.group_members ENABLE ROW LEVEL SECURITY;
-- A member sees the roster of groups they belong to, and their own memberships.
CREATE POLICY group_members_visible ON community.group_members FOR SELECT
    USING (
        user_id = app.current_user_id()
        OR EXISTS (
            SELECT 1 FROM community.group_members mine
            WHERE mine.group_id = group_members.group_id
              AND mine.user_id = app.current_user_id()
              AND mine.left_at IS NULL
        )
    );
CREATE POLICY group_members_self_write ON community.group_members FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());

ALTER TABLE community.challenges ENABLE ROW LEVEL SECURITY;
CREATE POLICY challenges_visible ON community.challenges FOR SELECT
    USING (
        group_id IS NULL  -- platform-wide
        OR EXISTS (
            SELECT 1 FROM community.group_members m
            WHERE m.group_id = challenges.group_id
              AND m.user_id = app.current_user_id()
              AND m.left_at IS NULL
        )
    );


-- =============================================================================
-- POST-APPLY VERIFICATION — run these and check the output before trusting RLS
-- =============================================================================
--
-- 1. The application role must not be able to bypass policies:
--
--   SELECT rolname, rolsuper, rolbypassrls
--   FROM pg_roles WHERE rolname IN ('app_rw', 'app_ro');
--   -- expect rolsuper = f and rolbypassrls = f for both
--
-- 2. The application role must not own any table (owners bypass RLS unless
--    FORCE ROW LEVEL SECURITY is set):
--
--   SELECT schemaname, tablename, tableowner
--   FROM pg_tables
--   WHERE schemaname IN ('identity','training','analytics','coaching',
--                        'billing','rewards','partners','community','notifications')
--     AND tableowner <> 'app_migrator';
--   -- expect zero rows
--
-- 3. Every athlete-scoped table must have RLS enabled and at least one policy:
--
--   SELECT c.relname, c.relrowsecurity, count(p.polname) AS policies
--   FROM pg_class c
--   JOIN pg_namespace n ON n.oid = c.relnamespace
--   LEFT JOIN pg_policy p ON p.polrelid = c.oid
--   WHERE n.nspname IN ('identity','training','analytics','coaching',
--                       'billing','rewards','community','notifications')
--     AND c.relkind = 'r'
--   GROUP BY 1, 2
--   ORDER BY c.relrowsecurity, 1;
--   -- inspect any row with relrowsecurity = f and confirm it is intentionally
--   -- global (subscription_plans, reward_policies, algorithm_versions, partners)
--
-- 4. Isolation smoke test, as app_rw, in a transaction:
--
--   BEGIN;
--     SET LOCAL app.current_user_id = '<athlete A uuid>';
--     SELECT count(*) FROM training.activities;              -- A's count
--     SET LOCAL app.current_user_id = '<athlete B uuid>';
--     SELECT count(*) FROM training.activities;              -- B's count
--     RESET app.current_user_id;
--     SELECT count(*) FROM training.activities;              -- MUST be 0
--   ROLLBACK;
--
-- The automated equivalent of step 4 lives in the security test suite and runs
-- in CI against every endpoint. See docs/09-testing-and-model-governance.md.
--
-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- Disabling RLS removes the tenant-isolation backstop. Do not do this to
-- "unblock" a query — fix the query. To disable for one table during an
-- investigation:
--   ALTER TABLE <schema>.<table> DISABLE ROW LEVEL SECURITY;
-- =============================================================================
