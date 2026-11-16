"""
valuation_coverage.py -- how far a valuation benchmark could reach, measured BEFORE building one.

Two questions the PLUTO coverage numbers cannot answer on their own:

  1. Of the properties we actually FLAGGED, how many have PLUTO attributes (so a model could give
     them an expected value) -- compared with the project-wide baseline? Condo units are absent
     from PLUTO, so if the flagged set is condo-heavy the model reaches fewer flagged cases than
     the 66% project-wide figure suggests.
  2. How many real SALES could train and test a model: deeds with a recorded price, on a property
     PLUTO describes, that cover a single property (a deed spanning many lots has one price for
     all of them and cannot be attributed to any one).

Reads (all produced by earlier steps):
    data/processed/property_attributes.csv       from pluto_attributes.py
    data/processed/signal_over_mortgaging.csv    from signals.py   (category == 'candidate')
    data/processed/signal_rapid_flips.csv        from signals.py   (category == 'candidate')
    data/processed/signal_serial_refinancing.csv from signals.py
    plus the raw ACRIS pull, via signals.load_and_join() (same loader the detectors use)
Writes:
    data/processed/valuation_sales.csv   one row per usable deed sale with its PLUTO attributes,
                                         the starting table for the model. Nothing is modelled here.

USAGE (from the scripts folder, inside the activated venv; signals.py and pluto_geocode.py beside it):
    python valuation_coverage.py
"""

import sys

import pandas as pd

import pluto_geocode as pg

SMALL_RES_MAX_UNITS = 4  # "small residential" = 1 to 4 residential units, with a building on the lot
EXTRA_PRICE_FLOORS = [100_000, 200_000]


def load_candidate_bbls(out_dir):
    """Distinct BBLs per signal source, using the same 'candidate' definition case_builder.py uses."""
    sources = {
        "over-mortgaging": ("signal_over_mortgaging.csv", True),
        "rapid flips": ("signal_rapid_flips.csv", True),
        "serial refinancing": ("signal_serial_refinancing.csv", False),
    }
    found = {}
    for label, (fname, has_category) in sources.items():
        path = out_dir / fname
        if not path.exists():
            print(f"  (missing {fname}; skipping {label})")
            continue
        df = pd.read_csv(path, dtype={"bbl": str})
        if has_category and "category" in df.columns:
            df = df[df["category"] == "candidate"]
        found[label] = set(df["bbl"].dropna())
    if found:
        found["any flagged signal"] = set().union(*found.values())
    return found


def add_flags(attrs):
    a = attrs.copy()
    units = pd.to_numeric(a.get("unitsres"), errors="coerce")
    area = pd.to_numeric(a.get("bldgarea"), errors="coerce")
    a["small_residential"] = a["in_pluto"] & units.between(1, SMALL_RES_MAX_UNITS) & (area > 0)
    return a


def coverage_row(bbls, attrs_by_bbl):
    sub = attrs_by_bbl.reindex(sorted(bbls))
    n = len(sub)
    known = sub["in_pluto"].notna()  # BBLs in the signal files but absent from property_attributes.csv
    return {
        "n": n,
        "in_pluto": int(sub["in_pluto"].fillna(False).sum()),
        "small_res": int(sub["small_residential"].fillna(False).sum()),
        "condo_unit": int((sub["attr_status"] == "condo_unit_lot_not_in_pluto").sum()),
        "not_listed": int((~known).sum()),
    }


def print_flagged(candidates, attrs):
    by_bbl = attrs.set_index("bbl")
    print("\nFlagged properties vs PLUTO attributes (distinct BBLs):")
    print(f"  {'':24s}{'n':>8s}  {'in PLUTO':>16s}  {'small residential':>19s}  {'condo-unit lots':>17s}")
    base = coverage_row(set(by_bbl.index), by_bbl)
    rows = [("ALL project properties", base)] + [(k, coverage_row(v, by_bbl)) for k, v in candidates.items()]
    for label, r in rows:
        n = max(r["n"], 1)
        print(f"  {label:24s}{r['n']:>8,}  {r['in_pluto']:>8,} ({r['in_pluto'] / n:>5.1%})  "
              f"{r['small_res']:>9,} ({r['small_res'] / n:>5.1%})  {r['condo_unit']:>8,} ({r['condo_unit'] / n:>5.1%})")
        if r["not_listed"]:
            print(f"  {'':24s}  ({r['not_listed']:,} of these BBLs are not in property_attributes.csv at all)")
    print("  'small residential' = in PLUTO, 1 to 4 residential units, building area > 0.")
    print("  If a flagged row's in-PLUTO share is well below the ALL row, the flags skew toward condo units.")


def usable_sales(joined, signals_mod, attrs):
    deeds = joined[joined["doc_type"].isin(signals_mod.DEED_TYPES)].copy()
    deeds = deeds.dropna(subset=["document_amt", "recorded_datetime", "bbl"])
    deeds = deeds.drop_duplicates(subset=["bbl", "document_id"])
    props_per_doc = deeds.groupby("document_id")["bbl"].transform("nunique")
    deeds["single_property_deed"] = props_per_doc == 1
    deeds = deeds.rename(columns={"document_amt": "price"})
    keep = ["bbl", "document_id", "recorded_datetime", "price", "single_property_deed"]
    deeds = deeds[keep].merge(attrs[attrs["in_pluto"]].drop(columns=["in_pluto", "attr_status"]), on="bbl", how="inner")
    deeds["borough"] = deeds["bbl"].str[0]
    return deeds


def print_sales(all_deeds, deeds, signals_mod):
    floor = signals_mod.MIN_REAL_PRICE
    print("\nDeed sales available for a valuation model:")
    print(f"  deed rows with a recorded price (unique bbl+document) : {len(all_deeds):,}")
    print(f"    on a property PLUTO describes                       : {len(deeds):,}")
    single = deeds[deeds["single_property_deed"]]
    print(f"    ...and the deed covers a single property            : {len(single):,}")
    for label, price_floor in [(f"price >= {floor:,.0f} (the detectors' own floor)", floor)] + \
                              [(f"price >= {f:,.0f}", f) for f in EXTRA_PRICE_FLOORS]:
        sub = single[single["price"] >= price_floor]
        small = sub[sub["small_residential"]]
        print(f"      {label:44s}{len(sub):>9,}   of which small residential {len(small):>9,}")
    core = single[(single["price"] >= EXTRA_PRICE_FLOORS[0]) & single["small_residential"]]
    if len(core):
        print(f"\nCore training pool (single-property deed, price >= {EXTRA_PRICE_FLOORS[0]:,}, small residential): "
              f"{len(core):,} sales on {core['bbl'].nunique():,} properties")
        by = core.groupby("borough")["price"].agg(["size", "median"])
        names = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}
        for b, r in by.iterrows():
            print(f"  {names.get(b, b):14s}{int(r['size']):>8,} sales   median price {r['median']:>12,.0f}")
        span = core["recorded_datetime"]
        print(f"  recorded between {span.min():%Y-%m-%d} and {span.max():%Y-%m-%d} "
              f"({(span.max() - span.min()).days} days: too short to learn a market trend; any model "
              f"must treat time carefully)")
    else:
        print("\nNo core training pool at these thresholds.")


def main():
    out_dir = pg.OUT_DIR
    attr_path = out_dir / "property_attributes.csv"
    if not attr_path.exists():
        raise SystemExit(f"{attr_path.name} not found. Run pluto_attributes.py first.")
    attrs = pd.read_csv(attr_path, dtype={"bbl": str, "landuse": str, "bldgclass": str})
    attrs = add_flags(attrs)

    candidates = load_candidate_bbls(out_dir)
    if candidates:
        print_flagged(candidates, attrs)

    try:
        import signals as signals_mod
    except ImportError as exc:
        raise SystemExit(f"Could not import signals.py ({exc}). Run this from the scripts folder.")
    print("\nLoading the ACRIS records (same loader signals.py uses)...")
    joined = signals_mod.load_and_join()
    all_deeds = joined[joined["doc_type"].isin(signals_mod.DEED_TYPES)].dropna(subset=["document_amt", "bbl"])
    all_deeds = all_deeds.drop_duplicates(subset=["bbl", "document_id"])
    deeds = usable_sales(joined, signals_mod, attrs)
    print_sales(all_deeds, deeds, signals_mod)

    deeds.to_csv(out_dir / "valuation_sales.csv", index=False)
    print(f"\nWrote {out_dir / 'valuation_sales.csv'} ({len(deeds):,} rows)")


if __name__ == "__main__":
    sys.exit(main())
