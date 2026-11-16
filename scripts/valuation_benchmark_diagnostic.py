from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
om_path = ROOT / "data" / "processed" / "signal_over_mortgaging.csv"
vr_path = ROOT / "data" / "processed" / "valuation_residuals.csv"

om = pd.read_csv(om_path, dtype=str)
d = pd.read_csv(vr_path, dtype={"bbl": str, "document_id": str})

if "category" in om.columns:
    om = om[om["category"] == "candidate"].copy()

print("\n1. OVER-MORTGAGING CANDIDATE COUNTS")
print("Candidate rows:", len(om))
print("Unique deed IDs:", om["deed_doc_id"].nunique())
print("Duplicate deed IDs:", om["deed_doc_id"].duplicated().sum())
print("Unique BBLs:", om["bbl"].nunique())

print("\n2. BENCHMARK MATCH COUNTS")
print("Benchmark rows:", len(d))
print("Unique document IDs:", d["document_id"].nunique())
print("Duplicate document IDs:", d["document_id"].duplicated().sum())

matched = om.merge(
    d[["document_id", "bbl"]],
    left_on="deed_doc_id",
    right_on="document_id",
    how="left",
    indicator=True
)

print("\nMerge results:")
print(matched["_merge"].value_counts())
print("Matched rows:", (matched["_merge"] == "both").sum())
print("Unmatched rows:", (matched["_merge"] == "left_only").sum())

print("\n3. BBL-LEVEL COUNTS")
candidate_bbls = set(om["bbl"].dropna())
scored_bbl_sales = d[d["bbl"].isin(candidate_bbls)]

print("Scored sales belonging to candidate BBLs:", len(scored_bbl_sales))
print("Distinct candidate BBLs in scored sales:", scored_bbl_sales["bbl"].nunique())

print("\n4. BBL-TO-CELL CONSISTENCY")
if "cell" in d.columns:
    cells_per_bbl = d.groupby("bbl")["cell"].nunique()
    print("BBLs appearing in multiple cells:", (cells_per_bbl > 1).sum())
    print(cells_per_bbl[cells_per_bbl > 1].head(20))
else:
    print("The residual file does not contain the cell column.")
