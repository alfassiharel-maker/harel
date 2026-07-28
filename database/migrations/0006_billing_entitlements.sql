-- =============================================================================
-- 0006 — Subscriptions, payments, entitlements
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0005.
--
-- NO CARD DATA IS EVER STORED. Every provider flow is provider-hosted; we keep
-- provider transaction identifiers and status only. There is deliberately no
-- column anywhere in this file that could hold a PAN, CVV or password.
-- =============================================================================

SET search_path = billing, public;


-- -----------------------------------------------------------------------------
-- subscription_plans — catalogue
-- -----------------------------------------------------------------------------
CREATE TABLE subscription_plans (
    code           TEXT PRIMARY KEY CHECK (code = lower(code)),
    name           TEXT NOT NULL,
    description    TEXT,
    -- Minor units. 1550 ILS = 15.50 shekels. Never a float.
    price_minor    BIGINT NOT NULL CHECK (price_minor >= 0),
    currency       CHAR(3) NOT NULL DEFAULT 'ILS',
    interval       TEXT NOT NULL DEFAULT 'month'
                       CHECK (interval IN ('month', 'year', 'lifetime', 'free')),
    trial_days     SMALLINT NOT NULL DEFAULT 0 CHECK (trial_days >= 0),
    -- Feature codes this plan grants, resolved into billing.entitlements.
    features       TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    -- Tier limits, notably the AI message quota (see docs/10-cost-model-and-risks.md).
    limits         JSONB NOT NULL DEFAULT '{}'::JSONB,
    is_active      BOOLEAN NOT NULL DEFAULT true,
    sort_order     SMALLINT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER subscription_plans_touch BEFORE UPDATE ON subscription_plans
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();


-- -----------------------------------------------------------------------------
-- subscriptions — local mirror; the PROVIDER is the source of truth
-- -----------------------------------------------------------------------------
-- Access is never granted from a client-side claim. A receipt is validated
-- server-side against the provider, and a nightly reconciliation job re-checks
-- every active subscription: store subscriptions expire, refund and get revoked
-- without necessarily sending us a webhook we successfully processed.
CREATE TABLE subscriptions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    plan_code                TEXT NOT NULL REFERENCES subscription_plans (code),
    provider                 TEXT NOT NULL CHECK (provider IN
                                 ('apple_iap', 'google_play', 'paypal', 'stripe', 'manual')),
    provider_subscription_id TEXT,
    -- Apple's original_transaction_id / Google's purchase token: the stable
    -- identity of a subscription across renewals.
    provider_account_ref     TEXT,
    status                   TEXT NOT NULL CHECK (status IN
                                 ('trialing', 'active', 'past_due', 'grace',
                                  'paused', 'canceled', 'expired', 'refunded')),
    current_period_start     TIMESTAMPTZ,
    current_period_end       TIMESTAMPTZ,
    trial_end                TIMESTAMPTZ,
    -- Set when the athlete cancels but retains access to period end.
    cancel_at                TIMESTAMPTZ,
    canceled_at              TIMESTAMPTZ,
    -- Store billing-retry grace. Access continues; dunning messaging starts.
    grace_until              TIMESTAMPTZ,
    auto_renew               BOOLEAN NOT NULL DEFAULT true,
    last_verified_at         TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_subscription_id)
);

-- One live subscription per athlete. A second one means a double charge.
CREATE UNIQUE INDEX subscriptions_one_live_idx ON subscriptions (user_id)
    WHERE status IN ('trialing', 'active', 'past_due', 'grace');
-- Reconciliation and dunning sweeps.
CREATE INDEX subscriptions_renewal_idx ON subscriptions (current_period_end)
    WHERE status IN ('trialing', 'active', 'grace');
CREATE INDEX subscriptions_user_idx ON subscriptions (user_id, created_at DESC);

CREATE TRIGGER subscriptions_touch BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON COLUMN subscriptions.last_verified_at IS
    'When we last confirmed this against the provider. A stale value is a '
    'reconciliation failure and must alert, not silently keep granting access.';


-- -----------------------------------------------------------------------------
-- payments — settled transactions
-- -----------------------------------------------------------------------------
CREATE TABLE payments (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                UUID NOT NULL REFERENCES identity.users (id) ON DELETE SET NULL,
    subscription_id        UUID REFERENCES subscriptions (id) ON DELETE SET NULL,
    provider               TEXT NOT NULL,
    -- Idempotency: providers retry, and a duplicate row here would double-count
    -- revenue and could double-credit rewards.
    provider_transaction_id TEXT NOT NULL,
    kind                   TEXT NOT NULL DEFAULT 'subscription'
                               CHECK (kind IN ('subscription', 'one_time', 'refund', 'chargeback')),
    gross_minor            BIGINT NOT NULL,
    -- Store commission / processor fee, so net revenue is known rather than
    -- estimated. This is the number the AI cost budget is measured against.
    fee_minor              BIGINT NOT NULL DEFAULT 0,
    tax_minor              BIGINT NOT NULL DEFAULT 0,
    net_minor              BIGINT NOT NULL,
    currency               CHAR(3) NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN
                               ('pending', 'settled', 'failed', 'refunded', 'disputed')),
    occurred_at            TIMESTAMPTZ NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_transaction_id)
);

CREATE INDEX payments_user_idx ON payments (user_id, occurred_at DESC);
CREATE INDEX payments_settlement_idx ON payments (occurred_at DESC) WHERE status = 'settled';


-- -----------------------------------------------------------------------------
-- payment_webhook_events — raw provider webhooks, APPEND-ONLY
-- -----------------------------------------------------------------------------
-- Signature verification result is recorded per event. An unverified webhook is
-- stored but MUST NOT change entitlement state: accepting an unsigned
-- "subscription active" callback is a free-subscription vulnerability.
CREATE TABLE payment_webhook_events (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider           TEXT NOT NULL,
    provider_event_id  TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    payload            JSONB NOT NULL,
    signature_verified BOOLEAN NOT NULL DEFAULT false,
    user_id            UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,
    process_attempts   SMALLINT NOT NULL DEFAULT 0,
    last_error         TEXT,
    UNIQUE (provider, provider_event_id)
);

CREATE INDEX payment_webhook_pending_idx ON payment_webhook_events (received_at)
    WHERE processed_at IS NULL;
-- Security review queue: unverified signatures are an attack signal.
CREATE INDEX payment_webhook_unverified_idx ON payment_webhook_events (received_at DESC)
    WHERE signature_verified = false;

CREATE TRIGGER payment_webhook_no_delete BEFORE DELETE ON payment_webhook_events
    FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();


-- -----------------------------------------------------------------------------
-- entitlements — what this athlete may access RIGHT NOW
-- -----------------------------------------------------------------------------
-- Single read path for every gated feature, covering both the subscription tier
-- and individually purchased "digital sensors". Feature checks never join
-- through subscriptions: a paused subscription, an admin comp and a one-off
-- sensor unlock all resolve to rows here.
CREATE TABLE entitlements (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    feature_code   TEXT NOT NULL CHECK (feature_code IN
                       ('ai_coach_basic', 'ai_coach_advanced', 'analytics_basic',
                        'analytics_advanced', 'plan_adaptive', 'sensor_running',
                        'sensor_swimming', 'sensor_cycling', 'sensor_strength',
                        'sensor_recovery', 'sensor_injury', 'community', 'export_data')),
    source         TEXT NOT NULL CHECK (source IN
                       ('subscription', 'one_time_purchase', 'promotion',
                        'admin_grant', 'partner_perk', 'trial')),
    source_ref     TEXT,
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL means "until revoked". Every subscription-sourced row has an expiry
    -- set to the period end plus grace, so a failed reconciliation degrades to
    -- loss of access rather than to permanent free access.
    expires_at     TIMESTAMPTZ,
    revoked_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX entitlements_live_idx ON entitlements (user_id, feature_code)
    WHERE revoked_at IS NULL;
CREATE INDEX entitlements_expiry_idx ON entitlements (expires_at)
    WHERE revoked_at IS NULL AND expires_at IS NOT NULL;

COMMENT ON TABLE entitlements IS
    'Authoritative feature access. Cached in Redis for 5 minutes maximum so a '
    'cancellation or refund takes effect promptly.';


-- -----------------------------------------------------------------------------
-- Seed the catalogue. Data change: review the numbers before running.
-- -----------------------------------------------------------------------------
INSERT INTO subscription_plans
    (code, name, description, price_minor, currency, interval, trial_days, features, limits, sort_order)
VALUES
    ('free', 'Free', 'Core tracking and a limited AI coach.', 0, 'ILS', 'free', 0,
     ARRAY['analytics_basic', 'community']::TEXT[],
     '{"ai_messages_per_month": 5, "history_days": 90, "plan_weeks": 2}'::JSONB, 0),
    ('premium_monthly', 'Premium', 'Full AI coach, advanced analytics, all sensors.',
     1550, 'ILS', 'month', 7,
     ARRAY['ai_coach_basic', 'ai_coach_advanced', 'analytics_basic', 'analytics_advanced',
           'plan_adaptive', 'sensor_running', 'sensor_swimming', 'sensor_cycling',
           'sensor_strength', 'sensor_recovery', 'community', 'export_data']::TEXT[],
     '{"ai_messages_per_month": 100, "history_days": null, "plan_weeks": 52}'::JSONB, 1)
ON CONFLICT (code) DO NOTHING;


-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- DROP TABLE IF EXISTS billing.entitlements, billing.payment_webhook_events,
--     billing.payments, billing.subscriptions, billing.subscription_plans CASCADE;
-- =============================================================================
