BEGIN;

-- ============================================================
-- 1. Nominal-transfer chain signals
-- ============================================================

CREATE TABLE IF NOT EXISTS signal_nominal_chains (
    signal_id BIGSERIAL PRIMARY KEY,
    bbl TEXT NOT NULL,
    deedA_doc_id TEXT,
    deedA_date DATE,
    deedA_amt NUMERIC,
    grantor_names TEXT,
    grantee_names TEXT,
    partial_interest_pct NUMERIC,
    grantee_looks_like_entity BOOLEAN,
    grantor_looks_like_individual BOOLEAN,
    shared_name_token TEXT,
    chain_type TEXT,
    followon_doc_id TEXT,
    followon_date DATE,
    followon_amt NUMERIC,
    gap_days INTEGER,
    continuity TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_nominal_chains_bbl
    ON signal_nominal_chains (bbl);

CREATE INDEX IF NOT EXISTS idx_signal_nominal_chains_deedA_doc_id
    ON signal_nominal_chains (deedA_doc_id);

CREATE INDEX IF NOT EXISTS idx_signal_nominal_chains_followon_doc_id
    ON signal_nominal_chains (followon_doc_id);

CREATE INDEX IF NOT EXISTS idx_signal_nominal_chains_dates
    ON signal_nominal_chains (deedA_date, followon_date);


-- ============================================================
-- 2. Repeated-transfer summaries
-- ============================================================

CREATE TABLE IF NOT EXISTS signal_repeated_transfers (
    signal_id BIGSERIAL PRIMARY KEY,
    bbl TEXT NOT NULL,
    n_recording_days INTEGER,
    span_days INTEGER,
    n_priced INTEGER,
    n_nominal INTEGER,
    chain_links INTEGER,
    possible_links INTEGER,
    true_chain BOOLEAN,
    first_date DATE,
    last_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_repeated_transfers_bbl
    ON signal_repeated_transfers (bbl);

CREATE INDEX IF NOT EXISTS idx_signal_repeated_transfers_dates
    ON signal_repeated_transfers (first_date, last_date);


-- ============================================================
-- 3. CEMA proxy signals
-- ============================================================

CREATE TABLE IF NOT EXISTS signal_cema_proxy (
    signal_id BIGSERIAL PRIMARY KEY,
    bbl TEXT NOT NULL,
    mtge_doc_id TEXT,
    source TEXT,
    companion_types TEXT,
    cema_proxy TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_cema_proxy_bbl
    ON signal_cema_proxy (bbl);

CREATE INDEX IF NOT EXISTS idx_signal_cema_proxy_mtge_doc_id
    ON signal_cema_proxy (mtge_doc_id);


-- ============================================================
-- 4. Adjusted valuation residuals
-- ============================================================

CREATE TABLE IF NOT EXISTS valuation_residuals_adjusted (
    residual_id BIGSERIAL PRIMARY KEY,
    bbl TEXT NOT NULL,
    document_id TEXT,
    price NUMERIC,
    expected_price NUMERIC,
    z NUMERIC,
    pct NUMERIC,
    z_adjusted NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_valuation_residuals_adjusted_bbl
    ON valuation_residuals_adjusted (bbl);

CREATE INDEX IF NOT EXISTS idx_valuation_residuals_adjusted_document_id
    ON valuation_residuals_adjusted (document_id);


-- ============================================================
-- 5. Permit nominal_chain in case_signals
-- ============================================================

-- Discover the existing CHECK constraint rather than assuming
-- its generated name.

DO $$
DECLARE
    constraint_record RECORD;
BEGIN
    FOR constraint_record IN
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class tbl
            ON tbl.oid = con.conrelid
        JOIN pg_namespace ns
            ON ns.oid = tbl.relnamespace
        WHERE tbl.relname = 'case_signals'
          AND ns.nspname = current_schema()
          AND con.contype = 'c'
          AND pg_get_constraintdef(con.oid) LIKE '%signal_type%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.case_signals DROP CONSTRAINT %I',
            current_schema(),
            constraint_record.conname
        );
    END LOOP;
END $$;

ALTER TABLE case_signals
    ADD CONSTRAINT case_signals_signal_type_check
    CHECK (
        signal_type IN (
            'flip',
            'over_mortgaging',
            'nominal_chain'
        )
    );

COMMIT;