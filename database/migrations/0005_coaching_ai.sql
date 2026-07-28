-- =============================================================================
-- 0005 — Digital twin, AI conversations, training plans
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0004.
-- =============================================================================

SET search_path = coaching, public;


-- -----------------------------------------------------------------------------
-- athlete_twin_snapshots — the Athlete Digital Twin, versioned over time
-- -----------------------------------------------------------------------------
-- A rolling model of how this athlete's body behaves. Snapshotted rather than
-- overwritten: showing "your aerobic capacity trend" and answering "did the
-- twin get better at predicting you?" both need history, and an overwritten
-- row can do neither.
CREATE TABLE athlete_twin_snapshots (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    -- Typed columns for the fields we filter and chart on; JSONB for the rest,
    -- so adding a twin attribute is not a migration.
    fitness_ctl          NUMERIC(8,2),
    aerobic_capacity     NUMERIC(6,2),
    -- Days to return to baseline readiness after a threshold-hour session, the
    -- single most useful personalised parameter we can learn.
    recovery_half_life_d NUMERIC(5,2),
    load_tolerance       NUMERIC(6,3),
    -- Fitted Riegel exponent, per athlete, for race-time prediction.
    fatigue_exponent     NUMERIC(5,4),
    improvement_rate     NUMERIC(10,6),
    strengths            TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    weaknesses           TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    attributes           JSONB NOT NULL DEFAULT '{}'::JSONB,
    -- How much of the twin is learned from data versus defaulted from population
    -- priors. Presented to the athlete, and used by the AI layer to hedge.
    confidence           NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    observation_days     INTEGER CHECK (observation_days >= 0),
    twin_version         TEXT NOT NULL,
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX athlete_twin_user_idx ON athlete_twin_snapshots (user_id, computed_at DESC);

COMMENT ON TABLE athlete_twin_snapshots IS
    'Per-athlete learned model. The latest row is the current twin; history '
    'supports trend display and twin-accuracy evaluation.';


-- -----------------------------------------------------------------------------
-- ai_conversations / ai_messages
-- -----------------------------------------------------------------------------
CREATE TABLE ai_conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    title       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);

CREATE INDEX ai_conversations_user_idx ON ai_conversations (user_id, updated_at DESC)
    WHERE archived_at IS NULL;
CREATE TRIGGER ai_conversations_touch BEFORE UPDATE ON ai_conversations
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Every turn, with full cost and provenance. This table is what makes AI cost
-- control, quota enforcement, evaluation and incident forensics possible; a
-- chat log without token and model columns supports none of them.
CREATE TABLE ai_messages (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID NOT NULL REFERENCES ai_conversations (id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content           TEXT NOT NULL,

    -- Provenance. provider is explicit so a multi-provider future needs no
    -- migration, and so a bad answer can be traced to the exact model.
    provider          TEXT CHECK (provider IN ('anthropic', 'openai', 'google', 'local')),
    model_id          TEXT,
    prompt_version    TEXT,
    -- Hash of the athlete context packet sent with this turn. Lets us reproduce
    -- exactly what the model saw without storing a second copy of the data.
    context_hash      TEXT,
    -- Which route handled it: a deterministic answer from the analytics engine
    -- costs nothing and must be distinguishable from a model call.
    route             TEXT CHECK (route IN ('deterministic', 'llm_chat', 'llm_deep_review', 'tool_loop')),

    input_tokens      INTEGER CHECK (input_tokens >= 0),
    output_tokens     INTEGER CHECK (output_tokens >= 0),
    cache_read_tokens INTEGER CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER CHECK (cache_write_tokens >= 0),
    -- Micro-USD (1e-6) so cost is an exact integer, never a float.
    cost_micro_usd    BIGINT CHECK (cost_micro_usd >= 0),
    latency_ms        INTEGER CHECK (latency_ms >= 0),
    finish_reason     TEXT,
    -- Set when a provider safety classifier declined, or when our own guardrail
    -- intervened (medical red flag, out of scope, prompt injection detected).
    safety_flags      TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    -- Numeric grounding check: did every number in the answer appear in the
    -- context packet? Failing this is a hallucination signal.
    grounded          BOOLEAN,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ai_messages_conversation_idx ON ai_messages (conversation_id, created_at);
-- Monthly cost and quota accounting per athlete.
CREATE INDEX ai_messages_user_cost_idx ON ai_messages (user_id, created_at DESC);
-- Safety review queue.
CREATE INDEX ai_messages_flagged_idx ON ai_messages (created_at DESC)
    WHERE safety_flags <> ARRAY[]::TEXT[];

CREATE TABLE ai_message_feedback (
    message_id  UUID PRIMARY KEY REFERENCES ai_messages (id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    rating      SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    reason_code TEXT CHECK (reason_code IN
                    ('wrong', 'unhelpful', 'too_generic', 'unsafe', 'great', 'other')),
    comment     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE ai_message_feedback IS
    'Primary signal for the Continuous Improvement System. Joined against '
    'prompt_version and model_id to detect a regression after a change.';


-- -----------------------------------------------------------------------------
-- ai_usage_counters — quota enforcement without scanning ai_messages
-- -----------------------------------------------------------------------------
CREATE TABLE ai_usage_counters (
    user_id        UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    -- Calendar month in the athlete's own timezone, as 'YYYY-MM'.
    period         TEXT NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    input_tokens   BIGINT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens  BIGINT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_micro_usd BIGINT NOT NULL DEFAULT 0 CHECK (cost_micro_usd >= 0),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, period)
);

COMMENT ON TABLE ai_usage_counters IS
    'Authoritative quota counter. Redis holds a hot copy for the fast path but '
    'may be flushed, so the limit is reconciled against this table.';


-- -----------------------------------------------------------------------------
-- training_plans / plan_sessions / plan_adaptations
-- -----------------------------------------------------------------------------
CREATE TABLE training_plans (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    goal_id           UUID REFERENCES training.athlete_goals (id) ON DELETE SET NULL,
    goal_type         TEXT NOT NULL,
    primary_sport     TEXT NOT NULL,
    secondary_sports  TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    start_date        DATE NOT NULL,
    weeks             SMALLINT NOT NULL CHECK (weeks BETWEEN 1 AND 104),
    sessions_per_week SMALLINT NOT NULL CHECK (sessions_per_week BETWEEN 1 AND 14),
    race_date         DATE,
    -- Inputs and generator version, so any plan is exactly reproducible and a
    -- generator change can be attributed.
    generator_version TEXT NOT NULL,
    request_params    JSONB NOT NULL DEFAULT '{}'::JSONB,
    ramp_cap_pct      NUMERIC(5,2),
    status            TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'completed', 'abandoned', 'superseded')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One active plan per athlete: two competing plans would make the daily
-- adaptation ambiguous.
CREATE UNIQUE INDEX training_plans_one_active_idx ON training_plans (user_id)
    WHERE status = 'active';
CREATE TRIGGER training_plans_touch BEFORE UPDATE ON training_plans
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE plan_sessions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id        UUID NOT NULL REFERENCES training_plans (id) ON DELETE CASCADE,
    user_id        UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    week_index     SMALLINT NOT NULL CHECK (week_index >= 0),
    phase          TEXT NOT NULL CHECK (phase IN
                       ('base', 'build', 'peak', 'taper', 'race', 'recovery')),
    scheduled_date DATE NOT NULL,
    sport          TEXT NOT NULL,
    title          TEXT NOT NULL,
    intensity      TEXT NOT NULL CHECK (intensity IN
                       ('recovery', 'easy', 'tempo', 'threshold', 'vo2max')),
    target_load    NUMERIC(8,2) NOT NULL CHECK (target_load >= 0),
    duration_min   SMALLINT NOT NULL CHECK (duration_min > 0),
    is_key_session BOOLEAN NOT NULL DEFAULT false,
    notes          TEXT,
    status         TEXT NOT NULL DEFAULT 'planned'
                       CHECK (status IN ('planned', 'adapted', 'completed', 'skipped', 'moved')),
    -- The activity that fulfilled this session, for compliance measurement.
    completed_activity_id UUID REFERENCES training.activities (id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX plan_sessions_user_date_idx ON plan_sessions (user_id, scheduled_date);
CREATE INDEX plan_sessions_plan_idx ON plan_sessions (plan_id, week_index);
CREATE TRIGGER plan_sessions_touch BEFORE UPDATE ON plan_sessions
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Every daily gating decision. Explainable AI needs the inputs kept alongside
-- the output, and evaluating "does adaptation improve outcomes?" needs the
-- counterfactual (what was planned before we changed it).
CREATE TABLE plan_adaptations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_session_id   UUID REFERENCES plan_sessions (id) ON DELETE CASCADE,
    user_id           UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    decision_date     DATE NOT NULL,
    action            TEXT NOT NULL CHECK (action IN
                          ('as_planned', 'reduce_intensity', 'reduce_volume',
                           'easy_only', 'rest')),
    -- Inputs at decision time.
    readiness_score   NUMERIC(5,2),
    readiness_band    TEXT,
    readiness_quality NUMERIC(4,3),
    injury_risk_band  TEXT,
    -- The exact text shown to the athlete, so the explanation is auditable.
    reason            TEXT NOT NULL,
    before_snapshot   JSONB,
    after_snapshot    JSONB,
    engine_version    TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX plan_adaptations_user_idx ON plan_adaptations (user_id, decision_date DESC);


-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- DROP TABLE IF EXISTS coaching.plan_adaptations, coaching.plan_sessions,
--     coaching.training_plans, coaching.ai_usage_counters,
--     coaching.ai_message_feedback, coaching.ai_messages,
--     coaching.ai_conversations, coaching.athlete_twin_snapshots CASCADE;
-- =============================================================================
