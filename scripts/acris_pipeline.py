"""
ACRIS pull + role decode + high-volume-filer flagging for the
real-estate-fraud-detection project.

Pipeline:
  1. Pull a sample from ACRIS Master, Legals, Parties (Socrata).
  2. Join Parties -> Master on document_id, decode each party's role
     using the real ACRIS Document Control Codes table (doc_control_codes.py).
  3. Flag entity-like names (LLC/Corp/Inc/LP/Trust) and normalize them.
  4. Flag "high-volume filers" -- names appearing on an abnormal number of
     DEED-class documents in the window (e.g. bulk timeshare operators).
     This is a NOISE FLAG, not a fraud flag: high volume alone means
     "investigate why", it does not mean "this is illegitimate". Real fraud
     rings might also be high-volume, so don't just silently drop these --
     inspect the flagged list before excluding anyone from downstream analysis.

Requires: pip install requests pandas
"""

import requests
import pandas as pd
import re
import os
from pathlib import Path
from datetime import datetime, timedelta
from doc_control_codes import DOC_CONTROL_CODES, role_bucket
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- Config ---
SAMPLE_SIZE = None           # no longer used directly -- see PAGE_SIZE / MAX_TOTAL_ROWS
PAGE_SIZE = 50000            # Socrata's practical per-request cap
MAX_TOTAL_ROWS = 400000      # safety cap so a pull can't run away; raise deliberately if needed
DAYS_BACK = 365
DOC_TYPES = None

HIGH_VOLUME_THRESHOLD = 10   # a name appearing on >N deed-class documents in the
                              # window gets flagged. Tune this once you see the
                              # distribution -- 10 is a starting guess, not a
                              # validated cutoff.

BASE = "https://data.cityofnewyork.us/resource/{}.json"
MASTER_ID = "bnx9-e6tj"
LEGALS_ID = "8h5j-fqxa"
PARTIES_ID = "636b-3b5g"

APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "").strip()


def get_headers():
    return {"X-App-Token": APP_TOKEN} if APP_TOKEN else {}


def get_with_retry(url, params, timeout, max_retries=4):
    """A pull this size takes several minutes -- a single transient timeout
    shouldn't force restarting the whole thing from scratch. Retries with
    exponential backoff on connection/timeout errors only; a real HTTP error
    (bad query, 4xx/5xx) still raises immediately via raise_for_status()."""
    import time
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=get_headers(), timeout=timeout)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            last_exc = e
            if attempt == max_retries:
                break
            wait = 5 * (2 ** (attempt - 1))  # 5s, 10s, 20s, 40s
            print(f"  [retry] connection issue (attempt {attempt}/{max_retries}), "
                  f"waiting {wait}s: {type(e).__name__}")
            time.sleep(wait)
    raise last_exc


def fetch_master_sample():
    cutoff = (datetime.utcnow() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT00:00:00")
    where_clauses = [f"recorded_datetime > '{cutoff}'"]
    if DOC_TYPES:
        types = ",".join(f"'{t}'" for t in DOC_TYPES)
        where_clauses.append(f"doc_type in ({types})")
    where = " AND ".join(where_clauses)

    frames = []
    offset = 0
    while True:
        params = {
            "$where": where,
            "$order": "recorded_datetime DESC",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        }
        resp = get_with_retry(BASE.format(MASTER_ID), params, timeout=120)
        page = pd.DataFrame(resp.json())
        print(f"[master] page at offset {offset}: {len(page)} rows")
        if page.empty:
            break
        frames.append(page)
        offset += len(page)
        if len(page) < PAGE_SIZE:
            break  # last page
        if offset >= MAX_TOTAL_ROWS:
            print(f"[master] hit MAX_TOTAL_ROWS={MAX_TOTAL_ROWS} safety cap, stopping "
                  f"(there may be more rows in the {DAYS_BACK}-day window -- raise the cap deliberately if needed)")
            break

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    print(f"[master] pulled {len(df)} total rows across {len(frames)} page(s), "
          f"covering up to {DAYS_BACK} days back (actual coverage depends on volume vs. the cap)")
    return df


def fetch_related(dataset_id, document_ids, label):
    import time
    chunk_size = 500  # kept conservative -- larger chunks risk exceeding Socrata's query length limits
    frames = []
    n_chunks = (len(document_ids) + chunk_size - 1) // chunk_size
    for chunk_num, i in enumerate(range(0, len(document_ids), chunk_size), start=1):
        chunk = document_ids[i:i + chunk_size]
        ids = ",".join(f"'{d}'" for d in chunk)
        params = {"$where": f"document_id in ({ids})", "$limit": 50000}
        resp = get_with_retry(BASE.format(dataset_id), params, timeout=120)
        frames.append(pd.DataFrame(resp.json()))
        if chunk_num % 10 == 0 or chunk_num == n_chunks:
            print(f"[{label}] chunk {chunk_num}/{n_chunks} done")
        time.sleep(0.2)  # be polite to the API given the much higher chunk count now
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    print(f"[{label}] pulled {len(df)} rows for {len(document_ids)} documents")
    return df


def normalize_name(n):
    n = str(n).upper().strip()
    n = re.sub(r"[.,]", "", n)
    n = re.sub(r"\s+", " ", n)
    return n


def get_role(doc_type, party_type):
    entry = DOC_CONTROL_CODES.get(doc_type)
    if entry is None:
        return "UNKNOWN_DOC_TYPE"
    _, p1, p2, p3 = entry
    if party_type == 1:
        return p1 or "N/A"
    elif party_type == 2:
        return p2 or "N/A"
    elif party_type == 3:
        return p3 or "N/A"
    return "UNEXPECTED_PARTY_TYPE"


INSTITUTIONAL_PATTERN = re.compile(
    r"\bBANK\b|TRUST COMPANY|NATIONAL ASSOCIATION|\bN\.?A\.?\b|\bFSB\b|"
    r"SAVINGS FUND|MORTGAGE|\bFEDERAL\b|FANNIE MAE|FREDDIE MAC",
    re.IGNORECASE,
)
GOVERNMENT_PATTERN = re.compile(
    r"CITY OF NEW YORK|METROPOLITAN TRANSPORTATION|STATE OF NEW YORK|\bHDFC\b|"
    r"HOUSING DEVELOPMENT FUND",
    re.IGNORECASE,
)
TIMESHARE_PATTERN = re.compile(
    r"TIMESHARE|VACATION OWNERS|RESORT",
    re.IGNORECASE,
)


def classify_filer(name, count, is_entity_flag, threshold=None):
    threshold = HIGH_VOLUME_THRESHOLD if threshold is None else threshold
    if count <= threshold:
        return "NORMAL"
    if TIMESHARE_PATTERN.search(name):
        return "TIMESHARE_NOISE"
    if INSTITUTIONAL_PATTERN.search(name):
        return "INSTITUTIONAL_LENDER_NOISE"
    if GOVERNMENT_PATTERN.search(name):
        return "GOVERNMENT_AGENCY_NOISE"
    if is_entity_flag:
        # A name flagged is_entity solely because it contains "TRUSTEE" is still a
        # natural person, not a corporate sponsor -- and repeat individual trustees
        # are a named threat pattern (nominee structures obscuring true ownership),
        # not noise to exclude. Give them their own retained category.
        if re.search(r"\bTRUSTEE\b", name, re.IGNORECASE):
            return "PROLIFIC_TRUSTEE"
        # High-volume entity that isn't a recognized lender/govt/timeshare pattern --
        # most likely a condo/co-op sponsor or developer selling units one deed at a
        # time. Still probably noise for a shell-network signal, but flagged under its
        # own label (not lumped into "timeshare") so it stays auditable/overridable.
        return "BULK_ENTITY_FILER"
    # Not an entity suffix, not institutional, not government, still high-volume ->
    # a real person appearing on many separate deed transactions. NEVER auto-exclude
    # this category -- this is a candidate signal, not noise.
    return "PROLIFIC_INDIVIDUAL"


def decode_and_flag(master, parties):
    merged = parties.merge(master[["document_id", "doc_type", "document_amt"]], on="document_id", how="left")
    # A stable, explicit row identifier -- survives every downstream .copy()/filter/to_csv(index=False)
    # in entity_resolution.py, unlike the pandas index (which to_csv drops) or a name-based key (which
    # isn't guaranteed unique). This is what lets the ETL join document_parties rows back to their
    # entity-resolution cluster assignment without ambiguity.
    merged["party_row_id"] = range(len(merged))
    # Socrata's JSON API returns numeric fields (like party_type) as strings,
    # not ints -- cast explicitly or every role lookup silently fails.
    merged["party_type"] = pd.to_numeric(merged["party_type"], errors="coerce").astype("Int64")
    merged["role"] = merged.apply(lambda r: get_role(r["doc_type"], r["party_type"]), axis=1)
    merged["role_bucket"] = merged["role"].apply(role_bucket)
    merged["is_entity"] = merged["name"].astype(str).str.contains(
        r"LLC|L\.L\.C|CORP|INC\b|\bLP\b|LIMITED PARTNERSHIP|LIMITED LIABILITY|"
        r"\bTRUST|\bASSOCIATION\b|\bAUTHORITY\b|\bCOMPANY\b|\bFUND\b|"
        r"\bPARTNERS\b|\bHOLDINGS\b|\bREALTY\b|\bPROPERTIES\b",
        case=False, na=False, regex=True
    )
    merged["name_norm"] = merged["name"].apply(normalize_name)

    unresolved = merged["role"].isin(["UNKNOWN_DOC_TYPE", "N/A", "UNEXPECTED_PARTY_TYPE"])
    print(f"Role decode: {unresolved.sum()} / {len(merged)} rows unresolved "
          f"({unresolved.sum()/len(merged)*100:.1f}%)")
    if unresolved.sum() > 0:
        print("Unresolved doc_types (send these -- table may need updating):",
              merged.loc[unresolved, "doc_type"].value_counts().to_dict())

    print("\n=== Role bucket breakdown (all doc types) ===")
    print(merged["role_bucket"].value_counts())

    print("\n=== Role breakdown by doc_category ===")
    merged["doc_category"] = merged["doc_type"].map(lambda dt: DOC_CONTROL_CODES.get(dt, (None,))[0])
    print(merged.groupby(["doc_category", "role"]).size().sort_values(ascending=False).head(30))

    # --- High-volume filer classification (DEED-class documents only) ---
    # A single "exclude everyone above threshold N" rule breaks down at scale: it lumps
    # together several genuinely different populations, one of which (prolific individual
    # filers) is exactly what a phantom-equity analysis should be looking AT, not filtering OUT.
    deed_types = [dt for dt, v in DOC_CONTROL_CODES.items() if v[0] == "DEEDS AND OTHER CONVEYANCES"]
    deed_rows = merged[merged["doc_type"].isin(deed_types)].copy()
    filer_counts = deed_rows["name_norm"].value_counts()
    high_volume_names = filer_counts[filer_counts > HIGH_VOLUME_THRESHOLD].index.tolist()

    name_to_count = filer_counts.to_dict()
    name_to_is_entity = merged.drop_duplicates("name_norm").set_index("name_norm")["is_entity"].to_dict()
    filer_class = {
        name: classify_filer(name, name_to_count.get(name, 0), name_to_is_entity.get(name, False))
        for name in high_volume_names
    }

    merged["filer_category"] = merged["name_norm"].map(filer_class).fillna("NORMAL")

    NOISE_CATEGORIES = {"TIMESHARE_NOISE", "INSTITUTIONAL_LENDER_NOISE", "GOVERNMENT_AGENCY_NOISE", "BULK_ENTITY_FILER"}
    merged["is_high_volume_filer"] = merged["filer_category"].isin(NOISE_CATEGORIES)  # kept for backward compat

    print(f"\n{len(high_volume_names)} names above the high-volume threshold "
          f"(>{HIGH_VOLUME_THRESHOLD} deed-class appearances), classified:")
    for cat in ["TIMESHARE_NOISE", "INSTITUTIONAL_LENDER_NOISE", "GOVERNMENT_AGENCY_NOISE",
                "BULK_ENTITY_FILER", "PROLIFIC_TRUSTEE", "PROLIFIC_INDIVIDUAL"]:
        names_in_cat = [n for n, c in filer_class.items() if c == cat]
        if not names_in_cat:
            continue
        tag = " <-- NOT excluded, flagged for review" if cat in ("PROLIFIC_TRUSTEE", "PROLIFIC_INDIVIDUAL") else ""
        print(f"\n  [{cat}] ({len(names_in_cat)}){tag}")
        for name in sorted(names_in_cat, key=lambda n: -filer_counts[n]):
            print(f"    {filer_counts[name]:>4}  {name}")

    print("\n--- Deed-class entities AFTER excluding noise categories only "
          "(PROLIFIC_INDIVIDUAL retained) ---")
    clean = deed_rows[deed_rows["is_entity"] & ~deed_rows["name_norm"].map(filer_class).isin(NOISE_CATEGORIES)]
    print(f"{len(clean)} entity-like party rows remain")
    print(clean["name_norm"].value_counts().head(25))

    return merged


def main():
    master = fetch_master_sample()
    if master.empty:
        print("No master rows returned.")
        return
    doc_ids = master["document_id"].dropna().unique().tolist()
    legals = fetch_related(LEGALS_ID, doc_ids, "legals")
    parties = fetch_related(PARTIES_ID, doc_ids, "parties")

    result = decode_and_flag(master, parties)

    import os
    raw_dir = os.path.join("..", "data", "raw")
    processed_dir = os.path.join("..", "data", "processed")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(processed_dir, exist_ok=True)

    master.to_csv(os.path.join(raw_dir, "acris_master_sample.csv"), index=False)
    legals.to_csv(os.path.join(raw_dir, "acris_legals_sample.csv"), index=False)
    result.to_csv(os.path.join(processed_dir, "acris_parties_analyzed.csv"), index=False)
    print(f"\nWrote {raw_dir}/acris_master_sample.csv, {raw_dir}/acris_legals_sample.csv, "
          f"{processed_dir}/acris_parties_analyzed.csv")


if __name__ == "__main__":
    main()
