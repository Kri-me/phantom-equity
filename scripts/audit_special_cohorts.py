from pathlib import Path
import pandas as pd
import numpy as np

# ============================================================
# PATHS
# ============================================================

ROOT = Path(".")
ELIGIBILITY_PATH = (
    ROOT / "outputs/valuation_audit/refined_eligibility/"
    "transaction_level_eligibility.csv"
)
ATTRIBUTES_PATH = ROOT / "data/processed/property_attributes.csv"

OUT = ROOT / "outputs/valuation_audit/special_cohorts"
OUT.mkdir(parents=True, exist_ok=True)

COHORTS = ["SC", "AP", "RG", "RV", "GR", "VR", "D5", "D6"]

ATTRIBUTE_COLUMNS = [
    "bbl", "attr_status", "lot_number", "in_pluto",
    "lotarea", "bldgarea", "resarea", "comarea",
    "numfloors", "numbldgs", "unitsres", "unitstotal",
    "landuse", "bldgclass", "yearbuilt", "condono",
    "histdist", "landmark", "assessland", "assesstot"
]

# ============================================================
# LOAD
# ============================================================

print("Loading eligibility and property attributes...")

elig = pd.read_csv(
    ELIGIBILITY_PATH,
    dtype={"document_id": str, "bbl": str, "acris_codes": str},
    low_memory=False
)

attrs = pd.read_csv(
    ATTRIBUTES_PATH,
    dtype={"bbl": str},
    low_memory=False
)

attrs = attrs[[c for c in ATTRIBUTE_COLUMNS if c in attrs.columns]].copy()

# Avoid duplicate attribute rows creating transaction multiplication.
duplicate_bbls = attrs["bbl"].duplicated(keep=False).sum()

if duplicate_bbls:
    print(f"WARNING: {duplicate_bbls} attribute rows have duplicate BBLs.")
    attrs = attrs.drop_duplicates("bbl", keep="first")

# ============================================================
# NORMALIZE COHORT CODES
# ============================================================

elig["acris_codes"] = (
    elig["acris_codes"]
    .fillna("")
    .astype(str)
    .str.strip()
    .str.upper()
)

# The existing eligibility table has one ACRIS code per transaction
# in the observed sample. Keep exact matching to avoid substring errors.
elig["cohort"] = elig["acris_codes"]

target = elig[elig["cohort"].isin(COHORTS)].copy()

print("\n========== COHORT EXTRACTION ==========")
print("Eligibility rows:", len(elig))
print("Selected transactions:", len(target))
print("Selected unique documents:", target["document_id"].nunique())
print("\n", target["cohort"].value_counts().sort_index().to_string())

# ============================================================
# JOIN
# ============================================================

d = target.merge(
    attrs,
    on="bbl",
    how="left",
    validate="many_to_one",
    suffixes=("", "_pluto"),
    indicator=True
)

print("\n========== JOIN INTEGRITY ==========")
print(d["_merge"].value_counts(dropna=False).to_string())
print("Transactions:", len(d))
print("Unique documents:", d["document_id"].nunique())

# ============================================================
# NORMALIZE NUMERIC FIELDS
# ============================================================

numeric_cols = [
    "lot_number", "lotarea", "bldgarea", "resarea", "comarea",
    "numfloors", "numbldgs", "unitsres", "unitstotal",
    "landuse", "yearbuilt", "condono", "assessland", "assesstot"
]

for c in numeric_cols:
    if c in d.columns:
        d[c] = pd.to_numeric(d[c], errors="coerce")

# ============================================================
# LOT NUMBER CLASSIFICATION
# ============================================================

def classify_lot(value):
    if pd.isna(value):
        return "UNKNOWN"
    value = int(value)
    if 1001 <= value <= 6999:
        return "CONDO_UNIT_STYLE_LOT"
    if 7501 <= value <= 7599:
        return "CONDO_BILLING_STYLE_LOT"
    return "OTHER_LOT_NUMBER"

d["lot_number_type"] = d["lot_number"].apply(classify_lot)

# ============================================================
# DERIVED DIAGNOSTIC FIELDS
# ============================================================

d["residential_area_share"] = np.where(
    d["bldgarea"] > 0,
    d["resarea"] / d["bldgarea"],
    np.nan
)

d["commercial_area_share"] = np.where(
    d["bldgarea"] > 0,
    d["comarea"] / d["bldgarea"],
    np.nan
)

d["building_to_lot_ratio"] = np.where(
    d["lotarea"] > 0,
    d["bldgarea"] / d["lotarea"],
    np.nan
)

d["pluto_attribute_match"] = d["_merge"].eq("both")

d["condono_present"] = (
    d["condono"].notna()
    if "condono" in d.columns
    else False
)

# ============================================================
# DIAGNOSTIC DECISIONS
# ============================================================

def diagnostic_decision(row):
    code = row["cohort"]
    landuse = row.get("landuse")
    bldgclass = row.get("bldgclass")
    units = row.get("unitsres")
    matched = row["pluto_attribute_match"]

    if not matched:
        return "MANUAL_REVIEW_NO_PLUTO_MATCH"

    if code == "SC":
        return "SEPARATE_CONDO_COHORT"

    if code == "AP":
        return "SEPARATE_APARTMENT_COHORT"

    if code == "RG":
        if landuse == 1:
            return "RECOVERY_CANDIDATE_RG_RESIDENTIAL"
        return "MANUAL_REVIEW_RG_NONSTANDARD_LANDUSE"

    if code == "RV":
        return "MANUAL_REVIEW_RV_VACANT_LAND_COMPONENT"

    if code == "GR":
        return "MANUAL_REVIEW_GR_GARAGE_CLASSIFICATION"

    if code == "VR":
        return "MANUAL_REVIEW_VR_VACANT_LAND_CONFLICT"

    if code in ["D5", "D6"]:
        return "MANUAL_REVIEW_D5_D6_UNIT_COUNT_CONFLICT"

    return "UNCLASSIFIED"

d["diagnostic_decision"] = d.apply(diagnostic_decision, axis=1)

# ============================================================
# 1. COHORT PROFILE
# ============================================================

profile_rows = []

for code, g in d.groupby("cohort", dropna=False):
    row = {
        "cohort": code,
        "transactions": len(g),
        "unique_documents": g["document_id"].nunique(),
        "unique_properties": g["bbl"].nunique(),
        "median_price": g["price"].median(),
        "pluto_match_count": g["pluto_attribute_match"].sum(),
        "pluto_match_pct": g["pluto_attribute_match"].mean() * 100,
    }

    for col in [
        "lotarea", "bldgarea", "resarea", "comarea",
        "numfloors", "numbldgs", "unitsres", "unitstotal",
        "yearbuilt", "condono", "residential_area_share",
        "commercial_area_share", "building_to_lot_ratio"
    ]:
        if col in g.columns:
            row[f"{col}_median"] = g[col].median()
            row[f"{col}_missing"] = g[col].isna().sum()

    profile_rows.append(row)

profile = pd.DataFrame(profile_rows)
profile.to_csv(OUT / "cohort_profiles.csv", index=False)

print("\n========== COHORT PROFILES ==========")
print(profile.to_string(index=False))

# ============================================================
# 2. LAND USE DISTRIBUTION
# ============================================================

landuse = (
    d.groupby(["cohort", "landuse"], dropna=False)
    .size()
    .reset_index(name="transactions")
)

landuse.to_csv(OUT / "cohort_landuse.csv", index=False)

print("\n========== LAND USE ==========")
print(landuse.to_string(index=False))

# ============================================================
# 3. BUILDING CLASS DISTRIBUTION
# ============================================================

building_class = (
    d.groupby(["cohort", "bldgclass"], dropna=False)
    .size()
    .reset_index(name="transactions")
    .sort_values(["cohort", "transactions"], ascending=[True, False])
)

building_class.to_csv(OUT / "cohort_building_classes.csv", index=False)

# ============================================================
# 4. CONDOMINIUM INVESTIGATION
# ============================================================

sc = d[d["cohort"] == "SC"].copy()

if len(sc):
    condo_lots = (
        sc.groupby(["lot_number_type", "attr_status"], dropna=False)
        .agg(
            transactions=("document_id", "size"),
            properties=("bbl", "nunique"),
            condo_numbers_present=("condono", "count"),
            median_area=("bldgarea", "median"),
            median_units=("unitsres", "median")
        )
        .reset_index()
    )

    condo_lots.to_csv(OUT / "sc_condo_lot_analysis.csv", index=False)

    condo_bbls = sc[
        [
            "document_id", "bbl", "price", "lot_number",
            "lot_number_type", "attr_status", "in_pluto",
            "condono", "bldgclass", "landuse",
            "lotarea", "bldgarea", "resarea",
            "unitsres", "unitstotal", "yearbuilt",
            "diagnostic_decision"
        ]
    ].copy()

    condo_bbls.to_csv(OUT / "sc_transaction_details.csv", index=False)

    print("\n========== SC CONDOMINIUM LOT ANALYSIS ==========")
    print(condo_lots.to_string(index=False))
else:
    print("\nNo SC transactions found in eligibility table.")

# ============================================================
# 5. RG RECOVERY CANDIDATES
# ============================================================

rg = d[d["cohort"] == "RG"].copy()

if len(rg):
    rg_summary = (
        rg.groupby(["landuse", "bldgclass"], dropna=False)
        .agg(
            transactions=("document_id", "size"),
            properties=("bbl", "nunique"),
            median_price=("price", "median"),
            median_units=("unitsres", "median"),
            median_building_area=("bldgarea", "median"),
            median_residential_area=("resarea", "median"),
            median_commercial_area=("comarea", "median"),
            median_residential_share=("residential_area_share", "median")
        )
        .reset_index()
    )

    rg_summary.to_csv(OUT / "rg_recovery_profile.csv", index=False)

# ============================================================
# 6. TRANSACTION-LEVEL OUTPUT
# ============================================================

output_columns = [
    "document_id", "bbl", "price", "cohort",
    "acris_descriptions", "acris_class", "pluto_class",
    "source_agreement", "bldgclass", "building_class_family",
    "landuse", "unitsres", "bldgarea",
    "attr_status", "lot_number", "lot_number_type",
    "in_pluto", "condono", "lotarea", "resarea",
    "comarea", "numfloors", "numbldgs", "unitstotal",
    "yearbuilt", "histdist", "landmark",
    "residential_area_share", "commercial_area_share",
    "building_to_lot_ratio", "proposed_decision",
    "diagnostic_decision"
]

output_columns = [c for c in output_columns if c in d.columns]

d[output_columns].to_csv(
    OUT / "special_cohort_transaction_details.csv",
    index=False
)

# ============================================================
# 7. DECISION SUMMARY
# ============================================================

decision_summary = (
    d.groupby(["cohort", "diagnostic_decision"], dropna=False)
    .agg(
        transactions=("document_id", "size"),
        properties=("bbl", "nunique")
    )
    .reset_index()
)

decision_summary.to_csv(
    OUT / "special_cohort_decision_summary.csv",
    index=False
)

# ============================================================
# 8. DATA QUALITY
# ============================================================

quality_rows = []

for code, g in d.groupby("cohort"):
    for col in [
        "lot_number", "lotarea", "bldgarea", "resarea",
        "comarea", "unitsres", "landuse", "condono"
    ]:
        if col not in g.columns:
            continue

        quality_rows.append({
            "cohort": code,
            "field": col,
            "rows": len(g),
            "missing": g[col].isna().sum(),
            "missing_pct": g[col].isna().mean() * 100,
            "zero_values": (g[col] == 0).sum()
        })

quality = pd.DataFrame(quality_rows)
quality.to_csv(OUT / "special_cohort_data_quality.csv", index=False)

# ============================================================
# FINAL REPORT
# ============================================================

print("\n========== DIAGNOSTIC DECISIONS ==========")
print(decision_summary.to_string(index=False))

print("\n========== DATA QUALITY ==========")
print(quality.to_string(index=False))

print("\n========== OUTPUT FILES ==========")
for f in sorted(OUT.glob("*.csv")):
    print(f"{f.name}: {f.stat().st_size:,} bytes")

print("\nAudit complete. No original files or model scripts were modified.")
