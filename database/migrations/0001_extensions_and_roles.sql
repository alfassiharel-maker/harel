-- =============================================================================
-- 0001 — Extensions, roles and shared helpers
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
--
-- Run as a superuser or a role with CREATEROLE. Later migrations run as
-- app_migrator.
--
-- Role separation is what makes row-level security meaningful: the application
-- must not be able to bypass its own policies.
-- =============================================================================

-- pgcrypto: gen_random_uuid() as a safety-net default, and digest() for token
-- hashing checks. citext: case-insensitive email without relying on the
-- application to lower() consistently.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- pg_trgm: partner/offer and group name search. Cheap to add now, awkward later
-- because indexes that depend on it cannot be created until it exists.
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- -----------------------------------------------------------------------------
-- Roles
-- -----------------------------------------------------------------------------
-- app_rw       : the application. NOT superuser, NOT bypassrls. This is the
--                whole point — a forgotten WHERE clause must fail closed.
-- app_ro       : read-only analytics and support tooling.
-- app_migrator : owns the schema; the only role that may run DDL.
--
-- Passwords are set out of band (secrets manager), never in a migration file.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
        CREATE ROLE app_rw NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro') THEN
        CREATE ROLE app_ro NOLOGIN;
    END IF;
END
$$;

-- Explicitly assert the security property rather than assuming the default.
ALTER ROLE app_rw NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE app_ro NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;


-- -----------------------------------------------------------------------------
-- Schemas, one per module (see docs/01-architecture.md §3)
-- -----------------------------------------------------------------------------
-- Separate schemas mean a module can later be extracted with
-- `pg_dump --schema=<name>` instead of a hand-written extraction script.

CREATE SCHEMA IF NOT EXISTS identity      AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS training      AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS analytics     AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS coaching      AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS billing       AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS rewards       AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS partners      AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS community     AUTHORIZATION app_migrator;
CREATE SCHEMA IF NOT EXISTS notifications AUTHORIZATION app_migrator;

COMMENT ON SCHEMA identity      IS 'Authentication, users, roles, consent, access grants, audit.';
COMMENT ON SCHEMA training      IS 'Athlete profile, provider links, activities, wellness.';
COMMENT ON SCHEMA analytics     IS 'Materialised metrics, data quality, algorithm governance.';
COMMENT ON SCHEMA coaching      IS 'Digital twin, AI conversations, training plans.';
COMMENT ON SCHEMA billing       IS 'Subscriptions, payments, entitlements.';
COMMENT ON SCHEMA rewards       IS 'Wallet ledger, reward policies, redemptions, payouts.';
COMMENT ON SCHEMA partners      IS 'Partner accounts, offers, attributed conversions.';
COMMENT ON SCHEMA community     IS 'Groups, challenges, achievements, sharing.';
COMMENT ON SCHEMA notifications IS 'Device tokens, preferences, delivery log.';

GRANT USAGE ON SCHEMA identity, training, analytics, coaching, billing,
                      rewards, partners, community, notifications
    TO app_rw, app_ro;

-- Defaults so later migrations do not have to remember to grant. Applies only to
-- objects created by app_migrator, which is the only role permitted to run DDL.
DO $$
DECLARE
    target_schema TEXT;
BEGIN
    FOREACH target_schema IN ARRAY ARRAY[
        'identity', 'training', 'analytics', 'coaching', 'billing',
        'rewards', 'partners', 'community', 'notifications'
    ]
    LOOP
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA %I '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw', target_schema);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA %I '
            'GRANT SELECT ON TABLES TO app_ro', target_schema);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA %I '
            'GRANT USAGE, SELECT ON SEQUENCES TO app_rw', target_schema);
    END LOOP;
END
$$;


-- -----------------------------------------------------------------------------
-- Shared helpers
-- -----------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION app_migrator;
GRANT USAGE ON SCHEMA app TO app_rw, app_ro;

-- Current request identity, read from the transaction-local setting that the
-- API sets after verifying the JWT. Returns NULL when unset, which makes every
-- RLS predicate that uses it evaluate to NULL -> no rows. Isolation fails closed.
CREATE OR REPLACE FUNCTION app.current_user_id() RETURNS UUID
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_user_id', true), '')::UUID;
$$;

CREATE OR REPLACE FUNCTION app.current_org_id() RETURNS UUID
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_org_id', true), '')::UUID;
$$;

CREATE OR REPLACE FUNCTION app.current_role_name() RETURNS TEXT
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_role', true), '');
$$;

COMMENT ON FUNCTION app.current_user_id() IS
    'Authenticated user id for this transaction, or NULL. NULL causes RLS '
    'predicates to filter every row, so a request that forgets to set the '
    'context sees nothing rather than everything.';

-- Touch trigger for mutable tables. Immutable tables do not get one.
CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

-- Guard for append-only tables. Belt and braces alongside the REVOKE below:
-- the REVOKE stops app_rw, this stops anyone connecting as a broader role.
CREATE OR REPLACE FUNCTION app.forbid_mutation() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'Table %.% is append-only; % is not permitted. Insert a compensating '
        'row instead.', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP;
END
$$;

COMMENT ON FUNCTION app.forbid_mutation() IS
    'Attach as a BEFORE UPDATE OR DELETE trigger on append-only tables '
    '(ledger entries, audit events, provider events, prediction records).';


-- =============================================================================
-- ROLLBACK (review before running — dropping schemas destroys data)
-- =============================================================================
-- DROP SCHEMA IF EXISTS app, notifications, community, partners, rewards,
--     billing, coaching, analytics, training, identity CASCADE;
-- DROP ROLE IF EXISTS app_rw, app_ro, app_migrator;
-- =============================================================================
