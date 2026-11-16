# Run Phantom Equity Locally

This walkthrough takes you from a fresh checkout to the local analyst dashboard. You do not need to understand every scoring rule before getting started. The system creates leads for investigation; it does not determine that fraud occurred.

## What you need

- Python 3.11 or newer
- PostgreSQL
- A Windows terminal such as PowerShell
- An internet connection for the ACRIS and PLUTO data pulls

The repository contains source code and setup files. It deliberately does not contain ACRIS/PLUTO extracts, generated datasets, database backups, or credentials. You will create those locally.

## 1. Set up Python

Open PowerShell in the repository folder and run:

    py -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    pip install -r requirements.txt

If PowerShell blocks activation, you can call the environment's Python directly as .venv\Scripts\python.exe instead of changing your system's script policy.

## 2. Configure PostgreSQL

Copy .env.example to a new file named .env in the repository root. Fill in the PostgreSQL host, port, database, user, and password. Keep this file private; it is excluded from Git.

Create the database and load the current schema from the repository root:

    createdb phantom_equity
    psql -d phantom_equity -f sql/schema.sql

If your database has another name, use the same name in .env. The schema file is for a fresh database. Existing installations may need an applicable migration from migrations; do not apply migrations blindly or more than once.

## 3. Download and prepare ACRIS inputs

The main pull uses NYC Open Data's public ACRIS API. A Socrata app token is optional and can improve request limits. If you use one, place it in the local .env as SOCRATA_APP_TOKEN. Never paste it into a Python file or commit it.

Run these commands from the scripts folder:

    cd scripts
    python acris_pipeline.py
    python entity_resolution.py

The first command downloads ACRIS master and legal records, decodes party roles, and writes the analyzed parties file. The second groups likely name variants into candidate entities. Name matching can produce false merges and false splits, so treat its clusters as suggestions that need review.

## 4. Build the dashboard data

Still in scripts/, run:

    python run_pipeline.py

The runner executes six steps in order:

1. signals.py detects rapid-flip, over-mortgaging, and serial-refinancing candidates.
2. transfer_signals.py creates transfer-chain, repeated-transfer, CEMA-proxy, and adjusted-residual outputs.
3. etl_load.py loads source records, entities, and signal outputs into PostgreSQL.
4. case_builder.py groups connected properties and evidence into cases.
5. build_entity_network.py rebuilds the property/entity relationship network.
6. corroboration_engine.py recalculates case triage scores and explanations.

This pipeline assumes the upstream ACRIS files and resolved-entity outputs already exist. It does not download those inputs or build the map coordinates. It also rebuilds database layers; back up database state you need before rerunning it. A timestamped run log is written under outputs/pipeline log/.

## 5. Add map positions and open the app

After the database contains properties, run:

    python pluto_geocode.py

This downloads and caches NYC PLUTO pages and writes property coordinates. Exact-lot positions are preferred; when a condo unit BBL is not listed by PLUTO, the map may use an approximate block-level position.

For a hosted dashboard, apply migration 004 and load the generated map, refinancing, and exclusion-audit files into PostgreSQL. See the [Neon and Streamlit deployment runbook](NEON_STREAMLIT_DEPLOYMENT.md) for the guided migration and deployment steps.

Start the dashboard from scripts/:

    streamlit run ../views/investigation_queue.py

Streamlit prints a local address, usually http://localhost:8501. Open it in a browser.

## 6. Use the dashboard

- Start at Home, then open the Case queue to inspect ranked groups of properties and evidence.
- Use Signal explorer and Transfer signals to examine individual patterns.
- Open source-document links and compare the underlying ACRIS records before drawing conclusions.
- Use Entity explorer and Map view for context. A map ring can represent an approximate block position rather than an exact property lot.
- Record review decisions in the app. These are stored in PostgreSQL separately from the generated signal files.

Case numbers can change after a pipeline rebuild. Keep notes using the BBL and document IDs as well as any case number.

## What the signals mean

- **Rapid flip:** two deed records on one property have a short gap and a large recorded-amount increase. The default screen uses a 180-day window and 20% increase; same-recording-session pairs, likely bulk/blanket patterns, and recurring patterns receive special handling.
- **Over-mortgaging:** a nearby mortgage amount is compared with a recorded deed amount. The default matching window is 14 days and the screen begins at a 0.95 mortgage-to-deed-amount ratio.
- **Serial refinancing:** consecutive mortgage records on a property are paired when no deed appears between them, within a one-year window. The borrower-change flag is a review clue, not a verdict.
- **Transfer and valuation signals:** nominal chains, repeat transfers, CEMA-proxy evidence, and valuation residuals help prioritize document review. A proxy or model residual does not establish what legally or economically happened.

These are starting rules, not calibrated fraud probabilities. ACRIS document_amt is not universally sale consideration: it can describe principal debt or another recorded obligation. Partial interests, exemptions, blanket loans, self-transfers, document type, and recording-date errors can all change how an amount should be interpreted.

## If something fails

- **Database connection error:** check the PostgreSQL service and the values in the root .env.
- **Missing CSV:** confirm you ran the preceding data-preparation steps from scripts/.
- **Case-score overflow on an older database:** check whether migration 003 has been applied. A new database should use the current sql/schema.sql.
- **No map data:** run pluto_geocode.py after properties have been loaded.
- **Document opens blank:** search the displayed document ID through the ACRIS search link in the dashboard.

For the current audit results and evidence boundaries, see [Findings and limitations](FINDINGS_AND_LIMITATIONS.md). For detailed internals, including the table model, algorithm flow, scoring formulas, and a guided study plan, see the comprehensive technical guide shared separately with the project owner.
