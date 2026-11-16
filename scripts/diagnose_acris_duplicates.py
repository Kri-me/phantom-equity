import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "valuation_audit" / "duplicate_diagnostics"
OUT.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------
# LOAD
# --------------------------------------------------

sales = pd.read_csv(
    ROOT / "data" / "processed" / "valuation_sales.csv",
    dtype={"document_id": str},
    low_memory=False
)

legal = pd.read_csv(
    ROOT / "data" / "raw" / "acris_legals_sample.csv",
    dtype={"document_id": str},
    low_memory=False
)

master = pd.read_csv(
    ROOT / "data" / "raw" / "acris_master_sample.csv",
    dtype={"document_id": str},
    low_memory=False
)

sales_ids = set(sales["document_id"].dropna())

legal_relevant = legal[legal["document_id"].isin(sales_ids)].copy()
master_relevant = master[master["document_id"].isin(sales_ids)].copy()

# --------------------------------------------------
# SUMMARY
# --------------------------------------------------

def duplicate_summary(df, label):
    counts = df.groupby("document_id").size()
    duplicated = counts[counts > 1]

    print(f"\n========== {label} ==========")
    print("Rows:", len(df))
    print("Unique documents:", df["document_id"].nunique())
    print("Documents with duplicates:", len(duplicated))
    print("Rows belonging to duplicated documents:",
          int(duplicated.sum()))
    print("Exact duplicate rows:", int(df.duplicated().sum()))

    if len(duplicated):
        print("\nRecords per duplicated document:")
        print(duplicated.value_counts().sort_index().to_string())

    return duplicated


legal_dupes = duplicate_summary(legal_relevant, "LEGAL RECORDS")
master_dupes = duplicate_summary(master_relevant, "MASTER RECORDS")

# --------------------------------------------------
# LEGAL DOCUMENT STRUCTURE
# --------------------------------------------------

legal_relevant["bbl"] = (
    legal_relevant["borough"].astype(str) + "-" +
    legal_relevant["block"].astype(str) + "-" +
    legal_relevant["lot"].astype(str)
)

legal_groups = legal_relevant.groupby("document_id")

legal_doc_summary = legal_groups.agg(
    legal_rows=("document_id", "size"),
    unique_bbls=("bbl", "nunique"),
    unique_property_types=("property_type", "nunique"),
    unique_record_types=("record_type", "nunique"),
    unique_partial_lot_values=("partial_lot", "nunique")
).reset_index()

legal_doc_summary["multiple_bbls"] = (
    legal_doc_summary["unique_bbls"] > 1
)

legal_doc_summary["multiple_property_types"] = (
    legal_doc_summary["unique_property_types"] > 1
)

legal_doc_summary.to_csv(
    OUT / "legal_document_structure.csv", index=False
)

# Save detailed legal records only for documents with multiple BBLs
multi_bbl_ids = set(
    legal_doc_summary.loc[
        legal_doc_summary["multiple_bbls"], "document_id"
    ]
)

legal_relevant[
    legal_relevant["document_id"].isin(multi_bbl_ids)
].to_csv(
    OUT / "multi_bbl_legal_records.csv", index=False
)

# --------------------------------------------------
# MASTER DOCUMENT STRUCTURE
# --------------------------------------------------

master_fields = [
    c for c in [
        "document_id", "crfn", "doc_type", "document_date",
        "document_amt", "recorded_datetime", "modified_date",
        "percent_trans", "good_through_date"
    ]
    if c in master_relevant.columns
]

master_doc_summary = master_relevant.groupby("document_id").agg(
    master_rows=("document_id", "size"),
    **{
        f"unique_{c}": (c, "nunique")
        for c in master_fields if c != "document_id"
    }
).reset_index()

master_doc_summary.to_csv(
    OUT / "master_document_structure.csv", index=False
)

# Detailed records for duplicate master documents
master_dupe_ids = set(master_dupes.index) if False else set(
    master_relevant.loc[
        master_relevant["document_id"].isin(master_dupes.index),
        "document_id"
    ]
)

# Use the actual duplicate document IDs
master_dupe_ids = set(master_dupes.index) if False else set(
    master_relevant["document_id"].value_counts()
    .loc[lambda x: x > 1].index
)

master_relevant[
    master_relevant["document_id"].isin(master_dupe_ids)
][master_fields].to_csv(
    OUT / "duplicate_master_records.csv", index=False
)

# --------------------------------------------------
# REPRESENTATIVE SAMPLES
# --------------------------------------------------

def save_samples(df, ids, filename, limit=10):
    selected = list(ids)[:limit]
    sample = df[df["document_id"].isin(selected)]
    sample.to_csv(OUT / filename, index=False)
    return selected


legal_sample_ids = save_samples(
    legal_relevant,
    legal_dupes.index,
    "sample_duplicate_legal_records.csv"
)

master_sample_ids = save_samples(
    master_relevant,
    master_dupes.index,
    "sample_duplicate_master_records.csv"
)

# --------------------------------------------------
# CONCISE TERMINAL REPORT
# --------------------------------------------------

print("\n========== VALUATION SALES COVERAGE ==========")
print("Valuation sales rows:", len(sales))
print("Unique valuation documents:", len(sales_ids))
print("Sales documents with legal matches:",
      legal_relevant["document_id"].nunique())
print("Sales documents with master matches:",
      master_relevant["document_id"].nunique())

print("\n========== LEGAL STRUCTURE ==========")
print("Documents covering multiple BBLs:",
      int(legal_doc_summary["multiple_bbls"].sum()))
print("Documents with multiple property types:",
      int(legal_doc_summary["multiple_property_types"].sum()))

print("\n========== MASTER STRUCTURE ==========")
print("Documents with multiple master rows:",
      len(master_dupes))

print("\n========== REPRESENTATIVE DOCUMENT IDS ==========")
print("Legal:", legal_sample_ids)
print("Master:", master_sample_ids)

print("\n========== OUTPUT FILES ==========")
for path in sorted(OUT.glob("*.csv")):
    print(path.name)

print("\nDiagnostic complete.")
