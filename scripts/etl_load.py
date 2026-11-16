"""
etl_load.py
Loads the ACRIS pipeline's output CSVs into the PostgreSQL case data model.

FULL RELOAD:
    1. Truncate existing tables in dependency order.
    2. Load documents, properties, legal records, and parties.
    3. Load resolved entities and backfill entity dates.
    4. Load existing flip and over-mortgaging signals.
    5. Load transfer-signal outputs and adjusted valuation residuals.

Transfer-signal inputs:
    signal_nominal_chains.csv
    signal_repeated_transfers.csv
    signal_cema_proxy.csv
    valuation_residuals_adjusted.csv

Requires:
    pip install psycopg2-binary pandas python-dotenv

Usage:
    python etl_load.py

Run from the scripts/ folder.
Paths are relative to the existing project layout:
    ../data/raw
    ../data/processed
"""

from dotenv import load_dotenv
import os
import sys
import pandas as pd
import psycopg2
import psycopg2.extras

sys.path.insert(0, ".")

from doc_control_codes import DOC_CONTROL_CODES
from acris_pipeline import INSTITUTIONAL_PATTERN, GOVERNMENT_PATTERN
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

RAW_DIR = os.path.join("..", "data", "raw")
PROCESSED_DIR = os.path.join("..", "data", "processed")


# ============================================================
# NULL AND TYPE HELPERS
# ============================================================

def nn(x):
    """Return None for pandas NaN/NaT values."""
    return None if pd.isna(x) else x


def nn_int(x):
    return None if pd.isna(x) else int(x)


def nn_float(x):
    return None if pd.isna(x) else float(x)


def nn_bool(x):
    return None if pd.isna(x) else bool(x)


def signal_value(x):
    """
    Normalize empty values read from transfer-signal CSVs.
    Unlike ordinary Python truth-value conversion, this does not
    accidentally interpret the string 'False' as True.
    """
    if x is None or pd.isna(x):
        return None
    value = str(x).strip()
    if value.lower() in {"", "nan", "none", "null", "nat"}:
        return None
    return value


def signal_int(x):
    value = signal_value(x)
    if value is None:
        return None
    number = pd.to_numeric(value, errors="raise")
    if not float(number).is_integer():
        raise ValueError(
            f"Expected an integer in transfer-signal data; received {value!r}"
        )
    return int(number)


def signal_float(x):
    value = signal_value(x)
    if value is None:
        return None
    return float(pd.to_numeric(value, errors="raise"))


def signal_bool(x):
    value = signal_value(x)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"true", "t", "1", "yes", "y"}:
        return True
    if normalized in {"false", "f", "0", "no", "n"}:
        return False
    raise ValueError(
        f"Unrecognized boolean value in transfer-signal data: {value!r}"
    )


def signal_date(x):
    value = signal_value(x)
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="raise")
    return parsed.date()


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    # If DATABASE_URL is set, only use it if it belongs to this project.
    database_url = os.environ.get("DATABASE_URL")
    if database_url and "profitability_diagnostics" not in database_url:
        database_url = database_url.replace(
            "postgresql+psycopg://",
            "postgresql://",
        )
        database_url = database_url.replace(
            "postgresql+psycopg2://",
            "postgresql://",
        )
        return psycopg2.connect(database_url)
    # Fallback to individual variables with phantom_equity as the default.
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "phantom_equity"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
    )


# ============================================================
# PROPERTY IDENTIFIERS
# ============================================================

def parse_bbl(bbl):
    borough, block, lot = bbl.split("-")
    return int(borough), int(block), int(lot)


# ============================================================
# FULL RELOAD
# ============================================================

def truncate_all(conn):
    """
    Clear existing pipeline tables before reloading source data.
    Transfer-signal tables are included so each pipeline run represents
    the current source outputs rather than accumulating duplicate rows.
    """
    tables = [
        "case_notes",
        "case_events",
        "case_signals",
        "case_entities",
        "case_properties",
        "cases",
        "entity_review_queue",
        "entity_aliases",
        "valuation_residuals_adjusted",
        "signal_cema_proxy",
        "signal_repeated_transfers",
        "signal_nominal_chains",
        "signal_over_mortgaging",
        "signal_flips",
        "document_parties",
        "document_legals",
        "documents",
        "entities",
        "properties",
    ]
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE "
            + ", ".join(tables)
            + " RESTART IDENTITY CASCADE"
        )
    conn.commit()
    print(f"Truncated {len(tables)} tables")


# ============================================================
# DOCUMENTS
# ============================================================

def load_documents(conn, master_df):
    rows = [
        (
            r["document_id"],
            r["doc_type"],
            DOC_CONTROL_CODES.get(r["doc_type"], (None,))[0],
            nn(r["document_date"]),
            nn(r["recorded_datetime"]),
            nn_float(r["document_amt"]),
        )
        for _, r in master_df.iterrows()
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO documents (
                document_id,
                doc_type,
                doc_category,
                document_date,
                recorded_datetime,
                document_amt
            )
            VALUES %s
            ON CONFLICT (document_id) DO NOTHING
            """,
            rows,
        )
    conn.commit()
    print(f"Loaded {len(rows)} documents")


# ============================================================
# PROPERTIES AND LEGAL RECORDS
# ============================================================

def load_properties_and_legals(conn, legals_df, master_df):
    legals = legals_df.dropna(
        subset=["borough", "block", "lot"]
    ).copy()

    legals["bbl"] = (
        legals["borough"].astype(int).astype(str)
        + "-"
        + legals["block"].astype(int).astype(str)
        + "-"
        + legals["lot"].astype(int).astype(str)
    )

    dated = legals.merge(
        master_df[["document_id", "document_date"]],
        on="document_id",
        how="left",
    )
    dated["document_date"] = pd.to_datetime(
        dated["document_date"],
        errors="coerce",
    )

    prop_rows = []
    for bbl, group in dated.groupby("bbl"):
        borough, block, lot = parse_bbl(bbl)
        has_dates = group["document_date"].notna().any()
        latest = (
            group.sort_values("document_date").iloc[-1]
            if has_dates
            else group.iloc[-1]
        )
        prop_rows.append(
            (
                bbl,
                borough,
                block,
                lot,
                nn(latest.get("street_number")),
                nn(latest.get("street_name")),
                nn(latest.get("unit")),
                nn(latest.get("property_type")),
                nn(group["document_date"].min()) if has_dates else None,
                nn(group["document_date"].max()) if has_dates else None,
            )
        )

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO properties (
                bbl,
                borough,
                block,
                lot,
                street_number,
                street_name,
                latest_unit,
                latest_property_type,
                first_seen_date,
                last_seen_date
            )
            VALUES %s
            ON CONFLICT (bbl) DO NOTHING
            """,
            prop_rows,
        )
    conn.commit()
    print(f"Loaded {len(prop_rows)} properties")

    legal_rows = [
        (
            r["document_id"],
            r["bbl"],
            nn(r.get("property_type")),
            nn(r.get("unit")),
            nn_bool(r.get("easement")),
            nn_bool(r.get("partial_lot")),
            nn_bool(r.get("air_rights")),
        )
        for _, r in legals.iterrows()
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO document_legals (
                document_id,
                bbl,
                property_type,
                unit,
                easement,
                partial_lot,
                air_rights
            )
            VALUES %s
            ON CONFLICT (document_id, bbl) DO NOTHING
            """,
            legal_rows,
        )
    conn.commit()
    print(f"Loaded {len(legal_rows)} document_legals rows")


# ============================================================
# DOCUMENT PARTIES
# ============================================================

def load_document_parties(conn, parties_df):
    rows = [
        (
            int(r["party_row_id"]),
            r["document_id"],
            nn_int(r["party_type"]),
            r["name"],
            r["name_norm"],
            nn(r.get("role")),
            nn(r.get("role_bucket")),
            nn(r.get("address_1")),
            nn(r.get("city")),
            nn(r.get("state")),
            nn(r.get("zip")),
            bool(r["is_entity"]),
            nn(r.get("filer_category")),
        )
        for _, r in parties_df.iterrows()
    ]

    with conn.cursor() as cur:
        if rows:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO document_parties (
                    party_id,
                    document_id,
                    party_type,
                    raw_name,
                    name_norm,
                    role,
                    role_bucket,
                    address_1,
                    city,
                    state,
                    zip,
                    is_entity,
                    filer_category
                )
                VALUES %s
                """,
                rows,
            )
            cur.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence(
                        'document_parties',
                        'party_id'
                    ),
                    COALESCE(
                        (SELECT MAX(party_id) FROM document_parties),
                        1
                    )
                )
                """
            )
    conn.commit()
    print(f"Loaded {len(rows)} document_parties rows")


# ============================================================
# ENTITY CLASSIFICATION AND RESOLUTION
# ============================================================

def classify_entity_kind(canonical_name, default_kind):
    """
    Institutional and government entities are reclassified using the
    patterns already defined in acris_pipeline.py.
    """
    if INSTITUTIONAL_PATTERN.search(canonical_name):
        return "institutional_lender"
    if GOVERNMENT_PATTERN.search(canonical_name):
        return "government_agency"
    return default_kind


def load_entities_and_link(conn, non_trustee_df, trustee_df):
    cur = conn.cursor()
    total_entities = 0
    total_review = 0

    for cluster_col, kind, df in [
        ("entity_cluster_id", "llc_or_company", non_trustee_df),
        ("trustee_cluster_id", "trustee", trustee_df),
    ]:
        if df.empty:
            continue

        for cluster_id, group in df.groupby(cluster_col):
            names = group["name"].tolist()
            canonical = max(set(names), key=names.count)
            entity_kind = classify_entity_kind(canonical, kind)

            cur.execute(
                """
                INSERT INTO entities (
                    canonical_name,
                    entity_kind,
                    resolution_status
                )
                VALUES (%s, %s, 'auto')
                RETURNING entity_id
                """,
                (canonical, entity_kind),
            )
            entity_id = cur.fetchone()[0]
            total_entities += 1

            party_ids = group["party_row_id"].astype(int).tolist()
            cur.execute(
                """
                UPDATE document_parties
                SET entity_id = %s
                WHERE party_id = ANY(%s)
                """,
                (entity_id, party_ids),
            )

            alias_rows = [
                (
                    entity_id,
                    row["name"],
                    row["name_norm"],
                    int(row["party_row_id"]),
                )
                for _, row in group.drop_duplicates("name").iterrows()
            ]
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO entity_aliases (
                    entity_id,
                    raw_name,
                    name_norm,
                    source_party_id
                )
                VALUES %s
                """,
                alias_rows,
            )

            # Match entity_resolution.py's definition of a multi-variant
            # cluster: distinct normalized spellings, not merely raw names.
            distinct_norm_names = group["name_norm2"].unique().tolist()
            distinct_raw_names = group["name"].unique().tolist()

            if len(distinct_norm_names) > 1:
                cur.execute(
                    """
                    UPDATE entities
                    SET resolution_status = 'needs_review'
                    WHERE entity_id = %s
                    """,
                    (entity_id,),
                )
                cur.execute(
                    """
                    INSERT INTO entity_review_queue (
                        entity_id,
                        cluster_type,
                        variant_names,
                        status
                    )
                    VALUES (%s, %s, %s, 'pending')
                    """,
                    (entity_id, kind, distinct_raw_names),
                )
                total_review += 1

    conn.commit()
    cur.close()
    print(
        f"Loaded {total_entities} entities "
        f"({total_review} flagged for review)"
    )


def backfill_entity_dates(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE entities e
            SET
                first_seen_date = sub.min_date,
                last_seen_date = sub.max_date
            FROM (
                SELECT
                    ea.entity_id,
                    MIN(d.document_date) AS min_date,
                    MAX(d.document_date) AS max_date
                FROM entity_aliases ea
                JOIN document_parties dp
                    ON dp.party_id = ea.source_party_id
                JOIN documents d
                    ON d.document_id = dp.document_id
                GROUP BY ea.entity_id
            ) sub
            WHERE e.entity_id = sub.entity_id
            """
        )
    conn.commit()
    print("Backfilled entity first/last seen dates")


# ============================================================
# EXISTING SIGNAL LOADERS
# ============================================================

def load_signal_flips(conn, candidates_df, recurring_df):
    rows = []
    for df, category in [
        (candidates_df, "candidate"),
        (recurring_df, "recurring_pattern"),
    ]:
        for _, r in df.iterrows():
            rows.append(
                (
                    r["bbl"],
                    r["sale1_doc_id"],
                    r["sale2_doc_id"],
                    nn_float(r["sale1_amt"]),
                    nn_float(r["sale2_amt"]),
                    nn_int(r["days_between"]),
                    nn_float(r["pct_price_increase"]),
                    bool(r["unit_or_type_mismatch"]),
                    category,
                )
            )

    if not rows:
        print("No signal_flips rows to load")
        return

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO signal_flips (
                bbl,
                sale1_document_id,
                sale2_document_id,
                sale1_amt,
                sale2_amt,
                days_between,
                pct_price_increase,
                unit_or_type_mismatch,
                category
            )
            VALUES %s
            """,
            rows,
        )
    conn.commit()
    print(f"Loaded {len(rows)} signal_flips rows")


def load_signal_over_mortgaging(conn, candidates_df, block_df):
    rows = []
    for df, category in [
        (candidates_df, "candidate"),
        (block_df, "block_pattern"),
    ]:
        for _, r in df.iterrows():
            rows.append(
                (
                    r["bbl"],
                    r["deed_doc_id"],
                    r["mtge_doc_id"],
                    nn_float(r["deed_amt"]),
                    nn_float(r["mtge_amt"]),
                    nn_float(r["mortgage_to_price_ratio"]),
                    nn_int(r["days_between"]),
                    bool(r["unit_or_type_mismatch"]),
                    category,
                )
            )

    if not rows:
        print("No signal_over_mortgaging rows to load")
        return

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO signal_over_mortgaging (
                bbl,
                deed_document_id,
                mtge_document_id,
                deed_amt,
                mtge_amt,
                mortgage_to_price_ratio,
                days_between,
                unit_or_type_mismatch,
                category
            )
            VALUES %s
            """,
            rows,
        )
    conn.commit()
    print(f"Loaded {len(rows)} signal_over_mortgaging rows")


# ============================================================
# TRANSFER-SIGNAL CSV READER
# ============================================================

def read_transfer_signal_csv(filename, required_columns):
    """
    Read a required transfer-signal CSV.
    Missing files or required columns stop the ETL instead of silently
    producing an incomplete database reload.
    """
    path = os.path.join(PROCESSED_DIR, filename)

    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Required transfer-signal CSV not found: {path}"
        )

    df = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
    )

    missing = set(required_columns) - set(df.columns)
    if missing:
        raise ValueError(
            f"{filename} is missing required columns: "
            f"{sorted(missing)}"
        )

    for column in df.columns:
        if column == "cema_proxy":
            # "none" is a valid evidence tier, not a null sentinel.
            values = [
                None if pd.isna(value) or not str(value).strip()
                else str(value).strip().lower()
                for value in df[column]
            ]
        else:
            values = [signal_value(value) for value in df[column]]
        # Keep SQL NULLs as Python None; pandas StringDtype can otherwise
        # turn missing cells into floating-point NaN during iterrows().
        df[column] = pd.Series(values, index=df.index, dtype=object)

    print(f"Read {len(df):,} rows from {filename}")
    return df


def load_transfer_signal_table(
    conn,
    filename,
    table_name,
    columns,
    converters=None,
    db_columns=None,
):
    """
    Load one transfer-signal CSV using explicit source and database
    column lists.

    `columns` contains CSV headers.
    `db_columns` contains PostgreSQL column names in the same order.

    signal_id and created_at are omitted from inserts because PostgreSQL
    supplies their default values.
    """
    from psycopg2 import sql

    converters = converters or {}
    insert_columns = db_columns or columns

    if len(columns) != len(insert_columns):
        raise ValueError(
            f"{filename}: source and database column lists must "
            "have the same length"
        )

    df = read_transfer_signal_csv(filename, columns)

    rows = []
    for row_number, (_, row) in enumerate(df.iterrows(), start=2):
        values = []
        for column in columns:
            value = row[column]
            converter = converters.get(column)
            if converter is not None:
                try:
                    value = converter(value)
                except (ValueError, TypeError) as exc:
                    raise ValueError(
                        f"{filename}, row {row_number}, "
                        f"column {column}: {exc}"
                    ) from exc
            values.append(value)
        rows.append(tuple(values))

    if not rows:
        print(f"No rows to load into {table_name}")
        return

    insert_query = sql.SQL(
        "INSERT INTO {} ({}) VALUES %s"
    ).format(
        sql.Identifier(table_name),
        sql.SQL(", ").join(
            sql.Identifier(column)
            for column in insert_columns
        ),
    )

    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                insert_query.as_string(conn),
                rows,
                page_size=5000,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(
        f"Loaded {len(rows):,} rows into {table_name} "
        f"from {filename}"
    )


# ============================================================
# NOMINAL TRANSFER CHAINS
# ============================================================

def load_signal_nominal_chains(conn):
    # Original CSV headers.
    columns = [
        "bbl",
        "deedA_doc_id",
        "deedA_date",
        "deedA_amt",
        "grantor_names",
        "grantee_names",
        "partial_interest_pct",
        "grantee_looks_like_entity",
        "grantor_looks_like_individual",
        "shared_name_token",
        "chain_type",
        "followon_doc_id",
        "followon_date",
        "followon_amt",
        "gap_days",
        "continuity",
    ]

    # PostgreSQL's unquoted identifiers were stored in lowercase.
    db_columns = [
        "bbl",
        "deeda_doc_id",
        "deeda_date",
        "deeda_amt",
        "grantor_names",
        "grantee_names",
        "partial_interest_pct",
        "grantee_looks_like_entity",
        "grantor_looks_like_individual",
        "shared_name_token",
        "chain_type",
        "followon_doc_id",
        "followon_date",
        "followon_amt",
        "gap_days",
        "continuity",
    ]

    converters = {
        "deedA_date": signal_date,
        "deedA_amt": signal_float,
        "partial_interest_pct": signal_float,
        "grantee_looks_like_entity": signal_bool,
        "grantor_looks_like_individual": signal_bool,
        "followon_date": signal_date,
        "followon_amt": signal_float,
        "gap_days": signal_int,
    }

    load_transfer_signal_table(
        conn=conn,
        filename="signal_nominal_chains.csv",
        table_name="signal_nominal_chains",
        columns=columns,
        converters=converters,
        db_columns=db_columns,
    )


# ============================================================
# REPEATED TRANSFERS
# ============================================================

def load_signal_repeated_transfers(conn):
    columns = [
        "bbl",
        "n_recording_days",
        "span_days",
        "n_priced",
        "n_nominal",
        "chain_links",
        "possible_links",
        "true_chain",
        "first_date",
        "last_date",
    ]
    converters = {
        "n_recording_days": signal_int,
        "span_days": signal_int,
        "n_priced": signal_int,
        "n_nominal": signal_int,
        "chain_links": signal_int,
        "possible_links": signal_int,
        "true_chain": signal_bool,
        "first_date": signal_date,
        "last_date": signal_date,
    }
    load_transfer_signal_table(
        conn=conn,
        filename="signal_repeated_transfers.csv",
        table_name="signal_repeated_transfers",
        columns=columns,
        converters=converters,
    )


# ============================================================
# CEMA PROXY
# ============================================================

def signal_cema_proxy_tier(value):
    if value is None or pd.isna(value):
        return None
    tier = str(value).strip().lower()
    if tier not in {"none", "medium", "strong"}:
        raise ValueError(
            f"Unrecognized CEMA proxy tier: {value!r}"
        )
    return tier


def load_signal_cema_proxy(conn):
    columns = [
        "bbl",
        "mtge_doc_id",
        "source",
        "companion_types",
        "cema_proxy",
    ]
    converters = {"cema_proxy": signal_cema_proxy_tier}
    load_transfer_signal_table(
        conn=conn,
        filename="signal_cema_proxy.csv",
        table_name="signal_cema_proxy",
        columns=columns,
        converters=converters,
    )


# ============================================================
# ADJUSTED VALUATION RESIDUALS
# ============================================================

def load_valuation_residuals_adjusted(conn):
    columns = [
        "bbl",
        "document_id",
        "price",
        "expected_price",
        "z",
        "pct",
        "z_adjusted",
    ]
    converters = {
        "price": signal_float,
        "expected_price": signal_float,
        "z": signal_float,
        "pct": signal_float,
        "z_adjusted": signal_float,
    }
    load_transfer_signal_table(
        conn=conn,
        filename="valuation_residuals_adjusted.csv",
        table_name="valuation_residuals_adjusted",
        columns=columns,
        converters=converters,
    )


# ============================================================
# EXISTING CSV READER
# ============================================================

def read_csv_or_empty(path, columns):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path)
        except pd.errors.EmptyDataError:
            pass
    return pd.DataFrame(columns=columns)


# ============================================================
# MAIN ETL
# ============================================================

def main():
    conn = get_connection()
    try:
        print(
            "Python connected to database:",
            conn.get_dsn_parameters()["dbname"],
        )
        print(
            "Python connected to host:",
            conn.get_dsn_parameters()["host"],
        )

        truncate_all(conn)

        # ----------------------------------------------------
        # CORE SOURCE DATA
        # ----------------------------------------------------
        master = pd.read_csv(
            os.path.join(RAW_DIR, "acris_master_sample.csv")
        )
        legals = pd.read_csv(
            os.path.join(RAW_DIR, "acris_legals_sample.csv")
        )
        parties_analyzed = pd.read_csv(
            os.path.join(PROCESSED_DIR, "acris_parties_analyzed.csv")
        )
        entities_resolved = pd.read_csv(
            os.path.join(PROCESSED_DIR, "entities_resolved.csv")
        )
        trustees_resolved = pd.read_csv(
            os.path.join(PROCESSED_DIR, "trustees_resolved.csv")
        )

        # ----------------------------------------------------
        # EXISTING FLIP AND MORTGAGING SIGNALS
        # ----------------------------------------------------
        flip_cols = [
            "bbl",
            "sale1_date",
            "sale1_amt",
            "sale1_doc_id",
            "sale1_unit",
            "sale1_type",
            "sale2_date",
            "sale2_amt",
            "sale2_doc_id",
            "sale2_unit",
            "sale2_type",
            "days_between",
            "pct_price_increase",
            "unit_or_type_mismatch",
        ]
        mtg_cols = [
            "bbl",
            "deed_date",
            "deed_amt",
            "deed_doc_id",
            "deed_unit",
            "deed_type",
            "mtge_date",
            "mtge_amt",
            "mtge_doc_id",
            "mtge_unit",
            "mtge_type",
            "days_between",
            "mortgage_to_price_ratio",
            "unit_or_type_mismatch",
        ]

        flips = read_csv_or_empty(
            os.path.join(PROCESSED_DIR, "signal_rapid_flips.csv"),
            flip_cols,
        )
        flips_recurring = read_csv_or_empty(
            os.path.join(
                PROCESSED_DIR,
                "signal_flips_recurring_pattern.csv",
            ),
            flip_cols,
        )
        over_mtg = read_csv_or_empty(
            os.path.join(
                PROCESSED_DIR,
                "signal_over_mortgaging.csv",
            ),
            mtg_cols,
        )
        over_mtg_block = read_csv_or_empty(
            os.path.join(
                PROCESSED_DIR,
                "signal_mortgaging_block_pattern.csv",
            ),
            mtg_cols,
        )

        # ----------------------------------------------------
        # LOAD CORE DATA
        # ----------------------------------------------------
        load_documents(conn, master)
        load_properties_and_legals(
            conn,
            legals,
            master,
        )
        load_document_parties(
            conn,
            parties_analyzed,
        )
        load_entities_and_link(
            conn,
            entities_resolved,
            trustees_resolved,
        )
        backfill_entity_dates(conn)

        # ----------------------------------------------------
        # LOAD EXISTING SIGNALS
        # ----------------------------------------------------
        load_signal_flips(
            conn,
            flips,
            flips_recurring,
        )
        load_signal_over_mortgaging(
            conn,
            over_mtg,
            over_mtg_block,
        )

        # ----------------------------------------------------
        # LOAD TRANSFER-SIGNAL OUTPUTS
        # ----------------------------------------------------
        print("\nLoading transfer-signal outputs...")
        load_signal_nominal_chains(conn)
        load_signal_repeated_transfers(conn)
        load_signal_cema_proxy(conn)
        load_valuation_residuals_adjusted(conn)

        print("\nETL load complete.")

    finally:
        conn.close()


if __name__ == "__main__":
    main()