-- =============================================================================
-- 0003 — Athlete profile, provider links, activities, wellness
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0002.
-- =============================================================================

SET search_path = training, public;


-- -----------------------------------------------------------------------------
-- athlete_profiles — 1:1 with identity.users
-- -----------------------------------------------------------------------------
-- Maps field-for-field onto backend.algorithms.types.AthleteProfile. Every
-- threshold is nullable: the analytics engine degrades to a lower-trust load
-- source rather than refusing to produce a number.
CREATE TABLE athlete_profiles (
    user_id                  UUID PRIMARY KEY
                                 REFERENCES identity.users (id) ON DELETE CASCADE,
    sex                      TEXT NOT NULL DEFAULT 'unspecified'
                                 CHECK (sex IN ('male', 'female', 'unspecified')),
    -- Date of birth rather than age, so age is always current. Exposed to the
    -- AI layer only as an age band (see docs/05-ai-architecture.md).
    birth_date               DATE,
    height_cm                NUMERIC(5,1) CHECK (height_cm BETWEEN 80 AND 260),
    weight_kg                NUMERIC(5,2) CHECK (weight_kg BETWEEN 25 AND 350),
    level                    TEXT NOT NULL DEFAULT 'intermediate'
                                 CHECK (level IN ('beginner', 'intermediate', 'advanced')),
    training_age_years       NUMERIC(4,1) CHECK (training_age_years >= 0),
    injuries_last_12m        SMALLINT NOT NULL DEFAULT 0 CHECK (injuries_last_12m >= 0),

    -- Physiological anchors. Ranges are sanity bounds, not clinical limits: they
    -- exist to reject sensor nonsense and typos, which would otherwise poison
    -- every derived metric.
    hr_max                   SMALLINT CHECK (hr_max BETWEEN 100 AND 240),
    hr_rest                  SMALLINT CHECK (hr_rest BETWEEN 25 AND 120),
    lthr                     SMALLINT CHECK (lthr BETWEEN 80 AND 230),
    -- Cycling FTP and running threshold power are DIFFERENT quantities in the
    -- same unit. Kept in separate columns; using one for the other silently
    -- corrupts load for that sport.
    ftp_watts                NUMERIC(6,1) CHECK (ftp_watts BETWEEN 30 AND 800),
    run_threshold_power_w    NUMERIC(6,1) CHECK (run_threshold_power_w BETWEEN 50 AND 800),
    threshold_pace_s_per_km  NUMERIC(6,1) CHECK (threshold_pace_s_per_km BETWEEN 120 AND 900),
    css_s_per_100m           NUMERIC(6,1) CHECK (css_s_per_100m BETWEEN 40 AND 300),
    vo2max                   NUMERIC(4,1) CHECK (vo2max BETWEEN 15 AND 100),

    -- Where each threshold came from, so the UI can distinguish "you tested
    -- this" from "we estimated it" and the AI can hedge appropriately.
    threshold_sources        JSONB NOT NULL DEFAULT '{}'::JSONB,
    thresholds_updated_at    TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (hr_rest IS NULL OR hr_max IS NULL OR hr_max > hr_rest)
);

CREATE TRIGGER athlete_profiles_touch BEFORE UPDATE ON athlete_profiles
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();


-- -----------------------------------------------------------------------------
-- athlete_goals
-- -----------------------------------------------------------------------------
CREATE TABLE athlete_goals (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    goal_type     TEXT NOT NULL CHECK (goal_type IN
                      ('race_time', 'endurance', 'strength', 'weight_loss', 'general_fitness')),
    primary_sport TEXT NOT NULL CHECK (primary_sport IN
                      ('run', 'bike', 'swim', 'strength', 'triathlon', 'other')),
    -- e.g. distance 10000 m in target_value 2400 s by race_date.
    target_distance_m NUMERIC(10,1),
    target_value      NUMERIC(12,3),
    target_unit       TEXT,
    race_date     DATE,
    priority      SMALLINT NOT NULL DEFAULT 1 CHECK (priority BETWEEN 1 AND 3),
    status        TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'achieved', 'abandoned', 'expired')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX athlete_goals_user_idx ON athlete_goals (user_id, status, priority);
CREATE TRIGGER athlete_goals_touch BEFORE UPDATE ON athlete_goals
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();


-- -----------------------------------------------------------------------------
-- personal_bests — feeds the personalised Riegel exponent
-- -----------------------------------------------------------------------------
CREATE TABLE personal_bests (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    sport        TEXT NOT NULL CHECK (sport IN ('run', 'bike', 'swim', 'other')),
    distance_m   NUMERIC(10,1) NOT NULL CHECK (distance_m > 0),
    time_s       NUMERIC(10,2) NOT NULL CHECK (time_s > 0),
    achieved_on  DATE NOT NULL,
    -- 'race' bests are trustworthy; 'estimated' ones (best segment of a training
    -- run) are not, and the prediction models weight them differently.
    source       TEXT NOT NULL DEFAULT 'activity'
                     CHECK (source IN ('race', 'activity', 'estimated', 'self_reported')),
    activity_id  UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, sport, distance_m, achieved_on)
);

CREATE INDEX personal_bests_user_idx ON personal_bests (user_id, sport, distance_m);


-- -----------------------------------------------------------------------------
-- provider_connections — one OAuth link per provider per athlete
-- -----------------------------------------------------------------------------
CREATE TABLE provider_connections (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    provider          TEXT NOT NULL CHECK (provider IN
                          ('garmin', 'apple_health', 'coros', 'polar', 'suunto', 'strava', 'manual')),
    provider_user_id  TEXT NOT NULL,
    -- Tokens are AES-256-GCM ciphertext, envelope-encrypted with a KMS key.
    -- kms_key_id is stored per row so keys can be rotated without a full
    -- re-encryption outage.
    access_token_ct   BYTEA,
    refresh_token_ct  BYTEA,
    kms_key_id        TEXT,
    token_expires_at  TIMESTAMPTZ,
    scopes            TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    status            TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'expired', 'revoked', 'error')),
    last_sync_at      TIMESTAMPTZ,
    last_error        TEXT,
    -- Provider-side backfill windows are limited; track how far back we have gone.
    backfilled_from   DATE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One live link per provider per athlete.
    UNIQUE (user_id, provider),
    -- And the provider account cannot be attached to two of our athletes, which
    -- would otherwise let one person's data land in another person's history.
    UNIQUE (provider, provider_user_id)
);

CREATE INDEX provider_connections_sync_idx ON provider_connections (last_sync_at)
    WHERE status = 'active';
CREATE TRIGGER provider_connections_touch BEFORE UPDATE ON provider_connections
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE provider_connections IS
    'Provider OAuth state. Token columns are ciphertext; the plaintext never '
    'exists outside a request that needs it.';


-- -----------------------------------------------------------------------------
-- provider_events — raw inbound webhooks, APPEND-ONLY
-- -----------------------------------------------------------------------------
-- Persisted before parsing. A parser bug then becomes a replay rather than
-- permanent data loss, and a provider changing its payload shape becomes a
-- backfill rather than an incident. Retention 90 days.
CREATE TABLE provider_events (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider           TEXT NOT NULL,
    -- Provider's own event identifier: the idempotency key. Providers retry.
    provider_event_id  TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    -- May be NULL if we cannot resolve the provider account to one of our users
    -- yet; the row is still kept so the event is not lost.
    user_id            UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    payload            JSONB NOT NULL,
    signature_verified BOOLEAN NOT NULL DEFAULT false,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,
    process_attempts   SMALLINT NOT NULL DEFAULT 0,
    last_error         TEXT,
    UNIQUE (provider, provider_event_id)
);

-- Partial index: the queue of work is a tiny fraction of the table.
CREATE INDEX provider_events_pending_idx ON provider_events (received_at)
    WHERE processed_at IS NULL;
CREATE INDEX provider_events_user_idx ON provider_events (user_id, received_at DESC);

CREATE TRIGGER provider_events_no_delete BEFORE DELETE ON provider_events
    FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();


-- -----------------------------------------------------------------------------
-- activities — one completed session, provider-normalised
-- -----------------------------------------------------------------------------
CREATE TABLE activities (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    sport               TEXT NOT NULL CHECK (sport IN
                            ('run', 'bike', 'swim', 'strength', 'other')),
    sub_sport           TEXT,
    -- UTC instant, plus the athlete's local calendar day. A 23:30 run belongs to
    -- that day for the athlete even when it is already tomorrow in UTC, and
    -- every daily metric keys on local_date.
    start_time          TIMESTAMPTZ NOT NULL,
    local_date          DATE NOT NULL,
    duration_s          INTEGER NOT NULL CHECK (duration_s > 0 AND duration_s < 604800),
    moving_time_s       INTEGER CHECK (moving_time_s > 0),
    distance_m          NUMERIC(10,1) CHECK (distance_m >= 0),
    avg_hr              NUMERIC(5,1) CHECK (avg_hr BETWEEN 20 AND 250),
    max_hr              NUMERIC(5,1) CHECK (max_hr BETWEEN 20 AND 250),
    avg_power           NUMERIC(7,1) CHECK (avg_power >= 0),
    normalized_power    NUMERIC(7,1) CHECK (normalized_power >= 0),
    max_power           NUMERIC(7,1) CHECK (max_power >= 0),
    avg_cadence         NUMERIC(6,1) CHECK (avg_cadence >= 0),
    elevation_gain_m    NUMERIC(8,1),
    calories            NUMERIC(8,1),
    total_strokes       INTEGER CHECK (total_strokes >= 0),
    pool_length_m       NUMERIC(5,1) CHECK (pool_length_m > 0),
    -- Athlete-reported perceived effort. The last-resort load source, and a
    -- valuable independent signal even when we have power.
    rpe                 NUMERIC(3,1) CHECK (rpe BETWEEN 1 AND 10),
    notes               TEXT,
    -- Half splits for aerobic decoupling, stored rather than recomputed from
    -- streams so decoupling survives stream archival.
    first_half          JSONB,
    second_half         JSONB,

    provider            TEXT NOT NULL DEFAULT 'manual',
    -- Kept on the row for traceability; global uniqueness is enforced by
    -- provider_activity_map so this table stays partitionable later.
    provider_activity_id TEXT,
    -- Anti-spoofing: set by the data-quality checks, read by the rewards module
    -- before any points are credited for this session.
    trust_score         NUMERIC(4,3) CHECK (trust_score BETWEEN 0 AND 1),
    is_manual           BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ,
    CHECK (max_hr IS NULL OR avg_hr IS NULL OR max_hr >= avg_hr),
    CHECK (moving_time_s IS NULL OR moving_time_s <= duration_s)
);

CREATE INDEX activities_user_time_idx  ON activities (user_id, start_time DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX activities_user_date_idx  ON activities (user_id, local_date)
    WHERE deleted_at IS NULL;
CREATE INDEX activities_user_sport_idx ON activities (user_id, sport, start_time DESC)
    WHERE deleted_at IS NULL;

CREATE TRIGGER activities_touch BEFORE UPDATE ON activities
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON COLUMN activities.trust_score IS
    'Anti-spoofing confidence, 0..1, from the data-quality checks. The rewards '
    'module refuses to credit points below the configured threshold.';


-- -----------------------------------------------------------------------------
-- provider_activity_map — ingest idempotency, deliberately separate
-- -----------------------------------------------------------------------------
-- A partitioned table's UNIQUE constraints must include the partition key, so a
-- UNIQUE (provider, provider_activity_id) on `activities` would permanently
-- block partitioning it. Holding the constraint here keeps both properties:
-- exactly-once ingest, and a partitionable activities table.
CREATE TABLE provider_activity_map (
    provider             TEXT NOT NULL,
    provider_activity_id TEXT NOT NULL,
    activity_id          UUID NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
    user_id              UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, provider_activity_id)
);

CREATE INDEX provider_activity_map_activity_idx ON provider_activity_map (activity_id);


-- -----------------------------------------------------------------------------
-- activity_laps
-- -----------------------------------------------------------------------------
CREATE TABLE activity_laps (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    activity_id   UUID NOT NULL REFERENCES activities (id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    lap_index     SMALLINT NOT NULL CHECK (lap_index >= 0),
    duration_s    INTEGER NOT NULL CHECK (duration_s > 0),
    distance_m    NUMERIC(10,1),
    avg_hr        NUMERIC(5,1),
    avg_power     NUMERIC(7,1),
    avg_cadence   NUMERIC(6,1),
    strokes       INTEGER,
    UNIQUE (activity_id, lap_index)
);

CREATE INDEX activity_laps_user_idx ON activity_laps (user_id);


-- -----------------------------------------------------------------------------
-- activity_streams — POINTER ONLY, samples live in object storage
-- -----------------------------------------------------------------------------
-- Per-second streams are the single largest data volume in the product. Keeping
-- them in Postgres is the difference between a ~50 GB and a multi-TB database at
-- 100k athletes, for data that is read once during analysis and rarely again.
CREATE TABLE activity_streams (
    activity_id     UUID PRIMARY KEY REFERENCES activities (id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    storage_bucket  TEXT NOT NULL,
    storage_key     TEXT NOT NULL,
    format          TEXT NOT NULL DEFAULT 'parquet'
                        CHECK (format IN ('parquet', 'fit', 'json_gz')),
    -- Which channels the file actually contains, so a consumer knows whether a
    -- power analysis is possible without downloading it.
    channels        TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    sample_count    INTEGER CHECK (sample_count >= 0),
    sample_interval_s NUMERIC(5,2),
    size_bytes      BIGINT CHECK (size_bytes >= 0),
    checksum_sha256 BYTEA,
    archived_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE activity_streams IS
    'Metadata and object-storage pointer for sample streams. archived_at marks '
    'a move to cold storage after the hot retention window.';


-- -----------------------------------------------------------------------------
-- daily_wellness — one row per athlete per local day
-- -----------------------------------------------------------------------------
-- Maps onto backend.algorithms.types.DailyWellness.
CREATE TABLE daily_wellness (
    user_id              UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    day                  DATE NOT NULL,
    hrv_rmssd_ms         NUMERIC(6,2) CHECK (hrv_rmssd_ms > 0 AND hrv_rmssd_ms < 400),
    resting_hr           NUMERIC(5,1) CHECK (resting_hr BETWEEN 25 AND 140),
    sleep_total_min      NUMERIC(6,1) CHECK (sleep_total_min BETWEEN 0 AND 1200),
    sleep_deep_min       NUMERIC(6,1) CHECK (sleep_deep_min >= 0),
    sleep_rem_min        NUMERIC(6,1) CHECK (sleep_rem_min >= 0),
    sleep_light_min      NUMERIC(6,1) CHECK (sleep_light_min >= 0),
    sleep_awake_min      NUMERIC(6,1) CHECK (sleep_awake_min >= 0),
    sleep_efficiency_pct NUMERIC(5,2) CHECK (sleep_efficiency_pct BETWEEN 0 AND 100),
    body_weight_kg       NUMERIC(5,2) CHECK (body_weight_kg BETWEEN 25 AND 350),
    spo2_pct             NUMERIC(5,2) CHECK (spo2_pct BETWEEN 50 AND 100),
    respiration_rate     NUMERIC(5,2),
    -- Subjective wellness, Hooper-style: 1 = best, 5 = worst.
    soreness             SMALLINT CHECK (soreness BETWEEN 1 AND 5),
    mood                 SMALLINT CHECK (mood BETWEEN 1 AND 5),
    stress               SMALLINT CHECK (stress BETWEEN 1 AND 5),
    fatigue              SMALLINT CHECK (fatigue BETWEEN 1 AND 5),
    source               TEXT NOT NULL DEFAULT 'garmin',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Composite PK doubles as the "latest 28 days for this athlete" index.
    PRIMARY KEY (user_id, day),
    CHECK (sleep_deep_min IS NULL OR sleep_total_min IS NULL
           OR sleep_deep_min <= sleep_total_min)
);

CREATE TRIGGER daily_wellness_touch BEFORE UPDATE ON daily_wellness
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Late-arriving FK: personal_bests references activities, created after it.
ALTER TABLE personal_bests
    ADD CONSTRAINT personal_bests_activity_fk
    FOREIGN KEY (activity_id) REFERENCES activities (id) ON DELETE SET NULL;


-- =============================================================================
-- ROLLBACK (review before running — destroys all training history)
-- =============================================================================
-- ALTER TABLE training.personal_bests DROP CONSTRAINT IF EXISTS personal_bests_activity_fk;
-- DROP TABLE IF EXISTS training.daily_wellness, training.activity_streams,
--     training.activity_laps, training.provider_activity_map, training.activities,
--     training.provider_events, training.provider_connections,
--     training.personal_bests, training.athlete_goals,
--     training.athlete_profiles CASCADE;
-- =============================================================================
