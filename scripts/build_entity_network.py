"""
build_entity_network.py

Populates entity_relationships with the explicit graph edges connecting
resolved entities to properties, including the role and relationship_class
that explain each relationship.

Stores all resolved entity-property relationships, including institutional
lenders, government agencies, and roles that do not form case connections.
Filtering remains a query-time responsibility through relationship_class
and is_connecting.

is_connecting uses case_builder.py's OWNERSHIP_CONNECTING_ROLES directly
to prevent the two definitions from drifting apart.

Requires:
    pip install psycopg2-binary python-dotenv

Run after etl_load.py, once document_parties, document_legals, and
entities have been loaded.

Usage:
    python build_entity_network.py
"""

import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from case_builder import (
    OWNERSHIP_CONNECTING_ROLES,
    ROLE_CANONICALIZATION,
)
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from case_builder import OWNERSHIP_CONNECTING_ROLES


env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)


# Full relationship taxonomy across the role strings produced by the
# ACRIS Document Control Codes decode. Unmapped roles fall back to
# OTHER_GENERIC rather than causing the build to fail.

OWNERSHIP_TRANSFER_ROLES = {
    "GRANTOR/SELLER",
    "GRANTOR",
    "GRANTEE/BUYER",
    "GRANTEE",
    "TRANSFEROR",
    "DESGNTD BENEFICIARY",
    "PARTY 1/GRANTOR",
    "PARTY 2/GRANTEE",
    "LIFE ESTATE RETAINED",
}

FINANCING_ROLES = {
    "MORTGAGEE/LENDER",
    "MORTGAGOR/BORROWER",
    "MORTGAGER/BORROWER",
    "MORTGAGEE",
    "MORTGAGOR",
    "SECURED PARTY",
    "DEBTOR",
    "PAYER",
}

ASSIGNMENT_ROLES = {
    "ASSIGNOR/OLD LENDER",
    "ASSIGNEE/NEW LENDER",
    "ASSIGNOR",
    "ASSIGNEE",
}

LEASE_ROLES = {
    "LESSOR/LANDLORD",
    "LESSEE/TENANT",
}


def get_connection():
    """Connect using the project's existing environment configuration."""
    database_url = os.environ.get("DATABASE_URL")

    if (
        database_url
        and "profitability_diagnostics" not in database_url
    ):
        database_url = database_url.replace(
            "postgresql+psycopg://",
            "postgresql://",
        )
        database_url = database_url.replace(
            "postgresql+psycopg2://",
            "postgresql://",
        )
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "phantom_equity"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def classify_relationship(role):
    """Map a document-party role to its broad relationship class."""
    if role in OWNERSHIP_TRANSFER_ROLES:
        return "OWNERSHIP_TRANSFER"

    if role in FINANCING_ROLES:
        return "FINANCING"

    if role in ASSIGNMENT_ROLES:
        return "ASSIGNMENT"

    if role in LEASE_ROLES:
        return "LEASE"

    return "OTHER_GENERIC"


def validate_tables(conn):
    """Check required tables before modifying the persisted network."""
    required_tables = {
        "document_parties",
        "document_legals",
        "entity_relationships",
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            """,
            (list(required_tables),),
        )
        existing_tables = {row[0] for row in cur.fetchall()}

    missing_tables = required_tables - existing_tables

    if missing_tables:
        raise RuntimeError(
            "Required database tables are missing: "
            + ", ".join(sorted(missing_tables))
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'entity_relationships'
            """
        )
        existing_columns = {row[0] for row in cur.fetchall()}

    required_columns = {
        "entity_id",
        "bbl",
        "document_id",
        "role",
        "relationship_class",
        "is_connecting",
    }

    missing_columns = required_columns - existing_columns

    if missing_columns:
        raise RuntimeError(
            "entity_relationships is missing required columns: "
            + ", ".join(sorted(missing_columns))
        )

    print("Database schema validation passed.")


def truncate_network(conn):
    """Clear the previous network within the active transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE entity_relationships RESTART IDENTITY"
        )

    print("Truncated entity_relationships.")


def build_relationships(conn):
    """Rebuild the complete entity-property relationship edge list."""
    with conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(
            """
            SELECT
                dp.entity_id,
                dl.bbl,
                dp.document_id,
                dp.role
            FROM document_parties dp
            JOIN document_legals dl
              ON dl.document_id = dp.document_id
            WHERE dp.entity_id IS NOT NULL
            """
        )
        rows = cur.fetchall()

    print(
        f"Fetched {len(rows):,} entity-property-document combinations."
    )

    insert_rows = []

    for row in rows:
        raw_role = row["role"]
        role = ROLE_CANONICALIZATION.get(raw_role, raw_role)

        insert_rows.append(
            (
                row["entity_id"],
                row["bbl"],
                row["document_id"],
                role,
                classify_relationship(role),
                role in OWNERSHIP_CONNECTING_ROLES,
            )
        )
    if insert_rows:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO entity_relationships (
                    entity_id,
                    bbl,
                    document_id,
                    role,
                    relationship_class,
                    is_connecting
                )
                VALUES %s
                """,
                insert_rows,
                page_size=5000,
            )

    print(
        f"Loaded {len(insert_rows):,} entity_relationships rows."
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relationship_class, COUNT(*)
            FROM entity_relationships
            GROUP BY relationship_class
            ORDER BY COUNT(*) DESC
            """
        )

        print("\nBreakdown by relationship_class:")
        for relationship_class, count in cur.fetchall():
            print(f"  {relationship_class}: {count:,}")

        cur.execute(
            """
            SELECT is_connecting, COUNT(*)
            FROM entity_relationships
            GROUP BY is_connecting
            ORDER BY is_connecting
            """
        )

        print("\nBreakdown by is_connecting:")
        for is_connecting, count in cur.fetchall():
            print(f"  {is_connecting}: {count:,}")


def main():
    conn = None

    try:
        conn = get_connection()
        print("Connected to the database.")

        validate_tables(conn)
        truncate_network(conn)
        build_relationships(conn)

        # Commit the truncate and rebuild together. If a preceding step
        # fails, the exception handler rolls the transaction back.
        conn.commit()

        print("\nEntity network build complete.")

    except Exception as exc:
        if conn is not None:
            conn.rollback()

        print(
            f"\nEntity network build failed: {exc}",
            file=sys.stderr,
        )
        raise

    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()