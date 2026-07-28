-- =============================================================================
-- 0002 — Identity, consent, access grants, audit
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0001.
-- =============================================================================

SET search_path = identity, public;


-- -----------------------------------------------------------------------------
-- organizations — the second tenancy axis (clubs, teams, coaching businesses)
-- -----------------------------------------------------------------------------
-- Direct-to-consumer athletes have no organization. Modelling it from the start
-- means multi-tenant isolation is one predicate everywhere rather than a later
-- retrofit through every table.
CREATE TABLE organizations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT        NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    slug          CITEXT      NOT NULL UNIQUE,
    kind          TEXT        NOT NULL DEFAULT 'club'
                              CHECK (kind IN ('club', 'team', 'coaching_business', 'corporate')),
    country_code  CHAR(2),
    status        TEXT        NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'suspended', 'closed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);

COMMENT ON TABLE organizations IS
    'Tenant boundary for club/coach deployments. NULL org on a user means a '
    'direct consumer athlete.';


-- -----------------------------------------------------------------------------
-- users
-- -----------------------------------------------------------------------------
CREATE TABLE users (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Home organization, if any. Membership in additional orgs lives in
    -- org_memberships; this column is the athlete's own tenant for RLS.
    org_id            UUID REFERENCES organizations (id) ON DELETE SET NULL,
    email             CITEXT      NOT NULL,
    -- Argon2id or bcrypt, algorithm and parameters encoded in the hash string.
    -- NULL for accounts created purely via a social/Apple sign-in.
    password_hash     TEXT,
    display_name      TEXT        NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 120),
    locale            TEXT        NOT NULL DEFAULT 'he-IL',
    timezone          TEXT        NOT NULL DEFAULT 'Asia/Jerusalem',
    -- Coarse role. Fine-grained cross-user access is data_access_grants only.
    role              TEXT        NOT NULL DEFAULT 'athlete'
                                  CHECK (role IN ('athlete', 'coach', 'partner', 'admin', 'support')),
    status            TEXT        NOT NULL DEFAULT 'active'
                                  CHECK (status IN ('pending_verification', 'active', 'suspended', 'closed')),
    email_verified_at TIMESTAMPTZ,
    last_login_at     TIMESTAMPTZ,
    -- Failed-login throttling state. Kept on the row so a lockout survives a
    -- Redis flush; Redis alone would make the control bypassable by restarting it.
    failed_logins     INTEGER     NOT NULL DEFAULT 0,
    locked_until      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Soft delete. GDPR erasure is a separate, audited procedure.
    deleted_at        TIMESTAMPTZ
);

-- Case-insensitive uniqueness, and only among live accounts, so a closed
-- account does not permanently block re-registration of an address.
CREATE UNIQUE INDEX users_email_active_key ON users (email) WHERE deleted_at IS NULL;
CREATE INDEX users_org_idx      ON users (org_id) WHERE deleted_at IS NULL;
CREATE INDEX users_role_idx     ON users (role)   WHERE deleted_at IS NULL;

CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();
CREATE TRIGGER organizations_touch BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON COLUMN users.password_hash IS
    'Argon2id preferred. NULL is valid for federated-only accounts.';


-- -----------------------------------------------------------------------------
-- org_memberships — a coach may serve several clubs
-- -----------------------------------------------------------------------------
CREATE TABLE org_memberships (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'coach', 'member')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (org_id, user_id)
);

CREATE INDEX org_memberships_user_idx ON org_memberships (user_id) WHERE revoked_at IS NULL;


-- -----------------------------------------------------------------------------
-- refresh_tokens — rotating, with reuse detection
-- -----------------------------------------------------------------------------
-- Stores a SHA-256 hash, never the token: a database disclosure must not hand
-- over live sessions. `family_id` groups the rotation chain, so presenting an
-- already-rotated token (the signature of a stolen token) lets us revoke the
-- entire family rather than just the one credential.
CREATE TABLE refresh_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    family_id    UUID        NOT NULL,
    token_hash   BYTEA       NOT NULL UNIQUE,
    -- Binds the token to a device so a leaked token used elsewhere is detectable.
    device_id    TEXT,
    user_agent   TEXT,
    ip_address   INET,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    rotated_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    revoked_reason TEXT CHECK (revoked_reason IN
                    ('logout', 'rotation', 'reuse_detected', 'password_change',
                     'admin_revoke', 'account_closed')),
    CHECK (expires_at > issued_at)
);

CREATE INDEX refresh_tokens_user_idx   ON refresh_tokens (user_id);
CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id);
-- Reaper for expired rows.
CREATE INDEX refresh_tokens_expiry_idx ON refresh_tokens (expires_at)
    WHERE revoked_at IS NULL;

COMMENT ON TABLE refresh_tokens IS
    'Opaque rotating refresh tokens, stored hashed. Presenting a token whose '
    'rotated_at is set means the token leaked: revoke the whole family_id.';


-- -----------------------------------------------------------------------------
-- mfa_credentials — TOTP. Table exists now; feature ships in Phase 2.
-- -----------------------------------------------------------------------------
CREATE TABLE mfa_credentials (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind         TEXT        NOT NULL DEFAULT 'totp' CHECK (kind IN ('totp', 'recovery_codes')),
    -- AES-256-GCM ciphertext; kms_key_id records which key so rotation is possible.
    secret_ct    BYTEA       NOT NULL,
    kms_key_id   TEXT        NOT NULL,
    confirmed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ
);

CREATE INDEX mfa_credentials_user_idx ON mfa_credentials (user_id) WHERE revoked_at IS NULL;


-- -----------------------------------------------------------------------------
-- consents — append-only, versioned
-- -----------------------------------------------------------------------------
-- GDPR requires demonstrating *what* was consented to and *when*. Withdrawal is
-- a new row with granted = false, never an update, so the history stands.
CREATE TABLE consents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    purpose      TEXT        NOT NULL CHECK (purpose IN (
                                 'terms_of_service', 'privacy_policy',
                                 'health_data_processing', 'marketing_email',
                                 'partner_data_sharing', 'ai_training_improvement')),
    document_version TEXT    NOT NULL,
    granted      BOOLEAN     NOT NULL,
    ip_address   INET,
    user_agent   TEXT,
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX consents_user_purpose_idx ON consents (user_id, purpose, recorded_at DESC);

CREATE TRIGGER consents_append_only BEFORE UPDATE OR DELETE ON consents
    FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();

COMMENT ON TABLE consents IS
    'Append-only consent ledger. Current state for a purpose is the latest row '
    'by recorded_at. Health data processing requires an explicit granted row.';


-- -----------------------------------------------------------------------------
-- data_access_grants — the ONLY way one person reads another athlete's data
-- -----------------------------------------------------------------------------
CREATE TABLE data_access_grants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The athlete who owns the data and who alone may create or revoke this.
    grantor_user_id UUID      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    grantee_user_id UUID      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- Least privilege: an array of scopes, not a boolean "can see everything".
    scopes        TEXT[]      NOT NULL CHECK (
                      cardinality(scopes) > 0
                      AND scopes <@ ARRAY['activities', 'metrics', 'wellness',
                                          'plans', 'goals', 'conversations']::TEXT[]),
    -- Expiry is mandatory: an open-ended grant to a coach the athlete stopped
    -- working with two years ago is exactly the leak we are preventing.
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    CHECK (grantor_user_id <> grantee_user_id),
    CHECK (expires_at > created_at)
);

CREATE INDEX data_access_grants_grantee_idx
    ON data_access_grants (grantee_user_id, grantor_user_id)
    WHERE revoked_at IS NULL;
CREATE INDEX data_access_grants_grantor_idx
    ON data_access_grants (grantor_user_id)
    WHERE revoked_at IS NULL;

COMMENT ON TABLE data_access_grants IS
    'Scoped, expiring, athlete-revocable access. Referenced directly by the RLS '
    'grant policies in 0009 — there is no application-only path to another '
    'user data.';


-- -----------------------------------------------------------------------------
-- audit_events — append-only
-- -----------------------------------------------------------------------------
CREATE TABLE audit_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Who acted. NULL for system/scheduler actions.
    actor_user_id UUID REFERENCES users (id) ON DELETE SET NULL,
    actor_role    TEXT,
    -- Whose data was affected. Populated for every cross-user health data read.
    subject_user_id UUID REFERENCES users (id) ON DELETE SET NULL,
    org_id        UUID REFERENCES organizations (id) ON DELETE SET NULL,
    action        TEXT        NOT NULL,
    resource_type TEXT        NOT NULL,
    resource_id   TEXT,
    outcome       TEXT        NOT NULL DEFAULT 'success'
                              CHECK (outcome IN ('success', 'denied', 'error')),
    ip_address    INET,
    request_id    TEXT,
    -- Detail must never contain health values or credentials — only identifiers
    -- and metadata. Enforced by review and by the log scrubber.
    detail        JSONB       NOT NULL DEFAULT '{}'::JSONB,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_events_subject_idx ON audit_events (subject_user_id, occurred_at DESC);
CREATE INDEX audit_events_actor_idx   ON audit_events (actor_user_id, occurred_at DESC);
CREATE INDEX audit_events_action_idx  ON audit_events (action, occurred_at DESC);

CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();

COMMENT ON TABLE audit_events IS
    'Append-only. Retained 7 years and survives account erasure in redacted '
    'form (subject_user_id set NULL, detail tombstoned).';


-- Append-only enforcement at the privilege level as well as the trigger level.
REVOKE UPDATE, DELETE ON identity.consents     FROM app_rw;
REVOKE UPDATE, DELETE ON identity.audit_events FROM app_rw;


-- =============================================================================
-- ROLLBACK (review before running — destroys accounts and audit history)
-- =============================================================================
-- DROP TABLE IF EXISTS identity.audit_events, identity.data_access_grants,
--     identity.consents, identity.mfa_credentials, identity.refresh_tokens,
--     identity.org_memberships, identity.users, identity.organizations CASCADE;
-- =============================================================================
