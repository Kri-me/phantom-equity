# Phantom Equity

Phantom Equity is a local analyst workbench for exploring unusual patterns in New York City property, deed, and mortgage records. It produces investigation leads and review tools; its signals and scores are not findings of fraud.

Start with the System Guide at docs/SYSTEM_GUIDE.md for the architecture, setup, data flow, pipeline, dashboard, limitations, and a two-week learning path.

## Repository contents

- scripts/ — source-data pulls, feature/signal generation, ETL, entity resolution, case building, scoring, and audit utilities.
- sql/schema.sql and migrations/ — PostgreSQL schema and changes for existing databases.
- views/investigation_queue.py — Streamlit analyst dashboard.
- docs/SYSTEM_GUIDE.md — technical and operator guide.

Downloaded ACRIS and PLUTO data, generated CSVs, pipeline logs, credentials, and the local virtual environment are deliberately excluded from Git. Create a local .env from .env.example and supply your own database settings and optional Socrata token.

## Quick start

1. Create and activate a Python virtual environment, then install requirements.txt.
2. Create a local .env from .env.example and configure PostgreSQL.
3. Create the phantom_equity database and initialize a fresh database with sql/schema.sql.
4. Follow the data preparation and pipeline sequence in the System Guide.

The pipeline rebuilds database tables. Review its documented behavior and protect any database containing analyst review work before rerunning it.
