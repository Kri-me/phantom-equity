import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "valuation_audit" / "duplicate_diagnostics"

sales = pd.read_csv(
    ROOT / "data" / "processed" / "valuation_sales.csv",
    dtype={"document_id": str, "bbl": str},
    low_memory=False
)

structure = pd.read_csv(
    OUT / "legal_document_structure.csv",
    dtype={"document_id": str}
)

# --------------------------------------------------
# DOCUMENT-LEVEL SALES SUMMARY
# --------------------------------------------------

sales_doc = sales.groupby("document_id").agg(
    sales_rows=("document_id", "size"),
    sales_bbls=("bbl", "nunique"),
    single_property_values=("single_property_deed", "nunique"),
    small_residential_values=("small_residential", "nunique"),
    prices=("price", "nunique")
).reset_index()

sales_doc["single_property_value"] = (
    sales.groupby("document_id")["single_property_deed"]
    .first().values
)

sales_doc["small_residential_value"] = (
    sales.groupby("document_id")["small_residential"]
    .first().values
)

# --------------------------------------------------
# COMBINE WITH LEGAL STRUCTURE
# --------------------------------------------------

df = structure.merge(
    sales_doc,
    on="document_id",
    how="left",
    validate="one_to_one"
)

df["legal_multi_bbl"] = df["unique_bbls"] > 1

# --------------------------------------------------
# CORE TRAINING ELIGIBILITY
# --------------------------------------------------

df["core_training_eligible"] = (
    df["single_property_value"].astype(str).str.lower().isin(
        ["true", "1"]
    )
    &
    df["small_residential_value"].astype(str).str.lower().isin(
        ["true", "1"]
    )
)

# --------------------------------------------------
# REPORT
# --------------------------------------------------

print("\n========== DOCUMENT-LEVEL COUNTS ==========")
print("Total documents:", len(df))
print("Multi-BBL legal documents:", df["legal_multi_bbl"].sum())
print("Multi-row valuation documents:",
      (df["sales_rows"] > 1).sum())

print("\n========== LEGAL MULTI-BBL VS SINGLE-PROPERTY FLAG ==========")
print(pd.crosstab(
    df["legal_multi_bbl"],
    df["single_property_value"],
    margins=True
).to_string())

print("\n========== LEGAL MULTI-BBL VS CORE ELIGIBILITY ==========")
print(pd.crosstab(
    df["legal_multi_bbl"],
    df["core_training_eligible"],
    margins=True
).to_string())

print("\n========== SINGLE-PROPERTY FLAG CONSISTENCY ==========")
print("Documents with inconsistent single_property_deed values:",
      (df["single_property_values"] > 1).sum())

print("Documents with inconsistent small_residential values:",
      (df["small_residential_values"] > 1).sum())

print("\n========== 178 DOCUMENT DISCREPANCY ==========")

discrepancy = df[
    (df["legal_multi_bbl"]) &
    (df["sales_rows"] == 1)
]

print("Count:", len(discrepancy))

print("\n========== MULTI-BBL DOCUMENTS ENTERING CORE ==========")

leaked = df[
    (df["legal_multi_bbl"]) &
    (df["core_training_eligible"])
]

print("Count:", len(leaked))

print("\n========== MULTI-BBL CORE EXAMPLES ==========")
print(leaked[
    ["document_id", "legal_rows", "unique_bbls",
     "sales_rows", "single_property_value",
     "small_residential_value"]
].head(15).to_string(index=False))

# --------------------------------------------------
# SAVE
# --------------------------------------------------

df.to_csv(
    OUT / "single_property_eligibility_audit.csv",
    index=False
)

discrepancy.to_csv(
    OUT / "legal_multi_bbl_single_sales_row.csv",
    index=False
)

leaked.to_csv(
    OUT / "multi_bbl_core_eligible.csv",
    index=False
)

print("\nAudit files saved to:", OUT)
