"""
valuation_benchmark.py -- an expected-price benchmark for deed sales, and residual flags.

WHAT THIS IS: for small residential properties, predict what a sale "should" fetch from physical
and locational attributes (PLUTO lot/building data, coordinates, borough, building class, month),
then measure how far each recorded price sits from that expectation. A large residual is a
CANDIDATE flag, never evidence of fraud: it can mean a family transfer, a data error, an unusual
building, or something worth a document review.

WHAT THIS IS NOT: a fraud detector with an accuracy. There are no fraud labels, so the only things
this script can validate are the model's pricing error and the mechanics of the flagging.

DESIGN CHOICES THAT MATTER
  * Training pool: single-property deeds, price >= 100,000, 1-4 residential units. Condo units are
    not in PLUTO and are out of scope; Manhattan and Staten Island may be too thin to model.
  * Sales between 10,000 and 100,000 are NOT used for training. They are SCORED by a model that
    never saw their area, because that is exactly where sub-market prices live.
  * Spatial validation: sales are grouped into ~1 km grid cells and whole cells are held out, so a
    property is never predicted by its own neighbours' sales. A leaky random split is also run to
    show how much it flatters the model (the gap is reported, not hidden).
  * Assessed values (assesstot/assessland) are NOT used: the city's valuations are partly built
    from sales, which would leak the answer.
  * The data covers under a year, so the model cannot learn a market trend; month is a feature
    only to absorb drift inside the window.
  * A plain baseline (median price per square foot by borough and building class) is scored
    alongside the model. If the model cannot beat it, the extra complexity has not earned its place.

THE REFRAMING OF OVER-MORTGAGING: the existing signal divides the mortgage by the PRICE, so a low
price inflates the ratio by construction. With an expected value we can ask which half is odd:
the price (far below expectation while the mortgage is plausible) or the mortgage (far above
expectation).

INPUTS (from earlier steps): data/processed/valuation_sales.csv (valuation_coverage.py),
property_geo.csv (pluto_geocode.py), and the signal CSVs (signals.py).
OUTPUTS: data/processed/valuation_residuals.csv and overmortgage_vs_expected.csv.

USAGE (scripts folder, activated venv; needs scikit-learn: pip install scikit-learn)
    python valuation_benchmark.py
    python valuation_benchmark.py --selftest    # mechanics check on synthetic data with injected anomalies
"""

import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold, KFold

import pluto_geocode as pg
import valuation_coverage as vc

CORE_MIN_PRICE = 100_000
SCORE_MIN_PRICE = 10_000     # mirrors the detectors' MIN_REAL_PRICE
N_FOLDS = 5
CELL_DEG = 0.01              # ~1.1 km north-south, ~0.85 km east-west
Z_FLAG = 3.0
MAX_CATEGORIES = 60

NUMERIC = ["lotarea", "bldgarea", "resarea", "comarea", "numfloors", "numbldgs", "unitsres", "unitstotal",
           "lotfront", "lotdepth", "bldgfront", "bldgdepth", "lat", "lon"]
CATEGORICAL = ["borough", "bldgclass", "landuse", "zonedist1"]
BOROUGH_NAMES = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}


# ---------------------------------------------------------------------------------------------
# data preparation
# ---------------------------------------------------------------------------------------------
def _as_bool(s):
    return s.map({True: True, False: False, "True": True, "False": False}).fillna(False).astype(bool)


def load_eligible(out_dir):
    path = out_dir / "valuation_sales.csv"
    if not path.exists():
        raise SystemExit(f"{path.name} not found. Run valuation_coverage.py first.")
    text = {c: str for c in ["bbl", "document_id", "landuse", "bldgclass", "zonedist1", "histdist", "landmark", "borough"]}
    df = pd.read_csv(path, dtype=text, parse_dates=["recorded_datetime"])
    geo = pd.read_csv(out_dir / "property_geo.csv", dtype={"bbl": str})[["bbl", "lat", "lon", "precision"]]
    df = df.merge(geo, on="bbl", how="left")
    df["single_property_deed"] = _as_bool(df["single_property_deed"])
    df["small_residential"] = _as_bool(df["small_residential"])
    n0 = len(df)
    df = df[df["single_property_deed"] & df["small_residential"] & (df["price"] >= SCORE_MIN_PRICE)]
    n1 = len(df)
    df = df.dropna(subset=["lat", "lon"])
    print(f"Sales file: {n0:,} rows -> {n1:,} single-property, small residential, price >= {SCORE_MIN_PRICE:,} "
          f"-> {len(df):,} with a position")
    return df.reset_index(drop=True)


def build_frame(df):
    d = df.copy()
    d["in_core"] = d["price"] >= CORE_MIN_PRICE
    d["cell"] = (np.floor(d["lat"] / CELL_DEG).astype(int).astype(str) + "_"
                 + np.floor(d["lon"] / CELL_DEG).astype(int).astype(str))
    sale_date = pd.to_datetime(d["recorded_datetime"])
    d["t_months"] = (sale_date - sale_date.min()).dt.days / 30.44
    if "yearbuilt" in d.columns:
        age = sale_date.dt.year - pd.to_numeric(d["yearbuilt"], errors="coerce")
        d["age"] = age.where(age >= 0)
    area = pd.to_numeric(d["bldgarea"], errors="coerce").where(lambda s: s > 0)
    lot = pd.to_numeric(d["lotarea"], errors="coerce").where(lambda s: s > 0)
    d["log_bldgarea"], d["log_lotarea"], d["far"] = np.log(area), np.log(lot), area / lot
    d["log_price"] = np.log(d["price"])
    return d


def feature_matrix(d):
    nums = [c for c in NUMERIC if c in d.columns] + [c for c in ["t_months", "age", "log_bldgarea", "log_lotarea", "far"] if c in d.columns]
    X = d[nums].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    cats = [c for c in CATEGORICAL if c in d.columns]
    for c in cats:
        s = d[c].astype("string").fillna("missing")
        keep = s.value_counts().head(MAX_CATEGORIES).index
        X[c] = s.where(s.isin(keep), "other").astype("category").cat.codes.astype(float)
    mask = [c in cats for c in X.columns]
    return X, mask


def make_model(cat_mask):
    return HistGradientBoostingRegressor(
        max_iter=300, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=20,
        l2_regularization=1.0, random_state=0, categorical_features=cat_mask,
    )


def baseline_predict(tr, te):
    """Median log price per square foot by borough and building-class letter."""
    def key(x):
        return x["borough"].astype(str) + "|" + x["bldgclass"].astype(str).str[:1]
    ppsf = tr["log_price"] - tr["log_bldgarea"]
    med = ppsf.groupby(key(tr)).median()
    return (te["log_bldgarea"] + key(te).map(med).fillna(ppsf.median())).to_numpy()


def metrics(y, p):
    ok = ~(np.isnan(y) | np.isnan(p))
    y, p = y[ok], p[ok]
    ape = np.abs(1 - np.exp(p - y))  # |actual - predicted| / actual
    ss = np.sum((y - y.mean()) ** 2)
    return {"n": int(ok.sum()), "mdape": float(np.median(ape)), "r2": float(1 - np.sum((y - p) ** 2) / ss) if ss else np.nan}


# ---------------------------------------------------------------------------------------------
# the benchmark
# ---------------------------------------------------------------------------------------------
def run_benchmark(df, verbose=True):
    d = build_frame(df)
    X, cat_mask = feature_matrix(d)
    y = d["log_price"].to_numpy()
    core = np.flatnonzero(d["in_core"].to_numpy())
    ext = np.flatnonzero(~d["in_core"].to_numpy())
    if len(core) < 200:
        raise SystemExit(f"Only {len(core)} sales in the training pool; too few to model.")
    Xc, yc, dc = X.iloc[core], y[core], d.iloc[core]
    groups = dc["cell"].to_numpy()

    spatial = np.full(len(core), np.nan)
    base_spatial = np.full(len(core), np.nan)
    models, fold_of_cell = [], {}
    for k, (tr, te) in enumerate(GroupKFold(n_splits=N_FOLDS).split(Xc, yc, groups)):
        m = make_model(cat_mask).fit(Xc.iloc[tr], yc[tr])
        spatial[te] = m.predict(Xc.iloc[te])
        base_spatial[te] = baseline_predict(dc.iloc[tr], dc.iloc[te])
        models.append(m)
        for c in set(groups[te]):
            fold_of_cell[c] = k

    randomcv = np.full(len(core), np.nan)
    for tr, te in KFold(n_splits=N_FOLDS, shuffle=True, random_state=0).split(Xc):
        randomcv[te] = make_model(cat_mask).fit(Xc.iloc[tr], yc[tr]).predict(Xc.iloc[te])

    pred = np.full(len(d), np.nan)
    pred[core] = spatial
    if len(ext):
        Xe, cells = X.iloc[ext], d["cell"].iloc[ext]
        fold_ids = cells.map(fold_of_cell)
        pe = np.full(len(ext), np.nan)
        for k, m in enumerate(models):
            sel = (fold_ids == k).to_numpy()
            if sel.any():
                pe[sel] = m.predict(Xe[sel])
        unknown = fold_ids.isna().to_numpy()
        if unknown.any():
            pe[unknown] = np.mean([m.predict(Xe[unknown]) for m in models], axis=0)
        pred[ext] = pe
    d["log_expected"] = pred
    d["expected_price"] = np.exp(pred)
    d["resid_log"] = d["log_price"] - d["log_expected"]
    d["resid_pct"] = d["price"] / d["expected_price"] - 1

    # robust z-score per borough, with centre/scale learned from the held-out core residuals only
    core_res = pd.DataFrame({"b": dc["borough"].to_numpy(), "r": yc - spatial})
    med = core_res.groupby("b")["r"].median()
    mad = core_res.assign(a=lambda x: (x["r"] - x["b"].map(med)).abs()).groupby("b")["a"].median()
    scale = (1.4826 * mad).clip(lower=0.05)
    d["z"] = (d["resid_log"] - d["borough"].map(med).fillna(med.median())) / d["borough"].map(scale).fillna(scale.median())
    d["flag_low"], d["flag_high"] = d["z"] <= -Z_FLAG, d["z"] >= Z_FLAG

    report = {
        "n_core": len(core), "n_ext": len(ext),
        "spatial": metrics(yc, spatial), "random": metrics(yc, randomcv), "baseline": metrics(yc, base_spatial),
        "by_borough": {b: {"model": metrics(yc[(dc["borough"] == b).to_numpy()], spatial[(dc["borough"] == b).to_numpy()]),
                           "baseline": metrics(yc[(dc["borough"] == b).to_numpy()], base_spatial[(dc["borough"] == b).to_numpy()])}
                       for b in sorted(dc["borough"].unique())},
    }
    if verbose:
        print_report(d, report)
    return d, report


def print_report(d, rep):
    s, r, b = rep["spatial"], rep["random"], rep["baseline"]
    print(f"\nTraining pool: {rep['n_core']:,} sales (price >= {CORE_MIN_PRICE:,}). Scored without training: {rep['n_ext']:,} "
          f"sales between {SCORE_MIN_PRICE:,} and {CORE_MIN_PRICE:,}.")
    print("\nPricing error out-of-fold (median absolute % error, lower is better; R2 on log price):")
    print(f"  spatial split (honest)       MdAPE {s['mdape']:.1%}   R2 {s['r2']:.3f}")
    print(f"  random split (leaky)         MdAPE {r['mdape']:.1%}   R2 {r['r2']:.3f}   <- how much leakage flatters the model")
    print(f"  baseline (median $/sq ft)    MdAPE {b['mdape']:.1%}   R2 {b['r2']:.3f}   <- the bar the model must clear")
    print("\nBy borough (spatial split):")
    for bo, v in rep["by_borough"].items():
        m, bl = v["model"], v["baseline"]
        print(f"  {BOROUGH_NAMES.get(bo, bo):14s} n={m['n']:>6,}  model MdAPE {m['mdape']:.1%}   baseline MdAPE {bl['mdape']:.1%}")
    core, ext = d[d["in_core"]], d[~d["in_core"]]
    print(f"\nResidual flags (|robust z| >= {Z_FLAG:g}; candidates only):")
    print(f"  training pool : {int(core['flag_low'].sum()):,} far below expectation, {int(core['flag_high'].sum()):,} far above, of {len(core):,}")
    if len(ext):
        print(f"  10k-100k sales: {int(ext['flag_low'].sum()):,} far below expectation of {len(ext):,}  "
              f"(many will be non-market transfers; only a document review can tell)")


# ---------------------------------------------------------------------------------------------
# relating the benchmark to the existing signals
# ---------------------------------------------------------------------------------------------
def compare_over_mortgaging(d, out_dir):
    path = out_dir / "signal_over_mortgaging.csv"
    if not path.exists():
        return None
    om = pd.read_csv(path, dtype={"bbl": str, "deed_doc_id": str, "mtge_doc_id": str})
    if "category" in om.columns:
        om = om[om["category"] == "candidate"]
    m = om.merge(d[["document_id", "price", "expected_price", "z"]], left_on="deed_doc_id", right_on="document_id", how="inner")
    if m.empty:
        print("\nOver-mortgaging reframing: no flagged deeds fall in the scored set.")
        return None
    m["mtge_to_expected"] = m["mtge_amt"] / m["expected_price"]
    m["price_vs_expected"] = pd.cut(m["z"], [-np.inf, -Z_FLAG, Z_FLAG, np.inf],
                                    labels=["price far below expectation", "price within range", "price far above expectation"])
    m["mortgage_vs_expected"] = pd.cut(m["mtge_to_expected"], [0, 1.0, 1.5, np.inf],
                                       labels=["mortgage <= 1.0x expected", "1.0-1.5x expected", "mortgage > 1.5x expected"])
    print(
    f"\nOver-mortgaging candidates matched to the benchmark: "
    f"{len(m):,} of {len(om):,} candidate deeds."
    )
    print(
    f"Unmatched candidates: {len(om) - len(m):,}. "
    "These have no corresponding transaction in the eligible scored benchmark dataset."
    )
    print("Which half is odd, the price or the mortgage?  (rows: price vs expected; columns: mortgage vs expected)")
    print(pd.crosstab(m["price_vs_expected"], m["mortgage_vs_expected"]).to_string())
    print("\nLargest mortgage relative to expected value:")
    cols = ["bbl", "price", "expected_price", "mtge_amt", "mortgage_to_price_ratio", "mtge_to_expected", "z"]
    print(m.sort_values("mtge_to_expected", ascending=False)[cols].head(8).round(2).to_string(index=False))
    m.to_csv(out_dir / "overmortgage_vs_expected.csv", index=False)
    return m


def compare_flag_rates(d, out_dir):
    cands = vc.load_candidate_bbls(out_dir)
    if not cands:
        return
    print("\nDo other signals' properties show more extreme residuals? (share of scored sales with |z| >= "
          f"{Z_FLAG:g}; counts shown)")
    extreme = d["flag_low"] | d["flag_high"]
    print(f"  {'':24s}{'sales':>8s}  {'flagged group':>16s}  {'everything else':>16s}")
    for label, bbls in cands.items():
        inn = d["bbl"].isin(bbls)
        if inn.sum() == 0:
            continue
        a, b = extreme[inn], extreme[~inn]
        print(f"  {label:24s}{int(inn.sum()):>8,}  {a.mean():>9.1%} ({int(a.sum()):>3,})  {b.mean():>9.1%} ({int(b.sum()):>4,})")
    print("  Caution: over-mortgaging divides the mortgage by the price, so a low price raises its ratio by")
    print("  construction. A higher rate there is partly built in and is NOT independent corroboration.")


def print_extremes(d, n=10):
    cols = ["bbl", "document_id", "price", "expected_price", "resid_pct", "z"]

    core = d[d["in_core"]].copy()
    extension = d[~d["in_core"]].copy()

    print("\nCORE TRAINING-POOL EXTREMES")
    print("Transactions priced at or above the training threshold.")

    for label, sub in [
        ("far below expectation", core[core["flag_low"]].nsmallest(n, "z")),
        ("far above expectation", core[core["flag_high"]].nlargest(n, "z")),
    ]:
        print(f"\n{label}:")
        if len(sub):
            print(sub[cols].round(2).to_string(index=False))
        else:
            print("None.")

    print("\nEXTENSION-POPULATION EXTREMES")
    print("Transactions priced below the training threshold.")
    print(
        f"These {len(extension):,} transactions were scored without being "
        "included in model training. Their residual flags are not directly "
        "comparable to core-population flags."
    )

    for label, sub in [
        ("lowest residuals", extension.nsmallest(n, "z")),
        ("highest residuals", extension.nlargest(n, "z")),
    ]:
        print(f"\n{label}:")
        if len(sub):
            print(sub[cols].round(2).to_string(index=False))
        else:
            print("None.")


def print_limits():
    print("""
WHAT THIS CAN AND CANNOT SAY
  Can:    how closely recorded prices follow a spatially validated expectation for small residential
          sales in the boroughs with enough data, and which sales sit unusually far from it.
  Cannot: that any flagged sale is fraudulent; anything about condo units, Manhattan or Staten Island
          where the pool is too thin; anything about price trends (under a year of data); or any
          fraud-detection accuracy, because there are no fraud labels.""")


# ---------------------------------------------------------------------------------------------
# mechanics check on synthetic data (NOT evidence about real fraud)
# ---------------------------------------------------------------------------------------------
def make_synthetic(n=9000, anomaly_rate=0.03, seed=0):
    rng = np.random.default_rng(seed)
    boro = rng.choice(["2", "3", "4"], n, p=[0.2, 0.4, 0.4])
    base = pd.Series(boro).map({"2": 330, "3": 520, "4": 430}).to_numpy()
    centre = {"2": (40.85, -73.88), "3": (40.65, -73.95), "4": (40.72, -73.80)}
    lat = np.array([centre[b][0] for b in boro]) + rng.normal(0, 0.05, n)
    lon = np.array([centre[b][1] for b in boro]) + rng.normal(0, 0.05, n)
    bldg = np.exp(rng.normal(7.4, 0.3, n))
    year = rng.integers(1900, 2016, n)
    spatial = 0.25 * np.sin(lat * 80) + 0.25 * np.cos(lon * 80)  # smooth neighbourhood effect
    # plus a premium that is specific to each ~1 km cell and not a smooth function of position: a
    # random split can partly learn it from the cell's other sales, a spatial split cannot
    cell_key = np.floor(lat / CELL_DEG).astype(int) * 100003 + np.floor(lon / CELL_DEG).astype(int)
    effects = {k: rng.normal(0, 0.15) for k in np.unique(cell_key)}
    cell_effect = np.array([effects[k] for k in cell_key])
    price = bldg * base * np.exp(spatial + cell_effect - 0.002 * (2026 - year) + rng.normal(0, 0.18, n))
    cls = rng.choice(["A1", "B2", "C0", "S2"], n)
    df = pd.DataFrame({
        "bbl": [f"{b}-{i}-1" for i, b in enumerate(boro)], "document_id": [f"D{i}" for i in range(n)],
        "recorded_datetime": pd.Timestamp("2025-10-01") + pd.to_timedelta(rng.integers(0, 330, n), unit="D"),
        "price": price, "single_property_deed": True, "small_residential": True, "borough": boro,
        "lotarea": bldg * 1.6, "bldgarea": bldg, "numfloors": rng.integers(1, 4, n), "unitsres": rng.integers(1, 5, n),
        "yearbuilt": year, "bldgclass": cls, "landuse": "01", "zonedist1": rng.choice(["R3", "R5", "R6"], n),
        "lat": lat, "lon": lon,
    })
    # four injected kinds: two blatant, two subtle (so the test shows sensitivity, not just success)
    kinds = ["low_blatant", "high_blatant", "low_subtle", "high_subtle"]
    factors = {"low_blatant": 0.2, "high_blatant": 3.5, "low_subtle": 0.6, "high_subtle": 1.7}
    kind = np.where(rng.random(n) < anomaly_rate, rng.choice(kinds, n), "none")
    for k, f in factors.items():
        df.loc[kind == k, "price"] *= f
    df["injected"] = kind
    df["price"] = df["price"].clip(lower=SCORE_MIN_PRICE)
    return df


def selftest():
    print("SELF-TEST on synthetic sales with injected anomalies (mechanics only):")
    df = make_synthetic()
    d, rep = run_benchmark(df, verbose=False)
    s, r, b = rep["spatial"], rep["random"], rep["baseline"]
    print(f"  spatial MdAPE {s['mdape']:.1%} | random-split MdAPE {r['mdape']:.1%} (leaky) | baseline MdAPE {b['mdape']:.1%}")
    core = d[d["in_core"]]
    for kind, factor, col in [("low_blatant", 0.2, "flag_low"), ("high_blatant", 3.5, "flag_high"),
                              ("low_subtle", 0.6, "flag_low"), ("high_subtle", 1.7, "flag_high")]:
        sub = core[core["injected"] == kind]
        print(f"  injected {kind:12s} (price x{factor}): flagged {sub[col].mean():>4.0%} of {len(sub)}")
    clean = core[core["injected"] == "none"]
    fp = (clean["flag_low"] | clean["flag_high"]).mean()
    print(f"  false-flag rate on un-injected sales: {fp:.1%} of {len(clean):,}")
    print("  This only shows the pipeline recovers anomalies it was handed. It says nothing about real fraud.")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    out_dir = pg.OUT_DIR
    df = load_eligible(out_dir)
    d, _ = run_benchmark(df)
    print_extremes(d)
    compare_over_mortgaging(d, out_dir)
    compare_flag_rates(d, out_dir)
    keep = ["bbl", "document_id", "recorded_datetime", "price", "expected_price", "resid_pct", "z",
            "flag_low", "flag_high", "in_core", "borough", "cell"]
    d[keep].to_csv(out_dir / "valuation_residuals.csv", index=False)
    print(f"\nWrote {out_dir / 'valuation_residuals.csv'} ({len(d):,} rows)")
    print_limits()


if __name__ == "__main__":
    sys.exit(main())
