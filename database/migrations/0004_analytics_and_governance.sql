-- =============================================================================
-- 0004 — Materialised metrics, data quality, algorithm governance
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0003.
-- =============================================================================

SET search_path = analytics, public;


-- -----------------------------------------------------------------------------
-- daily_metrics — the table the dashboard reads
-- -----------------------------------------------------------------------------
-- Written by the analytics worker, never computed in a request. A readiness
-- score needs a 28-day HRV baseline and a CTL needs a 42-day load history;
-- doing that per page load would put a 90-row walk on the critical path of
-- every dashboard open, for a value that changes once a day.
CREATE TABLE daily_metrics (
    user_id            UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    day                DATE NOT NULL,

    -- Load
    training_load      NUMERIC(8,2) NOT NULL DEFAULT 0 CHECK (training_load >= 0),
    -- Which input produced the load, and how much to trust it.
    load_source        TEXT CHECK (load_source IN
                           ('power', 'heart_rate', 'pace', 'rpe', 'none')),
    load_confidence    NUMERIC(4,3) CHECK (load_confidence BETWEEN 0 AND 1),
    ctl                NUMERIC(8,2) CHECK (ctl >= 0),
    atl                NUMERIC(8,2) CHECK (atl >= 0),
    tsb                NUMERIC(8,2),
    acwr               NUMERIC(6,3) CHECK (acwr >= 0),
    acwr_reliable      BOOLEAN,
    monotony           NUMERIC(6,3),
    strain             NUMERIC(10,2),
    ramp_pct           NUMERIC(7,2),

    -- Readiness
    readiness_score    NUMERIC(5,2) CHECK (readiness_score BETWEEN 0 AND 100),
    readiness_band     TEXT CHECK (readiness_band IN
                           ('compromised', 'limited', 'moderate', 'good', 'prime')),
    -- Ordered driver array from ReadinessResult. Stored, not recomputed, because
    -- the AI layer and the UI both need the same explanation the athlete saw.
    readiness_drivers  JSONB,
    -- Fraction of the readiness model that had data. Below 0.35 the API refuses
    -- to render a recommendation from this row.
    readiness_quality  NUMERIC(4,3) CHECK (readiness_quality BETWEEN 0 AND 1),

    -- Injury risk. Never presented without model_version and the unvalidated flag.
    injury_risk        NUMERIC(6,4) CHECK (injury_risk BETWEEN 0 AND 1),
    injury_risk_band   TEXT CHECK (injury_risk_band IN ('low', 'moderate', 'high', 'very_high')),
    injury_risk_drivers JSONB,
    injury_model_version TEXT,

    -- Efficiency snapshot for the day (per-sport values keyed by metric name).
    efficiency         JSONB,

    -- Provenance: which engine version produced this row, so a recompute after
    -- an algorithm change is targetable and an old row is never mistaken for new.
    engine_version     TEXT NOT NULL,
    computed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day)
);

-- Recompute sweeps: "every row older than the current engine version".
CREATE INDEX daily_metrics_engine_idx ON daily_metrics (engine_version, computed_at);

COMMENT ON TABLE daily_metrics IS
    'Materialised per-athlete-per-day analytics. Recomputed by the worker when '
    'an activity or wellness day lands, or when the engine version changes.';


-- -----------------------------------------------------------------------------
-- data_quality_flags — the Data Quality System
-- -----------------------------------------------------------------------------
-- Before a value influences an algorithm it is checked. What was rejected, and
-- why, is recorded: an athlete asking "why does the app think I did not train
-- yesterday?" deserves a real answer, and a silent rejection is unauditable.
CREATE TABLE data_quality_flags (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    -- What was checked.
    subject_type  TEXT NOT NULL CHECK (subject_type IN
                      ('activity', 'daily_wellness', 'profile', 'stream')),
    subject_id    TEXT NOT NULL,
    day           DATE,
    check_code    TEXT NOT NULL CHECK (check_code IN
                      ('missing_required_field', 'out_of_physiological_range',
                       'sensor_dropout', 'duplicate_record', 'impossible_progression',
                       'timestamp_inconsistent', 'gps_distance_mismatch',
                       'hr_flatline', 'power_spike', 'suspected_spoofing')),
    severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'reject')),
    -- What we did about it. 'excluded' means the value did not reach the engine.
    resolution    TEXT NOT NULL CHECK (resolution IN
                      ('accepted', 'accepted_downweighted', 'excluded', 'corrected', 'pending_review')),
    field_name    TEXT,
    observed_value TEXT,
    expected_range TEXT,
    detail        JSONB NOT NULL DEFAULT '{}'::JSONB,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX data_quality_flags_user_idx ON data_quality_flags (user_id, detected_at DESC);
CREATE INDEX data_quality_flags_subject_idx ON data_quality_flags (subject_type, subject_id);
-- Operator queue.
CREATE INDEX data_quality_flags_review_idx ON data_quality_flags (detected_at)
    WHERE resolution = 'pending_review';


-- -----------------------------------------------------------------------------
-- algorithm_versions — the version registry
-- -----------------------------------------------------------------------------
-- Requirement: keep algorithm versions, compare models, verify prediction
-- accuracy, keep change history, roll back. All four need the version to be a
-- first-class row, not a constant in a Python file.
CREATE TABLE algorithm_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL CHECK (name IN
                     ('training_load', 'readiness', 'efficiency', 'injury_risk',
                      'performance_prediction', 'plan_generator', 'digital_twin',
                      'data_quality')),
    version      TEXT NOT NULL,
    -- Every tunable constant, so a version is fully reproducible from this row.
    parameters   JSONB NOT NULL DEFAULT '{}'::JSONB,
    -- Only one version per algorithm may be active at a time.
    active_from  TIMESTAMPTZ,
    active_to    TIMESTAMPTZ,
    -- Offline evaluation result at promotion time (see eval_runs for detail).
    baseline_score NUMERIC(8,4),
    notes        TEXT,
    created_by   UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, version),
    CHECK (active_to IS NULL OR active_from IS NULL OR active_to > active_from)
);

-- Enforce "at most one active version per algorithm" in the database rather than
-- trusting deploy order.
CREATE UNIQUE INDEX algorithm_versions_one_active_idx
    ON algorithm_versions (name)
    WHERE active_from IS NOT NULL AND active_to IS NULL;


-- -----------------------------------------------------------------------------
-- prediction_records / prediction_outcomes — did the prediction come true?
-- -----------------------------------------------------------------------------
-- Predictions are written when made and scored when the horizon passes. Without
-- this pair, "our race predictor is accurate" is an opinion.
CREATE TABLE prediction_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    algorithm_name  TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    metric          TEXT NOT NULL,
    predicted_value NUMERIC(14,4) NOT NULL,
    interval_low    NUMERIC(14,4),
    interval_high   NUMERIC(14,4),
    unit            TEXT NOT NULL,
    confidence      NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    method          TEXT,
    -- The date the prediction is about.
    horizon_date    DATE NOT NULL,
    made_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX prediction_records_user_idx ON prediction_records (user_id, metric, made_at DESC);
CREATE INDEX prediction_records_scoring_idx ON prediction_records (horizon_date);

CREATE TRIGGER prediction_records_append_only BEFORE UPDATE OR DELETE
    ON prediction_records FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();

CREATE TABLE prediction_outcomes (
    prediction_id   UUID PRIMARY KEY REFERENCES prediction_records (id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    actual_value    NUMERIC(14,4) NOT NULL,
    absolute_error  NUMERIC(14,4) NOT NULL,
    percent_error   NUMERIC(10,4),
    within_interval BOOLEAN,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_activity_id UUID REFERENCES training.activities (id) ON DELETE SET NULL
);

COMMENT ON TABLE prediction_outcomes IS
    'Realised outcome for a prediction. within_interval aggregated over many '
    'rows gives calibration: a well-calibrated 95% interval should contain the '
    'actual value about 95% of the time.';


-- -----------------------------------------------------------------------------
-- experiment_assignments — A/B comparison of algorithm versions
-- -----------------------------------------------------------------------------
CREATE TABLE experiment_assignments (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    experiment   TEXT NOT NULL,
    variant      TEXT NOT NULL,
    -- Sticky: an athlete must not flip between a v1 and v2 readiness model day
    -- to day, or neither the athlete nor the experiment can be interpreted.
    assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ,
    UNIQUE (user_id, experiment)
);

CREATE INDEX experiment_assignments_experiment_idx
    ON experiment_assignments (experiment, variant);


-- -----------------------------------------------------------------------------
-- eval_runs — offline evaluation of an algorithm or prompt version
-- -----------------------------------------------------------------------------
CREATE TABLE eval_runs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    suite           TEXT NOT NULL,
    algorithm_name  TEXT,
    algorithm_version TEXT,
    prompt_version  TEXT,
    model_id        TEXT,
    -- Per-check scores; the suite decides pass/fail.
    scores          JSONB NOT NULL DEFAULT '{}'::JSONB,
    passed          BOOLEAN NOT NULL,
    case_count      INTEGER NOT NULL CHECK (case_count >= 0),
    failure_count   INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    git_sha         TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

CREATE INDEX eval_runs_suite_idx ON eval_runs (suite, started_at DESC);

COMMENT ON TABLE eval_runs IS
    'Gate for promoting an algorithm or prompt version. A version with no '
    'passing eval_run must not be set active in algorithm_versions.';


REVOKE UPDATE, DELETE ON analytics.prediction_records FROM app_rw;


-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- DROP TABLE IF EXISTS analytics.eval_runs, analytics.experiment_assignments,
--     analytics.prediction_outcomes, analytics.prediction_records,
--     analytics.algorithm_versions, analytics.data_quality_flags,
--     analytics.daily_metrics CASCADE;
-- =============================================================================
