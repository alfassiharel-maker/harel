-- =============================================================================
-- 0014 — Training ingest activation: sync state, backfill chunks, stream integrity
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0011.
-- Reversible: the additions are (DROP recovers the schema). Data written into the
--   new columns is not recoverable after a DROP, so a rollback loses sync progress
--   and trust-score provenance — an inconvenience, not a data-loss incident.
-- Lock impact: online-safe. Every ADD COLUMN is nullable with no default, which is
--   catalog-only in PostgreSQL 16 (no table rewrite). The new indexes are created
--   CONCURRENTLY and are therefore in a SEPARATE FILE — see the note below.
--
-- ⚠ THIS FILE MUST NOT BE RUN WITH --single-transaction.
--   It contains only transaction-safe statements, so it can be. But its companion
--   file 0014b (indexes, CONCURRENTLY) cannot. Apply 0014 first, then 0014b.
--
-- WHY THIS MIGRATION EXISTS
--
-- 0003 defined the ingest tables before there was an ingest pipeline. Building the
-- provider adapter layer surfaced three things the schema cannot currently express.
-- Each is a gap between what a column implies and what the pipeline actually needs.
--
-- GAP 1 — `trust_score` exists, but nothing records how it was computed.
--
--   `activities.trust_score` is there. `backend/integrations/quality.py` computes
--   it and carries a `MODEL_VERSION` ("quality-v1"), because the thresholds will be
--   re-tuned once real data arrives.
--
--   Without recording the version, re-tuning silently rewrites the meaning of every
--   historical score: an activity scored 0.65 under v1 and 0.45 under v2 is
--   indistinguishable from an activity whose data changed. `docs/17` §2.5 requires
--   the gate that applied to a held reward to be *snapshotted*, and that starts
--   here — a reward held in March must still be explainable in September.
--
-- GAP 2 — `backfilled_from` records a date, not progress.
--
--   `provider_connections.backfilled_from` says how far back we have reached. It
--   cannot express a partially-completed import: which 90-day chunks succeeded,
--   which failed, and how many times. A two-year Garmin backfill is ~8 chunks per
--   data type; without per-chunk state a single failure either restarts the whole
--   import or is silently skipped, and both are wrong.
--
--   Nor can it express incremental sync position. Garmin filters by *upload* time,
--   not activity time, so "sync everything since X" needs a persisted high-water
--   mark on upload time. Keeping it only in Redis would violate ADR-002 — Redis is
--   never the system of record, and a cache flush must not silently re-import or
--   skip a window.
--
-- GAP 3 — `checksum_sha256` is stored but never verified.
--
--   Streams live in object storage with only a pointer and a checksum in Postgres.
--   Nothing records that the checksum was ever *checked*, so a truncated upload
--   surfaces as an inexplicable analytics result months later rather than as a
--   failed verification at read time. `retention_class` is also needed before the
--   cold-storage sweep in 0021 can know what it may archive.
-- =============================================================================

SET search_path = training, public;


-- -----------------------------------------------------------------------------
-- provider_sync_state — incremental sync position, per athlete per provider
-- -----------------------------------------------------------------------------
CREATE TABLE provider_sync_state (
    user_id              UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    provider             TEXT NOT NULL,
    -- Opaque provider cursor where one is offered. Polar issues these; Garmin does
    -- not, hence nullable rather than required.
    cursor               TEXT,
    -- High-water mark on the provider's UPLOAD time, not activity time. Garmin
    -- filters by upload, so an activity synced today for a ride three days ago
    -- appears in today's window. Tracking activity time here would silently miss
    -- every late sync, which is the most common cause of "my ride never appeared".
    upload_high_water    TIMESTAMPTZ,
    last_success_at      TIMESTAMPTZ,
    last_attempt_at      TIMESTAMPTZ,
    -- Drives exponential backoff and the "reconnect this provider" prompt. Reset to
    -- zero on success, so a transient outage does not accumulate toward a disconnect.
    consecutive_failures SMALLINT NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_error_code      TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider)
);

CREATE TRIGGER provider_sync_state_touch BEFORE UPDATE ON provider_sync_state
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE provider_sync_state ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_all ON provider_sync_state FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());

COMMENT ON COLUMN provider_sync_state.upload_high_water IS
    'High-water mark on provider UPLOAD time. Must not be set from activity start '
    'time: a late-synced activity would then be skipped forever.';


-- -----------------------------------------------------------------------------
-- provider_backfill_jobs — per-chunk progress for a history import
-- -----------------------------------------------------------------------------
CREATE TABLE provider_backfill_jobs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    data_kind     TEXT NOT NULL CHECK (data_kind IN
                      ('activity_summary', 'activity_details', 'dailies', 'sleep', 'hrv')),
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    -- Deterministic key derived from (provider, user, kind, window), matching
    -- FetchTask.dedupe_key in the adapter layer. Idempotency for the enqueue path:
    -- re-requesting a backfill must not duplicate in-flight work, and arq jobs do
    -- run twice.
    chunk_key     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'abandoned')),
    attempts      SMALLINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error    TEXT,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_end > window_start),
    -- One row per logical chunk. UNIQUE here rather than on (user, provider, kind,
    -- window) because the key already encodes all four and comparing one text
    -- column is what the enqueue path actually does.
    UNIQUE (chunk_key)
);

CREATE INDEX provider_backfill_queue_idx ON provider_backfill_jobs (created_at)
    WHERE status IN ('pending', 'running');

CREATE TRIGGER provider_backfill_jobs_touch BEFORE UPDATE ON provider_backfill_jobs
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

ALTER TABLE provider_backfill_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_all ON provider_backfill_jobs FOR ALL
    USING (user_id = app.current_user_id())
    WITH CHECK (user_id = app.current_user_id());

COMMENT ON TABLE provider_backfill_jobs IS
    'Per-chunk state for a provider history import. Chunking bounds the blast '
    'radius of one failed request during a two-year backfill: a failure retries '
    '90 days, not the whole history.';


-- -----------------------------------------------------------------------------
-- activities — trust-score provenance and event lineage
-- -----------------------------------------------------------------------------
-- All nullable with no default: catalog-only in PG16, no table rewrite.

-- Which quality model produced trust_score. Without it, re-tuning thresholds
-- silently rewrites the meaning of every historical score.
ALTER TABLE activities ADD COLUMN trust_score_version TEXT;
ALTER TABLE activities ADD COLUMN trust_scored_at TIMESTAMPTZ;

-- The raw provider event this activity was derived from. Makes a parser bug
-- traceable from the wrong number back to the exact bytes that produced it, which
-- is the whole reason provider_events is persisted before parsing.
--
-- NOTE: this FK is a partitioning consideration. `provider_events` is
-- range-partitioned in 0021; an outbound FK *to* a partitioned table is legal in
-- PG16, but it constrains the partition-detach path (a detach fails while
-- referencing rows exist). 0021 must therefore drop this constraint before
-- detaching a partition, or model lineage as a plain UUID with no FK. Flagged for
-- the 0021 review rather than pre-emptively weakened here, because referential
-- integrity is worth having until it demonstrably blocks something.
ALTER TABLE activities ADD COLUMN source_event_id UUID
    REFERENCES provider_events (id) ON DELETE SET NULL;

-- Data-quality score, alongside trust. Separate columns because they answer
-- different questions and diverge: a dropped HR strap is low quality and fully
-- trusted (see backend/integrations/quality.py).
ALTER TABLE activities ADD COLUMN data_quality NUMERIC(4,3)
    CHECK (data_quality IS NULL OR data_quality BETWEEN 0 AND 1);

COMMENT ON COLUMN activities.trust_score_version IS
    'Quality-model version that produced trust_score. Required so a held reward '
    'stays explainable after the thresholds are re-tuned.';
COMMENT ON COLUMN activities.data_quality IS
    'Whether this measurement is good enough to compute on. Distinct from '
    'trust_score, which is whether the activity really happened.';


-- -----------------------------------------------------------------------------
-- activity_streams — integrity verification and retention class
-- -----------------------------------------------------------------------------
ALTER TABLE activity_streams ADD COLUMN checksum_verified_at TIMESTAMPTZ;

-- Drives the cold-storage sweep in 0021. 'hot' stays in the primary bucket;
-- 'cold' is archivable; 'pinned' is never archived (a race file the athlete
-- revisits, or one under a data-subject access request).
ALTER TABLE activity_streams ADD COLUMN retention_class TEXT NOT NULL DEFAULT 'hot'
    CHECK (retention_class IN ('hot', 'cold', 'pinned'));

ALTER TABLE activity_streams ADD COLUMN cold_storage_key TEXT;

COMMENT ON COLUMN activity_streams.checksum_verified_at IS
    'When checksum_sha256 was last confirmed against the stored object. NULL means '
    'never verified — a truncated upload is undetected until this runs.';


-- -----------------------------------------------------------------------------
-- provider_connections — scope drift and machine-readable errors
-- -----------------------------------------------------------------------------
-- `scopes` records what was granted at connect time. An athlete can narrow
-- permissions in Garmin's own settings afterwards without telling us, and the
-- product must degrade honestly rather than keep requesting data it may no longer
-- read. This records when we last confirmed the grant.
ALTER TABLE provider_connections ADD COLUMN granted_scopes_checked_at TIMESTAMPTZ;

-- `last_error` is a provider string, unsuitable for branching or for the UI.
-- A stable code lets the app say "reconnect Garmin" versus "Garmin is down".
ALTER TABLE provider_connections ADD COLUMN last_error_code TEXT;

COMMENT ON COLUMN provider_connections.granted_scopes_checked_at IS
    'When the granted scope set was last verified against the provider. Scopes can '
    'be narrowed provider-side without notifying us.';


-- -----------------------------------------------------------------------------
-- Verification — run these after applying
-- -----------------------------------------------------------------------------
-- 1. New tables must be RLS-protected and must NOT need an exemption row.
--    Both queries expect zero rows.
--
-- SELECT tablename FROM pg_tables
--  WHERE schemaname = 'training'
--    AND tablename IN ('provider_sync_state','provider_backfill_jobs')
--    AND NOT rowsecurity;
--
-- SELECT table_name FROM app.rls_exemptions
--  WHERE schema_name = 'training'
--    AND table_name IN ('provider_sync_state','provider_backfill_jobs');
--
-- 2. Confirm no table rewrite happened — relfilenode is unchanged by a
--    catalog-only ADD COLUMN. Capture before and compare after:
--
-- SELECT relname, relfilenode FROM pg_class
--  WHERE relname IN ('activities','activity_streams','provider_connections');
-- =============================================================================
