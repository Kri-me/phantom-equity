from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

SALES_PATH = ROOT / "data" / "processed" / "valuation_sales.csv"
LEGAL_PATH = ROOT / "data" / "raw" / "acris_legals_sample.csv"

OUT = ROOT / "outputs" / "valuation_audit" / "refined_eligibility"
OUT.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------
# ACRIS official property-type classifications
# ---------------------------------------------------------

ACRIS = {
    **{
        f"D{i}": ("RESIDENTIAL", f"Dwelling only - {i} family")
        for i in range(1, 7)
    },
    "SC": ("RESIDENTIAL", "Single residential condo unit"),
    "AP": ("RESIDENTIAL", "Apartment building"),
    "RG": ("RESIDENTIAL_REVIEW", "1-2 family dwelling with attached garage"),
    "RV": ("RESIDENTIAL_REVIEW", "1-2 family dwelling with vacant land"),
    "F1": ("MIXED_USE", "1-3 family with store / office"),
    "FS": ("MIXED_USE", "4 family with store / office"),
    "F5": ("MIXED_USE", "5-6 family with store / office"),
    "CR": ("COMMERCIAL", "Commercial real estate"),
    "OF": ("COMMERCIAL", "Office building"),
    "RB": ("COMMERCIAL", "Retail building"),
    "IB": ("SPECIAL_PURPOSE", "Industrial building"),
    "RS": ("SPECIAL_PURPOSE", "Religious structure"),
    "VN": ("SPECIAL_PURPOSE", "Non-residential vacant land"),
    "VR": ("RESIDENTIAL_REVIEW", "Residential vacant land"),
    "GR": ("RESIDENTIAL_REVIEW", "Garage, 1 or 2 family only"),
    "OT": ("UNKNOWN", "Other"),
    "D5": ("RESIDENTIAL", "Dwelling only - 5 family"),
    "D6": ("RESIDENTIAL", "Dwelling only - 6 family"),
}

# PLUTO land-use categories
LANDUSE = {
    1: "RESIDENTIAL",
    2: "RESIDENTIAL",
    3: "RESIDENTIAL",
    4: "MIXED_USE",
    5: "COMMERCIAL",
    6: "NONRESIDENTIAL",
    7: "NONRESIDENTIAL",
    8: "NONRESIDENTIAL",
    9: "NONRESIDENTIAL",
    10: "NONRESIDENTIAL",
    11: "NONRESIDENTIAL",
}

# ---------------------------------------------------------
# Load data
# ---------------------------------------------------------

sales = pd.read_csv(
    SALES_PATH,
    dtype={"document_id": str, "bbl": str},
    low_memory=False
)

legal = pd.read_csv(
    LEGAL_PATH,
    dtype={
        "document_id": str,
        "borough": str,
        "block": str,
        "lot": str,
        "property_type": str,
    },
    low_memory=False
)

required_sales = {
    "document_id", "single_property_deed", "small_residential",
    "price", "bldgclass", "landuse", "borough", "unitsres", "bldgarea"
}

missing = required_sales - set(sales.columns)
if missing:
    raise ValueError(f"Missing sales columns: {sorted(missing)}")

required_legal = {"document_id", "property_type"}
missing = required_legal - set(legal.columns)
if missing:
    raise ValueError(f"Missing legal columns: {sorted(missing)}")

sales["document_id"] = sales["document_id"].astype(str)
legal["document_id"] = legal["document_id"].astype(str)

legal["property_type"] = (
    legal["property_type"].astype("string").str.strip().str.upper()
)

# One record per document, preserving all legal property types.
legal_types = (
    legal.dropna(subset=["property_type"])
    .groupby("document_id")["property_type"]
    .agg(lambda x: sorted(set(x)))
    .rename("acris_types")
    .reset_index()
)

# ---------------------------------------------------------
# Reconstruct current core population
# ---------------------------------------------------------

core = sales[
    sales["single_property_deed"].astype(bool)
    & sales["small_residential"].astype(bool)
    & (pd.to_numeric(sales["price"], errors="coerce") >= 100000)
].copy()

core = core.merge(
    legal_types,
    on="document_id",
    how="left",
    validate="one_to_one"
)

def normalize_acris_types(value):
    if isinstance(value, (list, tuple, set, np.ndarray, pd.Series)):
        return sorted({
            str(x).strip().upper()
            for x in value
            if pd.notna(x)
        })

    if isinstance(value, str) and value.strip():
        return [value.strip().upper()]

    return []


core["acris_types"] = core["acris_types"].apply(normalize_acris_types)

core["landuse"] = pd.to_numeric(core["landuse"], errors="coerce")
core["unitsres"] = pd.to_numeric(core["unitsres"], errors="coerce")
core["bldgarea"] = pd.to_numeric(core["bldgarea"], errors="coerce")

# ---------------------------------------------------------
# Classify ACRIS types at transaction level
# ---------------------------------------------------------

def classify_acris(types):
    if types is None or (
        types.size == 0 if isinstance(types, np.ndarray) else not types
    ):
        return "UNKNOWN"

    groups = [
        ACRIS.get(code, ("UNKNOWN", "Unmapped code"))[0]
        for code in types
    ]

    # Multiple legal types are treated conservatively.
    unique = set(groups)

    if unique == {"RESIDENTIAL"}:
        return "RESIDENTIAL"

    if unique <= {"RESIDENTIAL", "RESIDENTIAL_REVIEW"}:
        return "RESIDENTIAL_REVIEW"

    if "UNKNOWN" in unique:
        return "UNKNOWN"

    if "MIXED_USE" in unique:
        return "MIXED_USE"

    if "COMMERCIAL" in unique:
        return "COMMERCIAL"

    if "SPECIAL_PURPOSE" in unique:
        return "SPECIAL_PURPOSE"

    return "CONFLICT"

core["acris_class"] = core["acris_types"].apply(classify_acris)
core["acris_codes"] = core["acris_types"].apply(
    lambda x: "|".join(x)
)

core["acris_descriptions"] = core["acris_types"].apply(
    lambda codes: " | ".join(
        f"{c}: {ACRIS.get(c, ('UNKNOWN', 'Unmapped code'))[1]}"
        for c in codes
    )
)

# ---------------------------------------------------------
# Classify PLUTO land use
# ---------------------------------------------------------

core["pluto_class"] = core["landuse"].map(LANDUSE).fillna("UNKNOWN")

# ---------------------------------------------------------
# Cross-source agreement
# ---------------------------------------------------------

def agreement(row):
    a = row["acris_class"]
    p = row["pluto_class"]

    if a == "UNKNOWN" or p == "UNKNOWN":
        return "UNRESOLVED"

    if a == "RESIDENTIAL" and p == "RESIDENTIAL":
        return "AGREE_RESIDENTIAL"

    if a == "RESIDENTIAL_REVIEW" and p == "RESIDENTIAL":
        return "RESIDENTIAL_REVIEW"

    if a == p:
        return "AGREE_NONRESIDENTIAL"

    if a == "MIXED_USE" or p == "MIXED_USE":
        return "MIXED_USE_DISAGREEMENT"

    if a == "RESIDENTIAL" and p != "RESIDENTIAL":
        return "ACRIS_RESIDENTIAL_PLUTO_NONRESIDENTIAL"

    if p == "RESIDENTIAL" and a != "RESIDENTIAL":
        return "PLUTO_RESIDENTIAL_ACRIS_NONRESIDENTIAL"

    return "OTHER_DISAGREEMENT"

core["source_agreement"] = core.apply(agreement, axis=1)

# ---------------------------------------------------------
# Three eligibility scenarios
# ---------------------------------------------------------

core["current_eligible"] = True

core["pluto_only_eligible"] = (
    core["landuse"].isin([1, 2, 3])
    & (core["bldgarea"] > 0)
    & (core["unitsres"] >= 1)
)

core["combined_eligible"] = (
    core["landuse"].isin([1, 2, 3])
    & (core["acris_class"] == "RESIDENTIAL")
    & (core["bldgarea"] > 0)
    & (core["unitsres"] >= 1)
)

core["review_required"] = (
    core["acris_class"].isin(
        ["RESIDENTIAL_REVIEW", "UNKNOWN", "CONFLICT"]
    )
    | core["source_agreement"].isin(
        [
            "MIXED_USE_DISAGREEMENT",
            "ACRIS_RESIDENTIAL_PLUTO_NONRESIDENTIAL",
            "PLUTO_RESIDENTIAL_ACRIS_NONRESIDENTIAL",
            "OTHER_DISAGREEMENT",
            "UNRESOLVED",
        ]
    )
)

def decision(row):
    if row["combined_eligible"]:
        return "RETAIN"

    if row["review_required"]:
        return "MANUAL_REVIEW"

    return "EXCLUDE"

core["proposed_decision"] = core.apply(decision, axis=1)

# ---------------------------------------------------------
# Building-class review indicator
# ---------------------------------------------------------

core["building_class_prefix"] = (
    core["bldgclass"].astype("string").str[:1]
)

# This is a review indicator, not a substitute for official
# building-class code interpretation.
core["building_class_family"] = core["building_class_prefix"].map({
    "A": "ONE_FAMILY",
    "B": "TWO_FAMILY",
    "C": "WALKUP_APARTMENT",
    "D": "ELEVATOR_APARTMENT",
    "S": "MIXED_RESIDENTIAL_COMMERCIAL",
    "H": "HOTEL",
    "K": "STORE_OR_RETAIL",
    "O": "OFFICE",
    "F": "FACTORY_OR_INDUSTRIAL",
    "I": "HOSPITAL_OR_INSTITUTIONAL",
    "M": "RELIGIOUS_OR_SPECIAL",
    "G": "GARAGE",
}).fillna("OTHER_OR_UNREVIEWED")

# ---------------------------------------------------------
# Save transaction-level audit
# ---------------------------------------------------------

columns = [
    "document_id", "bbl", "price", "borough",
    "bldgclass", "building_class_family",
    "landuse", "unitsres", "bldgarea",
    "acris_codes", "acris_descriptions",
    "acris_class", "pluto_class",
    "source_agreement",
    "current_eligible", "pluto_only_eligible",
    "combined_eligible", "review_required",
    "proposed_decision",
]

available = [c for c in columns if c in core.columns]

core[available].to_csv(
    OUT / "transaction_level_eligibility.csv",
    index=False
)

# ---------------------------------------------------------
# Summary tables
# ---------------------------------------------------------

def scenario_summary():
    rows = []

    for label, col in [
        ("Current", "current_eligible"),
        ("PLUTO only", "pluto_only_eligible"),
        ("Combined", "combined_eligible"),
    ]:
        subset = core[core[col]]

        rows.append({
            "scenario": label,
            "transactions": len(subset),
            "unique_documents": subset["document_id"].nunique(),
            "unique_properties": (
                subset["bbl"].nunique()
                if "bbl" in subset.columns else np.nan
            ),
            "share_of_current_core": len(subset) / len(core),
        })

    return pd.DataFrame(rows)

scenario_summary().to_csv(
    OUT / "scenario_summary.csv",
    index=False
)

core.groupby(
    ["acris_class", "pluto_class", "source_agreement"],
    dropna=False
).size().rename("transactions").reset_index().to_csv(
    OUT / "source_agreement_summary.csv",
    index=False
)

core.groupby(
    ["acris_codes", "acris_class", "pluto_class"],
    dropna=False
).agg(
    transactions=("document_id", "size"),
    properties=("bbl", "nunique") if "bbl" in core.columns
    else ("document_id", "nunique"),
    median_price=("price", "median"),
    median_units=("unitsres", "median"),
    median_area=("bldgarea", "median"),
).reset_index().to_csv(
    OUT / "property_type_landuse_summary.csv",
    index=False
)

core.groupby(
    ["bldgclass", "building_class_family", "acris_class", "pluto_class"],
    dropna=False
).size().rename("transactions").reset_index().to_csv(
    OUT / "building_class_crosscheck.csv",
    index=False
)

core[core["proposed_decision"] == "MANUAL_REVIEW"][
    available
].to_csv(
    OUT / "manual_review_transactions.csv",
    index=False
)

core[core["proposed_decision"] == "EXCLUDE"][
    available
].to_csv(
    OUT / "excluded_transactions.csv",
    index=False
)

# ---------------------------------------------------------
# Console summary
# ---------------------------------------------------------

print("\n========== CORE RECONSTRUCTION ==========")
print("Rows:", len(core))
print("Unique documents:", core["document_id"].nunique())

print("\n========== SCENARIO SUMMARY ==========")
print(scenario_summary().to_string(index=False))

print("\n========== ACRIS / PLUTO AGREEMENT ==========")
print(
    core["source_agreement"]
    .value_counts(dropna=False)
    .to_string()
)

print("\n========== PROPOSED DECISIONS ==========")
print(
    core["proposed_decision"]
    .value_counts(dropna=False)
    .to_string()
)

print("\n========== UNMAPPED ACRIS TYPES ==========")
all_codes = sorted({
    code
    for types in core["acris_types"]
    for code in types
})

unmapped = [c for c in all_codes if c not in ACRIS]
print(unmapped if unmapped else "None")

print("\nAudit outputs:", OUT)