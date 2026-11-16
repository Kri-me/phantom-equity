"""Load CSV-backed dashboard assets into PostgreSQL for hosted deployments.

Run after signals.py and pluto_geocode.py have generated all three input CSVs.
For a Neon target, use --target-neon and paste the direct (non-pooler) URL when
prompted. The URL is requested with hidden input and is never printed.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "processed"
load_dotenv(ROOT / ".env", override=False)

ASSETS = {
    "signal_serial_refinancing": {
        "file": "signal_serial_refinancing.csv",
        "columns": ["bbl", "mtge1_date", "mtge1_amt", "mtge1_doc_id", "mtge2_date",
                    "mtge2_amt", "mtge2_doc_id", "days_between", "amt_increase", "borrower_changed"],
        "string": ["bbl", "mtge1_doc_id", "mtge2_doc_id"],
        "dates": ["mtge1_date", "mtge2_date"],
        "keys": ["bbl", "mtge1_doc_id", "mtge2_doc_id"],
    },
    "signal_party_overlap_exclusions": {
        "file": "signal_party_overlap_exclusions.csv",
        "columns": ["bbl", "deed_doc_id", "deed_date", "deed_amt", "mtge_doc_id", "mtge_date",
                    "mtge_amt", "days_between", "mortgage_to_price_ratio", "deed_buyer_names", "mtge_doc_names"],
        "string": ["bbl", "deed_doc_id", "mtge_doc_id"],
        "dates": ["deed_date", "mtge_date"],
        "keys": ["bbl", "deed_doc_id", "mtge_doc_id"],
    },
    "property_geo": {
        "file": "property_geo.csv",
        "columns": ["bbl", "lat", "lon", "precision"],
        "string": ["bbl", "precision"],
        "dates": [],
        "keys": ["bbl"],
    },
}


def connect(target_neon: bool):
    if target_neon:
        dsn = getpass.getpass("Paste Neon direct connection URL (input hidden): ").strip()
        if not dsn or "-pooler." in dsn:
            raise SystemExit("Use Neon’s direct (non-pooler) connection URL for this bulk asset load.")
        return psycopg2.connect(dsn)
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "phantom_equity"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def prepare_rows(spec):
    path = DATA / spec["file"]
    if not path.exists():
        raise FileNotFoundError(f"Required dashboard input is missing: {path}")
    df = pd.read_csv(path, dtype={c: "string" for c in spec["string"]}, low_memory=False)
    source_rows = len(df)
    missing = sorted(set(spec["columns"]) - set(df.columns))
    if missing:
        raise ValueError(f"{path.name} is missing required columns: {', '.join(missing)}")
    df = df[spec["columns"]].copy().drop_duplicates()
    if df.duplicated(spec["keys"]).any():
        raise ValueError(f"{path.name} has conflicting rows with the same natural key")
    for column in spec["dates"]:
        df[column] = pd.to_datetime(df[column], errors="coerce").dt.date
    rows = []
    for row in df.itertuples(index=False, name=None):
        clean = []
        for value in row:
            if pd.isna(value):
                clean.append(None)
            elif hasattr(value, "item"):
                clean.append(value.item())
            else:
                clean.append(value)
        rows.append(tuple(clean))
    return path, rows, source_rows


def load_assets(conn):
    assets = {name: prepare_rows(spec) for name, spec in ASSETS.items()}
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT to_regclass('public.signal_serial_refinancing'),
                       to_regclass('public.signal_party_overlap_exclusions'),
                       to_regclass('public.property_geo'),
                       to_regclass('public.dashboard_asset_meta')
            """)
            if any(value is None for value in cur.fetchone()):
                raise RuntimeError("Dashboard tables are missing; apply migrations/004_dashboard_assets_and_reviews.sql first.")
            for name, (path, rows, source_rows) in assets.items():
                columns = ASSETS[name]["columns"]
                cur.execute(f"TRUNCATE TABLE {name}")
                placeholders = ", ".join(["%s"] * len(columns))
                col_sql = ", ".join(columns)
                if rows:
                    psycopg2.extras.execute_values(
                        cur,
                        f"INSERT INTO {name} ({col_sql}) VALUES %s",
                        rows,
                        template=f"({placeholders})",
                        page_size=1000,
                    )
                cur.execute("""
                    INSERT INTO dashboard_asset_meta (asset_name, loaded_at, row_count)
                    VALUES (%s, now(), %s)
                    ON CONFLICT (asset_name) DO UPDATE
                    SET loaded_at = EXCLUDED.loaded_at, row_count = EXCLUDED.row_count
                """, (name, len(rows)))
                duplicate_count = source_rows - len(rows)
                suffix = f"; skipped {duplicate_count:,} exact duplicate rows" if duplicate_count else ""
                print(f"Loaded {len(rows):,} rows into {name} from {path.name}{suffix}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-neon", action="store_true",
                        help="Prompt for a Neon direct URL instead of using local .env settings.")
    args = parser.parse_args()
    conn = connect(args.target_neon)
    try:
        load_assets(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
