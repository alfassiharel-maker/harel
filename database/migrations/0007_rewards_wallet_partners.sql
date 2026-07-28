-- =============================================================================
-- 0007 — Wallet double-entry ledger, reward policies, payouts, partners
-- =============================================================================
-- REVIEW REQUIRED. Not executed against any database. See database/README.md.
-- Run as app_migrator. Depends on 0006.
--
-- CORE INVARIANT: a wallet balance is DERIVED from immutable ledger entries and
-- is never stored as a mutable column. A balance column would be corruptible by
-- any bug in any code path that touches it, unauditable after the fact, and
-- impossible to reconcile. The ledger makes every shekel explainable.
-- =============================================================================

SET search_path = rewards, public;


-- -----------------------------------------------------------------------------
-- reward_policies — versioned rules, including the split percentages
-- -----------------------------------------------------------------------------
-- The specification requires the reward split to be changeable. Versioning it
-- means a change applies to future earnings only; every historic ledger entry
-- records the version that governed it, so history is never rewritten.
CREATE TABLE reward_policies (
    version        TEXT PRIMARY KEY,
    -- Points awarded per rule code, and the conversion rate to currency.
    earn_rules     JSONB NOT NULL DEFAULT '{}'::JSONB,
    points_per_minor_unit NUMERIC(12,6) NOT NULL CHECK (points_per_minor_unit > 0),
    -- Split of an earned reward. Must sum to 10000 basis points.
    redeemable_share_bps  INTEGER NOT NULL CHECK (redeemable_share_bps BETWEEN 0 AND 10000),
    withdrawable_share_bps INTEGER NOT NULL CHECK (withdrawable_share_bps BETWEEN 0 AND 10000),
    -- Payout gates.
    min_payout_minor      BIGINT NOT NULL DEFAULT 5000 CHECK (min_payout_minor >= 0),
    payout_cooling_off_days SMALLINT NOT NULL DEFAULT 30 CHECK (payout_cooling_off_days >= 0),
    -- Points expiry, if any.
    expiry_months  SMALLINT CHECK (expiry_months > 0),
    active_from    TIMESTAMPTZ,
    active_to      TIMESTAMPTZ,
    notes          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (redeemable_share_bps + withdrawable_share_bps = 10000),
    CHECK (active_to IS NULL OR active_from IS NULL OR active_to > active_from)
);

CREATE UNIQUE INDEX reward_policies_one_active_idx ON reward_policies ((true))
    WHERE active_from IS NOT NULL AND active_to IS NULL;

COMMENT ON TABLE reward_policies IS
    'Versioned reward economics. Exactly one active version at a time, enforced '
    'by a unique partial index rather than by convention.';


-- -----------------------------------------------------------------------------
-- wallets — holds NO balance
-- -----------------------------------------------------------------------------
CREATE TABLE wallets (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES identity.users (id) ON DELETE RESTRICT,
    currency   CHAR(3) NOT NULL DEFAULT 'ILS',
    status     TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'frozen', 'closed')),
    -- Set when fraud review freezes the wallet; earnings still accrue but
    -- redemption and payout are blocked.
    frozen_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, currency)
);

-- ON DELETE RESTRICT above is deliberate: a wallet with financial history must
-- not vanish because a user row was deleted. GDPR erasure anonymises the wallet
-- and retains the ledger, which is the standard lawful-basis carve-out.

CREATE TRIGGER wallets_touch BEFORE UPDATE ON wallets
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();


-- -----------------------------------------------------------------------------
-- wallet_transactions — groups balanced ledger entries
-- -----------------------------------------------------------------------------
CREATE TABLE wallet_transactions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id      UUID NOT NULL REFERENCES wallets (id) ON DELETE RESTRICT,
    user_id        UUID NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN
                       ('earn', 'redeem', 'payout', 'reversal', 'expiry', 'adjustment')),
    reason_code    TEXT NOT NULL,
    reward_policy_version TEXT REFERENCES reward_policies (version),
    -- External reference: the activity, challenge, referral, redemption or
    -- payout this transaction settles. Combined with reason_code it is the
    -- dedupe key that stops one workout being paid twice.
    reference_type TEXT,
    reference_id   TEXT,
    -- Points at reversal: the transaction being reversed.
    reverses_transaction_id UUID REFERENCES wallet_transactions (id),
    description    TEXT,
    created_by     UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX wallet_transactions_wallet_idx ON wallet_transactions (wallet_id, created_at DESC);
-- Idempotency for automated earning: one credit per (kind, reason, reference).
CREATE UNIQUE INDEX wallet_transactions_dedupe_idx
    ON wallet_transactions (wallet_id, kind, reason_code, reference_type, reference_id)
    WHERE reference_id IS NOT NULL;


-- -----------------------------------------------------------------------------
-- wallet_ledger_entries — IMMUTABLE, double-entry
-- -----------------------------------------------------------------------------
CREATE TABLE wallet_ledger_entries (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID NOT NULL REFERENCES wallet_transactions (id) ON DELETE RESTRICT,
    wallet_id      UUID NOT NULL REFERENCES wallets (id) ON DELETE RESTRICT,
    user_id        UUID NOT NULL,
    account        TEXT NOT NULL CHECK (account IN
                       ('house', 'earned', 'promotional', 'redeemable',
                        'withdrawable', 'spent', 'expired', 'reversed')),
    direction      TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    -- Always positive; direction carries the sign. A signed amount plus a
    -- direction column is two sources of truth for one fact.
    amount_minor   BIGINT NOT NULL CHECK (amount_minor > 0),
    currency       CHAR(3) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX wallet_ledger_balance_idx
    ON wallet_ledger_entries (wallet_id, account, direction);
CREATE INDEX wallet_ledger_transaction_idx ON wallet_ledger_entries (transaction_id);
CREATE INDEX wallet_ledger_user_idx ON wallet_ledger_entries (user_id, created_at DESC);

CREATE TRIGGER wallet_ledger_append_only BEFORE UPDATE OR DELETE
    ON wallet_ledger_entries FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();

COMMENT ON TABLE wallet_ledger_entries IS
    'Immutable double-entry lines. Balance = SUM(credit) - SUM(debit) per '
    '(wallet, account). Corrections are compensating transactions, never edits.';

-- Balance view. Reads go through this so no caller invents its own arithmetic.
CREATE VIEW wallet_balances AS
SELECT
    wallet_id,
    user_id,
    account,
    currency,
    SUM(CASE WHEN direction = 'credit' THEN amount_minor ELSE 0 END)
      - SUM(CASE WHEN direction = 'debit' THEN amount_minor ELSE 0 END) AS balance_minor
FROM wallet_ledger_entries
GROUP BY wallet_id, user_id, account, currency;

COMMENT ON VIEW wallet_balances IS
    'Derived balances. If this becomes slow, add a periodic snapshot row per '
    'wallet and sum only entries after it - do not add a mutable balance column.';

-- Trial-balance check, run nightly by the reconciliation job. Any row returned
-- is a bug: a transaction whose entries do not net to zero.
CREATE VIEW wallet_unbalanced_transactions AS
SELECT
    transaction_id,
    SUM(CASE WHEN direction = 'credit' THEN amount_minor ELSE -amount_minor END) AS imbalance_minor
FROM wallet_ledger_entries
GROUP BY transaction_id
HAVING SUM(CASE WHEN direction = 'credit' THEN amount_minor ELSE -amount_minor END) <> 0;


-- -----------------------------------------------------------------------------
-- reward_events — earn events awaiting (or having produced) a transaction
-- -----------------------------------------------------------------------------
CREATE TABLE reward_events (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    rule_code      TEXT NOT NULL CHECK (rule_code IN
                       ('workout_completed', 'streak_maintained', 'goal_achieved',
                        'challenge_completed', 'friend_invited', 'content_created',
                        'partner_purchase', 'profile_completed')),
    reference_type TEXT NOT NULL,
    reference_id   TEXT NOT NULL,
    points         BIGINT NOT NULL CHECK (points >= 0),
    -- Anti-fraud gate: an event from a low-trust activity is held for review
    -- rather than credited. See training.activities.trust_score.
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'credited', 'rejected', 'held_for_review')),
    rejection_reason TEXT,
    reward_policy_version TEXT REFERENCES reward_policies (version),
    transaction_id UUID REFERENCES wallet_transactions (id) ON DELETE SET NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One reward per rule per source object, ever.
    UNIQUE (user_id, rule_code, reference_type, reference_id)
);

CREATE INDEX reward_events_pending_idx ON reward_events (created_at)
    WHERE status IN ('pending', 'held_for_review');
CREATE INDEX reward_events_user_idx ON reward_events (user_id, occurred_at DESC);


-- -----------------------------------------------------------------------------
-- partners, offers, conversions
-- -----------------------------------------------------------------------------
SET search_path = partners, public;

CREATE TABLE partners (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    slug                CITEXT NOT NULL UNIQUE,
    category            TEXT NOT NULL CHECK (category IN
                            ('sports_retail', 'equipment', 'nutrition', 'gym',
                             'coaching', 'events', 'other')),
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'active', 'paused', 'terminated')),
    -- Basis points, so 750 = 7.5%. Integer arithmetic avoids float drift on
    -- money.
    commission_rate_bps INTEGER NOT NULL DEFAULT 0
                            CHECK (commission_rate_bps BETWEEN 0 AND 10000),
    currency            CHAR(3) NOT NULL DEFAULT 'ILS',
    contact_email       CITEXT,
    contract_ref        TEXT,
    -- Partner staff log in as identity.users with role 'partner', linked here.
    owner_user_id       UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX partners_name_trgm_idx ON partners USING gin (name gin_trgm_ops);
CREATE TRIGGER partners_touch BEFORE UPDATE ON partners
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE partner_offers (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id     UUID NOT NULL REFERENCES partners (id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    description    TEXT,
    -- Either a percentage off or a fixed credit; exactly one must be set.
    discount_bps   INTEGER CHECK (discount_bps BETWEEN 0 AND 10000),
    credit_minor   BIGINT CHECK (credit_minor > 0),
    currency       CHAR(3) NOT NULL DEFAULT 'ILS',
    -- Cost to the athlete in wallet value, if redeeming is not free.
    cost_minor     BIGINT NOT NULL DEFAULT 0 CHECK (cost_minor >= 0),
    terms          TEXT,
    valid_from     TIMESTAMPTZ NOT NULL,
    valid_to       TIMESTAMPTZ NOT NULL,
    -- NULL means unlimited.
    inventory      INTEGER CHECK (inventory >= 0),
    redeemed_count INTEGER NOT NULL DEFAULT 0 CHECK (redeemed_count >= 0),
    status         TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'active', 'paused', 'expired')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to > valid_from),
    CHECK ((discount_bps IS NOT NULL) <> (credit_minor IS NOT NULL)),
    CHECK (inventory IS NULL OR redeemed_count <= inventory)
);

CREATE INDEX partner_offers_live_idx ON partner_offers (valid_to)
    WHERE status = 'active';
CREATE TRIGGER partner_offers_touch BEFORE UPDATE ON partner_offers
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

CREATE TABLE partner_conversions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id      UUID NOT NULL REFERENCES partners (id) ON DELETE RESTRICT,
    offer_id        UUID REFERENCES partner_offers (id) ON DELETE SET NULL,
    user_id         UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    -- The partner's own order identifier: the idempotency key for their reports.
    partner_order_ref TEXT NOT NULL,
    gross_minor     BIGINT NOT NULL CHECK (gross_minor >= 0),
    commission_minor BIGINT NOT NULL CHECK (commission_minor >= 0),
    commission_rate_bps INTEGER NOT NULL,
    currency        CHAR(3) NOT NULL,
    status          TEXT NOT NULL DEFAULT 'reported'
                        CHECK (status IN ('reported', 'confirmed', 'rejected', 'reversed', 'paid')),
    attributed_at   TIMESTAMPTZ NOT NULL,
    confirmed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (partner_id, partner_order_ref)
);

CREATE INDEX partner_conversions_partner_idx ON partner_conversions (partner_id, attributed_at DESC);
CREATE INDEX partner_conversions_user_idx ON partner_conversions (user_id, attributed_at DESC);


-- -----------------------------------------------------------------------------
-- redemptions and payouts
-- -----------------------------------------------------------------------------
SET search_path = rewards, public;

CREATE TABLE redemptions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES identity.users (id) ON DELETE RESTRICT,
    wallet_id      UUID NOT NULL REFERENCES wallets (id) ON DELETE RESTRICT,
    offer_id       UUID REFERENCES partners.partner_offers (id) ON DELETE SET NULL,
    amount_minor   BIGINT NOT NULL CHECK (amount_minor > 0),
    currency       CHAR(3) NOT NULL,
    -- The code or voucher the athlete presents at the partner.
    voucher_code   TEXT,
    status         TEXT NOT NULL DEFAULT 'reserved'
                       CHECK (status IN ('reserved', 'issued', 'used', 'expired', 'canceled')),
    transaction_id UUID REFERENCES wallet_transactions (id) ON DELETE SET NULL,
    expires_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX redemptions_voucher_idx ON redemptions (voucher_code)
    WHERE voucher_code IS NOT NULL;
CREATE INDEX redemptions_user_idx ON redemptions (user_id, created_at DESC);
CREATE TRIGGER redemptions_touch BEFORE UPDATE ON redemptions
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- Payouts move real money out. Every safety property is explicit here because a
-- duplicated payout cannot be undone by a code fix.
CREATE TABLE payouts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES identity.users (id) ON DELETE RESTRICT,
    wallet_id           UUID NOT NULL REFERENCES wallets (id) ON DELETE RESTRICT,
    provider            TEXT NOT NULL CHECK (provider IN ('paypal', 'bank_transfer', 'partner_credit')),
    -- Encrypted destination reference. Never a raw bank account in plaintext.
    destination_ref_ct  BYTEA,
    kms_key_id          TEXT,
    amount_minor        BIGINT NOT NULL CHECK (amount_minor > 0),
    currency            CHAR(3) NOT NULL,
    -- Frozen record of every gate evaluated at request time: balance, account
    -- age, KYC state, fraud score, cooling-off. Auditable after the fact.
    eligibility_snapshot JSONB NOT NULL DEFAULT '{}'::JSONB,
    status              TEXT NOT NULL DEFAULT 'requested'
                            CHECK (status IN ('requested', 'under_review', 'approved',
                                              'rejected', 'processing', 'paid',
                                              'failed', 'needs_manual_reconciliation')),
    -- Sent to the provider so a retry cannot double-pay.
    idempotency_key     TEXT NOT NULL UNIQUE,
    provider_payout_id  TEXT,
    -- A human must approve. Automation proposes; a person decides.
    decided_by          UUID REFERENCES identity.users (id) ON DELETE SET NULL,
    decided_at          TIMESTAMPTZ,
    decision_note       TEXT,
    transaction_id      UUID REFERENCES wallet_transactions (id) ON DELETE SET NULL,
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'approved' OR decided_by IS NOT NULL)
);

CREATE INDEX payouts_review_queue_idx ON payouts (requested_at)
    WHERE status IN ('requested', 'under_review', 'needs_manual_reconciliation');
CREATE INDEX payouts_user_idx ON payouts (user_id, requested_at DESC);
CREATE TRIGGER payouts_touch BEFORE UPDATE ON payouts
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON COLUMN payouts.status IS
    'needs_manual_reconciliation is for ambiguous provider responses. Such a '
    'payout is NEVER auto-retried: a delayed payout is recoverable, a duplicated '
    'one is not.';


-- -----------------------------------------------------------------------------
-- fraud_signals — includes workout spoofing
-- -----------------------------------------------------------------------------
CREATE TABLE fraud_signals (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES identity.users (id) ON DELETE CASCADE,
    signal_code  TEXT NOT NULL CHECK (signal_code IN
                     ('impossible_performance', 'duplicate_activity_across_accounts',
                      'manual_activity_burst', 'referral_ring', 'device_shared',
                      'gps_teleport', 'payout_velocity', 'chargeback_history',
                      'multi_account_same_device')),
    severity     TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    score        NUMERIC(5,4) CHECK (score BETWEEN 0 AND 1),
    reference_type TEXT,
    reference_id TEXT,
    detail       JSONB NOT NULL DEFAULT '{}'::JSONB,
    resolved_at  TIMESTAMPTZ,
    resolution   TEXT CHECK (resolution IN ('false_positive', 'confirmed', 'action_taken')),
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX fraud_signals_user_idx ON fraud_signals (user_id, detected_at DESC);
CREATE INDEX fraud_signals_open_idx ON fraud_signals (severity, detected_at DESC)
    WHERE resolved_at IS NULL;


REVOKE UPDATE, DELETE ON rewards.wallet_ledger_entries FROM app_rw;
GRANT SELECT ON rewards.wallet_balances, rewards.wallet_unbalanced_transactions
    TO app_rw, app_ro;


-- -----------------------------------------------------------------------------
-- Seed the initial reward policy. Review the economics before running.
-- -----------------------------------------------------------------------------
INSERT INTO reward_policies
    (version, earn_rules, points_per_minor_unit, redeemable_share_bps,
     withdrawable_share_bps, min_payout_minor, payout_cooling_off_days,
     expiry_months, active_from, notes)
VALUES
    ('v1', '{"workout_completed": 10, "streak_maintained": 25, "goal_achieved": 100,
             "challenge_completed": 150, "friend_invited": 300, "profile_completed": 50}'::JSONB,
     10.0, 5000, 5000, 5000, 30, 24, now(),
     'Launch policy: 10 points = 1 agora; rewards split 50/50 between partner '
     'credit and withdrawable value, per the product specification. Both shares '
     'are configurable by adding a new policy version.')
ON CONFLICT (version) DO NOTHING;


-- =============================================================================
-- ROLLBACK
-- =============================================================================
-- DROP VIEW IF EXISTS rewards.wallet_unbalanced_transactions, rewards.wallet_balances;
-- DROP TABLE IF EXISTS rewards.fraud_signals, rewards.payouts, rewards.redemptions,
--     partners.partner_conversions, partners.partner_offers, partners.partners,
--     rewards.reward_events, rewards.wallet_ledger_entries,
--     rewards.wallet_transactions, rewards.wallets, rewards.reward_policies CASCADE;
-- =============================================================================
