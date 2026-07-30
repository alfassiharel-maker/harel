-- =============================================================================
-- 0014b — Training ingest indexes (CONCURRENTLY)
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0014.
-- Reversible: yes (DROP INDEX CONCURRENTLY).
--
-- ⚠ DO NOT RUN THIS FILE WITH --single-transaction.
--
--   `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block. Wrapping it
--   fails with 25001. That is the entire reason these two statements are split out
--   of 0014 rather than sitting alongside the ALTERs.
--
--   Apply as:
--       psql "$DATABASE_URL" --set ON_ERROR_STOP=on \
--            -f database/migrations/0014b_training_ingest_indexes.sql
--
--   Note the trade-off this buys: CONCURRENTLY takes no long write lock, so ingest
--   keeps running during the build. The cost is that a failure leaves an INVALID
--   index behind rather than rolling back. Check for one before retrying:
--
--       SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
--
--   and DROP INDEX CONCURRENTLY it first. A silently invalid index is worse than a
--   missing one: the planner ignores it while it still costs write throughput.
--
-- WHY THESE TWO AND NOTHING ELSE
--
--   The project rule is that no index is created for a query we do not yet make
--   (`docs/02` §3). Each index costs write throughput on `activities`, which is the
--   hottest table in the system. These two serve queries the ingest worker makes on
--   every run, so they are justified now; anything speculative waits for a
--   production EXPLAIN and arrives in 0031.
-- =============================================================================

-- Query: the trust-scoring sweep. After ingest writes an activity, a worker scores
-- it. Finding unscored activities without this index means scanning the athlete's
-- whole history on every pass.
--
-- Partial on `trust_score IS NULL` so the index holds only the work queue, not the
-- entire table — it stays small permanently because rows leave it once scored.
CREATE INDEX CONCURRENTLY IF NOT EXISTS activities_unscored_idx
    ON training.activities (user_id, start_time DESC)
    WHERE trust_score IS NULL AND deleted_at IS NULL;

-- Query: the stream-integrity sweep. Finds objects whose checksum has never been
-- verified. Also partial, and also self-draining as verification proceeds.
CREATE INDEX CONCURRENTLY IF NOT EXISTS activity_streams_unverified_idx
    ON training.activity_streams (created_at)
    WHERE checksum_verified_at IS NULL;


-- -----------------------------------------------------------------------------
-- Verification — run after applying
-- -----------------------------------------------------------------------------
-- Both indexes must exist AND be valid. Two rows expected, both indisvalid = true.
--
-- SELECT i.indexrelid::regclass AS index_name, i.indisvalid
--   FROM pg_index i
--  WHERE i.indexrelid::regclass::text IN
--        ('activities_unscored_idx', 'activity_streams_unverified_idx');
-- =============================================================================
