import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "valuation_audit" / "eligibility_audit"
OUT.mkdir(parents=True, exist_ok=True)

sales = pd.read_csv(
    ROOT / "data" / "processed" / "valuation_sales.csv",
    dtype={"document_id": str, "bbl": str},
    low_memory=False
)

legal = pd.read_csv(
    ROOT / "data" / "raw" / "acris_legals_sample.csv",
    dtype={"document_id": str},
    low_memory=False
)

# --------------------------------------------------
# CORE TRAINING POPULATION
# --------------------------------------------------

for col in ["single_property_deed", "small_residential"]:
    sales[col] = (
        sales[col].astype(str).str.lower()
        .isin(["true", "1"])
    )

core = sales[
    sales["single_property_deed"]
    & sales["small_residential"]
    & (sales["price"] >= 100000)
].copy()

print("\n========== CORE POPULATION ==========")
print("Rows:", len(core))
print("Documents:", core["document_id"].nunique())
print("Properties:", core["bbl"].nunique())

# --------------------------------------------------
# BUILDING CLASS DISTRIBUTION
# --------------------------------------------------

def distribution(df, column):
    result = (
        df.groupby(column, dropna=False)
        .agg(
            transactions=("document_id", "nunique"),
            properties=("bbl", "nunique"),
            median_price=("price", "median"),
            median_units=("unitsres", "median"),
            median_area=("bldgarea", "median")
        )
        .sort_values("transactions", ascending=False)
    )

    result["transaction_share"] = (
        result["transactions"] / result["transactions"].sum()
    )

    return result


for col in ["bldgclass", "landuse", "borough"]:
    result = distribution(core, col)

    print(f"\n========== BY {col.upper()} ==========")
    print(result.to_string())

    result.to_csv(OUT / f"core_by_{col}.csv")

# --------------------------------------------------
# ACRIS LEGAL PROPERTY TYPE
# --------------------------------------------------

legal_types = (
    legal.groupby("document_id")
    .agg(
        legal_rows=("document_id", "size"),
        legal_bbls=("lot", "size"),
        property_types=(
            "property_type",
            lambda x: "|".join(sorted(x.dropna().astype(str).unique()))
        ),
        property_type_count=("property_type", "nunique")
    )
    .reset_index()
)

audit = core.merge(
    legal_types,
    on="document_id",
    how="left",
    validate="many_to_one"
)

print("\n========== ACRIS PROPERTY TYPE ==========")

property_summary = (
    audit.groupby("property_types", dropna=False)
    .agg(
        transactions=("document_id", "nunique"),
        properties=("bbl", "nunique"),
        median_price=("price", "median")
    )
    .sort_values("transactions", ascending=False)
)

property_summary["transaction_share"] = (
    property_summary["transactions"]
    / property_summary["transactions"].sum()
)

print(property_summary.to_string())

property_summary.to_csv(
    OUT / "core_by_acris_property_type.csv"
)

# --------------------------------------------------
# CROSS-CLASSIFICATION
# --------------------------------------------------

cross = pd.crosstab(
    audit["bldgclass"],
    audit["property_types"],
    margins=True
)

cross.to_csv(OUT / "building_class_vs_acris_type.csv")

print("\n========== BUILDING CLASS VS ACRIS TYPE ==========")
print(cross.to_string())

# --------------------------------------------------
# FLAG MIXED / COMMERCIAL CATEGORIES FOR REVIEW
# --------------------------------------------------

review_types = ["CR", "F1", "F4", "FS", "K4", "O5", "RH"]

review = audit[
    audit["property_types"].fillna("").apply(
        lambda x: any(t in x.split("|") for t in review_types)
    )
].copy()

review.to_csv(
    OUT / "commercial_mixed_use_candidates.csv",
    index=False
)

print("\n========== REVIEW CANDIDATES ==========")
print("Transactions:", review["document_id"].nunique())
print("Properties:", review["bbl"].nunique())
print("Saved commercial_mixed_use_candidates.csv")

print("\nAudit outputs:", OUT)
