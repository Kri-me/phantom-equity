"""
ACRIS sample pull for the real-estate-fraud-detection project.

Pulls a recent sample from the three ACRIS Real Property datasets
(Master, Legals, Parties), joins them on document_id, and writes
a single CSV + a quick data-quality summary to the console.

Requires: pip install requests pandas
"""

import requests
import pandas as pd
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- Config ---
SAMPLE_SIZE = 5000          # rows pulled from Master (Legals/Parties pulled via matching document_id)
DAYS_BACK = 180              # how far back to sample from, to get "recent" transactions
DOC_TYPES = None             # e.g. ["DEED", "MTGE"] to filter, or None for all types

BASE = "https://data.cityofnewyork.us/resource/{}.json"
MASTER_ID = "bnx9-e6tj"
LEGALS_ID = "8h5j-fqxa"
PARTIES_ID = "636b-3b5g"

# NYC Open Data works without a token at lower request limits. Set
# SOCRATA_APP_TOKEN in the project-root .env to improve request limits.
APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "").strip()


def get_headers():
    return {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}


def fetch_master_sample():
    cutoff = ((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).replace(tzinfo=None).strftime("%Y-%m-%dT00:00:00"))
    where_clauses = [f"recorded_datetime > '{cutoff}'"]
    if DOC_TYPES:
        types = ",".join(f"'{t}'" for t in DOC_TYPES)
        where_clauses.append(f"doc_type in ({types})")
    where = " AND ".join(where_clauses)

    params = {
        "$where": where,
        "$order": "recorded_datetime DESC",
        "$limit": SAMPLE_SIZE,
    }
    resp = requests.get(BASE.format(MASTER_ID), params=params, headers=get_headers(), timeout=60)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json())
    print(f"[master] pulled {len(df)} rows")
    return df


def fetch_related(dataset_id, document_ids, label):
    # Socrata doesn't support arbitrarily long IN clauses well, so chunk it.
    chunk_size = 500
    frames = []
    for i in range(0, len(document_ids), chunk_size):
        chunk = document_ids[i:i + chunk_size]
        ids = ",".join(f"'{d}'" for d in chunk)
        params = {
            "$where": f"document_id in ({ids})",
            "$limit": 50000,
        }
        resp = requests.get(BASE.format(dataset_id), params=params, headers=get_headers(), timeout=60)
        resp.raise_for_status()
        frames.append(pd.DataFrame(resp.json()))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    print(f"[{label}] pulled {len(df)} rows for {len(document_ids)} documents")
    return df


def summarize(df, name):
    print(f"\n=== {name}: shape {df.shape} ===")
    if df.empty:
        print("EMPTY — check filters/date range/connectivity")
        return
    null_pct = (df.isna().mean() * 100).round(1).sort_values(ascending=False)
    print("Null % by column (top 10):")
    print(null_pct.head(10))
    print("\nSample rows:")
    print(df.head(3).to_string())


def main():
    master = fetch_master_sample()
    if master.empty:
        print("No master rows returned — check date range, doc type filters, or connectivity.")
        return

    doc_ids = master["document_id"].dropna().unique().tolist()

    legals = fetch_related(LEGALS_ID, doc_ids, "legals")
    parties = fetch_related(PARTIES_ID, doc_ids, "parties")

    summarize(master, "MASTER")
    summarize(legals, "LEGALS")
    summarize(parties, "PARTIES")

    # Basic party-type breakdown — tells you how much of this is LLC/corp vs individual
    if not parties.empty and "party_type" in parties.columns:
        print("\nParty type counts:")
        print(parties["party_type"].value_counts())
    if not parties.empty and "name" in parties.columns:
        llc_like = parties["name"].str.contains(r"LLC|L\.L\.C|CORP|INC|LP\b", case=False, na=False, regex=True)
        print(f"\n{llc_like.sum()} / {len(parties)} party names look like entities (LLC/Corp/Inc/LP)")

    master.to_csv("acris_master_sample.csv", index=False)
    legals.to_csv("acris_legals_sample.csv", index=False)
    parties.to_csv("acris_parties_sample.csv", index=False)
    print("\nWrote acris_master_sample.csv, acris_legals_sample.csv, acris_parties_sample.csv")


if __name__ == "__main__":
    main()
