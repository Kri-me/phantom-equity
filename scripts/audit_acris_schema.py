"""
audit_acris_schema.py -- read-only audit of this project's ACRIS data against the official documentation.

Answers five questions about the data as it actually sits on disk and in Postgres:
  1. Which official Master fields are present (document_date, recorded_datetime, modified_date, crfn,
     document_amt, percent_trans...) in the raw CSVs and in the database tables?
  2. Is the "transaction" relationship preserved anywhere? ACRIS publishes no transaction-number field;
     documents recorded together share the first 13 characters of document_id. This tests that
     convention on the real data (does the id's date match recorded_datetime? how big are bundles?).
  3. Which document types do the detectors use, and what do the official descriptions say they are?
  4. Is the pipeline able to tell ordinary deeds from correction deeds and mortgage instruments, and
     can referee's / executor's / administrator's deeds be identified? (No ACRIS doc type exists for
     those, so party names are scanned.)
  5. Does the Legals data (partial_lot, easement, air_rights, subterranean_rights, multi-lot rows) carry
     information the pipeline currently throws away?

Nothing is modified. Optional pieces (database, live control-code diff) are skipped with a note if they
cannot run.

USAGE (scripts folder, activated venv; signals.py and pluto_geocode.py beside it):
    python audit_acris_schema.py
"""

import os
import re
import sys
import urllib.request
import json

import pandas as pd

import pluto_geocode as pg

RAW = pg.ROOT / "data" / "raw"
OUT = pg.OUT_DIR

# --- official field lists (ACRIS - Real Property Master / Legals dictionaries, NYC Open Data) -----------
OFFICIAL_MASTER = ["document_id", "record_type", "crfn", "recorded_borough", "doc_type", "document_date",
                   "document_amt", "recorded_datetime", "modified_date", "reel_yr", "reel_nbr", "reel_pg",
                   "percent_trans", "good_through_date"]
OFFICIAL_LEGALS = ["document_id", "record_type", "borough", "block", "lot", "easement", "partial_lot",
                   "air_rights", "subterranean_rights", "property_type", "street_number", "street_name",
                   "unit", "good_through_date"]
LEGALS_ALIASES = {"unit": {"unit", "addr_unit"}}
KEY_DB = {"documents": ["crfn", "modified_date", "document_amt", "document_date", "recorded_datetime", "percent_trans"],
          "document_legals": ["partial_lot", "easement", "air_rights", "subterranean_rights"]}

# --- the 34 codes in the official class "DEEDS AND OTHER CONVEYANCES", with official descriptions -------
DEED_CLASS = {
    "DEED": ("DEED", "ordinary conveyance"), "DEEDO": ("DEED, OTHER", "ordinary conveyance"),
    "DEEDP": ("DEED, PRE RPT TAX", "ordinary conveyance"), "DEED, RC": ("DEED WITH RESTRICTIVE COVENANT", "ordinary conveyance"),
    "DEED, LE": ("LIFE ESTATE DEED", "ordinary conveyance"), "IDED": ("IN REM DEED", "ordinary conveyance"),
    "REIT": ("REAL ESTATE INV TRUST DEED", "ordinary conveyance"),
    "CORRD": ("CORRECTION DEED", "correction / restatement"), "DEED COR": ("CORRECT INDEX/DEED-OFFICE USE", "correction / restatement"),
    "CORR, LE": ("CORRECT LIFE ESTATE OFFICE USE", "correction / restatement"), "SI CORR": ("SI BILLING UPDATE OFFICE USE", "correction / restatement"),
    "CONDEED": ("CONFIRMATORY DEED", "correction / restatement"),
    "CNTR": ("CONTRACT OF SALE", "contract"), "MCON": ("MEMORANDUM OF CONTRACT", "contract"), "ACON": ("ASSIGN/TERM OF CONTRACT/BID", "contract"),
    "LEAS": ("LEASE", "lease"), "MLEA": ("MEMORANDUM OF LEASE", "lease"), "SUBL": ("SUBORDINATION OF LEASE", "lease"),
    "EASE": ("EASEMENT", "easement / air / development rights"), "AIRRIGHT": ("AIR RIGHTS", "easement / air / development rights"),
    "DEVR": ("DEVELOPMENT RIGHTS", "easement / air / development rights"),
    "CDEC": ("CONDO DECLARATION", "declaration / merger"), "SCDEC": ("DECLARATION OF CONDO IN CONDO", "declaration / merger"),
    "DECM": ("DECLARATION OF MERGER", "declaration / merger"),
    "CODP": ("CONDEMNATION PROCEEDINGS", "court / government"), "NAPP": ("NOTICE OF APPROPRIATION", "court / government"),
    "DCTO": ("COURT ORDER ADVERSE POSS.", "court / government"), "VAC": ("VACATE ORDER", "court / government"),
    "TODD": ("TRANSFER ON DEATH DEED", "transfer-on-death"), "RTOD": ("REVOCATION OF TOD DEED", "transfer-on-death"),
    "ASTU": ("UNIT ASSIGNMENT", "other"), "TORREN": ("TORREN", "other"), "LTPA": ("LETTERS  PATENT", "other"),
    "DEED, TS": ("TIMESHARE DEED", "other"),
}
MORTGAGE_CLASS_NOTES = {
    "MTGE": "MORTGAGE (the only mortgage type the detectors use)", "ASST": "ASSIGNMENT, MORTGAGE",
    "M&CON": "MORTGAGE AND CONSOLIDATION", "SPRD": "MORTGAGE SPREADER AGREEMENT", "CMTG": "COLLATERAL MORTGAGE",
    "AGMT": "AGREEMENT (generic)", "AL&R": "ASSIGNMENT OF LEASES AND RENTS", "XXXX": "APPRT BREAKDWN OFFICE USE ONLY",
    "APPRT": "APP. ORDER BREAKDWN OFFICE USE", "AMTX": "ADDITIONAL MORTGAGE TAX", "CORRM": "CORRECTION MORTGAGE",
    "ASPM": "ASSUMPTION OF MORTGAGE", "SMTG": "SUNDRY MORTGAGE", "MMTG": "MASTER MORTGAGE",
}
CAPACITY_TOKENS = ["REFEREE", "EXECUTOR", "EXECUTRIX", "ADMINISTRATOR", "ESTATE OF", "SHERIFF", "GUARDIAN",
                   "COMMITTEE", "RECEIVER", "TRUSTEE", "CONSERVATOR"]
CONVEYANCE_CODES = {c for c, (_, g) in DEED_CLASS.items() if g == "ordinary conveyance"}


def head(n, title):
    print(f"\n{'=' * 100}\n{n}. {title}\n{'=' * 100}")


def read_header(path):
    return list(pd.read_csv(path, nrows=0).columns) if path.exists() else None


def pct(a, b):
    return f"{a / b:.1%}" if b else "n/a"


# ---------------------------------------------------------------------------------------------
def check_fields():
    head(1, "FIELDS: official dictionary vs the raw CSVs and the database")
    for name, official, aliases in [("acris_master_sample.csv", OFFICIAL_MASTER, {}),
                                    ("acris_legals_sample.csv", OFFICIAL_LEGALS, LEGALS_ALIASES)]:
        cols = read_header(RAW / name)
        if cols is None:
            print(f"  {name}: NOT FOUND in {RAW}")
            continue
        have = set(cols)
        missing = [f for f in official if f not in have and not (aliases.get(f) and aliases[f] & have)]
        extra = [c for c in cols if c not in official and not any(c in a for a in aliases.values())]
        print(f"  {name}: {len(cols)} columns; official fields missing: {missing or 'none'}; extra: {extra or 'none'}")
    print("  The five fields you asked about, in the raw master file: "
          + ", ".join(f"{f}={'yes' if f in (read_header(RAW / 'acris_master_sample.csv') or []) else 'NO'}"
                      for f in ["document_date", "recorded_datetime", "modified_date", "crfn", "document_amt"]))
    check_db()


def check_db():
    try:
        import psycopg2
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=pg.ROOT / ".env", override=True)
        conn = psycopg2.connect(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
                                dbname=os.environ.get("PGDATABASE", "phantom_equity"), user=os.environ.get("PGUSER", "postgres"),
                                password=os.environ.get("PGPASSWORD", ""))
        conn.autocommit = True
    except Exception as exc:
        print(f"  database: not checked ({exc})")
        return
    cur = conn.cursor()
    for table, wanted in KEY_DB.items():
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
        cols = {r[0] for r in cur.fetchall()}
        if not cols:
            print(f"  database table {table}: NOT FOUND")
            continue
        print(f"  database table {table}: {len(cols)} columns; key fields present: "
              + ", ".join(f"{c}={'yes' if c in cols else 'NO'}" for c in wanted))
        for flag in [c for c in ("partial_lot", "easement", "air_rights", "subterranean_rights") if c in cols and table == "document_legals"]:
            cur.execute(f"SELECT {flag}::text, COUNT(*) FROM {table} GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
            print(f"    {flag} values stored: {cur.fetchall()}  (official: partial_lot is P/E; the others Y/N)")
    conn.close()


# ---------------------------------------------------------------------------------------------
def check_ids(master):
    head(2, "TRANSACTION RELATIONSHIP: is a bundle id preserved or derivable?")
    d = master.drop_duplicates("document_id").copy()
    print(f"  master rows: {len(master):,}; unique document_id: {len(d):,}; "
          f"duplicate ids: {len(master) - len(d):,}; id lengths: {dict(d['document_id'].str.len().value_counts())}")
    if "crfn" in d.columns:
        crfn = d["crfn"].dropna()
        print(f"  crfn present on {pct(len(crfn), len(d))} of documents; duplicated crfn values: {int(crfn.duplicated().sum()):,}")
    id_date = pd.to_datetime(d["document_id"].str[:8], format="%Y%m%d", errors="coerce")
    rec = d["recorded_datetime"].dt.normalize()
    ok = id_date.notna() & rec.notna()
    lag = (rec - id_date)[ok].dt.days
    print(f"  first 8 id characters parse as a date: {pct(int(id_date.notna().sum()), len(d))}")
    print(f"  that date equals recorded_datetime's date: {pct(int((lag == 0).sum()), int(ok.sum()))}; "
          f"within 1 day: {pct(int((lag.abs() <= 1).sum()), int(ok.sum()))}")
    doc_after = (d["document_date"].dt.normalize() > rec)
    print(f"  document_date later than the recording date (impossible if both were right): {int(doc_after.sum()):,}"
          f" ({pct(int(doc_after.sum()), len(d))})")
    old = d["document_date"] < pd.Timestamp("1990-01-01")
    print(f"  document_date before 1990: {int(old.sum()):,}; most repeated document_date values: "
          f"{dict(d['document_date'].value_counts().head(3))}")
    bundle = d["document_id"].str[:13]
    sizes = bundle.value_counts()
    same_day = d.assign(b=bundle).groupby("b")["recorded_datetime"].agg(lambda s: s.dt.normalize().nunique() == 1)
    print(f"  bundles (shared first 13 chars): {len(sizes):,}; documents per bundle: median {int(sizes.median())}, "
          f"max {int(sizes.max())}; multi-document bundles: {pct(int((sizes > 1).sum()), len(sizes))}")
    print(f"  bundles whose documents all share one recorded date: {pct(int(same_day.sum()), len(same_day))}"
          "  (a convention the data supports only if this is ~100%)")
    print("  VERDICT: no transaction-number column is stored; the relationship is derivable from document_id only.")
    return d


# ---------------------------------------------------------------------------------------------
def check_doc_types(master, signals_mod):
    head(3, "DOCUMENT TYPES: what the detectors call a 'deed', against the official descriptions")
    d = master.drop_duplicates("document_id")
    in_class = d[d["doc_type"].isin(DEED_CLASS)]
    floor = getattr(signals_mod, "MIN_REAL_PRICE", 10000)
    print(f"  signals.DEED_TYPES has {len(signals_mod.DEED_TYPES)} codes = the whole official class "
          f"'DEEDS AND OTHER CONVEYANCES' ({len(DEED_CLASS)} codes). Documents in it: {len(in_class):,}\n")
    print(f"  {'code':10s}{'official description':36s}{'group':36s}{'docs':>9s}{'amt>0':>9s}{f'>={floor:,}':>9s}")
    for code, (desc, group) in sorted(DEED_CLASS.items(), key=lambda kv: (kv[1][1], kv[0])):
        sub = d[d["doc_type"] == code]
        if len(sub):
            amt = pd.to_numeric(sub["document_amt"], errors="coerce")
            print(f"  {code:10s}{desc[:34]:36s}{group[:34]:36s}{len(sub):>9,}{pct(int((amt > 0).sum()), len(sub)):>9s}"
                  f"{pct(int((amt >= floor).sum()), len(sub)):>9s}")
    conv = int(in_class["doc_type"].isin(CONVEYANCE_CODES).sum())
    print(f"\n  ordinary conveyance codes: {conv:,} of {len(in_class):,} ({pct(conv, len(in_class))}); "
          f"the rest are contracts, leases, easements, declarations, corrections, court/government records.")
    mort = d[d["doc_type"].isin(MORTGAGE_CLASS_NOTES)]
    print("\n  mortgage-class documents (detectors use MTGE only): " +
          ", ".join(f"{c} {int((mort['doc_type'] == c).sum()):,}" for c in MORTGAGE_CLASS_NOTES if (mort['doc_type'] == c).any()))
    print("  'XXXX' and 'APPRT' are described officially as office-use apportionment breakdown records.")
    # impact on the existing candidate files
    types = d.set_index(d["document_id"].astype(str))["doc_type"]
    for fname, cols in [("signal_over_mortgaging.csv", ["deed_doc_id"]), ("signal_rapid_flips.csv", ["sale1_doc_id", "sale2_doc_id"])]:
        path = OUT / fname
        if not path.exists():
            continue
        s = pd.read_csv(path, dtype={c: str for c in cols})
        if "category" in s.columns:
            s = s[s["category"] == "candidate"]
        t = pd.concat([s[c] for c in cols]).map(types)
        non = t[~t.isin(CONVEYANCE_CODES)]
        print(f"  {fname}: {len(t):,} deed documents behind the candidates; NOT ordinary conveyances: {len(non):,}"
              f" {dict(non.value_counts()) if len(non) else ''}")


# ---------------------------------------------------------------------------------------------
def check_capacity(master, signals_mod):
    head(4, "DEED VARIANTS: can referee's, executor's, correction deeds be told apart?")
    print("  Official doc types: correction deeds exist (CORRD, DEED COR, CORR LE, SI CORR; CONDEED restates a prior deed).")
    print("  There is NO doc type for referee's, executor's, administrator's or guardian's deeds: they are")
    print("  recorded as DEED. The capacity appears only in the party names or the Remarks dataset.")
    try:
        _buyer, seller, _borrower, _all = signals_mod.load_party_lookups()
    except Exception as exc:
        print(f"  party lookups unavailable ({exc}); skipping the name scan.")
        return
    d = master.drop_duplicates("document_id")
    deeds = d[d["doc_type"].isin(CONVEYANCE_CODES)].copy()
    deeds["amt"] = pd.to_numeric(deeds["document_amt"], errors="coerce")
    print(f"\n  scanning grantor names on {len(deeds):,} ordinary-conveyance documents:")
    names = deeds["document_id"].map(lambda i: " | ".join(seller.get(int(i) if str(i).isdigit() else i, set())).upper())
    flagged = pd.Series(False, index=deeds.index)
    for tok in CAPACITY_TOKENS:
        hit = names.str.contains(tok, regex=False)
        flagged |= hit
        sub = deeds[hit]
        if len(sub):
            print(f"    {tok:14s}{len(sub):>8,} deeds   median amount {sub['amt'].median():>12,.0f}   "
                  f"nominal (<10k): {pct(int((sub['amt'] < 10000).sum()), len(sub))}")
    print(f"    any capacity token: {int(flagged.sum()):,} deeds ({pct(int(flagged.sum()), len(deeds))}); "
          f"median amount {deeds[flagged]['amt'].median():,.0f} vs {deeds[~flagged]['amt'].median():,.0f} for the rest")
    print("  The pipeline's own party classes (PROLIFIC_TRUSTEE etc.) are volume-based, not capacity-based.")


# ---------------------------------------------------------------------------------------------
def check_legals(master):
    head(5, "LEGALS: what the pipeline discards that could resolve multi-property and partial-interest transfers")
    path = RAW / "acris_legals_sample.csv"
    if not path.exists():
        print("  legals file not found")
        return
    lg = pd.read_csv(path, dtype={"document_id": str})
    print("  signals.load_and_join keeps only document_id, borough, block, lot, property_type and unit.")
    for flag in ("partial_lot", "easement", "air_rights", "subterranean_rights"):
        if flag in lg.columns:
            print(f"    {flag:20s}: {dict(lg[flag].astype('string').fillna('(blank)').value_counts().head(5))}")
        else:
            print(f"    {flag:20s}: column absent from the raw file")
    lg = lg.dropna(subset=["borough", "block", "lot"]).copy()
    lg["lot"] = pd.to_numeric(lg["lot"], errors="coerce")
    per_doc = lg.groupby("document_id").agg(n_lots=("lot", "size"), n_blocks=("block", "nunique"),
                                            lo=("lot", "min"), hi=("lot", "max"))
    types = master.drop_duplicates("document_id").set_index("document_id")["doc_type"]
    per_doc["doc_type"] = per_doc.index.map(types)
    deeds = per_doc[per_doc["doc_type"].isin(CONVEYANCE_CODES)]
    multi = deeds[deeds["n_lots"] > 1]
    print(f"\n  ordinary-conveyance deeds: {len(deeds):,}; covering more than one lot: {len(multi):,} ({pct(len(multi), len(deeds))})")
    if len(multi):
        same_block = multi[multi["n_blocks"] == 1]
        compact = same_block[(same_block["hi"] - same_block["lo"]) < same_block["n_lots"] + 2]
        print(f"    all lots on one block: {pct(len(same_block), len(multi))}; lots numerically adjacent (a likely single parcel): "
              f"{pct(len(compact), len(multi))}; two lots only: {pct(int((multi['n_lots'] == 2).sum()), len(multi))}")
        pt = lg[lg["document_id"].isin(multi.index)]["property_type"].value_counts().head(6)
        print(f"    property types on multi-lot deeds: {dict(pt)}")
    for flag in ("air_rights", "subterranean_rights", "easement", "partial_lot"):
        if flag in lg.columns:
            vals = lg[flag].astype("string").str.upper()
            hit = lg[vals.isin(["Y", "P"])]
            deeds_hit = hit[hit["document_id"].map(types).isin(CONVEYANCE_CODES)]
            print(f"    {flag}: {hit['document_id'].nunique():,} documents flagged, {deeds_hit['document_id'].nunique():,} of them ordinary deeds")
    print("  Legals gives no price per lot, so it can classify and group a multi-lot deed but cannot split its price.")


def check_percent_trans(master):
    d = master.drop_duplicates("document_id")
    d = d[d["doc_type"].isin(CONVEYANCE_CODES)]
    if "percent_trans" not in d.columns or d.empty:
        return
    p = pd.to_numeric(d["percent_trans"], errors="coerce")
    print(f"\n  percent_trans ('reported percentage of interest transferred') on {len(d):,} ordinary deeds: "
          f"missing {pct(int(p.isna().sum()), len(d))}, 0: {pct(int((p == 0).sum()), len(d))}, "
          f"100: {pct(int((p == 100).sum()), len(d))}, partial (0-100): {pct(int(p.between(0.001, 99.999).sum()), len(d))}, "
          f">100: {pct(int((p > 100).sum()), len(d))}")


def diff_control_codes(signals_mod):
    head(6, "CONTROL CODES: local doc_control_codes.py vs the live official table (optional, needs internet)")
    try:
        with urllib.request.urlopen("https://data.cityofnewyork.us/resource/7isb-wh4c.json?$limit=1000", timeout=60) as r:
            live = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print(f"  not checked ({exc})")
        return
    from doc_control_codes import DOC_CONTROL_CODES as local
    norm = lambda s: re.sub(r"\s+", " ", s or "").strip() or None
    off = {r["doc__type"]: (norm(r.get("class_code_description")), norm(r.get("party1_type")), norm(r.get("party2_type")), norm(r.get("party3_type"))) for r in live}
    loc = {k: (norm(v[0]), norm(v[1]), norm(v[2]), norm(v[3])) for k, v in local.items()}
    diffs = [k for k in set(off) & set(loc) if off[k] != loc[k]]
    print(f"  live codes {len(off)}, local codes {len(loc)}; only live: {sorted(set(off) - set(loc)) or 'none'}; "
          f"only local: {sorted(set(loc) - set(off)) or 'none'}; class/role differences: {sorted(diffs) or 'none'}")


def main():
    try:
        import signals as signals_mod
    except ImportError as exc:
        raise SystemExit(f"Could not import signals.py ({exc}); run this from the scripts folder.")
    check_fields()
    mpath = RAW / "acris_master_sample.csv"
    if not mpath.exists():
        raise SystemExit(f"{mpath} not found.")
    master = pd.read_csv(mpath, dtype={"document_id": str, "crfn": str})
    for c in ("document_date", "recorded_datetime", "modified_date"):
        if c in master.columns:
            master[c] = pd.to_datetime(master[c], errors="coerce")
    check_ids(master)
    check_doc_types(master, signals_mod)
    check_capacity(master, signals_mod)
    check_legals(master)
    check_percent_trans(master)
    diff_control_codes(signals_mod)
    print("\nAudit complete. Nothing was modified.")


if __name__ == "__main__":
    sys.exit(main())
