# Phantom Equity System Guide

This guide is a working map of the repository and a two-week path for learning it. It describes the current code, not a production deployment plan.

## 1. What the system does

Phantom Equity combines NYC ACRIS real-property records with PLUTO property attributes and locations. Python scripts create candidate signals, load evidence into PostgreSQL, connect related properties into cases, and calculate transparent triage scores. A Streamlit dashboard lets an analyst inspect documents, entities, maps, and review decisions.

The system is for investigation support. A signal is a reason to inspect source records, not a conclusion about fraud or intent. A score ranks work; it is not a probability.

## 2. Repository map

| Path | Purpose |
| --- | --- |
| scripts/acris_pipeline.py | Pulls ACRIS master and legal records, decodes party roles, and writes initial raw and processed CSVs. |
| scripts/acris_sample_pull.py | Smaller standalone ACRIS sampling utility. |
| scripts/doc_control_codes.py | Shared ACRIS document-type and party-role reference mappings. |
| scripts/signals.py | Property-level rapid-flip, over-mortgaging, and serial-refinancing signals. |
| scripts/transfer_signals.py | Nominal chains, repeated transfers, CEMA-proxy evidence, and adjusted valuation residuals. |
| scripts/entity_resolution.py | Groups likely company and trustee name variants; outputs are candidate identity clusters and need review. |
| scripts/etl_load.py | Full-reloads core evidence and signal tables from local CSVs into PostgreSQL. |
| scripts/case_builder.py | Rebuilds cases from eligible signal/property/entity links and calculates an initial triage score. |
| scripts/build_entity_network.py | Rebuilds entity/property relationship tables used in the network explorer. |
| scripts/corroboration_engine.py | Recalculates case-level triage scores and explanatory notes. |
| scripts/run_pipeline.py | Runs six downstream steps in order and writes a timestamped log under outputs/pipeline log/. It does not fetch ACRIS, resolve entities, or geocode PLUTO. |
| scripts/pluto_geocode.py | Downloads/caches PLUTO pages and produces exact-lot or block-level map positions. |
| scripts/pluto_attributes.py | Produces PLUTO building/lot features used in valuation work. |
| scripts/valuation_*.py | Separate valuation benchmark and diagnostic work; this experimental path is not in run_pipeline.py. |
| scripts/audit_*.py, diagnose_*.py, inspect_*.py | Focused data-quality, eligibility, provenance, and duplicate investigations. |
| sql/schema.sql | Complete current schema for a fresh database. |
| migrations/ | Incremental SQL changes for older databases; do not blindly run against a fresh database or reapply changes. |
| views/investigation_queue.py | Streamlit application and its analyst review interface. |
| data/raw/, data/processed/, outputs/ | Local inputs and generated files. Keep these outside source control. |

## 3. Data flow

    NYC ACRIS API
      -> data/raw/acris_master_sample.csv
      -> data/raw/acris_legals_sample.csv
      -> data/processed/acris_parties_analyzed.csv
      -> data/processed/entities_resolved.csv (+ trustees_resolved.csv when produced)

    ACRIS CSVs
      -> signals.py
      -> transfer_signals.py
      -> data/processed/signal_*.csv
      -> etl_load.py
      -> PostgreSQL evidence, entity, and signal tables
      -> case_builder.py
      -> build_entity_network.py
      -> corroboration_engine.py
      -> Streamlit investigation dashboard

    PostgreSQL properties + NYC PLUTO
      -> pluto_geocode.py
      -> data/processed/property_geo.csv
      -> dashboard map

The valuation benchmark is a separate branch that uses valuation_sales.csv, PLUTO attributes, and related diagnostics. It is deliberately not included in the six-step dashboard pipeline while price provenance and cohort validity are still being audited.

## 4. First-time local setup

1. Install Python and PostgreSQL. From the repository root, create a virtual environment and install the repository dependencies:

       py -m venv .venv
       .\.venv\Scripts\Activate.ps1
       python -m pip install --upgrade pip
       pip install -r requirements.txt

2. Copy .env.example to .env. Set PGHOST, PGPORT, PGDATABASE, PGUSER, and PGPASSWORD, or set DATABASE_URL. Set SOCRATA_APP_TOKEN only if you have created a token; unauthenticated public pulls have lower request limits.

3. Create a PostgreSQL database named phantom_equity (or use the name configured in .env) and initialize a new database from the repository root:

       createdb phantom_equity
       psql -d phantom_equity -f sql/schema.sql

   schema.sql represents the current fresh-install schema, including the text CEMA evidence tier and widened case score. Migrations are for databases created from earlier schema versions; identify which have already been applied before using one.

4. Data-pull and downstream scripts expect the repository layout and are designed to be run from scripts/. Source CSVs are not shipped in Git. Pull or rebuild the required ACRIS inputs before running downstream steps.

## 5. Data preparation and operating sequence

From an activated virtual environment, in PowerShell:

       cd scripts
       python acris_pipeline.py
       python entity_resolution.py

These create the initial ACRIS extracts and candidate entity-resolution outputs. Inspect the outputs and any review queue rather than treating fuzzy identity matches as verified.

For the database-backed dashboard pipeline, first ensure the PostgreSQL database has the current schema and the expected source CSVs. Then run:

       python run_pipeline.py

The runner executes:

1. signals.py
2. transfer_signals.py
3. etl_load.py
4. case_builder.py
5. build_entity_network.py
6. corroboration_engine.py

etl_load.py truncates and reloads a set of database tables. Case and relationship-building scripts also rebuild their respective layers. Back up any database state you need to keep before a full run. The dashboard's signal_reviews table is a separate analyst record store and is not part of the ETL reload.

For the map, run python pluto_geocode.py after property records exist in PostgreSQL. The script caches downloaded PLUTO pages under data/raw/pluto_pages/; --refresh forces a new download. Run python pluto_attributes.py when preparing valuation features.

## 6. Starting the dashboard

From scripts/:

       streamlit run ../views/investigation_queue.py

The app reads PostgreSQL configuration from the project-root .env, the serial-refinancing signal CSV from data/processed/, and map coordinates from data/processed/property_geo.csv. ACRIS document links open source documents; the dashboard also provides an ACRIS search link if a particular document page does not load.

Analyst review decisions are stored in PostgreSQL in signal_reviews, keyed by signal type, BBL, and document IDs. They are separate from the generated signal CSVs.

## 7. How to read the main signal families

- **Over-mortgaging:** compares a mortgage amount with a recorded deed amount. ACRIS document_amt can represent principal debt or another obligation; it is not universally sale consideration. Read the deed and mortgage records before interpreting the ratio.
- **Rapid flips:** compares recorded amounts on paired deed documents over a short period. The percentage change is a screening feature and depends on both amounts being comparable transactions for the same property interest.
- **Serial refinancing:** pairs successive mortgage records on a property, with borrower changes as an annotation. Ordinary refinancing is common; the signal alone is not suspicious.
- **Nominal chains and repeated transfers:** summarize low-amount transfers, possible follow-on deeds/financing, and recording frequency. Check continuity and the actual documents.
- **CEMA proxies:** label evidence patterns consistent with consolidations; they are not determinations that a CEMA occurred.
- **Adjusted valuation residuals:** statistical deviations from the benchmark, not a judgment about the validity of the source sale.

Watch for duplicate recordings, blanket loans recorded against many unit BBLs, self-transfers, partial interests, tax exemptions, consolidations, and mismatched condo unit/property types. Some document-date fields are unreliable; serial refinancing uses recording timestamps, while some other signal families still use execution dates.

## 8. Database concepts

- **Properties** use BBL identifiers.
- **Documents** hold ACRIS master-level fields, including document type, document date, recording timestamp, and recorded amount.
- **Document legals** bridge one document to one or more BBLs and store unit/property details.
- **Document parties** contain raw and normalized party names plus decoded ACRIS roles.
- **Entities and aliases** store candidate merged identities; review fuzzy merges and splits.
- **Signal tables** store generated evidence. Some signals are kept in CSV and rendered directly, rather than as a dedicated table.
- **Cases and link tables** group properties, entities, signals, and timeline evidence.
- **Entity relationships** supply the dashboard network view.
- **Signal reviews** store analyst decisions independently of pipeline output.

## 9. Source-control and GitHub file policy

### Include in Git

- Python source in scripts/ and views/.
- sql/schema.sql and reviewed SQL migrations.
- requirements.txt, README.md, .gitignore, .env.example, and documentation under docs/.

### Keep out of Git

- .env, acris token.txt, database passwords, API tokens, and Streamlit secrets.
- All downloaded ACRIS/PLUTO extracts and generated CSV/JSON files under data/raw/ and data/processed/.
- Valuation audit exports, pipeline logs, and all other generated files under outputs/.
- The local .venv/, Python bytecode/caches, and one-off diagnostic artifacts.

The project data currently occupies hundreds of megabytes and includes names and addresses from public records. Even when public, raw bulk extracts and analyst outputs are unnecessary for reproducing the source code and should not be committed. Treat a database backup as private data, not as a repository asset.

The ACRIS pull scripts now read SOCRATA_APP_TOKEN from .env instead of source code. A token that has ever been exposed in source should be revoked and replaced before publishing; ignoring .env only protects future commits, not an already-published Git history.

## 10. Two-week learning path

Plan for one short session each weekday. Keep a notebook of open questions and trace one sample BBL/document through the code as you go.

### Week 1 — understand the evidence

1. **Repository orientation:** Read this guide and README.md. Find the raw/processed/output folders locally and note which files are downloaded versus generated.
2. **ACRIS records:** Read scripts/acris_pipeline.py and scripts/doc_control_codes.py. Learn master, legal, party, document-type, and party-role fields.
3. **Signal logic:** Read the assumptions near the top and the rapid-flip and over-mortgaging sections in scripts/signals.py. Trace one signal back to both source records.
4. **Refinancing and transfer signals:** Read serial refinancing in signals.py, then the opening sections of transfer_signals.py. Compare execution dates with recording timestamps and inspect how CEMA evidence is tiered.
5. **Price provenance:** Read scripts/valuation_coverage.py, scripts/audit_price_provenance.py, and scripts/valuation_benchmark.py. Note the unresolved limitation: document_amt is not uniformly verified sale consideration.

### Week 2 — understand the database and application

6. **Schema:** Read sql/schema.sql in layer order. Match each table to the data-flow diagram above.
7. **ETL:** Follow etl_load.py from main() through the truncate and load sections. Identify its CSV prerequisites and which tables it replaces.
8. **Cases and scores:** Read case_builder.py and corroboration_engine.py. Reproduce the score from one case's evidence and verify that it is a ranking heuristic.
9. **Dashboard and review trail:** Read views/investigation_queue.py by render_* function. Follow a document link, inspect a signal, and save a test review only in a database you are comfortable changing.
10. **Runbook and next improvements:** Read run_pipeline.py, inspect one pipeline log, and map a signal row from CSV to PostgreSQL to dashboard. List reliability, data-quality, and deployment questions before changing model logic.

## 11. Suggested reference files

Read in this order when investigating a pipeline issue:

1. scripts/run_pipeline.py for execution order and logs.
2. The failing script's input/output comments and main() function.
3. sql/schema.sql for the destination table definition.
4. The matching migration for an existing-database schema change.
5. The dashboard's corresponding render_* function for display and review behavior.

Before changing the valuation benchmark, finish the price-field provenance and transaction-validity audit and define which sales are eligible. Before deploying the dashboard, configure hosting, secrets, data/database access, and a backup strategy separately; this repository currently describes a local analyst application.
