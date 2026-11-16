-- =====================================================================
-- Project Phantom Equity — Shared Case Data Model
-- PostgreSQL schema
--
-- Layers, in dependency order:
--   1. Raw evidence   (properties, documents, document_legals, document_parties)
--   2. Resolved entities (entities, entity_aliases, entity_review_queue)
--   3. Signals        (signal_flips, signal_over_mortgaging,
--                       signal_nominal_chains, signal_repeated_transfers,
--                       signal_cema_proxy, valuation_residuals_adjusted)
--   4. Cases          (cases, case_properties, case_entities, case_signals,
--                       case_events, case_notes)
--   5. Entity-resolution network (entity_relationships, entity_entity_edges)
--
-- Design note: `cases` is deliberately decoupled from both `properties` and
-- `entities` via many-to-many link tables, rather than anchored to one or
-- the other. A phantom-equity case can be a single suspicious property OR a
-- shell network spanning many properties -- forcing one shape onto the other
-- would break one of the two. `case_signals` is polymorphic (signal_type +
-- signal_id) so a case can cite evidence from multiple signal tables without
-- requiring a foreign key across different target tables.
-- =====================================================================


-- ---------------------------------------------------------------------
-- LAYER 1: Raw evidence
-- ---------------------------------------------------------------------

-- One row per distinct NYC property (borough+block+lot).
CREATE TABLE properties (
    bbl              TEXT PRIMARY KEY,           -- e.g. '1-1259-1284'
    borough          SMALLINT NOT NULL,
    block            INTEGER NOT NULL,
    lot              INTEGER NOT NULL,
    street_number    TEXT,
    street_name      TEXT,
    latest_unit      TEXT,                        -- most recently seen unit label, if any
    latest_property_type TEXT,                     -- most recently seen ACRIS property_type code
    first_seen_date  DATE,
    last_seen_date   DATE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_properties_block ON properties (borough, block);

-- Mirrors ACRIS Master: one row per recorded document.
CREATE TABLE documents (
    document_id      TEXT PRIMARY KEY,            -- ACRIS document_id, 16-char
    doc_type         TEXT NOT NULL,                -- e.g. 'DEED', 'MTGE', 'SAT'
    doc_category     TEXT,                         -- from Document Control Codes: 'DEEDS AND OTHER CONVEYANCES', etc.
    document_date    DATE,                         -- execution date (can lag recording by weeks)
    recorded_datetime TIMESTAMPTZ,                 -- actual filing timestamp -- more reliable for
                                                    -- same-session detection than document_date
    document_amt     NUMERIC(15, 2),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_documents_type_date ON documents (doc_type, document_date);
CREATE INDEX idx_documents_recorded ON documents (recorded_datetime);
CREATE INDEX idx_documents_amt ON documents (document_amt);

-- Bridge: a document can legally span multiple properties (bulk sales,
-- blanket mortgages, spreader instruments). This is where that fact lives.
CREATE TABLE document_legals (
    document_id      TEXT NOT NULL REFERENCES documents(document_id),
    bbl              TEXT NOT NULL REFERENCES properties(bbl),
    property_type    TEXT,                         -- ACRIS property_type code for THIS (doc, bbl) pair
    unit             TEXT,
    easement         BOOLEAN,
    partial_lot      BOOLEAN,
    air_rights       BOOLEAN,
    PRIMARY KEY (document_id, bbl)
);
CREATE INDEX idx_document_legals_bbl ON document_legals (bbl);

-- One row per party on a document, with the decoded role.
CREATE TABLE document_parties (
    party_id         BIGSERIAL PRIMARY KEY,
    document_id      TEXT NOT NULL REFERENCES documents(document_id),
    party_type       SMALLINT,                     -- raw ACRIS 1/2/3
    raw_name         TEXT NOT NULL,
    name_norm        TEXT NOT NULL,                 -- normalized (suffix-canonicalized) name
    role             TEXT,                          -- decoded, e.g. 'GRANTOR/SELLER'
    role_bucket      TEXT,                          -- 'SIDE_1' / 'SIDE_2' / 'OTHER/GENERIC'
    address_1        TEXT,
    city             TEXT,
    state            TEXT,
    zip              TEXT,
    is_entity        BOOLEAN NOT NULL DEFAULT FALSE,
    filer_category   TEXT,                          -- 'TIMESHARE_NOISE' | 'INSTITUTIONAL_LENDER_NOISE'
                                                     -- | 'GOVERNMENT_AGENCY_NOISE' | 'BULK_ENTITY_FILER'
                                                     -- | 'PROLIFIC_TRUSTEE' | 'PROLIFIC_INDIVIDUAL' | 'NORMAL'
    entity_id        BIGINT,                        -- FK to entities, added after that table exists (below)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_document_parties_document ON document_parties (document_id);
CREATE INDEX idx_document_parties_name_norm ON document_parties (name_norm);
CREATE INDEX idx_document_parties_filer_category ON document_parties (filer_category);


-- ---------------------------------------------------------------------
-- LAYER 2: Resolved entities
-- ---------------------------------------------------------------------

-- A resolved real-world entity (a company, or a person acting as trustee/
-- individual filer). One entity can have many raw name variants (aliases).
CREATE TABLE entities (
    entity_id        BIGSERIAL PRIMARY KEY,
    canonical_name   TEXT NOT NULL,                 -- best-guess display name for the cluster
    entity_kind      TEXT NOT NULL CHECK (entity_kind IN
                        ('llc_or_company', 'trustee', 'individual',
                         'institutional_lender', 'government_agency', 'unknown')),
    resolution_status TEXT NOT NULL DEFAULT 'auto' CHECK (resolution_status IN
                        ('auto', 'confirmed', 'needs_review')),
    first_seen_date  DATE,
    last_seen_date   DATE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_entities_kind_status ON entities (entity_kind, resolution_status);

ALTER TABLE document_parties
    ADD CONSTRAINT fk_document_parties_entity
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id);

-- Every raw name variant that was merged into an entity, for auditability --
-- so a resolution decision can always be traced back and reversed.
CREATE TABLE entity_aliases (
    alias_id         BIGSERIAL PRIMARY KEY,
    entity_id        BIGINT NOT NULL REFERENCES entities(entity_id),
    raw_name         TEXT NOT NULL,
    name_norm        TEXT NOT NULL,
    source_party_id  BIGINT REFERENCES document_parties(party_id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_entity_aliases_entity ON entity_aliases (entity_id);
CREATE INDEX idx_entity_aliases_name_norm ON entity_aliases (name_norm);

-- Multi-variant clusters that fuzzy matching produced but a human hasn't
-- confirmed. Mirrors clusters_needing_review.csv from the resolution script --
-- this is where "candidate merge, not confirmed identity" lives permanently.
CREATE TABLE entity_review_queue (
    review_id        BIGSERIAL PRIMARY KEY,
    entity_id        BIGINT REFERENCES entities(entity_id),
    cluster_type     TEXT NOT NULL CHECK (cluster_type IN ('llc_or_company', 'trustee')),
    variant_names   TEXT[] NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending', 'confirmed', 'rejected', 'split')),
    reviewed_by      TEXT,
    reviewed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_entity_review_queue_status ON entity_review_queue (status);


-- ---------------------------------------------------------------------
-- LAYER 3: Signals
-- ---------------------------------------------------------------------

-- Existing flip signal. All candidates are retained, with category used
-- to distinguish candidate signals from recurring patterns.
CREATE TABLE signal_flips (
    signal_id            BIGSERIAL PRIMARY KEY,
    bbl                  TEXT NOT NULL REFERENCES properties(bbl),
    sale1_document_id    TEXT NOT NULL REFERENCES documents(document_id),
    sale2_document_id    TEXT NOT NULL REFERENCES documents(document_id),
    sale1_amt            NUMERIC(15, 2) NOT NULL,
    sale2_amt            NUMERIC(15, 2) NOT NULL,
    days_between         INTEGER NOT NULL,
    pct_price_increase   NUMERIC(10, 1) NOT NULL,
    unit_or_type_mismatch BOOLEAN NOT NULL DEFAULT FALSE,
    category             TEXT NOT NULL DEFAULT 'candidate' CHECK (category IN
                            ('candidate', 'recurring_pattern')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_signal_flips_bbl ON signal_flips (bbl);
CREATE INDEX idx_signal_flips_category ON signal_flips (category);

-- Existing over-mortgaging signal.
CREATE TABLE signal_over_mortgaging (
    signal_id            BIGSERIAL PRIMARY KEY,
    bbl                  TEXT NOT NULL REFERENCES properties(bbl),
    deed_document_id     TEXT NOT NULL REFERENCES documents(document_id),
    mtge_document_id     TEXT NOT NULL REFERENCES documents(document_id),
    deed_amt             NUMERIC(15, 2) NOT NULL,
    mtge_amt             NUMERIC(15, 2) NOT NULL,
    mortgage_to_price_ratio NUMERIC(10, 3) NOT NULL,
    days_between         INTEGER NOT NULL,
    unit_or_type_mismatch BOOLEAN NOT NULL DEFAULT FALSE,
    category             TEXT NOT NULL DEFAULT 'candidate' CHECK (category IN
                            ('candidate', 'block_pattern')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_signal_over_mortgaging_bbl ON signal_over_mortgaging (bbl);
CREATE INDEX idx_signal_over_mortgaging_category ON signal_over_mortgaging (category);
CREATE INDEX idx_signal_over_mortgaging_ratio ON signal_over_mortgaging (mortgage_to_price_ratio);


-- Nominal-transfer chain candidates emitted by transfer_signals.py.
-- Stores the deed pair, party-name evidence, transfer characteristics,
-- and follow-on deed needed to inspect possible nominal consideration chains.
CREATE TABLE signal_nominal_chains (
    signal_id                  BIGSERIAL PRIMARY KEY,
    bbl                        TEXT NOT NULL,
    deedA_doc_id               TEXT,
    deedA_date                 DATE,
    deedA_amt                  NUMERIC,
    grantor_names              TEXT,
    grantee_names              TEXT,
    partial_interest_pct       NUMERIC,
    grantee_looks_like_entity  BOOLEAN,
    grantor_looks_like_individual BOOLEAN,
    shared_name_token          TEXT,
    chain_type                 TEXT,
    followon_doc_id            TEXT,
    followon_date              DATE,
    followon_amt               NUMERIC,
    gap_days                   INTEGER,
    continuity                 TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_signal_nominal_chains_bbl
    ON signal_nominal_chains (bbl);
CREATE INDEX idx_signal_nominal_chains_deedA_doc_id
    ON signal_nominal_chains (deedA_doc_id);
CREATE INDEX idx_signal_nominal_chains_followon_doc_id
    ON signal_nominal_chains (followon_doc_id);
CREATE INDEX idx_signal_nominal_chains_dates
    ON signal_nominal_chains (deedA_date, followon_date);


-- Repeated-transfer summaries emitted by transfer_signals.py.
-- These are contextual transfer-pattern evidence, not automatically
-- independent case signals.
CREATE TABLE signal_repeated_transfers (
    signal_id          BIGSERIAL PRIMARY KEY,
    bbl                TEXT NOT NULL,
    n_recording_days   INTEGER,
    span_days          INTEGER,
    n_priced           INTEGER,
    n_nominal          INTEGER,
    chain_links        INTEGER,
    possible_links     INTEGER,
    true_chain         BOOLEAN,
    first_date         DATE,
    last_date          DATE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_signal_repeated_transfers_bbl
    ON signal_repeated_transfers (bbl);
CREATE INDEX idx_signal_repeated_transfers_dates
    ON signal_repeated_transfers (first_date, last_date);


-- CEMA proxy evidence emitted by transfer_signals.py.
-- A proxy classification is not treated as proof of a CEMA transaction.
CREATE TABLE signal_cema_proxy (
    signal_id          BIGSERIAL PRIMARY KEY,
    bbl                TEXT NOT NULL,
    mtge_doc_id        TEXT,
    source             TEXT,
    companion_types    TEXT,
    cema_proxy         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_signal_cema_proxy_bbl
    ON signal_cema_proxy (bbl);
CREATE INDEX idx_signal_cema_proxy_mtge_doc_id
    ON signal_cema_proxy (mtge_doc_id);


-- Adjusted valuation residuals emitted by the valuation analysis.
-- These preserve both the original residual measures and the adjusted
-- standardized residual for downstream investigation.
CREATE TABLE valuation_residuals_adjusted (
    residual_id       BIGSERIAL PRIMARY KEY,
    bbl               TEXT NOT NULL,
    document_id       TEXT,
    price             NUMERIC,
    expected_price    NUMERIC,
    z                 NUMERIC,
    pct               NUMERIC,
    z_adjusted        NUMERIC,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_valuation_residuals_adjusted_bbl
    ON valuation_residuals_adjusted (bbl);
CREATE INDEX idx_valuation_residuals_adjusted_document_id
    ON valuation_residuals_adjusted (document_id);


-- ---------------------------------------------------------------------
-- LAYER 4: Cases
-- ---------------------------------------------------------------------

CREATE TABLE cases (
    case_id          BIGSERIAL PRIMARY KEY,
    title            TEXT NOT NULL,
    summary          TEXT,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN
                        ('open', 'under_review', 'confirmed', 'dismissed')),
    score            NUMERIC(12, 2),                 -- corroboration-engine output; nullable until scored
    created_by       TEXT NOT NULL DEFAULT 'system', -- 'system' for auto-generated, else investigator name
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cases_status ON cases (status);
CREATE INDEX idx_cases_score ON cases (score DESC NULLS LAST);

-- Many-to-many: a case can involve multiple properties (a shell network
-- spanning several parcels); a property can appear in multiple cases.
CREATE TABLE case_properties (
    case_id          BIGINT NOT NULL REFERENCES cases(case_id),
    bbl              TEXT NOT NULL REFERENCES properties(bbl),
    role_in_case     TEXT NOT NULL DEFAULT 'related' CHECK (role_in_case IN ('primary', 'related')),
    PRIMARY KEY (case_id, bbl)
);
CREATE INDEX idx_case_properties_bbl ON case_properties (bbl);

-- Many-to-many: a case can involve multiple resolved entities.
CREATE TABLE case_entities (
    case_id          BIGINT NOT NULL REFERENCES cases(case_id),
    entity_id        BIGINT NOT NULL REFERENCES entities(entity_id),
    role_in_case     TEXT,                          -- free text: 'suspect', 'buyer', 'seller', 'trustee', etc.
    PRIMARY KEY (case_id, entity_id)
);
CREATE INDEX idx_case_entities_entity ON case_entities (entity_id);

-- Polymorphic link from a case to whichever signal(s) support it. No FK
-- constraint on signal_id (can't FK across two possible target tables in
-- plain Postgres) -- application code resolves via signal_type + signal_id.
CREATE TABLE case_signals (
    case_id          BIGINT NOT NULL REFERENCES cases(case_id),
    signal_type      TEXT NOT NULL CHECK (
        signal_type IN ('flip', 'over_mortgaging', 'nominal_chain')
    ),
    signal_id        BIGINT NOT NULL,
    weight           NUMERIC(5, 2),                 -- optional contribution weight for the score
    PRIMARY KEY (case_id, signal_type, signal_id)
);

-- Chronological events for the timeline view. Not every event needs a
-- document (investigator actions, status changes) so document_id is nullable.
CREATE TABLE case_events (
    event_id         BIGSERIAL PRIMARY KEY,
    case_id          BIGINT NOT NULL REFERENCES cases(case_id),
    event_date       TIMESTAMPTZ NOT NULL,
    event_type       TEXT NOT NULL,                 -- e.g. 'document_recorded', 'status_change', 'note_added'
    document_id      TEXT REFERENCES documents(document_id),
    description      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_case_events_case_date ON case_events (case_id, event_date);

-- Investigator annotations, for the investigation-queue view.
CREATE TABLE case_notes (
    note_id          BIGSERIAL PRIMARY KEY,
    case_id          BIGINT NOT NULL REFERENCES cases(case_id),
    author           TEXT NOT NULL,
    note_text        TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_case_notes_case ON case_notes (case_id);


-- ---------------------------------------------------------------------
-- LAYER 5: Entity-resolution network structure
-- ---------------------------------------------------------------------

-- Explicit, persisted graph edges: entity <-> property, with the role and
-- relationship_class that justify the edge. This is the direct backing data
-- for the network-graph view. Independent of case-building: it covers every
-- resolved entity's relationship history, not just properties touching a
-- candidate signal.
CREATE TABLE entity_relationships (
    relationship_id  BIGSERIAL PRIMARY KEY,
    entity_id        BIGINT NOT NULL REFERENCES entities(entity_id),
    bbl              TEXT NOT NULL REFERENCES properties(bbl),
    document_id      TEXT NOT NULL REFERENCES documents(document_id),
    role             TEXT,                          -- decoded role, e.g. 'GRANTOR/SELLER'
    relationship_class TEXT NOT NULL CHECK (relationship_class IN
                        ('OWNERSHIP_TRANSFER', 'FINANCING', 'ASSIGNMENT',
                         'LEASE', 'OTHER_GENERIC')),
    is_connecting    BOOLEAN NOT NULL,               -- mirrors case_builder.py's union-find
                                                      -- eligibility at the time this was built
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_entity_relationships_entity ON entity_relationships (entity_id);
CREATE INDEX idx_entity_relationships_bbl ON entity_relationships (bbl);
CREATE INDEX idx_entity_relationships_class ON entity_relationships (relationship_class);

-- Direct entity-to-entity edges are derivable from entity_relationships
-- via a self-join, so they do not need separate storage.
CREATE VIEW entity_entity_edges AS
SELECT DISTINCT
    LEAST(a.entity_id, b.entity_id) AS entity_id_1,
    GREATEST(a.entity_id, b.entity_id) AS entity_id_2,
    a.document_id,
    a.bbl
FROM entity_relationships a
JOIN entity_relationships b
    ON a.document_id = b.document_id AND a.entity_id < b.entity_id;