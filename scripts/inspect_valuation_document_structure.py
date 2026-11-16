import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "valuation_audit" / "duplicate_diagnostics"

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

structure = pd.read_csv(
    OUT / "legal_document_structure.csv",
    dtype={"document_id": str}
)

# --------------------------------------------------
# SALES ROW STRUCTURE
# --------------------------------------------------

print("\n========== SALES DATA COLUMNS ==========")
print(sales.columns.tolist())

sales_counts = sales.groupby("document_id").agg(
    sales_rows=("document_id", "size"),
    unique_bbls=("bbl", "nunique") if "bbl" in sales.columns
    else ("document_id", "size")
).reset_index()

print("\n========== SALES ROW DISTRIBUTION ==========")
print(sales_counts["sales_rows"].value_counts().sort_index().head(20))

print("\nDocuments with multiple sales rows:",
      (sales_counts["sales_rows"] > 1).sum())

print("Documents with multiple sales BBLs:",
      (sales_counts["unique_bbls"] > 1).sum())

# --------------------------------------------------
# JOIN LEGAL STRUCTURE TO SALES STRUCTURE
# --------------------------------------------------

combined = structure.merge(
    sales_counts,
    on="document_id",
    how="left",
    validate="one_to_one",
    suffixes=("", "_sales")
)

print("\n========== MULTI-BBL DOCUMENT COMPARISON ==========")

print(pd.crosstab(
    combined["multiple_bbls"],
    combined["sales_rows"] > 1
))

print("\n========== DOCUMENTS WITH MOST LEGAL BBLs ==========")

top = combined.sort_values(
    ["unique_bbls", "legal_rows"],
    ascending=False
).head(20)

print(top[
    ["document_id", "legal_rows", "unique_bbls",
     "unique_property_types", "sales_rows"]
].to_string(index=False))

# --------------------------------------------------
# SAVE TARGETED RECORDS
# --------------------------------------------------

top_ids = set(top["document_id"])

legal[legal["document_id"].isin(top_ids)].to_csv(
    OUT / "top_multi_bbl_legal_records.csv",
    index=False
)

sales[sales["document_id"].isin(top_ids)].to_csv(
    OUT / "top_multi_bbl_valuation_records.csv",
    index=False
)

# --------------------------------------------------
# SAVE ALL DOCUMENTS WITH MULTIPLE SALES ROWS
# --------------------------------------------------

sales_counts[sales_counts["sales_rows"] > 1].to_csv(
    OUT / "multiple_valuation_sales_documents.csv",
    index=False
)

print("\n========== OUTPUTS ==========")
print("top_multi_bbl_legal_records.csv")
print("top_multi_bbl_valuation_records.csv")
print("multiple_valuation_sales_documents.csv")
