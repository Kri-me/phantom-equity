"""
Phantom-equity signals: rapid flip, over-mortgaging, and serial-refinancing detection.

All three signals work at the PROPERTY level (borough+block+lot), not the party level --
they ask "what happened to this specific parcel over time", not "who is this entity".
The exceptions are detect_over_mortgaging's party-overlap check and
detect_serial_refinancing's borrower-change flag (see their docstrings), which are
party-level facts used to validate or annotate a property-level pairing.

IMPORTANT FRAMING: there is no labeled ground truth for fraud in this dataset (see
project notes). Everything below produces CANDIDATE flags consistent with known
phantom-equity typologies, not confirmed fraud. Every threshold here (flip window,
price-change %, mortgage/price ratio, refinancing window) is a starting guess based on
general real-estate knowledge, not validated against this data's actual distribution --
inspect the flagged output and tune before treating counts as meaningful.

DATE FIELD CAVEAT: document_date is UNRELIABLE for some MORTGAGES & INSTRUMENTS doc_types
in this dataset -- confirmed in real data on several BBLs where an AGMT/ASST/TL&R row's
document_date sat decades off from the rest of its own closing bundle's correctly-dated
rows (e.g. a 1936 date on a row recorded, per recorded_datetime, in 2026), and CERT/ZONE
rows showed the identical placeholder date 1977-08-18 recurring across unrelated parcels.
recorded_datetime showed zero such anomalies across every row inspected. detect_rapid_flips
and detect_over_mortgaging still key off document_date (their corrupted-date failure mode
is more likely a false NEGATIVE -- a bad date sorts far outside any matching window and
simply fails to pair -- except inside flag_bulk_multi_lot's round-amount date-clustering,
where a corrupted date on one leg of a genuine duplicate could let it slip through
unexcluded; not yet separately audited). detect_serial_refinancing uses recorded_datetime
from the start, since it was designed after this was found.

Requires: pip install pandas
"""

import pandas as pd
import os
import sys
sys.path.insert(0, ".")
from collections import defaultdict
from doc_control_codes import DOC_CONTROL_CODES

# --- Config (starting guesses -- tune after inspecting real flagged output) ---
FLIP_WINDOW_DAYS = 180          # deed-to-deed gap on the same property, within which a resale is "rapid"
FLIP_PRICE_INCREASE_PCT = 0.20  # 20%+ price jump on a rapid resale is flagged
PURCHASE_MONEY_WINDOW_DAYS = 14 # a mortgage recorded within this many days of a deed is treated as
                                 # "purchase money" (financing the purchase itself) rather than an
                                 # unrelated later refinance -- outside this window, don't pair them
OVER_MORTGAGE_RATIO = 0.95      # mortgage_amt / deed_amt above this is flagged. Normal purchase-money
                                 # LTV is usually 70-97%, but ratios AT or ABOVE 100% (borrowing more
                                 # than the stated sale price) are the more interesting phantom-equity
                                 # signal -- flagging from 0.95 catches both the aggressive-but-legal
                                 # cases and the genuinely anomalous ones; inspect the distribution.
MIN_REAL_PRICE = 10000          # deeds below this are treated as nominal/token consideration (gifts,
                                 # corrective deeds, intra-family transfers, LLC estate-planning
                                 # transfers) rather than real sales, and excluded from both signals --
                                 # a $1 or $10 "price" produces meaningless percentage math

# Buyer-side roles ACRIS actually produces on a deed (the buyer-side subset of
# case_builder.py's OWNERSHIP_CONNECTING_ROLES -- only the deed's BUYER needs to appear
# on a purported financing document to validate it as real purchase-money financing;
# the seller's presence there wouldn't).
BUYER_SIDE_ROLES = {"GRANTEE/BUYER", "GRANTEE", "DESGNTD BENEFICIARY", "PARTY 2/GRANTEE"}

# Seller-side counterpart (the seller-side subset of case_builder.py's OWNERSHIP_CONNECTING_ROLES).
# Used to detect self-transfers: a deed where a buyer-side name also appears on the seller side
# (e.g. a co-owner deeding to herself to remove a spouse from title) is not an arm's-length sale,
# so its recorded consideration is not a real price. Confirmed in real data: a $10,000 deed from
# IBUDE, JOHN O and IBUDE, NATASHA S to IBUDE, NATASHA S paired with her $257,000 same-day
# mortgage produced a fake 25.7x ratio.
SELLER_SIDE_ROLES = {"GRANTOR/SELLER", "GRANTOR", "TRANSFEROR", "PARTY 1/GRANTOR"}

# MTGE's own party-role labels, per DOC_CONTROL_CODES. Only meaningful because
# GENUINE_MTGE_TYPES is restricted to plain "MTGE" -- no need for a broader set of
# label variants the way BUYER_SIDE_ROLES/SELLER_SIDE_ROLES need, since every document
# this gets checked against is already known to be doc_type MTGE specifically.
BORROWER_ROLE = "MORTGAGOR/BORROWER"


# Ordinary conveyance codes only. The official class "DEEDS AND OTHER CONVEYANCES" also holds timeshare
# deeds (DEED, TS), correction deeds (CORRD, DEED COR, CONDEED...), contracts (CNTR, MCON, ACON), leases,
# easements, development rights, declarations and court/government records -- none of which is an
# arm's-length sale. Confirmed in real data: 2,190 of 3,949 nominal chains (55%) started from DEED, TS,
# almost all on seven timeshare-building BBLs that merged into one case scoring 17,315.
# This set matches the "ordinary conveyance" group in audit_acris_schema.py's DEED_CLASS.
ORDINARY_CONVEYANCE_TYPES = frozenset({"DEED", "DEEDO", "DEEDP", "DEED, RC", "DEED, LE", "IDED", "REIT"})
DEED_TYPES = frozenset(
    dt for dt, v in DOC_CONTROL_CODES.items()
    if v[0] == "DEEDS AND OTHER CONVEYANCES" and dt in ORDINARY_CONVEYANCE_TYPES
)

# Restricted to plain, unqualified new-money mortgage recordings only -- NOT every doc_type
# DOC_CONTROL_CODES categorizes as "MORTGAGES & INSTRUMENTS". Confirmed in real data that the
# broader category caused real harm: AGMT (a generic multi-party agreement, not a mortgage in
# substance) misattributed a 177-page unrelated financing instrument to a deed it had nothing
# to do with (case #44, a 13.5x false-positive ratio). Every other non-MTGE type in this
# category carries a name that disqualifies it as fresh purchase-money financing: SPRD/CMTG/
# M&CON (spreader/consolidated instruments that fold EXISTING debt into one inflated face
# value -- the same CEMA-style artifact already found once by hand), AL&R (secures an
# EXISTING loan a separate MTGE document already represents, not new money), and the
# low-volume CORRM/ASPM/CERR/AMTX/XXXX/SMTG, whose names (correction, reduction, payer,
# ACRIS's own "unclassified" code) all suggest a restatement of existing debt rather than
# fresh financing -- excluded out of caution given real but unexplained dollar amounts on
# each, pending a clearer reference than doc_control_codes.py provides.
GENUINE_MTGE_TYPES = frozenset({"MTGE"})

REFI_WINDOW_DAYS = 365  # consecutive genuine mortgages within this many days, with no
                         # intervening deed, are treated as serial refinancing on the same
                         # property -- a starting guess, not validated against this data.


# Timeshare buildings are identified by their documents, not only by property_type. In the Nov 20
# data every one of the recurring-pattern BBLs (1-1010-1905, 1-1283-1002/1003, 1-1006-1302..1305) and
# two more (1-1009-37, 1-1027-17xx) had hundreds to thousands of "DEED, TS" documents, yet mortgages and
# ordinary-typed rows on the same BBLs still reached serial refinancing and the case queue because their
# property_type was not "TS". Any BBL with at least TIMESHARE_BBL_MIN_DOCS timeshare deeds is excluded
# outright from every signal that builds on load_and_join().
TIMESHARE_DEED_TYPE = "DEED, TS"
TIMESHARE_BBL_MIN_DOCS = 1


def timeshare_bbls(merged):
    """BBLs with at least TIMESHARE_BBL_MIN_DOCS distinct DEED, TS documents, with their document counts.
    Must be computed BEFORE the property_type == TS rows are dropped, or the DEED, TS rows that identify
    the building would already be gone."""
    ts = merged[merged["doc_type"] == TIMESHARE_DEED_TYPE]
    counts = ts.groupby("bbl")["document_id"].nunique()
    return counts[counts >= TIMESHARE_BBL_MIN_DOCS]


def load_and_join(raw_dir="../data/raw"):
    master = pd.read_csv(os.path.join(raw_dir, "acris_master_sample.csv"))
    legals = pd.read_csv(os.path.join(raw_dir, "acris_legals_sample.csv"))

    master["document_date"] = pd.to_datetime(master["document_date"], errors="coerce")
    master["recorded_datetime"] = pd.to_datetime(master["recorded_datetime"], errors="coerce")
    master["document_amt"] = pd.to_numeric(master["document_amt"], errors="coerce")

    # A document can span multiple lots (e.g. a multi-parcel sale). We keep one row per
    # (document_id, BBL) pair -- a document with 3 lots contributes to 3 properties' histories.
    legals = legals.dropna(subset=["borough", "block", "lot"]).copy()
    legals["bbl"] = (
        legals["borough"].astype(str) + "-" +
        legals["block"].astype(str) + "-" +
        legals["lot"].astype(str)
    )

    merged = legals[["document_id", "bbl", "property_type", "unit"]].merge(
        master[["document_id", "doc_type", "document_date", "recorded_datetime", "document_amt"]],
        on="document_id", how="inner"
    )

    # property_type "TS" corresponds to the same timeshare-interval noise already identified at
    # the party-classification level (acris_pipeline.py's TIMESHARE_NOISE category) -- confirmed
    # by street address overlap (7 Avenue, West 57th Street -- the same Hilton Club/Manhattan Club
    # buildings). Bulk interval sales at these addresses would otherwise flood the flip and
    # over-mortgaging signals the same way they flooded raw entity-frequency counts earlier.
    ts_bbl_counts = timeshare_bbls(merged)
    n_ts = (merged["property_type"] == "TS").sum()
    merged = merged[merged["property_type"] != "TS"]
    print(f"Excluded {n_ts} timeshare-interval (property_type=TS) rows before signal detection")

    # BBL-level timeshare exclusion (v13): see the comment above TIMESHARE_DEED_TYPE.
    on_ts_bbl = merged["bbl"].isin(ts_bbl_counts.index)
    n_ts_bbl_rows = int(on_ts_bbl.sum())
    merged = merged[~on_ts_bbl]
    top = ", ".join(f"{b} ({n})" for b, n in ts_bbl_counts.sort_values(ascending=False).head(5).items())
    n_small = int((ts_bbl_counts < 5).sum())
    print(f"Excluded {n_ts_bbl_rows} further row(s) on {len(ts_bbl_counts)} timeshare BBL(s) "
          f"(any '{TIMESHARE_DEED_TYPE}' document recorded). Largest: {top or 'none'}")
    if n_small:
        print(f"  ({n_small} of those BBL(s) have fewer than 5 timeshare deeds -- check they really are "
              f"timeshare buildings; raise TIMESHARE_BBL_MIN_DOCS to exclude fewer)")

    return merged


def load_party_lookups(processed_dir="../data/processed"):
    """
    Returns four dicts, all keyed by document_id -> set of name_norm:
      - buyer_names: only parties in BUYER_SIDE_ROLES (a deed's actual buyer(s))
      - seller_names: only parties in SELLER_SIDE_ROLES (a deed's seller(s)/grantor(s))
      - borrower_names: only parties with role == BORROWER_ROLE (an MTGE's borrower(s))
      - all_names: every party on the document regardless of role

    all_names is deliberately role-agnostic because the financing-side document in a
    purchase-money pair isn't always a simple mortgage with MORTGAGOR/MORTGAGEE roles --
    it can be a large multi-party AGREEMENT whose role labels are generic PARTY 1/PARTY 2
    (undecodable as borrower/lender without per-document-type validation, same reasoning
    as case_builder.py's OWNERSHIP_CONNECTING_ROLES comment). Checking "does the deed's
    buyer appear ANYWHERE on this document" is the only check that doesn't depend on
    trusting those generic labels.
    """
    path = os.path.join(processed_dir, "acris_parties_analyzed.csv")
    parties = pd.read_csv(path)
    parties = parties.dropna(subset=["name_norm"])

    buyer_names = defaultdict(set)
    seller_names = defaultdict(set)
    borrower_names = defaultdict(set)
    all_names = defaultdict(set)
    for doc_id, role, name_norm in zip(parties["document_id"], parties["role"], parties["name_norm"]):
        all_names[doc_id].add(name_norm)
        if role in BUYER_SIDE_ROLES:
            buyer_names[doc_id].add(name_norm)
        elif role in SELLER_SIDE_ROLES:
            seller_names[doc_id].add(name_norm)
        if role == BORROWER_ROLE:
            borrower_names[doc_id].add(name_norm)
    return buyer_names, seller_names, borrower_names, all_names


def same_recording_session(doc_id_a, doc_id_b, min_shared_prefix=13):
    """
    ACRIS document_ids are 16 characters, structurally date-plus-sequence. When two documents
    were filed together in the same recording session (a title company recording a whole
    closing's worth of instruments at once, or a bulk batch of related filings), their
    document_ids share a long common prefix and differ only in the last 1-3 digits.

    Confirmed in real data as the dominant explanation for most remaining outliers after every
    other filter: 12 of 14 remaining extreme candidates in one run shared a 13+ character
    document_id prefix. This is a much more direct and reliable signal than date proximity --
    document_date (the execution date on the instrument) can lag the actual recording by weeks,
    while the document_id's embedded sequence directly reflects recording order.

    A same-session pair is NOT two independent transactions -- it's most likely a corrective/
    confirmatory instrument pair, or multiple related deeds/mortgages from one closing recorded
    together, which is exactly the shape that produces a fake enormous "price increase" or
    "mortgage ratio" when treated as if they were unrelated events.
    """
    a, b = str(doc_id_a), str(doc_id_b)
    shared = 0
    for ca, cb in zip(a, b):
        if ca == cb:
            shared += 1
        else:
            break
    return shared >= min_shared_prefix


def flag_bulk_multi_lot(df, amt_col="document_amt", date_col="document_date", date_tolerance_days=30):
    """
    Detect rows that are part of a shared economic amount recorded against MORE THAN ONE
    distinct property. This catches three related artifacts:
      1. A single document_id legally spanning multiple lots (a bulk/portfolio sale, or a
         blanket mortgage) -- its one recorded amount gets joined against every lot it touches.
      2. Multi-unit development financing recorded as SEPARATE instruments per lot (spreader /
         consolidation & extension mortgages), each carrying the FULL project loan amount under
         a DIFFERENT document_id -- same economic loan, different document numbers.
      3. The same as #2, recorded across a wide date range because a large building's sellout
         takes months.

    A PRECISE (non-round) amount is flagged wherever it spans >1 distinct property, regardless
    of date -- two unrelated developments will never coincidentally share an 11-digit,
    cent-precise figure, so no date check is needed or helpful here.

    A SUSPICIOUSLY ROUND amount (e.g. $2,100,000.00) genuinely can recur by coincidence across
    unrelated transactions somewhere in a 285k-row dataset, so it still needs a date check --
    but that check must be done via TEMPORAL CLUSTERING (chaining consecutive occurrences where
    the gap is within tolerance), not a single global min/max span. A global span was proven
    wrong in practice: two lots sharing the identical $2,100,000 mortgage on the same date were
    NOT excluded, because some unrelated coincidental $2,100,000 mortgage elsewhere in the
    dataset, recorded months away, pulled the overall min/max span past the tolerance -- one
    distant coincidence was silently protecting an obviously real duplicate.

    Currency values must never be compared by exact float equality -- sub-cent floating-point
    noise can make two genuinely identical dollar amounts fail to match bit-for-bit -- so
    amounts are rounded to the cent before grouping.

    Returns the set of DataFrame index labels to exclude (row-level, not amount-level, since
    a round amount's occurrences can split into an excluded cluster and unrelated singletons).
    """
    def is_suspiciously_round(amt):
        return (amt % 1000 == 0) or (amt % 500 == 0)

    rounded_amt = df[amt_col].round(2)
    working = df.assign(_rounded=rounded_amt)

    excluded_idx = set()
    for amt, group in working.groupby("_rounded"):
        if group["bbl"].nunique() <= 1:
            continue  # only one property involved -- nothing to exclude regardless of amount type
        if not is_suspiciously_round(amt):
            excluded_idx.update(group.index)  # precise amount: exclude regardless of date spread
            continue
        # Round amount: chain-cluster by date gap, and only exclude rows within a cluster that
        # itself spans >1 distinct property -- an isolated coincidental match elsewhere is left alone.
        g = group.sort_values(date_col)
        dates, idxs, bbls = g[date_col].tolist(), g.index.tolist(), g["bbl"].tolist()
        cluster_start = 0
        for i in range(1, len(g) + 1):
            if i == len(g) or (dates[i] - dates[i - 1]).days > date_tolerance_days:
                cluster_bbls = set(bbls[cluster_start:i])
                if len(cluster_bbls) > 1:
                    excluded_idx.update(idxs[cluster_start:i])
                cluster_start = i
    return excluded_idx


def detect_rapid_flips(joined):
    """For each property, look at consecutive DEED transactions and flag pairs that are
    both close together in time AND show a large price increase."""
    deeds = joined[joined["doc_type"].isin(DEED_TYPES)].dropna(subset=["document_date"]).copy()

    excluded_idx = flag_bulk_multi_lot(deeds)
    n_bulk = len(excluded_idx)
    deeds = deeds[~deeds.index.isin(excluded_idx)]

    deeds = deeds.sort_values(["bbl", "document_date"])

    flips = []
    n_same_session = 0
    for bbl, group in deeds.groupby("bbl"):
        if len(group) < 2:
            continue
        rows = group.to_dict("records")
        for i in range(len(rows) - 1):
            a, b = rows[i], rows[i + 1]
            days_between = (b["document_date"] - a["document_date"]).days
            if days_between < 0 or days_between > FLIP_WINDOW_DAYS:
                continue
            if pd.isna(a["document_amt"]) or pd.isna(b["document_amt"]) or a["document_amt"] < MIN_REAL_PRICE or b["document_amt"] < MIN_REAL_PRICE:
                continue  # can't compute a meaningful price change without two real sale amounts
            if same_recording_session(a["document_id"], b["document_id"]):
                n_same_session += 1
                continue  # same closing's paperwork (e.g. corrective/confirmatory deed), not a real flip
            pct_change = (b["document_amt"] - a["document_amt"]) / a["document_amt"]
            if pct_change >= FLIP_PRICE_INCREASE_PCT:
                # A mismatch here means the two "sales" likely refer to different legal interests
                # recorded under the same BBL (e.g. a garage/storage allocation vs. the residential
                # unit itself) -- not a real flip of the same asset. Flag rather than silently drop,
                # since we can't fully decode these type codes and a false negative here (missing a
                # real flip) is worse than asking a human to glance at the flag.
                type_mismatch = (
                    (pd.notna(a.get("property_type")) and pd.notna(b.get("property_type")) and
                     a.get("property_type") != b.get("property_type")) or
                    (pd.notna(a.get("unit")) and pd.notna(b.get("unit")) and a.get("unit") != b.get("unit"))
                )
                flips.append({
                    "bbl": bbl,
                    "sale1_date": a["document_date"], "sale1_amt": a["document_amt"],
                    "sale1_doc_id": a["document_id"], "sale1_unit": a.get("unit"), "sale1_type": a.get("property_type"),
                    "sale2_date": b["document_date"], "sale2_amt": b["document_amt"],
                    "sale2_doc_id": b["document_id"], "sale2_unit": b.get("unit"), "sale2_type": b.get("property_type"),
                    "days_between": days_between,
                    "pct_price_increase": round(pct_change * 100, 1),
                    "unit_or_type_mismatch": type_mismatch,
                })
    n_mismatch = sum(f["unit_or_type_mismatch"] for f in flips)
    flips_df = pd.DataFrame(flips)

    # A genuine flip is a one-off event for a given property. A BBL appearing repeatedly in this
    # list (confirmed in real data: one unit recurred 9 times across a year, always the same
    # low-price-then-same-day-high-price shape) is much more likely a structural/regulatory
    # recording pattern for that specific unit (e.g. income-restricted resale documentation,
    # or a second timeshare-style property using a different property_type than the one already
    # excluded) than 9 independent flip events. Split these out rather than count them as candidates.
    RECURRING_THRESHOLD = 2
    if not flips_df.empty:
        bbl_counts = flips_df["bbl"].value_counts()
        recurring_bbls = set(bbl_counts[bbl_counts >= RECURRING_THRESHOLD].index)
        recurring = flips_df[flips_df["bbl"].isin(recurring_bbls)]
        flips_df = flips_df[~flips_df["bbl"].isin(recurring_bbls)]
    else:
        recurring = flips_df

    print(f"  ({n_bulk} deed rows excluded as bulk/portfolio sales spanning multiple "
          f"properties, before flip detection)")
    print(f"  ({n_same_session} candidate pair(s) skipped as same-recording-session "
          f"document pairs, not independent transactions)")
    print(f"  ({n_mismatch} of {len(flips)} candidate flips have a unit/property_type mismatch "
          f"between the two legs -- likely different legal interests under the same BBL, not a "
          f"real flip; still included in output but flagged, not silently dropped)")
    if not recurring.empty:
        print(f"  ({len(recurring)} flip(s) removed as RECURRING PATTERN on "
              f"{recurring['bbl'].nunique()} BBL(s) appearing {RECURRING_THRESHOLD}+ times -- "
              f"likely structural/regulatory recording pattern, not independent flips: "
              f"{sorted(recurring['bbl'].unique())}")
    return flips_df, recurring


def detect_over_mortgaging(joined, buyer_names, seller_names, all_names, exclusion_log=None):
    """
    Pair each DEED with the nearest MTGE-class document recorded shortly after it on the
    same property (purchase-money financing), and flag where the loan amount is high
    relative to price.

    PARTY-OVERLAP SAFEGUARD: same-BBL, same-time-window proximity is not by itself evidence
    that a "mortgage-class" document actually finances a given deed. Confirmed in real data:
    a $7.1M deed paired against a document recorded 18 days later on the same BBL produced a
    13.5x ratio -- but that document turned out to be a 177-page multi-property AGREEMENT
    between two entities with ZERO relationship to the deed's actual buyer, almost certainly
    referencing this BBL as one of many parcels in an unrelated portfolio financing. Before
    picking the "nearest" mortgage-class document in the matching window, any candidate whose
    parties have NO overlap with the deed's buyer is excluded -- the same treatment already
    given to same-recording-session pairs in detect_rapid_flips: proximity that's confirmed
    not to represent a real relationship gets excluded outright, not just flagged. This is
    only applied when BOTH sides have resolvable party names; if either side's parties can't
    be determined, the candidate proceeds exactly as before rather than guessing.

    AUDIT LOG: if `exclusion_log` (a list) is passed, one dict per party-overlap exclusion
    is appended to it -- the deed, the excluded financing document, their ratio, and both
    sides' names -- so the exclusion can be spot-checked for false exclusions (a real
    purchase-money pair dropped because the exact name_norm differs by spelling/variant).
    Purely observational: it never changes what is excluded.
    """
    deeds = joined[joined["doc_type"].isin(DEED_TYPES)].dropna(subset=["document_date", "document_amt"]).copy()
    deeds = deeds[deeds["document_amt"] >= MIN_REAL_PRICE]  # exclude nominal/token consideration
    mtges = joined[joined["doc_type"].isin(GENUINE_MTGE_TYPES)].dropna(subset=["document_date", "document_amt"]).copy()
    mtges = mtges[mtges["document_amt"] > 0]

    # A mortgage that covers MULTIPLE properties has its full face amount recorded once, but
    # that amount isn't attributable to any single parcel -- comparing it against each
    # property's price independently massively overstates the ratio for every one it touches.
    # This includes both a single document_id legally spanning multiple lots, AND multi-unit
    # development financing recorded as separate spreader/consolidation instruments per lot
    # (different document_ids, same economic loan amount) -- flag_bulk_multi_lot catches both.
    excluded_idx = flag_bulk_multi_lot(mtges)
    n_blanket = len(excluded_idx)
    mtges = mtges[~mtges.index.isin(excluded_idx)]

    # When a property has MULTIPLE deeds on the exact same date, this is very likely one closing
    # recorded as several instruments -- a nominal/token deed alongside the real one (an LLC
    # transfer, a corrective deed), not two independent sales. Confirmed in real data: BBL
    # 2-3177-15 showed a $70,000 deed AND a $725,000 deed on the same date, both independently
    # paired against the same mortgage -- the $70,000 leg produced a fake 132x ratio. Collapsing
    # to the maximum deed amount per (bbl, date) uses the more plausible real price and removes
    # the artificial duplicate entirely.
    deeds = deeds.loc[deeds.groupby(["bbl", "document_date"])["document_amt"].idxmax()]

    results = []
    n_no_party_overlap = 0
    n_self_transfer = 0
    for bbl, deed_group in deeds.groupby("bbl"):
        candidate_mtges = mtges[mtges["bbl"] == bbl]
        if candidate_mtges.empty:
            continue
        for _, deed in deed_group.iterrows():
            window_mtges = candidate_mtges[
                (candidate_mtges["document_date"] >= deed["document_date"]) &
                (candidate_mtges["document_date"] <= deed["document_date"] + pd.Timedelta(days=PURCHASE_MONEY_WINDOW_DAYS))
            ]
            if window_mtges.empty:
                continue

            deed_buyers = buyer_names.get(deed["document_id"], set())

            # SELF-TRANSFER GUARD: if any buyer-side name also appears on the deed's seller side,
            # this is a co-owner/title-clearing transfer (e.g. removing a spouse from title), not
            # an arm's-length sale -- its recorded consideration is nominal, so a ratio computed
            # against it is meaningless. Excluded outright, same treatment as same-session pairs.
            if deed_buyers & seller_names.get(deed["document_id"], set()):
                n_self_transfer += 1
                continue

            if deed_buyers:
                # Filter BEFORE picking "nearest" -- an unrelated instrument that merely
                # recorded first would otherwise silently shadow a genuinely related
                # mortgage recorded a day or two later in the same window.
                valid_doc_ids = [
                    did for did in window_mtges["document_id"]
                    if not all_names.get(did) or (deed_buyers & all_names.get(did, set()))
                ]
                if exclusion_log is not None:
                    valid_set = set(valid_doc_ids)
                    for _, ex in window_mtges[~window_mtges["document_id"].isin(valid_set)].iterrows():
                        exclusion_log.append({
                            "bbl": bbl,
                            "deed_doc_id": deed["document_id"], "deed_date": deed["document_date"],
                            "deed_amt": deed["document_amt"],
                            "mtge_doc_id": ex["document_id"], "mtge_date": ex["document_date"],
                            "mtge_amt": ex["document_amt"],
                            "days_between": (ex["document_date"] - deed["document_date"]).days,
                            "mortgage_to_price_ratio": round(ex["document_amt"] / deed["document_amt"], 3),
                            "deed_buyer_names": " | ".join(sorted(deed_buyers)),
                            "mtge_doc_names": " | ".join(sorted(all_names.get(ex["document_id"], set()))),
                        })
                n_no_party_overlap += len(window_mtges) - len(valid_doc_ids)
                window_mtges = window_mtges[window_mtges["document_id"].isin(valid_doc_ids)]
                if window_mtges.empty:
                    continue

            # nearest mortgage after the deed, among party-validated (or unresolvable) candidates
            mtge = window_mtges.sort_values("document_date").iloc[0]
            ratio = mtge["document_amt"] / deed["document_amt"]
            if ratio >= OVER_MORTGAGE_RATIO:
                type_mismatch = (
                    (pd.notna(deed.get("property_type")) and pd.notna(mtge.get("property_type")) and
                     deed.get("property_type") != mtge.get("property_type")) or
                    (pd.notna(deed.get("unit")) and pd.notna(mtge.get("unit")) and deed.get("unit") != mtge.get("unit"))
                )
                results.append({
                    "bbl": bbl,
                    "deed_date": deed["document_date"], "deed_amt": deed["document_amt"],
                    "deed_doc_id": deed["document_id"], "deed_unit": deed.get("unit"), "deed_type": deed.get("property_type"),
                    "mtge_date": mtge["document_date"], "mtge_amt": mtge["document_amt"],
                    "mtge_doc_id": mtge["document_id"], "mtge_unit": mtge.get("unit"), "mtge_type": mtge.get("property_type"),
                    "days_between": (mtge["document_date"] - deed["document_date"]).days,
                    "mortgage_to_price_ratio": round(ratio, 3),
                    "unit_or_type_mismatch": type_mismatch,
                })
    result_df = pd.DataFrame(results)
    n_mismatch = sum(r["unit_or_type_mismatch"] for r in results)

    # The blanket-amount check above only catches a SHARED exact amount. A large development's
    # construction loan can instead show up as DIFFERENT amounts per unit that are clearly one
    # declining balance being paid down as units close -- confirmed in real data: a tower with
    # units on floors 21, 29, 33, 51, 66 showed mortgages of $81.1M / $77.3M / $65.7M / $61.6M /
    # $57.6M, a clean monotonically decreasing sequence by closing date. No amount-matching
    # approach can catch this since the numbers genuinely differ. Instead: if a single BLOCK
    # (borough+block, ignoring lot) has several DIFFERENT lots independently flagged as
    # over-mortgaged, that's much more likely one building's shared/declining project financing
    # than several coincidentally-unrelated over-mortgaging events on adjacent lots.
    BLOCK_CLUSTER_THRESHOLD = 3
    if not result_df.empty:
        result_df["_block"] = result_df["bbl"].str.rsplit("-", n=1).str[0]
        lots_per_block = result_df.groupby("_block")["bbl"].nunique()
        suspect_blocks = set(lots_per_block[lots_per_block >= BLOCK_CLUSTER_THRESHOLD].index)
        block_pattern = result_df[result_df["_block"].isin(suspect_blocks)].drop(columns="_block")
        result_df = result_df[~result_df["_block"].isin(suspect_blocks)].drop(columns="_block")
    else:
        block_pattern = result_df

    print(f"  ({n_blanket} mortgage rows excluded as blanket/multi-property financing)")
    print(f"  ({n_no_party_overlap} candidate financing document(s) excluded as having no party "
          f"overlap between the deed's buyer and the document's parties -- likely an unrelated "
          f"instrument coincidentally recorded on the same BBL within the matching window)")
    print(f"  ({n_self_transfer} deed(s) skipped as self-transfers -- a buyer-side name also "
          f"appears on the seller side, so the recorded price is nominal, not a real sale)")
    print(f"  ({n_mismatch} of {len(results)} candidates have a unit/property_type mismatch "
          f"between the deed and mortgage -- still included but flagged, not silently dropped)")
    if not block_pattern.empty:
        print(f"  ({len(block_pattern)} candidate(s) removed as BUILDING-LEVEL BLANKET FINANCING "
              f"SUSPECTED -- {block_pattern['bbl'].str.rsplit('-', n=1).str[0].nunique()} block(s) "
              f"with {BLOCK_CLUSTER_THRESHOLD}+ distinct lots each independently over-mortgaged, "
              f"likely one shared/declining construction loan, not independent fraud candidates: "
              f"{sorted(block_pattern['bbl'].unique())}")
    return result_df, block_pattern


SCRIPT_VERSION = "v13-timeshare-bbl-exclusion"


def detect_serial_refinancing(joined, borrower_names):
    """
    Flags properties with two or more GENUINE new-money mortgage recordings close together
    in time, with NO deed (ownership transfer) recorded between them -- the same property
    taking on new mortgage debt repeatedly in a short window, without a sale in between to
    explain why fresh financing was needed again.

    This is a different shape of equity-stripping than detect_over_mortgaging: that one
    catches one aggressive loan against one purchase at a single moment; this one catches a
    PATTERN of repeated borrowing over time on a property that hasn't changed hands -- a
    separate phantom-equity typology (serial equity extraction), not a more sensitive
    version of the same check.

    Uses recorded_datetime, not document_date -- see the module docstring's DATE FIELD
    CAVEAT. This signal was designed after that caveat was found, so it never inherits the
    document_date reliability problem detect_rapid_flips and detect_over_mortgaging still
    carry.

    BORROWER-CHANGE HANDLING: unlike detect_over_mortgaging's party-overlap check, which
    EXCLUDES a confirmed-unrelated financing document outright, a borrower change between
    two consecutive mortgages on the same property is FLAGGED, not excluded. The two
    situations aren't equivalent: an unrelated document coincidentally sharing a BBL is
    confirmed noise, but a changed borrower with no recorded deed in between could itself
    BE the interesting signal -- an undisclosed change of control -- so it's surfaced for a
    human to look at rather than silently dropped either way.

    INPUT GUARDS (v11): duplicate (bbl, document_id) rows are dropped so a document is never
    paired with itself, and mortgages whose exact amount recurs across several properties
    (one project loan recorded per unit) are excluded via flag_bulk_multi_lot.

    SESSION GUARD (v12): two consecutive mortgages whose document_ids share a recording-session
    prefix (same_recording_session) are one closing recorded as several instruments -- spreader,
    consolidation or gap mortgages, split notes -- not repeated borrowing. In the Nov 20 run 19 of
    the 20 printed pairs were of this kind. Such pairs are skipped and counted, never silently lost.
    The guard is deliberately NOT applied to detect_over_mortgaging, where a deed and its purchase
    mortgage are legitimately recorded together.
    """
    mtges = joined[joined["doc_type"].isin(GENUINE_MTGE_TYPES)].dropna(
        subset=["recorded_datetime", "document_amt"]
    ).copy()
    mtges = mtges[mtges["document_amt"] > 0]

    # DUPLICATE-DOCUMENT GUARD: one document_id can span several legal entries for the same BBL.
    # Without this the same mortgage is paired against itself (0 days, $0 "increase").
    n_before = len(mtges)
    mtges = mtges.drop_duplicates(subset=["bbl", "document_id"])
    n_dupes = n_before - len(mtges)

    # BLANKET-FINANCING GUARD: one project loan recorded per unit looks like every unit refinancing
    # twice. Same shared-amount-across-properties test the flip and over-mortgaging detectors use,
    # keyed on recorded_datetime (document_date is unreliable on some document types).
    blanket_idx = flag_bulk_multi_lot(mtges, amt_col="document_amt", date_col="recorded_datetime")
    n_blanket = len(blanket_idx)
    mtges = mtges[~mtges.index.isin(blanket_idx)]
    print(f"  (serial refinancing input: removed {n_dupes} duplicate-document row(s) and "
          f"{n_blanket} blanket-financing row(s) before pairing)")

    deeds = joined[joined["doc_type"].isin(DEED_TYPES)].dropna(subset=["recorded_datetime"]).copy()
    deed_dates_by_bbl = deeds.groupby("bbl")["recorded_datetime"].apply(list).to_dict()

    results = []
    n_same_session = 0
    for bbl, mtge_group in mtges.groupby("bbl"):
        if len(mtge_group) < 2:
            continue
        deed_dates = sorted(deed_dates_by_bbl.get(bbl, []))
        rows = mtge_group.sort_values("recorded_datetime").to_dict("records")
        for i in range(len(rows) - 1):
            a, b = rows[i], rows[i + 1]
            days_between = (b["recorded_datetime"] - a["recorded_datetime"]).days
            if days_between < 0 or days_between > REFI_WINDOW_DAYS:
                continue
            # A deed recorded strictly between the two mortgages means the property changed
            # hands (or at least a conveyance was recorded) in between -- that's new-owner
            # purchase-money financing, already covered by detect_over_mortgaging, not serial
            # refinancing by an unchanged owner.
            if any(a["recorded_datetime"] < dd < b["recorded_datetime"] for dd in deed_dates):
                continue
            # SESSION GUARD: same closing's paperwork, not repeated borrowing (see docstring).
            if same_recording_session(a["document_id"], b["document_id"]):
                n_same_session += 1
                continue
            borrower_a = borrower_names.get(a["document_id"], set())
            borrower_b = borrower_names.get(b["document_id"], set())
            borrower_changed = bool(borrower_a) and bool(borrower_b) and not (borrower_a & borrower_b)
            results.append({
                "bbl": bbl,
                "mtge1_date": a["recorded_datetime"], "mtge1_amt": a["document_amt"], "mtge1_doc_id": a["document_id"],
                "mtge2_date": b["recorded_datetime"], "mtge2_amt": b["document_amt"], "mtge2_doc_id": b["document_id"],
                "days_between": days_between,
                "amt_increase": round(b["document_amt"] - a["document_amt"], 2),
                "borrower_changed": borrower_changed,
            })
    print(f"  ({n_same_session} consecutive-mortgage pair(s) skipped as same-recording-session "
          f"instruments -- one closing recorded as several documents, not repeated borrowing)")
    result_df = pd.DataFrame(results)
    n_borrower_changed = sum(r["borrower_changed"] for r in results)
    print(f"  ({n_borrower_changed} of {len(results)} serial-refinancing candidates show a "
          f"borrower-name change between the two mortgages -- possible undisclosed change of "
          f"control with no recorded deed in between; flagged, not excluded)")
    return result_df


def main():
    print(f"[signals.py {SCRIPT_VERSION}]")
    joined = load_and_join()
    print(f"Joined {len(joined)} (document, property) rows across "
          f"{joined['bbl'].nunique()} distinct properties")

    buyer_names, seller_names, borrower_names, all_names = load_party_lookups()
    print(f"Loaded party names for {len(all_names)} documents "
          f"({len(buyer_names)} with a resolvable buyer-side party)")

    flips, recurring_flips = detect_rapid_flips(joined)
    print(f"\n{len(flips)} rapid-flip candidates "
          f"(resale within {FLIP_WINDOW_DAYS} days, price up {FLIP_PRICE_INCREASE_PCT*100:.0f}%+)")
    if not flips.empty:
        print(flips.sort_values("pct_price_increase", ascending=False).head(20).to_string(index=False))

    overlap_exclusion_log = []
    over_mtg, block_pattern_mtg = detect_over_mortgaging(
        joined, buyer_names, seller_names, all_names, exclusion_log=overlap_exclusion_log
    )
    print(f"\n{len(over_mtg)} over-mortgaging candidates "
          f"(mortgage/{'>' if OVER_MORTGAGE_RATIO<1 else '>='} {OVER_MORTGAGE_RATIO*100:.0f}% of deed price, "
          f"recorded within {PURCHASE_MONEY_WINDOW_DAYS} days of the deed)")
    if not over_mtg.empty:
        print(over_mtg.sort_values("mortgage_to_price_ratio", ascending=False).head(20).to_string(index=False))

    refis = detect_serial_refinancing(joined, borrower_names)
    print(f"\n{len(refis)} serial-refinancing candidates "
          f"(2+ genuine mortgages within {REFI_WINDOW_DAYS} days, no intervening deed)")
    if not refis.empty:
        print(refis.sort_values("days_between").head(20).to_string(index=False))

    out_dir = os.path.join("..", "data", "processed")
    os.makedirs(out_dir, exist_ok=True)
    flips.to_csv(os.path.join(out_dir, "signal_rapid_flips.csv"), index=False)
    over_mtg.to_csv(os.path.join(out_dir, "signal_over_mortgaging.csv"), index=False)
    recurring_flips.to_csv(os.path.join(out_dir, "signal_flips_recurring_pattern.csv"), index=False)
    block_pattern_mtg.to_csv(os.path.join(out_dir, "signal_mortgaging_block_pattern.csv"), index=False)
    refis.to_csv(os.path.join(out_dir, "signal_serial_refinancing.csv"), index=False)
    pd.DataFrame(overlap_exclusion_log).to_csv(
        os.path.join(out_dir, "signal_party_overlap_exclusions.csv"), index=False
    )
    print(f"Wrote {len(overlap_exclusion_log)} party-overlap exclusion row(s).")
    print(f"\nWrote {out_dir}/signal_rapid_flips.csv, signal_over_mortgaging.csv, and "
          f"signal_serial_refinancing.csv (plus the separated-out recurring-pattern and "
          f"block-pattern files)")

    # Guardrail: extreme values this far past a normal candidate are far more likely to be an
    # unexcluded bulk/blanket artifact slipping through than a genuine finding -- surfaced this
    # exact way twice already (a $475M blanket mortgage, a bulk-sale deed at 19,000%+). Rather
    # than rely on manually eyeballing every run for this, flag it automatically every time.
    SANITY_CEILING_PCT = 1000
    SANITY_CEILING_RATIO = 20
    extreme_flips = flips[flips["pct_price_increase"] > SANITY_CEILING_PCT] if not flips.empty else flips
    extreme_mtg = over_mtg[over_mtg["mortgage_to_price_ratio"] > SANITY_CEILING_RATIO] if not over_mtg.empty else over_mtg
    if len(extreme_flips) or len(extreme_mtg):
        print(f"\n*** SANITY CHECK WARNING ***")
        if len(extreme_flips):
            print(f"{len(extreme_flips)} flip(s) show a >{SANITY_CEILING_PCT}% price increase -- "
                  f"this strongly suggests an unexcluded bulk/portfolio artifact, not a real flip. "
                  f"Inspect these specific BBLs before trusting them: {extreme_flips['bbl'].tolist()}")
        if len(extreme_mtg):
            print(f"{len(extreme_mtg)} over-mortgage candidate(s) show a >{SANITY_CEILING_RATIO}x ratio -- "
                  f"this strongly suggests an unexcluded blanket-financing artifact, not real over-mortgaging. "
                  f"Inspect these specific BBLs before trusting them: {extreme_mtg['bbl'].tolist()}")


if __name__ == "__main__":
    main()
