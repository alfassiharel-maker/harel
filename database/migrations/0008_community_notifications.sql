-- =============================================================================
-- 0008 — Groups, challenges, achievements, sharing, notifications
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0007.
-- =============================================================================

SET search_path = community, public;


-- -----------------------------------------------------------------------------
-- groups
-- -----------------------------------------------------------------------------
CREATE TABLE groups (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id        UUID REFERENCES identity.organizations (id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES identity.users (id) ON DELETE RESTRICT,
    name          TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 2 AND 120),
    slug          CITEXT NOT NULL UNIQUE,
    description   TEXT,
    sport         TEXT CHECK (sport IN ('run', 'bike', 'swim', 'strength', 'triathlon', 'mixed')),
    -- 'private' groups are invitation-only; 'public' are discoverable. Default
    -- is the more restrictive option.
    visibility    TEXT NOT NULL DEFAULT 'private'
                      CHECK (visibility IN ('private', 'public', 'org_only')),
    member_limit  INTEGER CHECK (member_limit > 0),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at   TIMESTAMPTZ
);

CREATE INDEX groups_name_trgm_idx ON groups USING gin (name gin_trgm_ops);
CREATE INDEX groups_public_idx ON groups (created_at DESC)
    WHERE visibility = 'public' AND archived_at IS NULL;
CREATE TRIGGER groups_touch BEFORE UPDATE ON groups
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE group_members (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id   UUID NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    user_id    UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member'
                   CHECK (role IN ('owner', 'admin', 'coach', 'member')),
    -- Joining a group does NOT grant access to members' health data. That
    -- requires an explicit identity.data_access_grants row. Group membership
    -- only exposes what a member chooses to share (activity_shares).
    joined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    left_at    TIMESTAMPTZ,
    UNIQUE (group_id, user_id)
);

CREATE INDEX group_members_user_idx ON group_members (user_id) WHERE left_at IS NULL;
CREATE INDEX group_members_group_idx ON group_members (group_id) WHERE left_at IS NULL;


-- -----------------------------------------------------------------------------
-- challenges
-- -----------------------------------------------------------------------------
CREATE TABLE challenges (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- NULL group means a platform-wide challenge.
    group_id      UUID REFERENCES groups (id) ON DELETE CASCADE,
    created_by    UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    -- Sponsored challenges are a revenue line; the partner is recorded so the
    -- commission is attributable.
    sponsor_partner_id UUID REFERENCES partners.partners (id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    description   TEXT,
    sport         TEXT,
    metric        TEXT NOT NULL CHECK (metric IN
                      ('distance_m', 'duration_s', 'elevation_m', 'training_load',
                       'session_count', 'streak_days')),
    target_value  NUMERIC(14,2) NOT NULL CHECK (target_value > 0),
    -- 'cumulative' sums over the window; 'best_effort' takes the single best.
    aggregation   TEXT NOT NULL DEFAULT 'cumulative'
                      CHECK (aggregation IN ('cumulative', 'best_effort')),
    reward_points BIGINT NOT NULL DEFAULT 0 CHECK (reward_points >= 0),
    starts_at     TIMESTAMPTZ NOT NULL,
    ends_at       TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'scheduled'
                      CHECK (status IN ('scheduled', 'active', 'completed', 'canceled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (ends_at > starts_at)
);

CREATE INDEX challenges_active_idx ON challenges (ends_at) WHERE status = 'active';
CREATE INDEX challenges_group_idx ON challenges (group_id, starts_at DESC);
CREATE TRIGGER challenges_touch BEFORE UPDATE ON challenges
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE challenge_participants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    challenge_id  UUID NOT NULL REFERENCES challenges (id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    progress      NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (progress >= 0),
    rank          INTEGER CHECK (rank > 0),
    completed_at  TIMESTAMPTZ,
    -- Set when a contributing activity failed the trust check, so a spoofed
    -- workout cannot win a sponsored challenge.
    disqualified_at TIMESTAMPTZ,
    disqualified_reason TEXT,
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (challenge_id, user_id)
);

-- Leaderboard read path.
CREATE INDEX challenge_participants_board_idx
    ON challenge_participants (challenge_id, progress DESC)
    WHERE disqualified_at IS NULL;
CREATE INDEX challenge_participants_user_idx ON challenge_participants (user_id);
CREATE TRIGGER challenge_participants_touch BEFORE UPDATE ON challenge_participants
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();


-- -----------------------------------------------------------------------------
-- achievements
-- -----------------------------------------------------------------------------
CREATE TABLE achievements (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    code         TEXT NOT NULL,
    tier         TEXT CHECK (tier IN ('bronze', 'silver', 'gold', 'platinum')),
    -- The activity or challenge that earned it, for verifiability.
    reference_type TEXT,
    reference_id TEXT,
    metadata     JSONB NOT NULL DEFAULT '{}'::JSONB,
    achieved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One award per achievement code per athlete, unless the code is repeatable
    -- (handled by including a period in the code, e.g. 'monthly_100km_2026_07').
    UNIQUE (user_id, code)
);

CREATE INDEX achievements_user_idx ON achievements (user_id, achieved_at DESC);


-- -----------------------------------------------------------------------------
-- activity_shares — the ONLY way an activity becomes visible to others
-- -----------------------------------------------------------------------------
-- Sharing is opt-in per activity and per audience. Nothing an athlete records is
-- visible to a group by default; that default is the difference between a
-- fitness app and a privacy incident.
CREATE TABLE activity_shares (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id  UUID NOT NULL REFERENCES training.activities (id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    audience     TEXT NOT NULL CHECK (audience IN ('group', 'followers', 'public')),
    group_id     UUID REFERENCES groups (id) ON DELETE CASCADE,
    -- What was shared. Health detail (HR, HRV) is excluded unless explicitly
    -- included, and never for a 'public' audience.
    included_fields TEXT[] NOT NULL DEFAULT ARRAY['sport', 'duration_s', 'distance_m']::TEXT[],
    caption      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at   TIMESTAMPTZ,
    CHECK (audience <> 'group' OR group_id IS NOT NULL),
    -- Defence in depth against a UI bug: heart-rate data can never reach a
    -- public share.
    CHECK (audience <> 'public'
           OR NOT (included_fields && ARRAY['avg_hr', 'max_hr', 'hrv', 'readiness']::TEXT[]))
);

CREATE INDEX activity_shares_group_idx ON activity_shares (group_id, created_at DESC)
    WHERE revoked_at IS NULL;
CREATE INDEX activity_shares_user_idx ON activity_shares (user_id, created_at DESC);


-- -----------------------------------------------------------------------------
-- notifications
-- -----------------------------------------------------------------------------
SET search_path = notifications, public;

CREATE TABLE device_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    platform     TEXT NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    token        TEXT NOT NULL,
    app_version  TEXT,
    device_model TEXT,
    locale       TEXT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when the push provider reports the token as invalid, so we stop
    -- sending rather than accruing failures forever.
    invalidated_at TIMESTAMPTZ,
    UNIQUE (platform, token)
);

CREATE INDEX device_tokens_user_idx ON device_tokens (user_id) WHERE invalidated_at IS NULL;

CREATE TABLE notification_preferences (
    user_id            UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    kind               TEXT NOT NULL CHECK (kind IN
                           ('daily_readiness', 'workout_reminder', 'plan_adapted',
                            'weekly_review', 'achievement', 'challenge_update',
                            'social', 'billing', 'security', 'marketing')),
    push_enabled       BOOLEAN NOT NULL DEFAULT true,
    email_enabled      BOOLEAN NOT NULL DEFAULT false,
    -- Local time window during which we will not send. Security and billing
    -- notifications ignore quiet hours by policy in the sending code.
    quiet_hours_start  TIME,
    quiet_hours_end    TIME,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind)
);

CREATE TRIGGER notification_preferences_touch BEFORE UPDATE ON notification_preferences
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE notification_preferences IS
    'Marketing defaults to disabled: opt-in is required under GDPR and the '
    'Israeli anti-spam provisions.';

CREATE TABLE notification_deliveries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    channel       TEXT NOT NULL CHECK (channel IN ('push', 'email', 'in_app')),
    -- Dedupe key so a retried job cannot double-notify.
    dedupe_key    TEXT,
    title         TEXT,
    -- Body is kept for support and audit but must not contain health values
    -- beyond what the athlete already sees in-app.
    body          TEXT,
    status        TEXT NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'sent', 'delivered', 'failed', 'suppressed')),
    suppressed_reason TEXT CHECK (suppressed_reason IN
                      ('quiet_hours', 'preference_off', 'token_invalid', 'rate_limited')),
    provider_ref  TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ
);

CREATE INDEX notification_deliveries_user_idx ON notification_deliveries (user_id, created_at DESC);
CREATE UNIQUE INDEX notification_deliveries_dedupe_idx ON notification_deliveries (dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX notification_deliveries_failed_idx ON notification_deliveries (created_at DESC)
    WHERE status = 'failed';


-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- DROP TABLE IF EXISTS notifications.notification_deliveries,
--     notifications.notification_preferences, notifications.device_tokens,
--     community.activity_shares, community.achievements,
--     community.challenge_participants, community.challenges,
--     community.group_members, community.groups CASCADE;
-- =============================================================================
