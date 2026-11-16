"""
pluto_attributes.py -- lot and building attributes from NYC PLUTO, for the valuation benchmark.

SOURCE: NYC Open Data, "Primary Land Use Tax Lot Output (PLUTO)", Socrata dataset 64uk-42ks.
Same download machinery as pluto_geocode.py (shared keep-alive connection, retries, page cache);
this script only asks for different columns.

WHAT YOU GET (data/processed/property_attributes.csv), one row per project BBL:
    bbl, attr_status, lot_number, in_pluto, and the PLUTO fields below (blank where unavailable).

    attr_status = "pluto_lot"                    exact BBL found in PLUTO -> attributes usable
                  "condo_unit_lot_not_in_pluto"  not in PLUTO and lot >= 1001 (the range typically used
                                                 for condo units; PLUTO holds ONE record per condo
                                                 COMPLEX, so unit lots are absent)
                  "not_in_pluto_other"           not in PLUTO for some other reason
    Rows that are not "pluto_lot" have no attributes. They are kept in the file on purpose, so the
    coverage of any model built on it is explicit instead of silently shrinking.

FIELDS: lot/building size (lotarea, bldgarea, resarea, comarea, numfloors, numbldgs, lotfront,
lotdepth, bldgfront, bldgdepth), use (unitsres, unitstotal, landuse, bldgclass, zonedist1), age
(yearbuilt), condo marker (condono), designations (histdist, landmark) and assessed values
(assessland, assesstot). The field list is checked against the live dataset at run time; any field
that no longer exists is skipped with a warning rather than breaking the download.

READ BEFORE MODELLING:
  * assessed value is NOT market value and varies by tax class. It is also a leakage risk when the
    target is a sale price: the city's valuations are partly built from sales. Keep assesstot/assessland
    out of the first model, or add them only as a clearly labelled ablation.
  * PLUTO is a CURRENT snapshot (updated roughly monthly), not point-in-time. A sale from months ago
    is described by today's building data.
  * yearbuilt is 0 when unknown; this script turns that into blank. bldgarea of 0 is kept (vacant lot).
  * PLUTO's own documentation notes that building-area errors are rarely corrected.

USAGE (from the scripts folder, inside the activated venv; needs pluto_geocode.py beside it):
    python pluto_attributes.py
    python pluto_attributes.py --refresh     # discard cached pages and re-download
If the download is interrupted, re-run it: finished pages are cached in data/raw/pluto_pages.
"""

import gzip
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request

import pandas as pd

import pluto_geocode as pg

ATTR_PAGE_SIZE = 10000  # wider rows than the coordinates pull, so smaller pages

WANTED_FIELDS = [
    "lotarea", "bldgarea", "resarea", "comarea", "numfloors", "numbldgs",
    "lotfront", "lotdepth", "bldgfront", "bldgdepth",
    "unitsres", "unitstotal", "landuse", "bldgclass", "zonedist1",
    "yearbuilt", "condono", "histdist", "landmark",
    "assessland", "assesstot",
]
TEXT_FIELDS = {"landuse", "bldgclass", "zonedist1", "histdist", "landmark"}


def discover_fields():
    """Column names that actually exist in the live dataset (lower-case), from the dataset's metadata.
    Returns None if the metadata cannot be read. There is deliberately NO guessing from sample rows:
    Socrata omits null values from each row, so a sample would wrongly make sparse columns look absent."""
    parsed = urllib.parse.urlparse(pg.DATASET_URL)
    meta_url = f"{parsed.scheme}://{parsed.netloc}/api/views/{pg.DATASET_ID}.json"
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(meta_url, timeout=pg.READ_TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
            cols = json.loads(raw.decode("utf-8")).get("columns", [])
            names = {c["fieldName"].lower() for c in cols if "fieldName" in c}
            if names:
                return names
        except Exception as exc:
            print(f"  could not read dataset metadata (attempt {attempt}/3): {exc}")
            time.sleep(2 * attempt)
    return None


def choose_fields(available):
    """(fields to request, fields skipped). `bbl` is always requested and must exist."""
    if available is None:
        print("  WARNING: could not verify field names against the live dataset; requesting them all.\n"
              "  If the download then fails with a 'no such column' error, a field was renamed:\n"
              "  remove it from WANTED_FIELDS at the top of this script and re-run.")
        return ["bbl"] + WANTED_FIELDS, []
    if "bbl" not in available:
        raise SystemExit("The dataset has no 'bbl' column; PLUTO's layout may have changed. Aborting.")
    present = [f for f in WANTED_FIELDS if f in available]
    return ["bbl"] + present, [f for f in WANTED_FIELDS if f not in available]


def build_attribute_table(rows, fields):
    """Raw Socrata rows -> one clean row per PLUTO lot, keyed by this project's BBL format."""
    df = pd.DataFrame(rows).reindex(columns=fields)  # Socrata omits nulls, so reindex guarantees columns
    n_raw = len(df)
    df["bbl"] = df["bbl"].map(pg.normalize_bbl)
    df = df.dropna(subset=["bbl"]).drop_duplicates("bbl")
    for col in fields:
        if col == "bbl":
            continue
        if col in TEXT_FIELDS:
            df[col] = df[col].astype("string").str.strip().replace("", pd.NA)
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "yearbuilt" in df.columns:
        df.loc[df["yearbuilt"] == 0, "yearbuilt"] = pd.NA  # 0 means unknown, not the year 0
    print(f"  {n_raw:,} raw rows -> {len(df):,} PLUTO lots with a usable BBL")
    return df.reset_index(drop=True)


def resolve_project(bbls, attrs):
    """Left-join PLUTO attributes onto the project's BBLs and label why any are missing."""
    out = pd.DataFrame({"bbl": sorted(set(bbls))})
    out["lot_number"] = pd.to_numeric(out["bbl"].str.rsplit("-", n=1).str[-1], errors="coerce")
    out = out.merge(attrs, how="left", on="bbl", indicator="_m")
    out["in_pluto"] = out["_m"] == "both"
    out["attr_status"] = "not_in_pluto_other"
    out.loc[~out["in_pluto"] & (out["lot_number"] >= 1001), "attr_status"] = "condo_unit_lot_not_in_pluto"
    out.loc[out["in_pluto"], "attr_status"] = "pluto_lot"
    first = ["bbl", "attr_status", "lot_number", "in_pluto"]
    return out.drop(columns="_m")[first + [c for c in out.columns if c not in first + ["_m"]]]


def report(out):
    n = len(out)
    print(f"\nProject properties: {n:,}")
    for status, count in out["attr_status"].value_counts().items():
        print(f"  {status:30s} {count:>8,} ({count / n:.1%})")
    have = out[out["in_pluto"]]
    if len(have):
        print(f"\nAmong the {len(have):,} found in PLUTO:")
        for label, mask in [
            ("building area > 0", have.get("bldgarea", pd.Series(dtype=float)) > 0),
            ("year built known", have.get("yearbuilt", pd.Series(dtype=float)).notna()),
            ("residential units > 0", have.get("unitsres", pd.Series(dtype=float)) > 0),
            ("lot area > 0", have.get("lotarea", pd.Series(dtype=float)) > 0),
            ("condo complex record", have.get("condono", pd.Series(dtype=float)) > 0),
        ]:
            print(f"  {label:24s} {int(mask.sum()):>8,} ({mask.mean():.1%})")
    print("\nCoverage by borough (1 Manhattan, 2 Bronx, 3 Brooklyn, 4 Queens, 5 Staten Island):")
    by = out.assign(boro=out["bbl"].str[0]).groupby("boro")["in_pluto"].agg(["size", "mean"])
    for boro, r in by.iterrows():
        print(f"  borough {boro}: {int(r['size']):>7,} properties, {r['mean']:.1%} in PLUTO")


def main():
    pg.OUT_DIR.mkdir(parents=True, exist_ok=True)
    pg.PAGE_SIZE = ATTR_PAGE_SIZE
    print("Checking PLUTO field names against the live dataset...")
    fields, skipped = choose_fields(discover_fields())
    if skipped:
        print(f"  skipping fields not in the dataset: {', '.join(skipped)}")
    select = ",".join(fields)
    tag = "attr_" + hashlib.md5(select.encode()).hexdigest()[:8] + "_"  # new field list -> new cache

    print(f"Downloading PLUTO attributes ({len(fields) - 1} fields, dataset {pg.DATASET_ID})...")
    rows = pg.fetch_all_pluto(refresh="--refresh" in sys.argv, select=select,
                              where="bbl IS NOT NULL", cache_tag=tag)
    attrs = build_attribute_table(rows, fields)

    bbls = pg.project_bbls()
    if bbls is None:
        attrs.to_csv(pg.OUT_DIR / "pluto_lots_attrs.csv", index=False)
        print("Wrote pluto_lots_attrs.csv only. Fix the database connection and re-run for property_attributes.csv.")
        return
    out = resolve_project(bbls, attrs)
    out.to_csv(pg.OUT_DIR / "property_attributes.csv", index=False)
    report(out)
    print(f"\nWrote {pg.OUT_DIR / 'property_attributes.csv'}")


if __name__ == "__main__":
    main()
