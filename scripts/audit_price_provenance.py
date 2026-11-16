#!/usr/bin/env python3
"""First-pass ACRIS price-provenance audit. Standard library only.

Run from the repository root or scripts/ directory. Reads existing CSVs and
writes a review ledger plus summaries under outputs/valuation_audit/price_provenance/.
Flags are triage cues, not decisions that a transaction is invalid.
"""
import csv
from collections import Counter, defaultdict
from pathlib import Path

SALES_REL = Path("data/processed/valuation_sales.csv")
MASTER_REL = Path("data/raw/acris_master_sample.csv")
LEGAL_REL = Path("data/raw/acris_legals_sample.csv")
OUT_REL = Path("outputs/valuation_audit/price_provenance")
NOMINAL_FLOOR = 10_000


def find_root():
    starts = [Path.cwd(), Path(__file__).resolve().parent, *Path(__file__).resolve().parents]
    seen = set()
    for start in starts:
        start = start.resolve()
        if start in seen:
            continue
        seen.add(start)
        if all((start / p).is_file() for p in (SALES_REL, MASTER_REL, LEGAL_REL)):
            return start
    raise SystemExit("Could not locate data/processed/valuation_sales.csv and the ACRIS source CSVs. Run from the extracted repository or place this script in its scripts/ folder.")


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def legal_bbl(row):
    try:
        return "-".join(str(int(float(row[k]))) for k in ("borough", "block", "lot"))
    except (TypeError, ValueError, KeyError):
        return ""


def main():
    root = find_root()
    sales = read_csv(root / SALES_REL)
    masters = read_csv(root / MASTER_REL)
    legals = read_csv(root / LEGAL_REL)

    by_sales = defaultdict(list)
    by_master = defaultdict(list)
    by_legal = defaultdict(list)
    for row in sales:
        by_sales[row["document_id"]].append(row)
    for i, row in enumerate(masters):
        by_master[row["document_id"]].append((i, row))
    for row in legals:
        if row.get("document_id") in by_sales:
            by_legal[row["document_id"]].append(row)

    out_dir = root / OUT_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = out_dir / "valuation_price_provenance_ledger.csv"
    summary_path = out_dir / "valuation_price_provenance_summary.csv"
    fields = [
        "document_id", "bbl_count", "sales_row_count", "saved_price",
        "saved_single_property", "doc_type", "master_version_count",
        "master_amount_versions", "latest_master_amount", "latest_modified_date",
        "latest_good_through_date", "latest_percent_trans", "legal_bbl_count",
        "legal_row_count", "property_types", "non_entire_lot_rows",
        "easement_rows", "air_right_rows", "subterranean_right_rows",
        "first_pass_status", "review_flags",
    ]
    flag_counts = Counter()
    status_counts = Counter()
    type_counts = Counter()

    with ledger_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for doc_id in sorted(by_sales):
            sale_rows = by_sales[doc_id]
            master_versions = by_master.get(doc_id, [])
            legal_rows = by_legal.get(doc_id, [])
            # Preserve all versions in the ledger; choose latest modified row for comparison.
            latest = max(master_versions, key=lambda x: (x[1].get("modified_date", ""), x[1].get("good_through_date", ""), x[0]))[1] if master_versions else {}
            saved_values = sorted({r.get("price", "") for r in sale_rows})
            saved_price = number(saved_values[0]) if len(saved_values) == 1 else None
            amount_versions = sorted({r.get("document_amt", "") for _, r in master_versions})
            latest_amount = number(latest.get("document_amt"))
            percent = number(latest.get("percent_trans"))
            sales_bbls = {r.get("bbl", "") for r in sale_rows if r.get("bbl")}
            legal_bbls = {legal_bbl(r) for r in legal_rows if legal_bbl(r)}
            property_types = sorted({r.get("property_type", "") for r in legal_rows if r.get("property_type")})
            doc_type = latest.get("doc_type", "")
            type_counts[doc_type or "MISSING"] += 1
            flags = []

            if not master_versions:
                flags.append("MASTER_RECORD_MISSING")
            if not legal_rows:
                flags.append("LEGAL_RECORD_MISSING")
            if len(master_versions) > 1:
                flags.append("MULTIPLE_MASTER_ROWS")
            if len(amount_versions) > 1:
                flags.append("CONFLICTING_MASTER_AMOUNTS")
            if saved_price is not None and latest_amount is not None and abs(saved_price - latest_amount) > 0.005:
                flags.append("SAVED_PRICE_DIFFERS_FROM_LATEST_MASTER")
            if saved_price is not None and saved_price < NOMINAL_FLOOR:
                flags.append("ZERO_OR_NOMINAL_AMOUNT_LT_10000")
            if percent is not None and abs(percent - 100.0) > 1e-9:
                flags.append("TRANSFER_PERCENT_NOT_100")
            if len(sales_bbls) > 1 or len(legal_bbls) > 1 or any(r.get("single_property_deed", "").lower() == "false" for r in sale_rows):
                flags.append("MULTI_PROPERTY_DOCUMENT")
            if doc_type and doc_type != "DEED":
                flags.append("NON_PLAIN_DEED_TYPE")
            if any(r.get("property_type") == "TS" for r in legal_rows):
                flags.append("TIMESHARE_PROPERTY_TYPE")
            if any(r.get("partial_lot") not in ("", "E") for r in legal_rows):
                flags.append("PARTIAL_LOT_INDICATOR")
            if any(r.get("easement") == "Y" for r in legal_rows):
                flags.append("EASEMENT_INDICATOR")
            if any(r.get("air_rights") == "Y" for r in legal_rows):
                flags.append("AIR_RIGHTS_INDICATOR")
            if any(r.get("subterranean_rights") == "Y" for r in legal_rows):
                flags.append("SUBTERRANEAN_RIGHTS_INDICATOR")

            status = "REVIEW_FLAGGED" if flags else "FIRST_PASS_NO_LOCAL_FLAG"
            status_counts[status] += 1
            flag_counts.update(flags)
            writer.writerow({
                "document_id": doc_id,
                "bbl_count": len(sales_bbls),
                "sales_row_count": len(sale_rows),
                "saved_price": saved_values[0] if len(saved_values) == 1 else "MULTIPLE_SAVED_PRICES",
                "saved_single_property": ";".join(sorted({r.get("single_property_deed", "") for r in sale_rows})),
                "doc_type": doc_type,
                "master_version_count": len(master_versions),
                "master_amount_versions": " | ".join(amount_versions),
                "latest_master_amount": latest.get("document_amt", ""),
                "latest_modified_date": latest.get("modified_date", ""),
                "latest_good_through_date": latest.get("good_through_date", ""),
                "latest_percent_trans": latest.get("percent_trans", ""),
                "legal_bbl_count": len(legal_bbls),
                "legal_row_count": len(legal_rows),
                "property_types": " | ".join(property_types),
                "non_entire_lot_rows": sum(r.get("partial_lot") not in ("", "E") for r in legal_rows),
                "easement_rows": sum(r.get("easement") == "Y" for r in legal_rows),
                "air_right_rows": sum(r.get("air_rights") == "Y" for r in legal_rows),
                "subterranean_right_rows": sum(r.get("subterranean_rights") == "Y" for r in legal_rows),
                "first_pass_status": status,
                "review_flags": ";".join(flags),
            })

    with summary_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["measure", "count"])
        writer.writerow(["sales_rows", len(sales)])
        writer.writerow(["documents", len(by_sales)])
        writer.writerow(["documents_missing_from_master", sum(d not in by_master for d in by_sales)])
        writer.writerow(["documents_missing_from_legals", sum(d not in by_legal for d in by_sales)])
        writer.writerow(["first_pass_no_local_flag", status_counts["FIRST_PASS_NO_LOCAL_FLAG"]])
        writer.writerow(["review_flagged", status_counts["REVIEW_FLAGGED"]])
        writer.writerow([])
        writer.writerow(["review_flag", "document_count"])
        for flag, count in flag_counts.most_common():
            writer.writerow([flag, count])
        writer.writerow([])
        writer.writerow(["doc_type", "document_count"])
        for doc_type, count in type_counts.most_common():
            writer.writerow([doc_type, count])

    print(f"Documents: {len(by_sales):,}; sales rows: {len(sales):,}")
    print(f"First-pass no local flag: {status_counts['FIRST_PASS_NO_LOCAL_FLAG']:,}")
    print(f"Review flagged: {status_counts['REVIEW_FLAGGED']:,}")
    print(f"Ledger: {ledger_path}")
    print(f"Summary: {summary_path}")
    print("Note: tax-return fields are not in these extracts; flags do not determine sale validity.")


if __name__ == "__main__":
    main()
