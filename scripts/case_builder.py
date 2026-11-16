"""
case_builder.py

Builds the cases layer from candidate signals loaded by etl_load.py.

Case-forming signal families:
    - signal_flips: candidate rapid-transfer signals
    - signal_over_mortgaging: candidate deed/mortgage signals
    - signal_nominal_chains: nominal transfer-chain signals

Recurring flip and block-pattern mortgage signals remain excluded.

CASE-CREATION LOGIC
-------------------
Each candidate signal is associated with a property and its relevant
documents. Resolved LLC/company and trustee entities on those documents
can connect properties into a shared investigative case.

Properties without shared eligible entities remain separate cases.

Nominal chains qualify as case-forming evidence when both document IDs
and the property identifier are present. Their underlying documents
are included in case timelines.

Repeated-transfer summaries, CEMA proxies, and adjusted valuation
residuals are not yet used to create or score cases.

SCORING
-------
The existing transparent heuristic is retained:
    +10 per candidate signal
    +15 per additional property
    +5 per linked entity
    -3 per unit/property-type mismatch

This is a triage heuristic, not a fraud probability.

FULL REBUILD
------------
The case layer is truncated and rebuilt from the currently loaded
candidate signals each time this script runs.

Requires:
    pip install psycopg2-binary python-dotenv

Usage:
    python case_builder.py
"""

from dotenv import load_dotenv
import os
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras


# ============================================================
# CONFIGURATION
# ============================================================

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# ============================================================
# SHARED CONSTANTS
# ============================================================

# Canonicalize role spelling variants produced by the ACRIS decode
# so downstream queries only ever see one spelling per role.
ROLE_CANONICALIZATION = {
    "PARTY ONE": "PARTY 1",
    "PARTY TWO": "PARTY 2",
    "MORTGAGER/BORROWER": "MORTGAGOR/BORROWER",
    "PARTY TWO/SECURED PA": "PARTY TWO/SECURED PARTY",
}

# Roles that indicate an ownership, control, or direct interest in
# the property, and therefore allow an entity to link two properties
# into a shared case.
#
# Financing and assignment roles (mortgagee, mortgagor, assignor,
# assignee, secured party, debtor, payer) are deliberately excluded:
# an institutional lender appearing on thousands of unrelated
# mortgages must not merge those properties into a single case.
#
# Shared with build_entity_network.py so is_connecting stays
# consistent with case-merging behavior.
OWNERSHIP_CONNECTING_ROLES = {
    # Ownership transfer (deeds, grants)
    "GRANTOR/SELLER",
    "GRANTOR",
    "GRANTEE/BUYER",
    "GRANTEE",
    "TRANSFEROR",
    "DESGNTD BENEFICIARY",
    "PARTY 1/GRANTOR",
    "PARTY 2/GRANTEE",
    "LIFE ESTATE RETAINED",

    # Lease (direct property interest)
    "LESSOR",
    "LESSOR/LANDLORD",
    "LESSEE",
    "LESSEE/TENANT",

    # Generic party slots (ACRIS did not decode direction).
    # Direction-ambiguous, so not used for round-trip detection.
    "PARTY 1",
    "PARTY 2",
}

# ============================================================
# SHARED CONSTANTS
# ============================================================

# Roles that indicate an ownership, control, or direct interest
# in the property, and therefore allow an entity to link two
# properties into a shared case.
#
# Financing and assignment roles (mortgagee, mortgagor, assignor,
# assignee, secured party, debtor, payer) are deliberately
# excluded: an institutional lender appearing on thousands of
# unrelated mortgages must not merge those properties into a
# single case.
#
# Shared with build_entity_network.py so is_connecting stays
# consistent with case-merging behavior.
#
# Party 1 / Party 2 are generic slots that ACRIS did not decode
# into a specific role. They indicate a direct property interest,
# so they connect; but because their direction is ambiguous they
# are NOT used for round-trip detection (see corroboration_engine.py).
OWNERSHIP_CONNECTING_ROLES = {
    # Ownership transfer (deeds, grants)
    "GRANTOR/SELLER",
    "GRANTOR",
    "GRANTEE/BUYER",
    "GRANTEE",
    "TRANSFEROR",
    "DESGNTD BENEFICIARY",
    "PARTY 1/GRANTOR",
    "PARTY 2/GRANTEE",
    "LIFE ESTATE RETAINED",

    # Lease (direct property interest)
    "LESSOR",
    "LESSOR/LANDLORD",
    "LESSEE",
    "LESSEE/TENANT",

    # Generic party slots (ACRIS did not decode direction)
    "PARTY 1",
    "PARTY ONE",
    "PARTY 2",
    "PARTY TWO",
}

# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():
    database_url = os.environ.get("DATABASE_URL")

    if database_url and "profitability_diagnostics" not in database_url:
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


# ============================================================
# UNION-FIND
# ============================================================

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)

        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]

        return x

    def union(self, a, b):
        root_a = self.find(a)
        root_b = self.find(b)

        if root_a != root_b:
            self.parent[root_a] = root_b


# ============================================================
# CASE-LAYER RESET
# ============================================================

def truncate_case_layer(conn):
    tables = [
        "case_notes",
        "case_events",
        "case_signals",
        "case_entities",
        "case_properties",
        "cases",
    ]

    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE "
            + ", ".join(tables)
            + " RESTART IDENTITY CASCADE"
        )

    conn.commit()

    print(f"Truncated {len(tables)} case-layer tables")


# ============================================================
# FETCH CASE-FORMING SIGNALS
# ============================================================

def fetch_candidate_signals(conn):
    """
    Retrieve candidate flip, over-mortgaging, and nominal-chain signals.

    A common record shape is used throughout the case-building logic:

        signal_id
        signal_type
        bbl
        doc_a
        doc_b
        unit_or_type_mismatch

    For nominal chains, doc_a is deedA_doc_id and doc_b is
    followon_doc_id.

    Nominal-chain records missing either document ID or their BBL
    are excluded because they cannot support a complete transfer
    timeline or reliable property-level case association.
    """
    query = """
        SELECT
            signal_id,
            'flip' AS signal_type,
            bbl,
            sale1_document_id AS doc_a,
            sale2_document_id AS doc_b,
            unit_or_type_mismatch
        FROM signal_flips
        WHERE category = 'candidate'

        UNION ALL

        SELECT
            signal_id,
            'over_mortgaging' AS signal_type,
            bbl,
            deed_document_id AS doc_a,
            mtge_document_id AS doc_b,
            unit_or_type_mismatch
        FROM signal_over_mortgaging
        WHERE category = 'candidate'

        UNION ALL

        SELECT
            signal_id,
            'nominal_chain' AS signal_type,
            bbl,
            deedA_doc_id AS doc_a,
            followon_doc_id AS doc_b,
            FALSE AS unit_or_type_mismatch
        FROM signal_nominal_chains
        WHERE bbl IS NOT NULL
          AND deedA_doc_id IS NOT NULL
          AND followon_doc_id IS NOT NULL
    """

    with conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(query)
        signals = cur.fetchall()

    # Reject malformed records that cannot be associated with a property
    # and two underlying documents.
    valid_signals = []

    for signal in signals:
        if not signal["bbl"]:
            continue

        if not signal["doc_a"] or not signal["doc_b"]:
            continue

        valid_signals.append(signal)

    excluded = len(signals) - len(valid_signals)

    if excluded:
        print(
            f"Excluded {excluded} malformed case-forming signal(s) "
            f"with missing property/document identifiers"
        )

    counts = defaultdict(int)

    for signal in valid_signals:
        counts[signal["signal_type"]] += 1

    print("Case-forming signals loaded:")

    for signal_type in (
        "flip",
        "over_mortgaging",
        "nominal_chain",
    ):
        print(f"  {signal_type}: {counts[signal_type]:,}")

    return valid_signals


# ============================================================
# FETCH ELIGIBLE ENTITIES BY DOCUMENT
# ============================================================

def fetch_document_entities(conn, doc_ids):
    """
    Retrieve eligible resolved entities appearing on the supplied
    documents.

    Only LLC/company and trustee entities may connect properties
    into a shared case.

    Institutional lenders and government agencies remain excluded
    from case-merging logic to avoid joining unrelated properties
    through high-volume financial institutions.

    Existing filer-category exclusions are preserved.
    """
    if not doc_ids:
        return defaultdict(set)

    with conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(
            """
            SELECT
                dp.document_id,
                dp.entity_id
            FROM document_parties dp
            JOIN entities e
                ON e.entity_id = dp.entity_id
            WHERE dp.document_id = ANY(%s)
            AND dp.entity_id IS NOT NULL
            AND e.entity_kind IN ('llc_or_company', 'trustee')
            AND dp.role = ANY(%s)
            AND (
                    dp.filer_category IS NULL
                    OR dp.filer_category NOT IN (
                        'TIMESHARE_NOISE',
                        'INSTITUTIONAL_LENDER_NOISE',
                        'GOVERNMENT_AGENCY_NOISE',
                        'BULK_ENTITY_FILER'
                    )
            )
            """,
            (list(doc_ids), list(OWNERSHIP_CONNECTING_ROLES)),
        )

        result = defaultdict(set)

        for row in cur.fetchall():
            result[row["document_id"]].add(row["entity_id"])

        return result


# ============================================================
# FETCH DOCUMENT TIMELINE INFORMATION
# ============================================================

def fetch_document_information(conn, doc_ids):
    """
    Fetch timeline metadata once for all documents used by the cases.
    """
    if not doc_ids:
        return {}

    with conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(
            """
            SELECT
                document_id,
                doc_type,
                document_date,
                recorded_datetime
            FROM documents
            WHERE document_id = ANY(%s)
            """,
            (list(doc_ids),),
        )

        return {
            row["document_id"]: row
            for row in cur.fetchall()
        }


# ============================================================
# CASE CONSTRUCTION
# ============================================================

def build_cases(conn):
    signals = fetch_candidate_signals(conn)

    if not signals:
        print(
            "No valid candidate signals found. "
            "No cases will be created."
        )
        return

    # --------------------------------------------------------
    # 1. Collect documents needed for entity linking
    # --------------------------------------------------------

    doc_ids_needed = set()

    for signal in signals:
        doc_ids_needed.add(signal["doc_a"])
        doc_ids_needed.add(signal["doc_b"])

    doc_entities = fetch_document_entities(
        conn,
        doc_ids_needed,
    )

    # --------------------------------------------------------
    # 2. Build the property/entity graph
    # --------------------------------------------------------

    union_find = UnionFind()

    signal_lookup = {}
    signal_bbls = defaultdict(set)
    bbl_entities = defaultdict(set)

    for signal in signals:
        signal_key = (
            signal["signal_type"],
            signal["signal_id"],
        )

        signal_lookup[signal_key] = signal

        bbl = signal["bbl"]
        signal_bbls[bbl].add(signal_key)

        property_node = f"prop:{bbl}"
        union_find.find(property_node)

        entities = (
            doc_entities.get(signal["doc_a"], set())
            | doc_entities.get(signal["doc_b"], set())
        )

        bbl_entities[bbl] |= entities

        for entity_id in entities:
            union_find.union(
                property_node,
                f"ent:{entity_id}",
            )

    # --------------------------------------------------------
    # 3. Aggregate connected components into cases
    # --------------------------------------------------------

    components = defaultdict(
        lambda: {
            "bbls": set(),
            "entities": set(),
            "signals": set(),
        }
    )

    for bbl, property_signals in signal_bbls.items():
        root = union_find.find(f"prop:{bbl}")

        components[root]["bbls"].add(bbl)
        components[root]["entities"] |= bbl_entities[bbl]
        components[root]["signals"] |= property_signals

    signal_type_counts = defaultdict(int)

    for signal in signals:
        signal_type_counts[signal["signal_type"]] += 1

    print(
        f"\n{len(signals):,} candidate signals across "
        f"{len(signal_bbls):,} properties "
        f"-> {len(components):,} case(s)"
    )

    print(
        "Signal composition: "
        f"{signal_type_counts['flip']:,} flips, "
        f"{signal_type_counts['over_mortgaging']:,} "
        f"over-mortgaging signals, "
        f"{signal_type_counts['nominal_chain']:,} nominal chains"
    )

    # --------------------------------------------------------
    # 4. Fetch document dates for case timelines
    # --------------------------------------------------------

    document_information = fetch_document_information(
        conn,
        doc_ids_needed,
    )

    # --------------------------------------------------------
    # 5. Persist cases and their evidence
    # --------------------------------------------------------

    cases_created = 0

    for root, component in components.items():
        property_count = len(component["bbls"])
        entity_count = len(component["entities"])
        signal_count = len(component["signals"])

        mismatch_count = sum(
            1
            for signal_key in component["signals"]
            if signal_lookup[signal_key]["unit_or_type_mismatch"]
        )

        # Existing triage heuristic retained.
        # This is not a calibrated fraud probability.
        score = (
            signal_count * 10
            + max(0, property_count - 1) * 15
            + entity_count * 5
            - mismatch_count * 3
        )

        title = (
            f"{property_count} "
            f"propert{'y' if property_count == 1 else 'ies'}, "
            f"{signal_count} "
            f"signal{'s' if signal_count != 1 else ''}"
        )

        if entity_count:
            title += (
                f", {entity_count} linked "
                f"entit{'y' if entity_count == 1 else 'ies'}"
            )

        summary = (
            f"Auto-generated from {signal_count} candidate signal(s)."
        )

        if signal_type_counts["nominal_chain"]:
            case_nominal_count = sum(
                1
                for signal_key in component["signals"]
                if signal_key[0] == "nominal_chain"
            )

            if case_nominal_count:
                summary += (
                    f" Includes {case_nominal_count} nominal "
                    f"transfer-chain signal(s)."
                )

        if mismatch_count:
            summary += (
                f" {mismatch_count} signal(s) flagged with a "
                "unit/property-type mismatch between the two legs; "
                "verify before treating as confirmed."
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cases (
                    title,
                    summary,
                    status,
                    score,
                    created_by
                )
                VALUES (%s, %s, 'open', %s, 'system')
                RETURNING case_id
                """,
                (title, summary, score),
            )

            case_id = cur.fetchone()[0]

            # -----------------------------------------------
            # Case properties
            # -----------------------------------------------

            property_role = (
                "primary"
                if property_count == 1
                else "related"
            )

            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO case_properties (
                    case_id,
                    bbl,
                    role_in_case
                )
                VALUES %s
                """,
                [
                    (case_id, bbl, property_role)
                    for bbl in component["bbls"]
                ],
            )

            # -----------------------------------------------
            # Case entities
            # -----------------------------------------------

            if component["entities"]:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO case_entities (
                        case_id,
                        entity_id
                    )
                    VALUES %s
                    """,
                    [
                        (case_id, entity_id)
                        for entity_id in component["entities"]
                    ],
                )

            # -----------------------------------------------
            # Case signals
            # -----------------------------------------------

            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO case_signals (
                    case_id,
                    signal_type,
                    signal_id
                )
                VALUES %s
                """,
                [
                    (case_id, signal_type, signal_id)
                    for signal_type, signal_id
                    in component["signals"]
                ],
            )

            # -----------------------------------------------
            # Timeline events
            # -----------------------------------------------

            case_document_ids = set()

            for signal_key in component["signals"]:
                signal = signal_lookup[signal_key]

                case_document_ids.add(signal["doc_a"])
                case_document_ids.add(signal["doc_b"])

            event_rows = []

            for document_id in case_document_ids:
                info = document_information.get(document_id)

                if not info:
                    continue

                event_date = (
                    info["recorded_datetime"]
                    or info["document_date"]
                )

                if event_date is None:
                    continue

                event_rows.append(
                    (
                        case_id,
                        event_date,
                        "document_recorded",
                        document_id,
                        f"{info['doc_type']} recorded",
                    )
                )

            if event_rows:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO case_events (
                        case_id,
                        event_date,
                        event_type,
                        document_id,
                        description
                    )
                    VALUES %s
                    """,
                    event_rows,
                )

        conn.commit()
        cases_created += 1

    print(f"Created {cases_created:,} cases")


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    conn = get_connection()

    try:
        print(
            "Connected to database:",
            conn.get_dsn_parameters()["dbname"],
        )

        truncate_case_layer(conn)
        build_cases(conn)

        print("\nCase build complete.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()