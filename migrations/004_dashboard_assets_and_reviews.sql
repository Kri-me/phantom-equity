-- Add materialized dashboard assets and persistent review state.


-- Dashboard-only assets built from processed CSVs. Load them with
-- scripts/load_dashboard_assets.py after signals.py and pluto_geocode.py.
CREATE TABLE IF NOT EXISTS signal_serial_refinancing (
    bbl TEXT NOT NULL,
    mtge1_date DATE,
    mtge1_amt NUMERIC(16,2),
    mtge1_doc_id TEXT NOT NULL,
    mtge2_date DATE,
    mtge2_amt NUMERIC(16,2),
    mtge2_doc_id TEXT NOT NULL,
    days_between INTEGER,
    amt_increase NUMERIC(16,2),
    borrower_changed BOOLEAN,
    PRIMARY KEY (bbl, mtge1_doc_id, mtge2_doc_id)
);
CREATE TABLE IF NOT EXISTS signal_party_overlap_exclusions (
    bbl TEXT NOT NULL,
    deed_doc_id TEXT NOT NULL,
    deed_date DATE,
    deed_amt NUMERIC(16,2),
    mtge_doc_id TEXT NOT NULL,
    mtge_date DATE,
    mtge_amt NUMERIC(16,2),
    days_between INTEGER,
    mortgage_to_price_ratio NUMERIC(18,8),
    deed_buyer_names TEXT,
    mtge_doc_names TEXT,
    PRIMARY KEY (bbl, deed_doc_id, mtge_doc_id)
);
CREATE TABLE IF NOT EXISTS property_geo (
    bbl TEXT PRIMARY KEY,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    precision TEXT NOT NULL CHECK (precision IN ('lot', 'block'))
);
CREATE TABLE IF NOT EXISTS dashboard_asset_meta (
    asset_name TEXT PRIMARY KEY,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_reviews (
    signal_type TEXT NOT NULL,
    bbl TEXT NOT NULL,
    doc_a TEXT NOT NULL,
    doc_b TEXT NOT NULL,
    status TEXT NOT NULL,
    closed_reason TEXT,
    note TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_type, bbl, doc_a, doc_b)
);
