from collections import Counter, defaultdict
from pathlib import Path
import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def get_connection():
    database_url = os.getenv("DATABASE_URL", "").strip()

    if database_url and "profitability_diagnostics" not in database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "phantom_equity"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
    )


# ============================================================
# SCORING WEIGHTS
# Preserve the existing corroboration model.
# ============================================================

WEIGHTS = {
    "signal_diversity": 30,
    "signal_volume": 5,
    "connecting_entity": 15,
    "evidence_only_entity": 3,
    "round_trip": 30,
    "temporal_180_days": 20,
    "temporal_365_days": 10,
    "mismatch_penalty": -5,
    # Added after reviewing real output (case 771 scored 17,315 on 2,193 signals; case 979 scored 185
    # on one signal). Both are starting guesses -- tune after inspecting the new queue.
    "signal_volume_cap": 20,        # volume points stop growing after this many extra signals
    "family_hint_penalty": -10,     # once per case if any nominal chain shares a grantor/grantee name token
}


# ============================================================
# DATABASE HELPERS
# ============================================================

def fetch_all(cursor, query, params=None):
    cursor.execute(query, params or ())
    return cursor.fetchall()


def table_exists(cursor, table_name):
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        );
        """,
        (table_name,),
    )
    return cursor.fetchone()[0]


# ============================================================
# CASE SIGNALS
# ============================================================

def fetch_case_signals(cursor, case_id):
    """
    Retrieve signals attached to a case.

    Supports:
      - flip
      - over_mortgaging
      - nominal_chain

    Nominal-chain records are treated as another signal family.
    Their presence does not independently prove wrongdoing.
    """

    query = """
        SELECT
            cs.signal_type,
            cs.signal_id,
            sf.bbl,
            sf.sale1_document_id AS doc_a,
            sf.sale2_document_id AS doc_b,
            sf.unit_or_type_mismatch
        FROM case_signals cs
        JOIN signal_flips sf
          ON cs.signal_type = 'flip'
         AND cs.signal_id = sf.signal_id
        WHERE cs.case_id = %s

        UNION ALL

        SELECT
            cs.signal_type,
            cs.signal_id,
            so.bbl,
            so.deed_document_id AS doc_a,
            so.mtge_document_id AS doc_b,
            so.unit_or_type_mismatch
        FROM case_signals cs
        JOIN signal_over_mortgaging so
          ON cs.signal_type = 'over_mortgaging'
         AND cs.signal_id = so.signal_id
        WHERE cs.case_id = %s

        UNION ALL

        SELECT
            cs.signal_type,
            cs.signal_id,
            snc.bbl,
            snc.deeda_doc_id AS doc_a,
            snc.followon_doc_id AS doc_b,
            FALSE AS unit_or_type_mismatch
        FROM case_signals cs
        JOIN signal_nominal_chains snc
          ON cs.signal_type = 'nominal_chain'
         AND cs.signal_id = snc.signal_id
        WHERE cs.case_id = %s

        ORDER BY signal_type, signal_id;
    """

    cursor.execute(query, (case_id, case_id, case_id))
    rows = cursor.fetchall()

    return [
        {
            "signal_type": row[0],
            "signal_id": row[1],
            "bbl": row[2],
            "doc_a": row[3],
            "doc_b": row[4],
            "unit_or_type_mismatch": bool(row[5]),
        }
        for row in rows
    ]

# ============================================================
# CASE ENTITIES AND RELATIONSHIPS
# ============================================================

def fetch_case_relationships(cursor, case_id):
    """
    Split the case's resolved entities into connecting and evidence-only.

    An entity is CONNECTING only if it holds an ownership-type (is_connecting) edge on at least TWO of the
    case's own properties, i.e. it actually links properties together. Edges on properties outside the case
    are ignored. Everything else on the case is evidence-only.

    (The earlier version counted any is_connecting edge anywhere in the database, which made nearly every
    entity "connecting" and let one-signal cases collect +15 per party.)
    """

    cursor.execute(
        """
        SELECT
            ce.entity_id,
            COUNT(DISTINCT er.bbl) FILTER (WHERE er.is_connecting) AS n_case_bbls
        FROM case_entities ce
        LEFT JOIN entity_relationships er
          ON er.entity_id = ce.entity_id
         AND er.bbl IN (SELECT bbl FROM case_properties WHERE case_id = %s)
        WHERE ce.case_id = %s
          AND ce.entity_id IS NOT NULL
        GROUP BY ce.entity_id;
        """,
        (case_id, case_id),
    )

    rows = cursor.fetchall()
    all_entity_ids = {row[0] for row in rows}
    connecting_entity_ids = {row[0] for row in rows if (row[1] or 0) >= 2}
    evidence_only_ids = all_entity_ids - connecting_entity_ids

    return connecting_entity_ids, evidence_only_ids


# ============================================================
# ROUND-TRIP DETECTION
# ============================================================

def detect_round_trip(cursor, case_id):
    """
    Detect an entity appearing exclusively in an outgoing role on one
    property and exclusively in an incoming role on another property.

    Roles are drawn from document_parties.role (populated by
    etl_load.py from the ACRIS Document Control Codes decode) and
    stored verbatim on entity_relationships.role.

    Party 1 / Party 2 are excluded from the direction sets because
    ACRIS did not decode their direction, so they cannot support a
    reliable round-trip test.

    Only edges on the case's OWN properties are considered, so a round trip
    needs the entity to be outgoing-only on one case property and
    incoming-only on another. A single-property case can never earn it.
    """

    OUTGOING_ROLES = {
        "GRANTOR/SELLER",
        "GRANTOR",
        "TRANSFEROR",
        "PARTY 1/GRANTOR",
        "ASSIGNOR",
        "ASSIGNOR/OLD LENDER",
        "LESSOR",
        "LESSOR/LANDLORD",
        "LIFE ESTATE RETAINED",
    }

    INCOMING_ROLES = {
        "GRANTEE/BUYER",
        "GRANTEE",
        "PARTY 2/GRANTEE",
        "ASSIGNEE",
        "ASSIGNEE/NEW LENDER",
        "LESSEE",
        "LESSEE/TENANT",
        "DESGNTD BENEFICIARY",
    }

    cursor.execute(
        """
        SELECT
            er.entity_id,
            er.bbl,
            UPPER(COALESCE(er.role, '')) AS role
        FROM entity_relationships er
        JOIN case_entities ce
          ON ce.entity_id = er.entity_id
        WHERE ce.case_id = %s
          AND er.entity_id IS NOT NULL
          AND er.bbl IS NOT NULL
          AND er.bbl IN (SELECT bbl FROM case_properties WHERE case_id = %s);
        """,
        (case_id, case_id),
    )

    entity_roles = defaultdict(lambda: defaultdict(set))

    for entity_id, bbl, role in cursor.fetchall():
        role = (role or "").strip()

        if role in OUTGOING_ROLES:
            entity_roles[entity_id][bbl].add("OUT")
        elif role in INCOMING_ROLES:
            entity_roles[entity_id][bbl].add("IN")

    for entity_id, property_roles in entity_roles.items():
        has_out_only = any(
            roles == {"OUT"}
            for roles in property_roles.values()
        )
        has_in_only = any(
            roles == {"IN"}
            for roles in property_roles.values()
        )

        if has_out_only and has_in_only:
            return True

    return False


# ============================================================
# TEMPORAL COHERENCE
# ============================================================

def fetch_case_date_span(cursor, case_id):
    cursor.execute(
        """
        SELECT
            MIN(event_date),
            MAX(event_date)
        FROM case_events
        WHERE case_id = %s
          AND event_date IS NOT NULL;
        """,
        (case_id,),
    )

    first_date, last_date = cursor.fetchone()

    if first_date is None or last_date is None:
        return None

    return (last_date - first_date).days


# ============================================================
# FAMILY HINT
# ============================================================

def fetch_family_hint_count(cursor, case_id):
    """
    Count the case's nominal chains whose grantor and grantee share a name token.
    transfer_signals.py documents this as a family hint that is usually benign
    (estate planning, restructuring), so it lowers the priority slightly.
    """

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM case_signals cs
        JOIN signal_nominal_chains snc
          ON cs.signal_type = 'nominal_chain'
         AND cs.signal_id = snc.signal_id
        WHERE cs.case_id = %s
          AND LOWER(COALESCE(snc.shared_name_token, '')) IN ('true', 't', '1', 'yes', 'y');
        """,
        (case_id,),
    )

    return cursor.fetchone()[0]


# ============================================================
# CASE SUMMARY
# ============================================================

def build_case_summary(
    signals,
    connecting_entity_count,
    evidence_only_entity_count,
    round_trip,
    date_span_days,
    mismatch_count,
):
    signal_counts = Counter(
        signal["signal_type"]
        for signal in signals
    )

    parts = [
        f"{len(signals)} signal(s)",
        f"{len(signal_counts)} signal type(s)",
        f"{connecting_entity_count} connecting entity/entities",
        f"{evidence_only_entity_count} evidence-only entity/entities",
    ]

    if signal_counts:
        signal_description = ", ".join(
            f"{signal_type}: {count}"
            for signal_type, count in sorted(signal_counts.items())
        )
        parts.append(f"Signal breakdown: {signal_description}")

    if round_trip:
        parts.append("Cross-property round-trip pattern detected")

    if date_span_days is not None:
        parts.append(f"Observed event span: {date_span_days} day(s)")

    if mismatch_count:
        parts.append(f"{mismatch_count} mismatch indicator(s)")

    parts.append(
        "Candidate for analyst review; these indicators do not establish fraud."
    )

    return ". ".join(parts) + "."


# ============================================================
# CORROBORATION SCORING
# ============================================================

def score_case(cursor, case_id):
    signals = fetch_case_signals(cursor, case_id)

    connecting_entity_ids, evidence_only_ids = (
        fetch_case_relationships(cursor, case_id)
    )

    round_trip = detect_round_trip(cursor, case_id)
    date_span_days = fetch_case_date_span(cursor, case_id)
    family_hint_count = fetch_family_hint_count(cursor, case_id)

    signal_types = {
        signal["signal_type"]
        for signal in signals
    }

    mismatch_count = sum(
        signal["unit_or_type_mismatch"]
        for signal in signals
    )

    # --------------------------------------------------------
    # Signal diversity
    # Preserve the existing weight: +30 for each additional
    # distinct signal type beyond the first.
    # --------------------------------------------------------

    diversity_points = max(0, len(signal_types) - 1) * (
        WEIGHTS["signal_diversity"]
    )

    # --------------------------------------------------------
    # Signal volume
    # Preserve the existing weight: +5 for each additional
    # signal beyond the first.
    # --------------------------------------------------------

    # Capped: one large case must not dwarf the whole queue.
    extra_signals = max(0, len(signals) - 1)
    volume_points = min(extra_signals, WEIGHTS["signal_volume_cap"]) * (
        WEIGHTS["signal_volume"]
    )

    # --------------------------------------------------------
    # Entity contribution
    # --------------------------------------------------------

    connecting_entity_points = (
        len(connecting_entity_ids)
        * WEIGHTS["connecting_entity"]
    )

    evidence_only_entity_points = (
        len(evidence_only_ids)
        * WEIGHTS["evidence_only_entity"]
    )

    # --------------------------------------------------------
    # Cross-property round-trip
    # --------------------------------------------------------

    round_trip_points = (
        WEIGHTS["round_trip"]
        if round_trip
        else 0
    )

    # --------------------------------------------------------
    # Temporal coherence
    # --------------------------------------------------------

    if date_span_days is not None and date_span_days <= 180:
        temporal_points = WEIGHTS["temporal_180_days"]

    elif date_span_days is not None and date_span_days <= 365:
        temporal_points = WEIGHTS["temporal_365_days"]

    else:
        temporal_points = 0

    # --------------------------------------------------------
    # Mismatch penalty
    # --------------------------------------------------------

    mismatch_points = (
        mismatch_count * WEIGHTS["mismatch_penalty"]
    )

    # --------------------------------------------------------
    # Final score
    # --------------------------------------------------------

    family_points = (
        WEIGHTS["family_hint_penalty"]
        if family_hint_count
        else 0
    )

    score_components = {
        "signal_diversity": diversity_points,
        "signal_volume": volume_points,
        "connecting_entities": connecting_entity_points,
        "evidence_only_entities": evidence_only_entity_points,
        "round_trip": round_trip_points,
        "temporal_coherence": temporal_points,
        "mismatch_penalty": mismatch_points,
        "family_hint_adjustment": family_points,
    }

    score = sum(score_components.values())

    summary = build_case_summary(
        signals=signals,
        connecting_entity_count=len(connecting_entity_ids),
        evidence_only_entity_count=len(evidence_only_ids),
        round_trip=round_trip,
        date_span_days=date_span_days,
        mismatch_count=mismatch_count,
    )

    audit_note = (
        "Corroboration scoring audit\n"
        f"Case ID: {case_id}\n"
        f"Total score: {score:.2f}\n"
        f"Signals: {len(signals)}\n"
        f"Distinct signal types: {len(signal_types)}\n"
        f"Connecting entities: {len(connecting_entity_ids)}\n"
        f"Evidence-only entities: {len(evidence_only_ids)}\n"
        f"Cross-property round-trip: {round_trip}\n"
        f"Event span (days): {date_span_days}\n"
        f"Mismatch indicators: {mismatch_count}\n"
        f"Nominal chains with shared name token: {family_hint_count}\n"
        f"Signal volume capped: {extra_signals > WEIGHTS['signal_volume_cap']}\n"
        "\nScore components:\n"
        + "\n".join(
            f"- {name}: {value:+.2f}"
            for name, value in score_components.items()
        )
        + "\n\nInterpretation: investigation-priority score, "
          "not a fraud probability or finding."
    )

    return score, summary, audit_note


# ============================================================
# UPDATE CASES AND AUDIT NOTES
# ============================================================

def update_case(cursor, case_id, score, summary, audit_note):
    cursor.execute(
        """
        UPDATE cases
        SET score = %s,
            summary = %s
        WHERE case_id = %s;
        """,
        (score, summary, case_id),
    )

    cursor.execute(
        """
        DELETE FROM case_notes
        WHERE case_id = %s
          AND author = 'corroboration_engine';
        """,
        (case_id,),
    )

    cursor.execute(
        """
        INSERT INTO case_notes (
            case_id,
            author,
            note_text
        )
        VALUES (%s, %s, %s);
        """,
        (
            case_id,
            "corroboration_engine",
            audit_note,
        ),
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 80)
    print("PHANTOM EQUITY — CORROBORATION ENGINE")
    print("=" * 80)

    connection = get_connection()

    try:
        connection.autocommit = False

        with connection.cursor() as cursor:
            required_tables = [
                "cases",
                "case_signals",
                "case_entities",
                "case_events",
                "case_notes",
                "signal_flips",
                "signal_over_mortgaging",
                "entity_relationships",
            ]

            missing_tables = [
                table
                for table in required_tables
                if not table_exists(cursor, table)
            ]

            if missing_tables:
                raise RuntimeError(
                    "Required database tables are missing: "
                    + ", ".join(missing_tables)
                )

            has_nominal_chain_table = table_exists(
                cursor,
                "signal_nominal_chains",
            )

            cursor.execute(
                """
                SELECT case_id
                FROM cases
                ORDER BY case_id;
                """
            )

            case_ids = [
                row[0]
                for row in cursor.fetchall()
            ]

            print(f"Cases to score: {len(case_ids):,}")

            if not has_nominal_chain_table:
                print(
                    "WARNING: signal_nominal_chains does not exist. "
                    "Nominal-chain signal support cannot be used."
                )

            for index, case_id in enumerate(case_ids, start=1):
                score, summary, audit_note = score_case(
                    cursor,
                    case_id,
                )

                update_case(
                    cursor,
                    case_id,
                    score,
                    summary,
                    audit_note,
                )

                if index % 100 == 0 or index == len(case_ids):
                    print(
                        f"Processed {index:,}/{len(case_ids):,} cases"
                    )

        connection.commit()

        print("\nCorroboration scoring completed.")
        print(f"Cases updated: {len(case_ids):,}")

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


if __name__ == "__main__":
    main()
