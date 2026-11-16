# Phantom Equity

Phantom Equity is a local analyst workbench for exploring unusual patterns in New York City property, deed, and mortgage records. It produces investigation leads and review tools; its signals and scores are not findings of fraud.

Start with the public walkthrough at docs/SYSTEM_GUIDE.md to install and run the local system. See [Findings and limitations](docs/FINDINGS_AND_LIMITATIONS.md) for current audit results and evidence boundaries. A separate comprehensive technical guide is maintained for the project owner.

## Repository contents

- scripts/ — source-data pulls, feature/signal generation, ETL, entity resolution, case building, scoring, and audit utilities.
- sql/schema.sql and migrations/ — PostgreSQL schema and changes for existing databases.
- views/investigation_queue.py — Streamlit analyst dashboard.
- docs/SYSTEM_GUIDE.md — public setup and usage walkthrough.
- docs/FINDINGS_AND_LIMITATIONS.md — current audit findings and interpretation limits.

Downloaded ACRIS and PLUTO data, generated CSVs, pipeline logs, credentials, and the local virtual environment are deliberately excluded from Git. Create a local .env from .env.example and supply your own database settings and optional Socrata token.

## Quick start

Follow the local setup and pipeline instructions in the [System Guide](docs/SYSTEM_GUIDE.md).

The pipeline rebuilds database tables. Review its documented behavior and protect any database containing analyst review work before rerunning it.
