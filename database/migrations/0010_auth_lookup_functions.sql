-- =============================================================================
-- 0010 — Authentication lookup functions
-- =============================================================================
-- REVIEW REQUIRED. See database/README.md.
-- Run as app_migrator. Depends on 0009.
--
-- WHY THIS MIGRATION EXISTS
--
-- Found while implementing authentication: the RLS policies in 0009 create a
-- chicken-and-egg problem for the two lookups that happen BEFORE a user is
-- identified.
--
--   * Login looks up a user by email. `users_self` is
--     `USING (id = app.current_user_id())`, and at that moment there is no
--     current user — so the lookup returns zero rows and login is impossible.
--   * Token refresh looks up a refresh token by its hash to discover which user
--     it belongs to. `refresh_tokens_self` has the same problem.
--
-- Three ways to resolve it, and why this one:
--
--   (a) Give the application a role that bypasses RLS. Rejected — it discards
--       the entire backstop for the sake of two queries.
--   (b) Exempt `users` and `refresh_tokens` from RLS. Rejected — those tables
--       hold credentials and are exactly what the backstop is for.
--   (c) Two narrow SECURITY DEFINER functions that return only the columns
--       authentication needs. Chosen.
--
-- Each function is deliberately minimal: a single lookup key, a fixed column
-- list, no free-form input, no dynamic SQL, and `search_path` pinned so it
-- cannot be hijacked. They are the ONLY RLS holes in the schema, they are
-- reviewable in one screen, and neither can enumerate or return bulk data.
--
-- Once a function has identified the user, the application sets
-- `app.current_user_id` and every subsequent statement is normally RLS-scoped
-- again — including the failed-login counter update and the token rotation.
--
-- -----------------------------------------------------------------------------
-- Tables intentionally WITHOUT row-level security, recorded here so the
-- omissions are deliberate rather than forgotten:
--
--   identity.organizations        tenant directory; no health data
--   training.provider_events      raw webhook payloads, written with no user
--                                 context by design; no user-facing read path
--   analytics.algorithm_versions  global configuration
--   analytics.eval_runs           global; no athlete data
--   billing.subscription_plans    public catalogue
--   rewards.reward_policies       global configuration
--   partners.*                    partner-scoped, guarded at the route layer;
--                                 contains no athlete health data
--   community.achievements etc.   covered in 0009 where athlete-scoped
-- =============================================================================

SET search_path = identity, public;


-- -----------------------------------------------------------------------------
-- lookup_user_for_authentication
-- -----------------------------------------------------------------------------
-- Returns only what the login flow needs to decide whether to authenticate, and
-- nothing else. Notably it does NOT return email, display_name, or any profile
-- data, so it cannot be used as a data-exfiltration primitive even if a caller
-- could pass arbitrary addresses.
--
-- Soft-deleted accounts are excluded here rather than in the caller, so a
-- forgotten check in application code cannot resurrect a closed account.
CREATE OR REPLACE FUNCTION identity.lookup_user_for_authentication(p_email CITEXT)
RETURNS TABLE (
    id                UUID,
    password_hash     TEXT,
    status            TEXT,
    role              TEXT,
    org_id            UUID,
    email_verified_at TIMESTAMPTZ,
    failed_logins     INTEGER,
    locked_until      TIMESTAMPTZ
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT u.id, u.password_hash, u.status, u.role, u.org_id,
           u.email_verified_at, u.failed_logins, u.locked_until
    FROM identity.users u
    WHERE u.email = p_email
      AND u.deleted_at IS NULL;
$$;

REVOKE ALL ON FUNCTION identity.lookup_user_for_authentication(CITEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.lookup_user_for_authentication(CITEXT) TO app_rw;

COMMENT ON FUNCTION identity.lookup_user_for_authentication(CITEXT) IS
    'Pre-authentication lookup by email. SECURITY DEFINER because RLS on '
    'identity.users would otherwise make login impossible - there is no current '
    'user yet. Returns credential-check columns only. Callers MUST compute a '
    'password hash even when no row is returned, so response timing does not '
    'reveal whether the account exists.';


-- -----------------------------------------------------------------------------
-- lookup_refresh_token
-- -----------------------------------------------------------------------------
-- Looked up by SHA-256 hash of the presented token. Returns the token's state
-- including `rotated_at`, because a token that has already been rotated is the
-- signature of a stolen credential and the caller must revoke the whole family.
CREATE OR REPLACE FUNCTION identity.lookup_refresh_token(p_token_hash BYTEA)
RETURNS TABLE (
    id             UUID,
    user_id        UUID,
    family_id      UUID,
    issued_at      TIMESTAMPTZ,
    expires_at     TIMESTAMPTZ,
    rotated_at     TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    user_status    TEXT,
    user_role      TEXT,
    user_org_id    UUID
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.id, t.user_id, t.family_id, t.issued_at, t.expires_at,
           t.rotated_at, t.revoked_at, u.status, u.role, u.org_id
    FROM identity.refresh_tokens t
    JOIN identity.users u ON u.id = t.user_id
    WHERE t.token_hash = p_token_hash
      AND u.deleted_at IS NULL;
$$;

REVOKE ALL ON FUNCTION identity.lookup_refresh_token(BYTEA) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.lookup_refresh_token(BYTEA) TO app_rw;

COMMENT ON FUNCTION identity.lookup_refresh_token(BYTEA) IS
    'Pre-authentication lookup by token hash. Lookup is by hash only, so it '
    'cannot be used to enumerate: an attacker who could call it already holds '
    'the token. Returns rotated_at so the caller can detect reuse and revoke '
    'the family.';


-- -----------------------------------------------------------------------------
-- revoke_token_family
-- -----------------------------------------------------------------------------
-- Reuse detection has to revoke every token in a family, and it happens in a
-- request where the presented credential is untrusted. Doing it in a definer
-- function means the revocation cannot be skipped by a caller that failed to
-- establish context, and keeps the write narrow: it only ever sets revocation
-- columns, and only for one family.
CREATE OR REPLACE FUNCTION identity.revoke_token_family(p_family_id UUID, p_reason TEXT)
RETURNS INTEGER
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    affected INTEGER;
BEGIN
    IF p_reason NOT IN ('logout', 'rotation', 'reuse_detected', 'password_change',
                        'admin_revoke', 'account_closed') THEN
        RAISE EXCEPTION 'invalid revocation reason: %', p_reason;
    END IF;

    UPDATE identity.refresh_tokens
       SET revoked_at = now(),
           revoked_reason = p_reason
     WHERE family_id = p_family_id
       AND revoked_at IS NULL;

    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END
$$;

REVOKE ALL ON FUNCTION identity.revoke_token_family(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity.revoke_token_family(UUID, TEXT) TO app_rw;

COMMENT ON FUNCTION identity.revoke_token_family(UUID, TEXT) IS
    'Revokes every live token in a rotation family. Used on logout-all and on '
    'reuse detection, where the caller cannot be trusted to have established a '
    'user context.';


-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- Dropping these makes login and token refresh impossible while RLS is enabled.
-- DROP FUNCTION IF EXISTS identity.revoke_token_family(UUID, TEXT);
-- DROP FUNCTION IF EXISTS identity.lookup_refresh_token(BYTEA);
-- DROP FUNCTION IF EXISTS identity.lookup_user_for_authentication(CITEXT);
-- =============================================================================
