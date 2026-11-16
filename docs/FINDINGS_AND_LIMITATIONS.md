# Findings and limitations

This note summarizes the current evidence from the Phantom Equity data and valuation audits. It describes what the available records and experiments support; it is not a determination that any transfer was fraudulent or that a price is legally verified consideration.

## Current findings

- **The valuation target is a recorded amount.** `valuation_coverage.py` copies ACRIS Master `document_amt` into `valuation_sales.csv.price`. The field is not reconstructed from transfer taxes or adjusted by `percent_trans`.
- **`document_amt` does not always mean sale consideration.** ACRIS documentation defines it as an amount associated with the instrument, which may be principal debt or another obligation. Deed type, transaction structure, partial interests, corrections, and related records affect interpretation.
- **The initial provenance screen is a review aid.** The audit covered 41,965 distinct documents associated with 44,522 valuation-sales rows. It left 17,700 documents without a first-pass local review flag and marked 24,265 for review. Those flags do not decide whether a transaction was a sale or whether its amount is valid; the difference in counts reflects the document versus sales-row grain.
- **Available extracts cannot complete a tax corroboration review.** The local Master and Legal extracts used in the audit did not provide tax-return fields, and private tax returns are not part of the public records examined. Transfer-tax amounts and exemptions, where available from other records, are context rather than a substitute for the consideration stated in source documents.
- **A provisional cohort sensitivity run produced only a small change.** In a paired comparison on the same 15,265 retained core records and fixed spatial folds, holding out 126 flagged records changed median absolute percentage error from 16.52% to 16.28% and log-price R² from 0.6378 to 0.6413. The simple median price-per-square-foot baseline changed very little. This does not validate the retained prices or establish better fraud detection.
- **Existing pipeline candidates did not have more extreme valuation residuals in that comparison.** Extreme residuals appeared in 5.22% of baseline signal-candidate sales versus 6.25% of other sales; under the provisional cohort the figures were 4.53% and 6.03%. This is descriptive overlap against unlabelled candidates, not a test of fraud-detection accuracy.

## What remains uncertain

- Whether each recorded amount represents total consideration, debt principal, a partial interest, a correction, or another instrument amount.
- Whether a transfer was arm's-length or otherwise comparable to the benchmark target.
- Whether exemptions, transfer taxes, or other transaction details corroborate consideration for every record.
- How much price-field error affects valuation residuals and downstream screening.
- Whether any signal, score, or residual identifies fraud. The project has no labeled fraud ground truth for measuring precision or recall.

## Current use and next step

Treat signal rows, case scores, entity matches, and valuation residuals as leads for source-record review. Do not describe them as findings, probabilities, or proof of consideration. The provisional price cohort remains an audit sensitivity option; it has not replaced the existing benchmark or been added to the dashboard pipeline. Before changing training data, preserve a source snapshot, make the cohort rule explicit and reproducible, and compare it with the current benchmark on the same evaluation records and spatial folds.

For installation and operation, see [System Guide](SYSTEM_GUIDE.md). For implementation details, see the project owner's separate technical guide.
