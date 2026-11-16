"""
transfer_signals.py -- four transfer-side signals, built on the same loaders signals.py uses.

  1. NOMINAL-TRANSFER-THEN-RESALE CHAIN. A deed at a token price (the first leg is excluded from the
     flip detector as "nominal") followed, within a window, by the grantee selling at a real price,
     or taking out a real mortgage. Each leg looks ordinary alone; only the chain is unusual. This is
     the shape of deed theft (forged transfer to a shell, then a quick sale or loan) AND of ordinary
     family and estate-planning transfers, so every row is a candidate and the columns say which way
     it leans: does the grantee look like an entity, does the grantor look like an individual, do the
     names share a surname token (a family hint), was a partial interest conveyed.
  2. REPEATED OWNERSHIP CHANGES. Properties with many deed recordings on different days, with or
     without price, and whether each transfer's grantor is the previous grantee (a true chain).
  3. PARTIAL-INTEREST ADJUSTMENT. A deed conveying 50% for $400k implies $800k, not $400k. This reads
     percent_trans from the raw master file, profiles it, and re-scores the valuation benchmark's
     residuals on the implied full price, showing how many "far below expectation" flags disappear.
  4. CEMA FOOTPRINT PROXY. A purchase CEMA assigns the seller's existing mortgage to the buyer's lender
     and consolidates it into the new one, so an assignment (ASST) recorded alongside the mortgage is
     part of its footprint. The proxy marks over-mortgaging and serial-refinancing mortgages that have
     an assignment or agreement document on the same property within a few days. It cannot see the
     taxable amount or exemption code, so it can only RAISE suspicion that a large mortgage is a
     consolidation. Its accuracy is unknown until it is checked against manual cover-page reviews.

Everything here is a candidate flag. No fraud labels exist, so none of it has an accuracy.

INPUTS: raw ACRIS pull and party files (via signals.py), data/raw/acris_master_sample.csv (percent_trans),
and optionally data/processed/valuation_residuals.csv, signal_over_mortgaging.csv, signal_serial_refinancing.csv.
OUTPUTS (data/processed): signal_nominal_chains.csv, signal_repeated_transfers.csv, signal_cema_proxy.csv,
valuation_residuals_adjusted.csv.

USAGE (scripts folder, activated venv; signals.py and pluto_geocode.py beside it):
    python transfer_signals.py
"""

import os
import re
import sys

import numpy as np
import pandas as pd

import pluto_geocode as pg
import signals

CHAIN_WINDOW_DAYS = 180        # nominal deed -> real resale by the grantee
MORTGAGE_WINDOW_DAYS = 90      # nominal deed -> real mortgage by the grantee
REPEAT_MIN_TRANSFERS = 3       # distinct recording days with a deed on one property
CEMA_WINDOW_DAYS = 3           # companion document recorded within this many days of the mortgage
CEMA_STRONG_TYPES = frozenset({"ASST"})                                  # assignment of mortgage
CEMA_MEDIUM_TYPES = frozenset({"AGMT", "CMTG", "M&CON", "SPRD", "AL&R"})  # agreements / consolidations
RAW_MASTER = os.path.join("..", "data", "raw", "acris_master_sample.csv")

ENTITY_TOKENS = {"LLC", "INC", "CORP", "CORPORATION", "LTD", "LP", "LLP", "TRUST", "COMPANY", "CO", "ASSOCIATES",
                 "PARTNERS", "HOLDINGS", "REALTY", "PROPERTIES", "GROUP", "FUND", "ESTATE", "BANK", "HDFC",
                 "ASSOCIATION", "ENTERPRISES", "INVESTORS", "CAPITAL", "MANAGEMENT", "VENTURES"}
GENERIC_TOKENS = ENTITY_TOKENS | {"STREET", "AVENUE", "AVE", "ST", "NEW", "YORK", "NYC", "THE", "AND", "OF"}
KNOWN_CASES = {
    "3-2325-1020": "manual review: CEMA (about $940k of $53M taxable, exemption 339EE)",
    "4-15744-17": "manual review: tax-exempt affordable-housing financing (not a CEMA)",
    "4-1457-31": "manual review: fresh money, mortgage carried no exemption",
}


# ---------------------------------------------------------------------------------------------
# name helpers (names are normalized strings from acris_parties_analyzed.csv)
# ---------------------------------------------------------------------------------------------
def _tokens(name):
    return re.findall(r"[A-Z0-9&]+", str(name).upper())


def looks_like_entity(names):
    for n in names:
        toks = set(_tokens(n))
        if toks & ENTITY_TOKENS or re.search(r"\bL\s?L\s?C\b", str(n).upper()):
            return True
    return False


def shares_name_token(names_a, names_b):
    """A family hint: any 4+ letter token (not a generic or entity word) common to both sides."""
    def toks(names):
        return {t for n in names for t in _tokens(n) if len(t) >= 4 and t not in GENERIC_TOKENS and not t.isdigit()}
    return bool(toks(names_a) & toks(names_b))


def _join(names, limit=3):
    names = sorted(names)
    return " | ".join(names[:limit]) + (" ..." if len(names) > limit else "")


# ---------------------------------------------------------------------------------------------
# data preparation
# ---------------------------------------------------------------------------------------------
def prepare_deeds(joined):
    deeds = joined[joined["doc_type"].isin(signals.DEED_TYPES)].dropna(subset=["bbl", "recorded_datetime"])
    deeds = deeds.drop_duplicates(subset=["bbl", "document_id"]).copy()
    deeds["single_property"] = deeds.groupby("document_id")["bbl"].transform("nunique") == 1
    return deeds.sort_values(["bbl", "recorded_datetime", "document_id"]).reset_index(drop=True)


def load_percent_trans(master_path=RAW_MASTER):
    if not os.path.exists(master_path):
        return None
    m = pd.read_csv(master_path, usecols=lambda c: c in ("document_id", "percent_trans"))
    if "percent_trans" not in m.columns:
        return None
    m["percent_trans"] = pd.to_numeric(m["percent_trans"], errors="coerce")
    return m.drop_duplicates("document_id").set_index("document_id")["percent_trans"]


# ---------------------------------------------------------------------------------------------
# 1. nominal transfer, then resale or mortgage
# ---------------------------------------------------------------------------------------------
def detect_nominal_chains(joined, buyer_names, seller_names, borrower_names, pct_by_doc=None):
    floor = signals.MIN_REAL_PRICE
    deeds = prepare_deeds(joined)
    mtges = joined[joined["doc_type"].isin(signals.GENUINE_MTGE_TYPES)].dropna(subset=["bbl", "recorded_datetime", "document_amt"])
    mtges = mtges[mtges["document_amt"] >= floor].drop_duplicates(subset=["bbl", "document_id"])
    mtges_by_bbl = {b: g for b, g in mtges.groupby("bbl")}

    rows, n_self, n_nominal, n_multi = [], 0, 0, 0
    for bbl, g in deeds.groupby("bbl"):
        recs = g.to_dict("records")
        for i, a in enumerate(recs):
            amt = a["document_amt"]
            if pd.isna(amt) or amt >= floor:
                continue
            n_nominal += 1
            if not a["single_property"]:
                n_multi += 1
                continue
            grantee, grantor = buyer_names.get(a["document_id"], set()), seller_names.get(a["document_id"], set())
            if grantee and grantor and (grantee & grantor):
                n_self += 1  # a co-owner re-titling to themselves, not a transfer to someone new
                continue
            base = {
                "bbl": bbl, "deedA_doc_id": a["document_id"], "deedA_date": a["recorded_datetime"], "deedA_amt": amt,
                "grantor_names": _join(grantor), "grantee_names": _join(grantee),
                "partial_interest_pct": (pct_by_doc.get(a["document_id"]) if pct_by_doc is not None else np.nan),
                "grantee_looks_like_entity": looks_like_entity(grantee) if grantee else np.nan,
                "grantor_looks_like_individual": (not looks_like_entity(grantor)) if grantor else np.nan,
                "shared_name_token": shares_name_token(grantor, grantee) if grantor and grantee else np.nan,
            }
            for b in recs[i + 1:]:  # first real resale by the grantee within the window
                gap = (b["recorded_datetime"] - a["recorded_datetime"]).days
                if gap > CHAIN_WINDOW_DAYS:
                    break
                if b["single_property"] and pd.notna(b["document_amt"]) and b["document_amt"] >= floor:
                    seller_b = seller_names.get(b["document_id"], set())
                    cont = "name_match" if grantee and seller_b and (grantee & seller_b) else "unknown" if not (grantee and seller_b) else "no_match"
                    if cont != "no_match":
                        rows.append({**base, "chain_type": "nominal_then_resale", "followon_doc_id": b["document_id"],
                                     "followon_date": b["recorded_datetime"], "followon_amt": b["document_amt"],
                                     "gap_days": gap, "continuity": cont})
                    break
            mg = mtges_by_bbl.get(bbl)
            if mg is not None and grantee:
                for m in mg.itertuples():
                    gap = (m.recorded_datetime - a["recorded_datetime"]).days
                    if 0 <= gap <= MORTGAGE_WINDOW_DAYS and (grantee & borrower_names.get(m.document_id, set())):
                        rows.append({**base, "chain_type": "nominal_then_mortgage", "followon_doc_id": m.document_id,
                                     "followon_date": m.recorded_datetime, "followon_amt": m.document_amt,
                                     "gap_days": gap, "continuity": "name_match"})
                        break
    out = pd.DataFrame(rows)
    print(f"  ({n_nominal:,} nominal deeds examined; {n_multi:,} skipped as multi-property transfers; "
          f"{n_self:,} skipped as self-transfers)")
    return out


# ---------------------------------------------------------------------------------------------
# 2. repeated ownership changes
# ---------------------------------------------------------------------------------------------
def detect_repeated_transfers(joined, buyer_names, seller_names):
    deeds = prepare_deeds(joined)
    deeds["day"] = deeds["recorded_datetime"].dt.normalize()
    floor = signals.MIN_REAL_PRICE
    counts = deeds.groupby("bbl")["day"].nunique()
    dist = counts.value_counts().sort_index()
    print("  deed-recording days per property: " + ", ".join(f"{k}: {v:,}" for k, v in dist.head(6).items())
          + (f", 6+: {int(dist[dist.index >= 6].sum()):,}" if (dist.index >= 6).any() else ""))
    rows = []
    for bbl in counts[counts >= REPEAT_MIN_TRANSFERS].index:
        g = deeds[deeds["bbl"] == bbl].sort_values(["recorded_datetime", "document_id"])
        per_day = g.groupby("day").first().reset_index().sort_values("day")  # one event per recording day
        docs = per_day["document_id"].tolist()
        links = sum(1 for p, n in zip(docs, docs[1:])
                    if buyer_names.get(p, set()) & seller_names.get(n, set()))
        amts = per_day["document_amt"]
        rows.append({
            "bbl": bbl, "n_recording_days": len(per_day), "span_days": int((per_day["day"].max() - per_day["day"].min()).days),
            "n_priced": int((amts >= floor).sum()), "n_nominal": int((amts < floor).sum()),
            "chain_links": links, "possible_links": len(docs) - 1,
            "true_chain": links == len(docs) - 1 and len(docs) > 1,
            "first_date": per_day["day"].min(), "last_date": per_day["day"].max(),
        })
    return pd.DataFrame(rows), dist


# ---------------------------------------------------------------------------------------------
# 3. partial-interest profile and benchmark adjustment
# ---------------------------------------------------------------------------------------------
def profile_percent_trans(deeds, pct):
    floor = signals.MIN_REAL_PRICE
    d = deeds.drop_duplicates("document_id").copy()
    d["pct"] = d["document_id"].map(pct)
    n = len(d)
    cat = pd.cut(d["pct"], [-np.inf, 0, 99.999, 100.001, np.inf], labels=["0 (unset?)", "partial (0-100)", "100", ">100"])
    cat = cat.cat.add_categories("missing").fillna("missing")
    print(f"\npercent_trans on {n:,} deeds:")
    for label, c in cat.value_counts().items():
        print(f"  {label:18s}{c:>8,} ({c / n:.1%})")
    priced = d["document_amt"] >= floor
    for name, mask in [("nominal deeds", ~priced & d["document_amt"].notna()), ("priced deeds", priced)]:
        sub = d[mask]
        if len(sub):
            part = sub["pct"].between(0.001, 99.999).mean()
            print(f"  partial-interest share among {name:14s}: {part:.1%} of {len(sub):,}")
    return d


def adjust_residuals(pct, out_dir):
    path = out_dir / "valuation_residuals.csv"
    if not path.exists() or pct is None:
        return None
    r = pd.read_csv(path, dtype={"bbl": str, "document_id": str, "borough": str})
    r["pct"] = r["document_id"].map({str(k): v for k, v in pct.items()})
    r["resid_log"] = np.log(r["price"] / r["expected_price"])
    # recover each borough's centre and scale from the saved z-scores: r = centre + scale * z exactly
    adj_parts = []
    for b, g in r.groupby("borough"):
        scale, centre = np.polyfit(g["z"], g["resid_log"], 1) if g["z"].nunique() > 1 else (np.nan, np.nan)
        implied = g["resid_log"] - np.log(np.where(g["pct"].between(0.001, 99.999), g["pct"] / 100.0, 1.0))
        adj_parts.append(pd.Series((implied - centre) / scale, index=g.index))
    r["z_adjusted"] = pd.concat(adj_parts)
    partial = r["pct"].between(0.001, 99.999)
    low = r["z"] <= -3
    print(f"\nPartial-interest adjustment of the valuation benchmark ({len(r):,} scored sales):")
    print(f"  sales conveying a partial interest: {int(partial.sum()):,}")
    print(f"  far-below-expectation flags before adjustment: {int(low.sum()):,}; after: {int((r['z_adjusted'] <= -3).sum()):,}")
    print(f"  flags that were partial-interest deeds: {int((low & partial).sum()):,}"
          f" ({(low & partial).sum() / max(low.sum(), 1):.1%} of flags, vs {partial.mean():.1%} of all sales)")
    r[["bbl", "document_id", "price", "expected_price", "z", "pct", "z_adjusted"]].to_csv(
        out_dir / "valuation_residuals_adjusted.csv", index=False)
    return r


# ---------------------------------------------------------------------------------------------
# 4. CEMA footprint proxy
# ---------------------------------------------------------------------------------------------
def cema_proxy(joined, mortgage_refs):
    """mortgage_refs: DataFrame[bbl, mtge_doc_id, source]. Returns one row each with companion documents."""
    comp = joined[joined["doc_type"].isin(CEMA_STRONG_TYPES | CEMA_MEDIUM_TYPES)].dropna(subset=["recorded_datetime"])
    comp_by_bbl = {b: g for b, g in comp.groupby("bbl")}
    stamp = joined.drop_duplicates(["bbl", "document_id"]).set_index(["bbl", "document_id"])["recorded_datetime"]
    out = []
    for ref in mortgage_refs.itertuples():
        when = stamp.get((ref.bbl, ref.mtge_doc_id), pd.NaT)
        types, tier = [], "none"
        g = comp_by_bbl.get(ref.bbl)
        if g is not None and pd.notna(when):
            near = g[(g["recorded_datetime"] - when).abs() <= pd.Timedelta(days=CEMA_WINDOW_DAYS)]
            types = sorted(set(near["doc_type"]))
            tier = "strong" if set(types) & CEMA_STRONG_TYPES else "medium" if types else "none"
        out.append({"bbl": ref.bbl, "mtge_doc_id": ref.mtge_doc_id, "source": ref.source,
                    "companion_types": ",".join(types), "cema_proxy": tier})
    return pd.DataFrame(out)


def mortgage_refs_from_signals(out_dir):
    refs = []
    p = out_dir / "signal_over_mortgaging.csv"
    if p.exists():
        om = pd.read_csv(p)
        om = om[om["category"] == "candidate"] if "category" in om.columns else om
        refs.append(pd.DataFrame({"bbl": om["bbl"], "mtge_doc_id": om["mtge_doc_id"], "source": "over_mortgaging"}))
    p = out_dir / "signal_serial_refinancing.csv"
    if p.exists():
        sr = pd.read_csv(p)
        for col in ("mtge1_doc_id", "mtge2_doc_id"):
            refs.append(pd.DataFrame({"bbl": sr["bbl"], "mtge_doc_id": sr[col], "source": "serial_refinancing"}))
    return pd.concat(refs, ignore_index=True).drop_duplicates() if refs else pd.DataFrame(columns=["bbl", "mtge_doc_id", "source"])


# ---------------------------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------------------------
def report_chains(ch):
    print(f"\n1. Nominal-transfer chains: {len(ch):,} candidates")
    if ch.empty:
        return
    print("  by type: " + ", ".join(f"{k} {v:,}" for k, v in ch["chain_type"].value_counts().items()))
    lean = ch[(ch["grantee_looks_like_entity"] == True) & (ch["grantor_looks_like_individual"] == True) & (ch["shared_name_token"] == False)]
    fam = ch[ch["shared_name_token"] == True]
    print(f"  grantor looks like an individual, grantee like an entity, no shared surname token: {len(lean):,}")
    print(f"  grantor and grantee share a name token (family hint, usually benign): {len(fam):,}")
    print(f"  resale within 30 days: {int(((ch['chain_type'] == 'nominal_then_resale') & (ch['gap_days'] <= 30)).sum()):,}")
    show = lean.sort_values("gap_days").head(8)[["bbl", "deedA_date", "grantor_names", "grantee_names", "followon_amt", "gap_days", "chain_type"]]
    if len(show):
        print("  shortest-gap individual -> entity chains (open both deeds in ACRIS):")
        print(show.to_string(index=False))


def report_cema(prox, joined):
    if prox.empty:
        return
    print(f"\n4. CEMA footprint proxy on {len(prox):,} flagged mortgages:")
    for src, g in prox.groupby("source"):
        vc = g["cema_proxy"].value_counts()
        print(f"  {src:20s} strong {vc.get('strong', 0):>5,}   medium {vc.get('medium', 0):>5,}   none {vc.get('none', 0):>5,}")
    print("  Known manual reviews (illustration only; three cases validate nothing):")
    for bbl, note in KNOWN_CASES.items():
        sub = prox[prox["bbl"] == bbl]
        verdict = ", ".join(f"{r.cema_proxy} [{r.companion_types or 'no companion docs'}]" for r in sub.itertuples()) or "not in the flagged set"
        print(f"    {bbl:12s} proxy: {verdict:46s} | {note}")


def main():
    out_dir = pg.OUT_DIR
    print(f"[transfer_signals.py] loading ACRIS records via signals.py ({signals.SCRIPT_VERSION})")
    joined = signals.load_and_join()
    buyer_names, seller_names, borrower_names, _all = signals.load_party_lookups()
    pct = load_percent_trans()
    print("\n--- 1. nominal-transfer chains")
    chains = detect_nominal_chains(joined, buyer_names, seller_names, borrower_names, pct)
    report_chains(chains)
    chains.to_csv(out_dir / "signal_nominal_chains.csv", index=False)

    print("\n--- 2. repeated ownership changes")
    rep, _dist = detect_repeated_transfers(joined, buyer_names, seller_names)
    print(f"\n2. Properties with {REPEAT_MIN_TRANSFERS}+ deed-recording days: {len(rep):,}"
          + (f" ({int(rep['true_chain'].sum()):,} are unbroken grantee-to-grantor chains)" if len(rep) else ""))
    if len(rep):
        print(rep.sort_values(["n_recording_days", "span_days"], ascending=[False, True]).head(8).to_string(index=False))
    rep.to_csv(out_dir / "signal_repeated_transfers.csv", index=False)

    print("\n--- 3. partial-interest transfers")
    if pct is None:
        print("  percent_trans not found in the raw master file; skipping.")
    else:
        profile_percent_trans(prepare_deeds(joined), pct)
        adjust_residuals(pct, out_dir)

    print("\n--- 4. CEMA footprint proxy")
    refs = mortgage_refs_from_signals(out_dir)
    prox = cema_proxy(joined, refs)
    report_cema(prox, joined)
    prox.to_csv(out_dir / "signal_cema_proxy.csv", index=False)
    print(f"\nWrote signal_nominal_chains.csv, signal_repeated_transfers.csv, signal_cema_proxy.csv"
          f"{', valuation_residuals_adjusted.csv' if pct is not None else ''} to {out_dir}")


if __name__ == "__main__":
    sys.exit(main())
