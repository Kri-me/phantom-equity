"""One-time, guided migration of the local Phantom Equity database to Neon.

The target URL is requested with hidden input, must be Neon’s direct (non-pooler)
URL, and is never printed or written to disk. The custom-format dump is stored
under outputs/neon_migration/ (ignored by Git) so it can be checked or reused.
"""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg2
from psycopg2.extensions import make_dsn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=True)


def parse_database_url(url: str, *, require_neon: bool = False):
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("Expected a postgresql:// connection URL")
    if not parsed.hostname or not parsed.username or not parsed.path.strip("/"):
        raise ValueError("Connection URL must include username, host, and database")
    if require_neon and not parsed.hostname.endswith(".neon.tech"):
        raise ValueError("Target URL does not look like a Neon connection URL")
    if require_neon and "-pooler." in parsed.hostname:
        raise ValueError("Use the direct (non-pooler) Neon URL for dump and restore")

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params = {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "user": unquote(parsed.username),
        "dbname": unquote(parsed.path.lstrip("/")),
    }
    for key in ("sslmode", "channel_binding", "connect_timeout", "application_name"):
        if key in query:
            params[key] = query[key]
    if require_neon and "sslmode" not in params:
        params["sslmode"] = "require"
    return make_dsn(**params), unquote(parsed.password or ""), params["dbname"]


def source_connection():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        dsn, password, dbname = parse_database_url(url)
        return dsn, password or os.environ.get("PGPASSWORD", ""), dbname
    params = {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": os.environ.get("PGPORT", "5432"),
        "dbname": os.environ.get("PGDATABASE", "phantom_equity"),
        "user": os.environ.get("PGUSER", "postgres"),
    }
    return make_dsn(**params), os.environ.get("PGPASSWORD", ""), params["dbname"]


def find_program(name: str, env_name: str) -> str:
    configured = os.environ.get(env_name)
    executable = configured or shutil.which(name)
    if executable:
        return executable
    candidates = sorted(Path(r"C:\Program Files\PostgreSQL").glob(f"*/bin/{name}.exe"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise FileNotFoundError(f"{name} was not found; install PostgreSQL command-line tools or set {env_name}.")


def run_tool(command, env):
    # Do not echo commands: target credentials are supplied through PGPASSWORD,
    # never through process arguments or console output.
    result = subprocess.run(command, env=env, check=False)
    if result.returncode:
        raise RuntimeError(f"Database migration command failed with exit code {result.returncode}.")


def main():
    target_url = getpass.getpass("Paste Neon direct connection URL (input hidden): ").strip()
    target_dsn, target_password, target_db = parse_database_url(target_url, require_neon=True)
    confirm = input(f"This will restore the local database into the EMPTY Neon database '{target_db}'. Type RESTORE to continue: ")
    if confirm != "RESTORE":
        raise SystemExit("Stopped without making changes.")

    source_dsn, source_password, source_db = source_connection()
    with psycopg2.connect(source_dsn, password=source_password) as source:
        with source.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            size_bytes = cur.fetchone()[0]
    print(f"Source database: {source_db}; size: {size_bytes:,} bytes")

    with psycopg2.connect(target_dsn, password=target_password) as target:
        with target.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            """)
            table_count = cur.fetchone()[0]
    if table_count:
        raise SystemExit(f"Target database has {table_count} user table(s). Use a fresh empty Neon database; nothing was restored.")

    dump_dir = ROOT / "outputs" / "neon_migration"
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dump_dir / "phantom_equity.dump"
    pg_dump = find_program("pg_dump", "PG_DUMP")
    pg_restore = find_program("pg_restore", "PG_RESTORE")

    source_env = os.environ.copy()
    if source_password:
        source_env["PGPASSWORD"] = source_password
    print("Creating a custom-format local database dump...")
    run_tool([
        pg_dump, "--format=custom", "--no-owner", "--no-acl",
        "--file", str(dump_path), "--dbname", source_dsn,
    ], source_env)

    target_env = os.environ.copy()
    if target_password:
        target_env["PGPASSWORD"] = target_password
    print("Restoring the dump to Neon...")
    run_tool([
        pg_restore, "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction",
        "--dbname", target_dsn, str(dump_path),
    ], target_env)

    migration = ROOT / "migrations" / "004_dashboard_assets_and_reviews.sql"
    with psycopg2.connect(target_dsn, password=target_password) as target:
        with target.cursor() as cur:
            cur.execute(migration.read_text(encoding="utf-8"))
        from load_dashboard_assets import load_assets
        load_assets(target)
        with target.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            final_size = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            public_tables = cur.fetchone()[0]
    print(f"Migration complete: {public_tables} public tables; {final_size:,} database bytes.")
    print(f"Local recovery dump retained at: {dump_path}")
    print("Create a separate read-only app role before putting a pooled URL into Streamlit secrets.")


if __name__ == "__main__":
    main()
