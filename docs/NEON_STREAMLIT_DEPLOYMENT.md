# Host Phantom Equity on Neon and Streamlit Community Cloud

This runbook moves the current local PostgreSQL snapshot to Neon and connects a public, read-only Streamlit deployment to it. It is written for the repository state reviewed 16 November 2026.

## Before you begin

The local database measured **672,364,223 bytes** (about 641 MiB). Neon currently lists 1 GB of storage per Free project, so this snapshot should fit with limited room for growth. Check actual Neon usage after restore and before adding more history. [Neon Free plan details](https://neon.com/blog/neon-free-plan-1-gb-per-project)

The database contains joined names, addresses, property relationships, and generated investigation candidates from public records. The app is intended to be viewable by anyone with its link, as a portfolio project. Its candidate signals and scores can be wrong and are not findings of wrongdoing. Public mode disables analyst review writes; the app database role should also be read-only.

## 1. Create a Neon project

1. Sign in to Neon and create a project named `phantom-equity`.
2. Choose a region close to the app’s users/host when available. Keep the default branch and database created by Neon unless you have a reason to change them.
3. In the Neon Console, open **Connect** and copy the **direct** connection string for the owner role. Keep it private. Do not paste it into GitHub or this repository.
4. Confirm the target database is empty. The migration script checks this before restoring.

Neon supplies both direct and pooled URLs. The restore and bulk asset import use the direct URL. The deployed web app uses the pooled URL, whose host includes `-pooler`. [Neon connection pooling guidance](https://neon.com/docs/connect/connection-pooling) [Neon PostgreSQL migration guidance](https://neon.com/docs/import/migrate-from-postgres)

## 2. Restore the local database and dashboard assets

From the repository root, run:

    python scripts/migrate_to_neon.py

The script asks for the Neon direct URL using hidden input, checks that the destination has no user tables, and asks you to type `RESTORE` before continuing. It then:

- makes a custom-format dump of the local `phantom_equity` database;
- restores that dump without local role ownership or ACLs;
- applies migration 004 for the hosted dashboard tables and review-state table;
- loads serial-refinancing candidates, party-overlap exclusions, and map positions from the ignored local processed CSVs;
- leaves a recovery dump in `outputs/neon_migration/`, which is excluded from Git.

If the restore fails, the target may be partly populated. Create a fresh empty Neon database/branch before retrying; the script deliberately refuses to restore into a database that already has user tables. Keep the recovery dump private and remove it when you have verified the migration and no longer need it.

For later asset-only refreshes, run `python scripts/load_dashboard_assets.py --target-neon` from the repository root and enter the direct URL when prompted. This replaces only the three dashboard asset tables, not the case data.

## 3. Create a restricted app role

Do not put Neon’s owner URL in Streamlit secrets. Create a separate role named `phantom_equity_app` in the Neon Console and set its password there. Then, in the Neon SQL Editor as the owner, grant it read access:

    GRANT CONNECT ON DATABASE neondb TO phantom_equity_app;
    GRANT USAGE ON SCHEMA public TO phantom_equity_app;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO phantom_equity_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT ON TABLES TO phantom_equity_app;

Replace `neondb` if your database has another name. Run the default-privileges statement as the role that owns tables so future tables created by that owner remain readable. The public app does not get table creation or write privileges; its review form is disabled too.

## 4. Deploy the dashboard

1. Open Streamlit Community Cloud and create an app from `Kri-me/phantom-equity`, branch `main`, entrypoint `views/investigation_queue.py`.
2. In the app’s **Secrets** settings, provide the pooled URL using the restricted role and enable public read-only mode:

       DATABASE_URL = "postgresql://phantom_equity_app:<password>@<your-endpoint>-pooler.<region>.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
       PUBLIC_READ_ONLY = true

   Use the actual pooled URL from Neon’s Connect dialog. Do not commit the URL or a `.streamlit/secrets.toml` file. The dashboard reads `DATABASE_URL` and `PUBLIC_READ_ONLY` from Streamlit secrets in the hosted app, and from the local `.env` during development. [Streamlit Community Cloud secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
3. Set the app’s visibility to public so recruiters can open it without signing in. Streamlit deploys the entrypoint from the selected GitHub branch. [Streamlit deployment instructions](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)

The dashboard data pages use PostgreSQL-backed assets after migration. Locally, the current CSV fallback continues to work.

## 5. Refreshing the hosted data later

A full `run_pipeline.py` refresh clears and rebuilds core evidence and case tables; it is not a safe read-only operation. Back up the Neon database first. Run ingestion/ETL with the Neon owner/direct connection in a private local environment, then run `pluto_geocode.py` and refresh the three dashboard assets. Never give the hosted dashboard the owner role.

After a refresh, the Streamlit app’s cached data can take up to five minutes to reload. Restart the app or wait for its cache to expire if it still shows the prior asset snapshot.

## Reference behavior

- Neon recommends pooled connections for web-app traffic and direct connections for `pg_dump` / `pg_restore` and admin work.
- Streamlit Community Cloud secrets belong in the app’s Secrets settings, not in Git.
- The dashboard is intentionally public and read-only. It displays public-record-derived identities and addresses alongside generated candidate labels; these labels are unverified leads.
