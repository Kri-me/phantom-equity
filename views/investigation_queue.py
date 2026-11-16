"""
investigation_queue.py

Streamlit-based investigation queue: browse cases produced by case_builder.py
and scored by corroboration_engine.py, drill into a case's properties, entities,
signals, and timeline, and record analyst review status/notes.

This is a READ-mostly tool over the case data model, with one write path:
upserting a signal-level review (status, closed-out reason, note) into
signal_reviews, which no pipeline script touches. It never
re-runs the ETL, case-building, or scoring pipelines -- run etl_load.py,
case_builder.py, and corroboration_engine.py first (in that order) to
populate/refresh what this shows.

Scores shown here are explainable heuristics for TRIAGE RANKING ONLY, not a
fraud probability -- see corroboration_engine.py's module docstring.

Requires: pip install streamlit psycopg2-binary pandas python-dotenv sqlalchemy
Usage: from the scripts/ folder: streamlit run investigation_queue.py
"""

import html
import itertools
import math
from datetime import datetime
import os
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import pydeck as pdk
import streamlit as st
from dotenv import load_dotenv
from pyvis.network import Network
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

REVIEW_STATUSES = ["under_review", "strong_candidate", "false_positive"]
CLOSED_REASONS = [
    "unrelated_document", "self_transfer", "cema_consolidation",
    "tax_exempt_affordable_housing", "other",
]
AUDIT_STATUSES = ["unclear", "correctly_excluded", "wrongly_excluded"]
AUDIT_REASONS = ["name_spelling_variant", "entity_variant_not_resolved", "other"]
EXCLUSION_CSV = (
    Path(__file__).resolve().parent.parent / "data" / "processed" / "signal_party_overlap_exclusions.csv"
)
SERIAL_REFI_CSV = (
    Path(__file__).resolve().parent.parent / "data" / "processed" / "signal_serial_refinancing.csv"
)
SERIAL_REFI_REASONS = [
    "duplicate_recording", "blanket_financing", "cema_consolidation", "ordinary_refinance", "other",
]
OVER_MORTGAGE_RATIO = 0.95
REVIEWS_DDL = """
    CREATE TABLE IF NOT EXISTS signal_reviews (
        signal_type   text NOT NULL,
        bbl           text NOT NULL,
        doc_a         text NOT NULL,
        doc_b         text NOT NULL,
        status        text NOT NULL,
        closed_reason text,
        note          text,
        reviewed_at   timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (signal_type, bbl, doc_a, doc_b)
    )
"""

OUT_ROLES = {"GRANTOR/SELLER", "GRANTOR", "TRANSFEROR", "PARTY 1/GRANTOR"}
IN_ROLES = {"GRANTEE/BUYER", "GRANTEE", "DESGNTD BENEFICIARY", "PARTY 2/GRANTEE"}

RELATIONSHIP_STYLE = {
    "OWNERSHIP_TRANSFER": {"color": "#4C72B0", "dashes": False},
    "FINANCING": {"color": "#DD8452", "dashes": True},
    "ASSIGNMENT": {"color": "#8172B3", "dashes": [2, 4]},
    "LEASE": {"color": "#55A868", "dashes": [6, 3]},
    "OTHER_GENERIC": {"color": "#B0B0B0", "dashes": False},
}
WIDER_EXPANSION_LIMIT = 15

ACRIS_DOC_URL = "https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentImageView?doc_id={}"
ACRIS_SEARCH_URL = "https://a836-acris.nyc.gov/DS/DocumentSearch/Index"
SIGNAL_EXPLORER_ROW_CAP = 1000
SANITY_CEILING_RATIO = 20.0


def page_header(title, icon=None, subtitle=None):
    """Render a consistent page header for app views."""
    if icon:
        st.markdown(f"## {icon} {title}")
    else:
        st.title(title)
    if subtitle:
        st.caption(subtitle)


def acris_link(doc_id):
    if doc_id is None or pd.isna(doc_id) or not str(doc_id).strip():
        return None
    return ACRIS_DOC_URL.format(str(doc_id).strip())


def doc_link_column(label):
    return st.column_config.LinkColumn(label, display_text=r"doc_id=(.*)$")


@st.cache_resource
def get_connection():
    database_url = os.environ.get("DATABASE_URL")
    if database_url and "profitability_diagnostics" not in database_url:
        database_url = database_url.replace("postgresql+psycopg://", "postgresql://")
        database_url = database_url.replace("postgresql+psycopg2://", "postgresql://")
        conn = psycopg2.connect(database_url)
    else:
        conn = psycopg2.connect(
            host=os.environ.get("PGHOST", "localhost"),
            port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ.get("PGDATABASE", "phantom_equity"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
        )
    conn.autocommit = True
    return conn


@st.cache_resource
def get_engine() -> Engine:
    """SQLAlchemy engine for pandas.read_sql."""
    database_url = os.environ.get("DATABASE_URL")

    if database_url and "profitability_diagnostics" not in database_url:
        url = database_url.replace("postgresql+psycopg://", "postgresql+psycopg2://")
        return create_engine(url, pool_pre_ping=True)

    user = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD", "")
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    dbname = os.environ.get("PGDATABASE", "phantom_equity")

    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
    return create_engine(url, pool_pre_ping=True)


def run_query(sql, params=None):
    return pd.read_sql(sql, get_engine(), params=params)


def run_write(sql, params):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


@st.cache_data(ttl=30)
def fetch_case_list():
    return run_query("""
        SELECT c.case_id, c.title, c.status, c.score, c.summary,
               COUNT(DISTINCT cp.bbl) AS n_properties,
               COUNT(DISTINCT (cs.signal_type, cs.signal_id)) AS n_signals,
               COUNT(DISTINCT ce.entity_id) AS n_entities
        FROM cases c
        LEFT JOIN case_properties cp ON cp.case_id = c.case_id
        LEFT JOIN case_signals cs ON cs.case_id = c.case_id
        LEFT JOIN case_entities ce ON ce.case_id = c.case_id
        GROUP BY c.case_id, c.title, c.status, c.score, c.summary
        ORDER BY c.score DESC NULLS LAST
    """)


def fetch_case_properties(case_id):
    return run_query("""
        SELECT cp.bbl, cp.role_in_case, p.borough, p.block, p.lot,
               p.street_number, p.street_name, p.latest_unit, p.latest_property_type
        FROM case_properties cp
        JOIN properties p ON p.bbl = cp.bbl
        WHERE cp.case_id = %(case_id)s
        ORDER BY cp.role_in_case DESC, cp.bbl
    """, {"case_id": case_id})


def fetch_case_entities(case_id):
    return run_query("""
        SELECT e.entity_id, e.canonical_name, e.entity_kind, e.resolution_status,
               BOOL_OR(er.is_connecting) AS is_connecting
        FROM case_entities ce
        JOIN entities e ON e.entity_id = ce.entity_id
        LEFT JOIN entity_relationships er
            ON er.entity_id = ce.entity_id
           AND er.bbl IN (SELECT bbl FROM case_properties WHERE case_id = %(case_id)s)
        WHERE ce.case_id = %(case_id)s
        GROUP BY e.entity_id, e.canonical_name, e.entity_kind, e.resolution_status
        ORDER BY is_connecting DESC NULLS LAST, e.canonical_name
    """, {"case_id": case_id})


def fetch_case_flips(case_id):
    return run_query("""
        SELECT sf.signal_id, sf.bbl, sf.category, sf.unit_or_type_mismatch,
               sf.sale1_amt, d1.document_date AS sale1_date, sf.sale1_document_id,
               sf.sale2_amt, d2.document_date AS sale2_date, sf.sale2_document_id,
               sf.days_between, sf.pct_price_increase
        FROM case_signals cs
        JOIN signal_flips sf ON sf.signal_id = cs.signal_id AND cs.signal_type = 'flip'
        LEFT JOIN documents d1 ON d1.document_id = sf.sale1_document_id
        LEFT JOIN documents d2 ON d2.document_id = sf.sale2_document_id
        WHERE cs.case_id = %(case_id)s
        ORDER BY sf.pct_price_increase DESC
    """, {"case_id": case_id})


def fetch_case_over_mortgaging(case_id):
    return run_query("""
        SELECT so.signal_id, so.bbl, so.category, so.unit_or_type_mismatch,
               so.deed_amt, d1.document_date AS deed_date, so.deed_document_id,
               so.mtge_amt, d2.document_date AS mtge_date, so.mtge_document_id,
               so.days_between, so.mortgage_to_price_ratio
        FROM case_signals cs
        JOIN signal_over_mortgaging so
            ON so.signal_id = cs.signal_id AND cs.signal_type = 'over_mortgaging'
        LEFT JOIN documents d1 ON d1.document_id = so.deed_document_id
        LEFT JOIN documents d2 ON d2.document_id = so.mtge_document_id
        WHERE cs.case_id = %(case_id)s
        ORDER BY so.mortgage_to_price_ratio DESC
    """, {"case_id": case_id})


def fetch_case_events(case_id):
    return run_query("""
        SELECT event_date, event_type, document_id, description
        FROM case_events
        WHERE case_id = %(case_id)s
        ORDER BY event_date
    """, {"case_id": case_id})


def fetch_case_network_edges(case_id):
    return run_query("""
        SELECT er.entity_id, e.canonical_name, e.entity_kind, er.bbl, er.role,
               er.relationship_class, er.is_connecting
        FROM case_entities ce
        JOIN entities e ON e.entity_id = ce.entity_id
        JOIN entity_relationships er ON er.entity_id = ce.entity_id
        JOIN case_properties cp ON cp.case_id = ce.case_id AND cp.bbl = er.bbl
        WHERE ce.case_id = %(case_id)s
    """, {"case_id": case_id})


def fetch_wider_entity_edges(entity_id, exclude_bbls, limit=WIDER_EXPANSION_LIMIT):
    df = run_query("""
        SELECT bbl, role, relationship_class, is_connecting
        FROM entity_relationships
        WHERE entity_id = %(entity_id)s AND is_connecting = TRUE
        ORDER BY bbl
    """, {"entity_id": int(entity_id)})
    df = df[~df["bbl"].isin(exclude_bbls)]
    return df.head(limit)


def fetch_nominal_chains(
    chain_type=None, continuity=None, entity_grantee_only=False,
    min_gap_days=None, limit=1000,
):
    conditions = []
    params = []

    if chain_type:
        conditions.append("chain_type = %s")
        params.append(chain_type)
    if continuity:
        conditions.append("continuity = %s")
        params.append(continuity)
    if entity_grantee_only:
        conditions.append("grantee_looks_like_entity IS TRUE")
    if min_gap_days is not None:
        conditions.append("gap_days >= %s")
        params.append(int(min_gap_days))

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(int(limit))

    return run_query(
        f"""
        SELECT
            signal_id, bbl, deeda_doc_id, deeda_date, deeda_amt,
            grantor_names, grantee_names, partial_interest_pct,
            grantee_looks_like_entity, grantor_looks_like_individual,
            shared_name_token, chain_type, followon_doc_id, followon_date,
            followon_amt, gap_days, continuity
        FROM signal_nominal_chains
        {where}
        ORDER BY gap_days ASC NULLS LAST, deeda_date DESC NULLS LAST
        LIMIT %s
        """,
        tuple(params),
    )


def fetch_repeated_transfers(
    true_chain_only=False, min_chain_links=0,
    min_recording_days=0, limit=1000,
):
    conditions = []
    params = []

    if true_chain_only:
        conditions.append("true_chain IS TRUE")
    if min_chain_links:
        conditions.append("chain_links >= %s")
        params.append(int(min_chain_links))
    if min_recording_days:
        conditions.append("n_recording_days >= %s")
        params.append(int(min_recording_days))

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(int(limit))

    return run_query(
        f"""
        SELECT
            signal_id, bbl, n_recording_days, span_days, n_priced,
            n_nominal, chain_links, possible_links, true_chain,
            first_date, last_date
        FROM signal_repeated_transfers
        {where}
        ORDER BY true_chain DESC NULLS LAST,
                chain_links DESC NULLS LAST,
                n_recording_days DESC NULLS LAST
        LIMIT %s
        """,
        tuple(params),
    )


def fetch_cema_proxies(proxy_only=True, source=None, limit=1000):
    conditions = []
    params = []

    if proxy_only:
        # cema_proxy is categorical (none/medium/strong), not boolean.
        conditions.append("cema_proxy IS NOT NULL")
        conditions.append("cema_proxy <> 'none'")

    if source:
        conditions.append("source = %s")
        params.append(source)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.append(int(limit))

    return run_query(
        f"""
        SELECT signal_id, bbl, mtge_doc_id, source, companion_types, cema_proxy
        FROM signal_cema_proxy
        {where}
        ORDER BY
            CASE cema_proxy
                WHEN 'strong' THEN 1
                WHEN 'medium' THEN 2
                WHEN 'none'   THEN 3
                ELSE 4
            END,
            bbl,
            mtge_doc_id
        LIMIT %s
        """,
        tuple(params),
    )


def fetch_adjusted_valuation_residuals(min_abs_z=0.0, limit=1000):
    return run_query(
        """
        SELECT residual_id, bbl, document_id, price, expected_price, z, pct, z_adjusted
        FROM valuation_residuals_adjusted
        WHERE ABS(COALESCE(z_adjusted, 0)) >= %s
        ORDER BY ABS(z_adjusted) DESC NULLS LAST
        LIMIT %s
        """,
        (float(min_abs_z), int(limit)),
    )


def fetch_transfer_signal_counts():
    return run_query(
        """
        SELECT
            (SELECT COUNT(*) FROM signal_nominal_chains) AS nominal_chains,
            (SELECT COUNT(*) FROM signal_repeated_transfers) AS repeated_transfers,
            (SELECT COUNT(*) FROM signal_cema_proxy) AS cema_proxies,
            (SELECT COUNT(*) FROM valuation_residuals_adjusted) AS adjusted_residuals
        """
    )


def build_case_network_html(case_id, props_df, edges_df, wider_entities=None):
    net = Network(height="600px", width="100%", directed=True, cdn_resources="in_line")
    net.barnes_hut(
        gravity=-8000,
        central_gravity=0.3,
        spring_length=200,
        spring_strength=0.05,
        damping=0.09,
        overlap=0.5,
    )
    net.set_options("""
{
  "interaction": {
    "zoomView": true,
    "dragView": true,
    "hover": true
  },
  "physics": {
    "barnesHut": {
      "gravitationalConstant": -8000,
      "centralGravity": 0.3,
      "springLength": 200,
      "springConstant": 0.05,
      "damping": 0.09
    },
    "stabilization": {
      "enabled": true,
      "iterations": 500,
      "updateInterval": 25
    }
  }
}
""")

    # Freeze after the initial layout settles — prevents the runaway zoom.

    addr_by_bbl = {
        row["bbl"]: f"{row.get('street_number', '') or ''} {row.get('street_name', '') or ''}".strip() or row["bbl"]
        for _, row in props_df.iterrows()
    }

    added_nodes = set()

    def add_property_node(bbl, context=False):
        if bbl in added_nodes:
            return
        added_nodes.add(bbl)
        label = addr_by_bbl.get(bbl, bbl)
        net.add_node(
            bbl, label=label, shape="box",
            color="#D9D9D9" if context else "#4C72B0",
            title=f"BBL {bbl}" + (" (outside this case)" if context else " (this case)"),
        )

    def add_entity_node(entity_id, canonical_name, entity_kind, is_connecting):
        node_id = f"e{entity_id}"
        if node_id in added_nodes:
            return node_id
        added_nodes.add(node_id)
        net.add_node(
            node_id, label=canonical_name, shape="ellipse",
            color="#DD8452" if is_connecting else "#8C8C8C",
            title=f"{canonical_name} ({entity_kind}) -- "
                f"{'connecting' if is_connecting else 'evidence-only'}",
        )
        return node_id

    for bbl in props_df["bbl"]:
        add_property_node(bbl, context=False)

    for _, row in edges_df.iterrows():
        node_id = add_entity_node(row["entity_id"], row["canonical_name"], row["entity_kind"], row["is_connecting"])
        add_property_node(row["bbl"], context=False)
        style = RELATIONSHIP_STYLE.get(row["relationship_class"], RELATIONSHIP_STYLE["OTHER_GENERIC"])
        title = f"{row['role']} ({row['relationship_class']})"
        if row["role"] in IN_ROLES:
            net.add_edge(node_id, row["bbl"], color=style["color"], dashes=style["dashes"], title=title)
        elif row["role"] in OUT_ROLES:
            net.add_edge(row["bbl"], node_id, color=style["color"], dashes=style["dashes"], title=title)
        else:
            net.add_edge(node_id, row["bbl"], color=style["color"], dashes=style["dashes"], title=title, arrows="")

    if wider_entities:
        case_bbls = set(props_df["bbl"])
        for _, erow in edges_df[edges_df["entity_id"].isin(wider_entities)].drop_duplicates("entity_id").iterrows():
            wider_df = fetch_wider_entity_edges(erow["entity_id"], case_bbls)
            node_id = f"e{erow['entity_id']}"
            for _, wrow in wider_df.iterrows():
                add_property_node(wrow["bbl"], context=True)
                title = f"{wrow['role']} ({wrow['relationship_class']}) -- outside this case"
                if wrow["role"] in IN_ROLES:
                    net.add_edge(node_id, wrow["bbl"], color="#D9D9D9", dashes=True, title=title)
                elif wrow["role"] in OUT_ROLES:
                    net.add_edge(wrow["bbl"], node_id, color="#D9D9D9", dashes=True, title=title)

    return _fit_on_load(net.generate_html())


@st.cache_resource
def ensure_reviews_table():
    run_write(REVIEWS_DDL, None)
    return True


@st.cache_data(ttl=30)
def fetch_reviews():
    return run_query("""
        SELECT signal_type, bbl, doc_a, doc_b, status, closed_reason, note, reviewed_at
        FROM signal_reviews
    """)


def add_review_columns(df, signal_type, a_col, b_col):
    rv = fetch_reviews()
    rv = rv[rv["signal_type"] == signal_type][["bbl", "doc_a", "doc_b", "status", "closed_reason"]]
    rv = rv.rename(columns={"status": "review", "closed_reason": "reason"})
    out = df.assign(_a=df[a_col].astype(str), _b=df[b_col].astype(str))
    out = out.merge(rv, how="left", left_on=["bbl", "_a", "_b"], right_on=["bbl", "doc_a", "doc_b"])
    return out.drop(columns=["_a", "_b", "doc_a", "doc_b"])


def render_review_form(signal_type, bbl, doc_a, doc_b, key,
    statuses=None, reasons=None, reason_status="false_positive"):
    statuses = statuses or REVIEW_STATUSES
    reasons = reasons or CLOSED_REASONS
    rv = fetch_reviews()
    match = rv[
        (rv["signal_type"] == signal_type) & (rv["bbl"] == str(bbl))
        & (rv["doc_a"] == str(doc_a)) & (rv["doc_b"] == str(doc_b))
    ]
    existing = match.iloc[0] if not match.empty else None

    st.subheader(f"Review: {signal_type.replace('_', '-')} on BBL {bbl}")
    cur_status = existing["status"] if existing is not None else statuses[0]
    status = st.selectbox(
        "Status", options=statuses,
        index=statuses.index(cur_status) if cur_status in statuses else 0,
        key=f"rv_status_{key}",
    )
    cur_reason = existing["closed_reason"] if existing is not None else None
    reason_options = ["(choose a reason)"] + reasons
    reason = st.selectbox(
        f"Reason (required for {reason_status})", options=reason_options,
        index=reason_options.index(cur_reason) if cur_reason in reason_options else 0,
        disabled=(status != reason_status), key=f"rv_reason_{key}",
    )
    cur_note = existing["note"] if existing is not None and pd.notna(existing["note"]) else ""
    note = st.text_area("Note", value=cur_note, key=f"rv_note_{key}")

    st.caption(
        "Saved against this signal's document pair, so it survives case_builder.py / "
        "etl_load.py rebuilds. A flagged candidate is still a candidate, never confirmed fraud."
    )
    if st.button("Save review", key=f"rv_save_{key}"):
        if status == reason_status and reason not in reasons:
            st.error(f"Choose a reason before saving a {reason_status}.")
            return
        run_write(
            """
            INSERT INTO signal_reviews (signal_type, bbl, doc_a, doc_b, status, closed_reason, note, reviewed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (signal_type, bbl, doc_a, doc_b) DO UPDATE
            SET status = EXCLUDED.status, closed_reason = EXCLUDED.closed_reason,
                note = EXCLUDED.note, reviewed_at = now()
            """,
            (signal_type, str(bbl), str(doc_a), str(doc_b), status,
             reason if status == reason_status else None, note.strip() or None),
        )
        st.cache_data.clear()
        st.rerun()
        # ============================================================
# TRANSFER SIGNALS PAGE
# ============================================================

def render_transfer_signals():
    """
    Transfer-signal explorer for nominal chains, repeated transfers,
    CEMA proxies, and adjusted valuation residuals.
    """
    page_header(
        VIEW_META["Transfer signals"][0],
        "Transfer signals",
        VIEW_META["Transfer signals"][1],
    )

    st.caption(
        "These signals identify potentially unusual property-transfer or valuation "
        "patterns. They are investigative leads, not confirmed fraud. Inspect the "
        "underlying documents and transaction context before drawing conclusions."
    )

    counts_df = fetch_transfer_signal_counts()
    if not counts_df.empty:
        counts = counts_df.iloc[0]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Nominal chains", f"{int(counts['nominal_chains']):,}")
        m2.metric("Repeated transfers", f"{int(counts['repeated_transfers']):,}")
        m3.metric("CEMA records", f"{int(counts['cema_proxies']):,}")
        m4.metric("Adjusted residuals", f"{int(counts['adjusted_residuals']):,}")

    tab_chains, tab_repeated, tab_cema, tab_valuation = st.tabs(
        ["Nominal chains", "Repeated transfers", "CEMA proxies", "Adjusted valuation"]
    )

    with tab_chains:
        st.subheader("Nominal transfer chains")
        st.caption(
            "Inspect nominal deed amounts, grantor/grantee continuity, and possible "
            "follow-on transfers. Short gaps and shared names are leads, not proof."
        )

        chain_type_options = ["All"]
        continuity_options = ["All"]

        try:
            options_df = run_query(
                """
                SELECT DISTINCT chain_type, continuity
                FROM signal_nominal_chains
                ORDER BY chain_type, continuity
                """
            )
            if not options_df.empty:
                chain_type_options += [
                    v for v in options_df["chain_type"].dropna().tolist()
                    if v not in chain_type_options
                ]
                continuity_options += [
                    v for v in options_df["continuity"].dropna().tolist()
                    if v not in continuity_options
                ]
        except Exception as exc:
            st.error(f"Could not load nominal-chain filter options: {exc}")

        c1, c2, c3 = st.columns(3)
        with c1:
            chain_type = st.selectbox("Chain type", chain_type_options, key="transfer_chain_type")
        with c2:
            continuity = st.selectbox("Continuity", continuity_options, key="transfer_chain_continuity")
        with c3:
            entity_grantee_only = st.checkbox(
                "Entity-like grantee only", value=False, key="transfer_entity_grantee"
            )

        min_gap = st.number_input(
            "Minimum days between deeds", min_value=0, value=0, step=1,
            key="transfer_chain_min_gap",
        )
        limit = st.number_input(
            "Maximum rows", min_value=100, max_value=5000, value=1000, step=100,
            key="transfer_chain_limit",
        )

        conditions = []
        params = []
        if chain_type != "All":
            conditions.append("chain_type = %s")
            params.append(chain_type)
        if continuity != "All":
            conditions.append("continuity = %s")
            params.append(continuity)
        if entity_grantee_only:
            conditions.append("grantee_looks_like_entity IS TRUE")
        conditions.append("COALESCE(gap_days, 0) >= %s")
        params.append(int(min_gap))

        where_sql = "WHERE " + " AND ".join(conditions)

        try:
            chains = run_query(
                f"""
                SELECT
                    signal_id, bbl, deeda_doc_id, deeda_date, deeda_amt,
                    grantor_names, grantee_names, partial_interest_pct,
                    grantee_looks_like_entity, grantor_looks_like_individual,
                    shared_name_token, chain_type, followon_doc_id,
                    followon_date, followon_amt, gap_days, continuity
                FROM signal_nominal_chains
                {where_sql}
                ORDER BY gap_days ASC NULLS LAST, deeda_date DESC NULLS LAST
                LIMIT %s
                """,
                tuple(params + [int(limit)]),
            )

            if chains.empty:
                st.info("No nominal-chain signals match these filters.")
            else:
                for column in ("deeda_doc_id", "followon_doc_id"):
                    if column in chains.columns:
                        chains[column] = chains[column].map(acris_link)

                st.write(f"{len(chains):,} nominal-chain signals")
                st.dataframe(
                    chains, width="stretch", hide_index=True,
                    column_config={
                        "deeda_doc_id": doc_link_column("Deed A document"),
                        "followon_doc_id": doc_link_column("Follow-on document"),
                    },
                )
        except Exception as exc:
            st.error(f"Could not load nominal-chain signals: {exc}")

    with tab_repeated:
        st.subheader("Repeated transfers")
        st.caption(
            "Properties with multiple recording dates or linked transfer events. "
            "A possible chain is not necessarily an economically meaningful transfer."
        )

        c1, c2, c3 = st.columns(3)
        with c1:
            true_chain_only = st.checkbox(
                "Confirmed chain flag only", value=False, key="transfer_true_chain_only"
            )
        with c2:
            min_links = st.number_input(
                "Minimum chain links", min_value=0, value=0, step=1,
                key="transfer_min_links",
            )
        with c3:
            min_recording_days = st.number_input(
                "Minimum recording days", min_value=0, value=0, step=1,
                key="transfer_min_recording_days",
            )

        repeated_limit = st.number_input(
            "Maximum rows", min_value=100, max_value=5000, value=1000, step=100,
            key="transfer_repeated_limit",
        )

        conditions = []
        params = []
        if true_chain_only:
            conditions.append("true_chain IS TRUE")
        conditions.append("COALESCE(chain_links, 0) >= %s")
        params.append(int(min_links))
        conditions.append("COALESCE(n_recording_days, 0) >= %s")
        params.append(int(min_recording_days))
        where_sql = "WHERE " + " AND ".join(conditions)

        try:
            repeated = run_query(
                f"""
                SELECT
                    signal_id, bbl, n_recording_days, span_days, n_priced,
                    n_nominal, chain_links, possible_links, true_chain,
                    first_date, last_date
                FROM signal_repeated_transfers
                {where_sql}
                ORDER BY true_chain DESC NULLS LAST,
                         chain_links DESC NULLS LAST,
                         n_recording_days DESC NULLS LAST
                LIMIT %s
                """,
                tuple(params + [int(repeated_limit)]),
            )

            if repeated.empty:
                st.info("No repeated-transfer signals match these filters.")
            else:
                st.write(f"{len(repeated):,} repeated-transfer signals")
                st.dataframe(repeated, width="stretch", hide_index=True)
        except Exception as exc:
            st.error(f"Could not load repeated-transfer signals: {exc}")

    with tab_cema:
        st.subheader("CEMA proxies")
        st.caption(
            "Mortgage records flagged as possible CEMA-related activity. "
            "These are proxies, not definitive identification of a CEMA transaction."
        )

        c1, c2 = st.columns(2)
        with c1:
            proxy_only = st.checkbox(
                "Show CEMA proxies only", value=True, key="transfer_cema_proxy_only"
            )
        with c2:
            cema_limit = st.number_input(
                "Maximum rows", min_value=100, max_value=5000, value=1000, step=100,
                key="transfer_cema_limit",
            )

        try:
            source_df = run_query(
                """
                SELECT DISTINCT source
                FROM signal_cema_proxy
                WHERE source IS NOT NULL
                ORDER BY source
                """
            )
            sources = ["All"]
            if not source_df.empty:
                sources += source_df["source"].dropna().tolist()

            selected_source = st.selectbox("Signal source", sources, key="transfer_cema_source")

            conditions = []
            params = []
            if proxy_only:
                conditions.append("cema_proxy IS NOT NULL")
                conditions.append("cema_proxy <> 'none'")
            if selected_source != "All":
                conditions.append("source = %s")
                params.append(selected_source)
            where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

            cema = run_query(
                f"""
                SELECT signal_id, bbl, mtge_doc_id, source, companion_types, cema_proxy
                FROM signal_cema_proxy
                {where_sql}
                ORDER BY CASE cema_proxy WHEN 'strong' THEN 1 WHEN 'medium' THEN 2 WHEN 'none'   THEN 3 ELSE 4 END, bbl
                LIMIT %s
                """,
                tuple(params + [int(cema_limit)]),
            )

            if cema.empty:
                st.info("No CEMA-proxy signals match these filters.")
            else:
                cema["mtge_doc_id"] = cema["mtge_doc_id"].map(acris_link)
                st.write(f"{len(cema):,} CEMA-proxy signals")
                st.dataframe(
                    cema, width="stretch", hide_index=True,
                    column_config={"mtge_doc_id": doc_link_column("Mortgage document")},
                )
        except Exception as exc:
            st.error(f"Could not load CEMA-proxy signals: {exc}")

    with tab_valuation:
        st.subheader("Adjusted valuation residuals")
        st.caption(
            "Differences between observed and expected prices after adjustment. "
            "Large residuals are investigation priorities, not proof of mispricing."
        )

        c1, c2 = st.columns(2)
        with c1:
            min_abs_z = st.number_input(
                "Minimum absolute adjusted z-score",
                min_value=0.0, value=2.0, step=0.5,
                key="transfer_min_abs_adjusted_z",
            )
        with c2:
            valuation_limit = st.number_input(
                "Maximum rows", min_value=100, max_value=5000, value=1000, step=100,
                key="transfer_valuation_limit",
            )

        try:
            residuals = run_query(
                """
                SELECT residual_id, bbl, document_id, price, expected_price,
                       z, pct, z_adjusted
                FROM valuation_residuals_adjusted
                WHERE ABS(COALESCE(z_adjusted, 0)) >= %s
                ORDER BY ABS(z_adjusted) DESC NULLS LAST
                LIMIT %s
                """,
                (float(min_abs_z), int(valuation_limit)),
            )

            if residuals.empty:
                st.info("No adjusted valuation residuals match this threshold.")
            else:
                residuals["document_id"] = residuals["document_id"].map(acris_link)
                st.write(f"{len(residuals):,} adjusted valuation residuals")
                st.dataframe(
                    residuals, width="stretch", hide_index=True,
                    column_config={"document_id": doc_link_column("Document")},
                )
        except Exception as exc:
            st.error(f"Could not load adjusted valuation residuals: {exc}")


# ============================================================
# SIGNAL EXPLORER
# ============================================================

def fetch_all_over_mortgaging():
    return run_query("""
        SELECT so.signal_id, so.bbl, so.category, so.unit_or_type_mismatch,
        so.deed_amt, so.deed_document_id, so.mtge_amt, so.mtge_document_id,
        so.days_between, so.mortgage_to_price_ratio,
        p.street_number, p.street_name, p.latest_unit,
        cs.case_id, c.score AS case_score
        FROM signal_over_mortgaging so
        LEFT JOIN properties p ON p.bbl = so.bbl
        LEFT JOIN case_signals cs
            ON cs.signal_id = so.signal_id AND cs.signal_type = 'over_mortgaging'
        LEFT JOIN cases c ON c.case_id = cs.case_id
    """)


def fetch_all_flips():
    return run_query("""
        SELECT sf.signal_id, sf.bbl, sf.category, sf.unit_or_type_mismatch,
        sf.sale1_amt, sf.sale1_document_id, sf.sale2_amt, sf.sale2_document_id,
        sf.days_between, sf.pct_price_increase,
        p.street_number, p.street_name, p.latest_unit,
        cs.case_id, c.score AS case_score
        FROM signal_flips sf
        LEFT JOIN properties p ON p.bbl = sf.bbl
        LEFT JOIN case_signals cs
        ON cs.signal_id = sf.signal_id AND cs.signal_type = 'flip'
        LEFT JOIN cases c ON c.case_id = cs.case_id
    """)


def _address(df):
    return (
        df["street_number"].fillna("").astype(str) + " " + df["street_name"].fillna("")
    ).str.strip()


def render_signal_explorer():
    """
    Signal-first view: individual signals ranked by magnitude,
    independent of case score.
    """
    page_header(
        VIEW_META["Signal explorer"][0],
        "Signal explorer",
        VIEW_META["Signal explorer"][1],
    )

    st.caption(
        "Individual signals ranked by magnitude, independent of case score. "
        "Every row is a candidate flag consistent with a known typology, "
        "not confirmed fraud. Reviewed rows have included false positives "
        "such as CEMA-via-MTGE, tax-exempt affordable housing, and "
        "self-transfers. The deed amounts used in price ratios and changes are "
        "recorded ACRIS amounts; they have not been independently verified as "
        "sale consideration. Open the document links before drawing conclusions."
    )

    signal_type = st.sidebar.radio(
        "Signal type",
        ["Over-mortgaging", "Rapid flips"],
        key="signal_explorer_type",
    )

    hide_mismatch = st.sidebar.checkbox(
        "Hide unit/type mismatch ('verify') rows",
        value=False,
        key="signal_explorer_hide_mismatch",
    )

    if signal_type == "Over-mortgaging":
        df = fetch_all_over_mortgaging()

        if df.empty:
            st.warning("No over-mortgaging signals found.")
            return

        df = df.assign(address=_address(df))
        df = add_review_columns(df, "over_mortgaging", "deed_document_id", "mtge_document_id")

        mag_col = "mortgage_to_price_ratio"
        max_mag = float(df[mag_col].max())

        min_mag = st.sidebar.number_input(
            "Minimum mortgage-to-price ratio",
            min_value=0.0, value=1.5, step=0.5,
            key="signal_explorer_min_ratio",
        )

        hide_ceiling = st.sidebar.checkbox(
            f"Hide ratios above {SANITY_CEILING_RATIO:.0f}x (sanity-ceiling trips)",
            value=False,
            key="signal_explorer_hide_ceiling",
        )

        max_days = st.sidebar.number_input(
            "Max days between deed and mortgage",
            min_value=0, value=14, step=1,
            key="signal_explorer_max_days",
        )

    else:
        df = fetch_all_flips()

        if df.empty:
            st.warning("No rapid-flip signals found.")
            return

        df = df.assign(address=_address(df))
        df = add_review_columns(df, "flip", "sale1_document_id", "sale2_document_id")

        mag_col = "pct_price_increase"
        max_mag = float(df[mag_col].max())

        min_mag = st.sidebar.number_input(
            "Minimum recorded deed-amount increase (%)",
            min_value=0.0, value=0.0, step=10.0,
            key="signal_explorer_min_increase",
        )

        hide_ceiling = False

        max_days = st.sidebar.number_input(
            "Max days between sales",
            min_value=0, value=180, step=1,
            key="signal_explorer_flip_max_days",
        )

    categories = sorted(df["category"].dropna().unique().tolist())
    cat_default = [c for c in categories if c == "candidate"] or categories

    cat_filter = st.sidebar.multiselect(
        "Category",
        options=categories,
        default=cat_default,
        key="signal_explorer_categories",
    )

    hide_fp = st.sidebar.checkbox(
        "Hide signals already marked false_positive",
        value=False,
        help=(
            "Dismissed rows remain visible by default in case a later "
            "pipeline change causes the same pair to resurface."
        ),
        key="signal_explorer_hide_fp",
    )

    mask = (df[mag_col] >= min_mag) & (df["days_between"] <= max_days)
    mask &= df["category"].isin(cat_filter) | df["category"].isna()

    if hide_mismatch:
        mask &= ~df["unit_or_type_mismatch"].fillna(False).astype(bool)

    if hide_fp:
        mask &= df["review"].fillna("") != "false_positive"

    if hide_ceiling and signal_type == "Over-mortgaging":
        mask &= df[mag_col] <= SANITY_CEILING_RATIO

    out = df[mask].sort_values(mag_col, ascending=False)

    st.write(
        f"{len(out)} of {len(df)} signals match "
        f"(maximum {mag_col}: {max_mag:,.2f}). "
        f"Showing up to {min(len(out), SIGNAL_EXPLORER_ROW_CAP)}."
    )

    out = out.head(SIGNAL_EXPLORER_ROW_CAP).reset_index(drop=True)

    out = out.assign(
        mismatch=out["unit_or_type_mismatch"].map({True: "verify", False: ""}).fillna(""),
        review=out["review"].fillna("unreviewed"),
        reason=out["reason"].fillna(""),
    )

    if signal_type == "Over-mortgaging":
        out = out.assign(
            deed_doc=out["deed_document_id"].map(acris_link),
            mtge_doc=out["mtge_document_id"].map(acris_link),
        )
        cols = [
            "bbl", "address", "latest_unit", "mortgage_to_price_ratio",
            "deed_amt", "mtge_amt", "days_between", "category",
            "mismatch", "review", "reason", "case_id", "case_score",
            "deed_doc", "mtge_doc",
        ]
        col_cfg = {
            "deed_doc": doc_link_column("Deed doc"),
            "mtge_doc": doc_link_column("Mortgage doc"),
        }
        stype = "over_mortgaging"
        a_col, b_col = "deed_document_id", "mtge_document_id"
    else:
        out = out.assign(
            sale1_doc=out["sale1_document_id"].map(acris_link),
            sale2_doc=out["sale2_document_id"].map(acris_link),
        )
        cols = [
            "bbl", "address", "latest_unit", "pct_price_increase",
            "sale1_amt", "sale2_amt", "days_between", "category",
            "mismatch", "review", "reason", "case_id", "case_score",
            "sale1_doc", "sale2_doc",
        ]
        col_cfg = {
            "sale1_doc": doc_link_column("Sale 1 doc"),
            "sale2_doc": doc_link_column("Sale 2 doc"),
        }
        stype = "flip"
        a_col, b_col = "sale1_document_id", "sale2_document_id"

    event = st.dataframe(
        with_badges(out[cols]),
        width="stretch",
        hide_index=True,
        column_config=col_cfg,
        on_select="rerun",
        selection_mode="single-row",
        key=f"explorer_{stype}",
    )

    st.caption(
        "case_id is not stable across case_builder.py rebuilds. "
        "Note findings by BBL. A signal without a case_id/case_score "
        "did not land in any case. Select a row to record a review."
    )

    selected = event.selection.rows

    if selected and selected[0] < len(out):
        r = out.iloc[selected[0]]
        st.divider()
        render_review_form(
            stype, r["bbl"], r[a_col], r[b_col],
            key=f"explorer_{stype}_{r['bbl']}_{r[a_col]}_{r[b_col]}",
        )


# ============================================================
# EXCLUSION AUDIT
# ============================================================

def _name_sim(a, b):
    a, b = a.strip().upper(), b.strip().upper()
    if not a or not b:
        return 0.0
    ta, tb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    return max(SequenceMatcher(None, a, b).ratio(), SequenceMatcher(None, ta, tb).ratio())


def _best_similarity(buyers, doc_names):
    bs = [x for x in str(buyers).split(" | ") if x and x != "nan"]
    ds = [x for x in str(doc_names).split(" | ") if x and x != "nan"]
    if not bs or not ds:
        return 0.0
    return max(_name_sim(b, d) for b in bs for d in ds)


@st.cache_data
def load_exclusions(path_str, mtime):
    df = pd.read_csv(path_str, dtype={"bbl": str, "deed_doc_id": str, "mtge_doc_id": str})
    df["name_similarity"] = [
        _best_similarity(b, d) for b, d in zip(df["deed_buyer_names"], df["mtge_doc_names"])
    ]
    return df


def render_exclusion_audit():
    page_header(VIEW_META["Exclusion audit"][0], "Exclusion audit", VIEW_META["Exclusion audit"][1])
    if not EXCLUSION_CSV.exists():
        st.warning(
            f"{EXCLUSION_CSV.name} not found. Apply the exclusion-log patch to signals.py and "
            "re-run it (python signals.py from the scripts folder) to produce it."
        )
        return
    mtime = EXCLUSION_CSV.stat().st_mtime
    df = load_exclusions(str(EXCLUSION_CSV), mtime)
    if df.empty:
        st.info("The exclusion log is empty -- no party-overlap exclusions were recorded.")
        return
    df = add_review_columns(df, "party_overlap_exclusion", "deed_doc_id", "mtge_doc_id")
    df = df.assign(review=df["review"].fillna("unreviewed"), reason=df["reason"].fillna(""))

    st.caption(
        f"Log from the last signals.py run ({pd.Timestamp(mtime, unit='s'):%Y-%m-%d %H:%M} UTC). "
        "Each row is one (deed, excluded financing document) pair. Rows below the "
        f"{OVER_MORTGAGE_RATIO}x ratio could not have been flagged either way, so they don't "
        "matter for false exclusions. Exclusion requires an exact-name mismatch, so a "
        "similarity of 1.0 means the names match once word order is ignored -- the strongest "
        "false-exclusion lead. Scores in the 0.85-0.99 range are spelling/suffix variants."
    )

    matters = df[df["mortgage_to_price_ratio"] >= OVER_MORTGAGE_RATIO]
    reviewed = matters[matters["review"] != "unreviewed"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Exclusions logged", f"{len(df):,}")
    c2.metric("Would have crossed threshold", f"{len(matters):,}")
    c3.metric("Of those, reviewed", f"{len(reviewed):,}")
    c4.metric("Wrongly excluded", int((matters["review"] == "wrongly_excluded").sum()))

    min_ratio = st.sidebar.number_input(
        "Minimum mortgage-to-price ratio", min_value=0.0, value=OVER_MORTGAGE_RATIO, step=0.05,
        help="Defaults to signals.py's OVER_MORTGAGE_RATIO.",
    )
    min_sim = st.sidebar.slider("Minimum name similarity", 0.0, 1.0, 0.0, 0.05)
    only_unreviewed = st.sidebar.checkbox("Only unreviewed", value=False)

    out = df[(df["mortgage_to_price_ratio"] >= min_ratio) & (df["name_similarity"] >= min_sim)]
    if only_unreviewed:
        out = out[out["review"] == "unreviewed"]
    out = out.sort_values(["name_similarity", "mortgage_to_price_ratio"], ascending=False)
    st.write(f"{len(out)} of {len(df)} logged exclusions match. "
            f"Showing the top {min(len(out), SIGNAL_EXPLORER_ROW_CAP)}, most similar names first.")
    out = out.head(SIGNAL_EXPLORER_ROW_CAP).reset_index(drop=True)
    out = out.assign(
        deed_doc=out["deed_doc_id"].map(acris_link),
        mtge_doc=out["mtge_doc_id"].map(acris_link),
        name_similarity=out["name_similarity"].round(3),
    )
    cols = ["bbl", "name_similarity", "deed_buyer_names", "mtge_doc_names",
            "mortgage_to_price_ratio", "deed_amt", "mtge_amt", "days_between",
            "review", "reason", "deed_doc", "mtge_doc"]
    event = st.dataframe(
        with_badges(out[cols]), width="stretch", hide_index=True,
        column_config={"deed_doc": doc_link_column("Deed doc"), "mtge_doc": doc_link_column("Mortgage doc")},
        on_select="rerun", selection_mode="single-row", key="exclusion_audit",
    )
    st.caption(
        "Select a row to record a verdict. In ACRIS, compare the deed's grantee with the "
        "mortgage's mortgagor: same party under a different spelling = wrongly_excluded."
    )
    selected = event.selection.rows
    if selected and selected[0] < len(out):
        r = out.iloc[selected[0]]
        st.divider()
        render_review_form(
            "party_overlap_exclusion", r["bbl"], r["deed_doc_id"], r["mtge_doc_id"],
            key=f"audit_{r['bbl']}_{r['deed_doc_id']}_{r['mtge_doc_id']}",
            statuses=AUDIT_STATUSES, reasons=AUDIT_REASONS, reason_status="wrongly_excluded",
        )


# ============================================================
# SERIAL REFINANCING
# ============================================================

def prep_serial_refi(df):
    df = df.copy()
    df["same_doc"] = df["mtge1_doc_id"] == df["mtge2_doc_id"]
    df["pairs_on_bbl"] = df.groupby("bbl")["bbl"].transform("size")
    sig = (
        df["mtge1_amt"].astype(str) + "|" + df["mtge2_amt"].astype(str) + "|"
        + df["mtge1_date"].astype(str) + "|" + df["mtge2_date"].astype(str)
    )
    df["bbls_same_signature"] = df.groupby(sig)["bbl"].transform("nunique")
    df["same_amount"] = df["amt_increase"] == 0
    return df


@st.cache_data
def load_serial_refi(path_str, mtime):
    df = pd.read_csv(path_str, dtype={"bbl": str, "mtge1_doc_id": str, "mtge2_doc_id": str})
    return prep_serial_refi(df)


def render_serial_refi():
    page_header(VIEW_META["Serial refinancing"][0], "Serial refinancing", VIEW_META["Serial refinancing"][1])
    if not SERIAL_REFI_CSV.exists():
        st.warning(f"{SERIAL_REFI_CSV.name} not found. Run signals.py (from the scripts folder) first.")
        return
    mtime = SERIAL_REFI_CSV.stat().st_mtime
    df = load_serial_refi(str(SERIAL_REFI_CSV), mtime)
    if df.empty:
        st.info("No serial-refinancing candidates in the last signals.py run.")
        return
    df = add_review_columns(df, "serial_refinancing", "mtge1_doc_id", "mtge2_doc_id")
    df = df.assign(review=df["review"].fillna("unreviewed"), reason=df["reason"].fillna(""))

    st.caption(
        f"From the last signals.py run ({pd.Timestamp(mtime, unit='s'):%Y-%m-%d %H:%M} UTC). "
        "Ordinary refinancing has the same shape as this signal, so a row here is a candidate "
        "to open the documents for, not evidence of anything on its own."
    )

    n_same_doc = int(df["same_doc"].sum())
    n_blanket = int((df["bbls_same_signature"] >= 3).sum())
    if n_same_doc:
        st.warning(
            f"{n_same_doc} row(s) pair a document with itself. That is the signature of the "
            "duplicate-document bug fixed in signals.py v11, so this CSV likely came from an "
            "older version. Re-run the v11 signals.py before drawing conclusions about volume."
        )
    if n_blanket:
        st.info(
            f"{n_blanket} row(s) share identical amounts and dates with 2+ other BBLs -- the "
            "signature of one project loan recorded per unit (blanket financing). v11's guard "
            "should remove these; if it was applied they warrant a look."
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Candidates", f"{len(df):,}")
    c2.metric("Distinct properties", f"{df['bbl'].nunique():,}")
    c3.metric("Borrower changed", f"{int(df['borrower_changed'].sum()):,}")
    c4.metric("Median days between", f"{df['days_between'].median():.0f}")

    borrower = st.sidebar.selectbox("Borrower", ["All", "Changed", "Unchanged"])
    max_days = st.sidebar.slider("Max days between mortgages", 0, 365, 365)
    hide_dupes = st.sidebar.checkbox("Hide same-document rows", value=True)
    hide_blanket = st.sidebar.checkbox("Hide identical pairs on 3+ BBLs", value=False)
    hide_same_amt = st.sidebar.checkbox("Hide identical-amount pairs", value=False)
    hide_fp = st.sidebar.checkbox("Hide signals already marked false_positive", value=False)
    only_unreviewed = st.sidebar.checkbox("Only unreviewed", value=False)

    chart_data = df[["days_between"]].copy()
    chart_data["days_between"] = pd.to_numeric(chart_data["days_between"], errors="coerce")
    chart_data = chart_data.dropna()
    if chart_data.empty:
        st.info("No usable mortgage-date gaps are available to chart.")
    else:
        chart_data["interval_start"] = (chart_data["days_between"] // 7 * 7).astype(int)
        chart_data["interval"] = chart_data["interval_start"].map(
            lambda start: f"{start:,}–{start + 6:,}"
        )
        chart_bins = (
            chart_data.groupby(["interval_start", "interval"], as_index=False)
            .size()
            .rename(columns={"size": "candidate_pairs"})
            .sort_values("interval_start")
            [["interval", "candidate_pairs"]]
        )
        st.echarts_chart(
            {
                "title": {"text": "Time between the two mortgages in each candidate pair", "left": "center"},
                "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
                "grid": {"left": 70, "right": 24, "top": 58, "bottom": 86, "containLabel": True},
                "dataset": {"source": chart_bins},
                "xAxis": {
                    "type": "category",
                    "name": "Days between recorded mortgages (7-day intervals)",
                    "nameLocation": "middle",
                    "nameGap": 48,
                    "axisLabel": {"hideOverlap": True},
                },
                "yAxis": {
                    "type": "value",
                    "name": "Candidate pairs",
                    "nameLocation": "middle",
                    "nameGap": 52,
                    "minInterval": 1,
                },
                "dataZoom": [
                    {"type": "inside", "xAxisIndex": 0, "filterMode": "filter"},
                    {"type": "slider", "xAxisIndex": 0, "filterMode": "filter", "height": 18, "bottom": 12},
                ],
                "series": [{
                    "type": "bar",
                    "name": "Candidate pairs",
                    "encode": {"x": "interval", "y": "candidate_pairs", "tooltip": ["interval", "candidate_pairs"]},
                    "barMaxWidth": 28,
                    "itemStyle": {"color": "#4C78A8"},
                }],
            },
            width="stretch",
            height=380,
            alt="Distribution of days between mortgages among serial-refinancing candidate pairs",
        )
    st.caption(
        "Each bar counts candidate pairs in a 7-day interval. Drag the slider below the chart or use "
        "the mouse wheel over the plot to zoom; reset by dragging the slider handles back out. This "
        "summarizes the full signal file; filters below affect the table only. A short interval alone "
        "does not show that a refinancing is unusual or improper."
    )

    sort_by = st.sidebar.selectbox(
        "Sort by",
        ["Fewest days between", "Largest amount increase", "Largest second mortgage", "Most pairs on property"],
    )

    mask = df["days_between"] <= max_days
    if borrower == "Changed":
        mask &= df["borrower_changed"]
    elif borrower == "Unchanged":
        mask &= ~df["borrower_changed"]
    if hide_dupes:
        mask &= ~df["same_doc"]
    if hide_blanket:
        mask &= df["bbls_same_signature"] < 3
    if hide_same_amt:
        mask &= ~df["same_amount"]
    if hide_fp:
        mask &= df["review"] != "false_positive"
    if only_unreviewed:
        mask &= df["review"] == "unreviewed"

    sort_map = {
        "Fewest days between": ("days_between", True),
        "Largest amount increase": ("amt_increase", False),
        "Largest second mortgage": ("mtge2_amt", False),
        "Most pairs on property": ("pairs_on_bbl", False),
    }
    col, asc = sort_map[sort_by]
    out = df[mask].sort_values(col, ascending=asc)
    st.write(f"{len(out)} of {len(df)} candidates match. "
            f"Showing the top {min(len(out), SIGNAL_EXPLORER_ROW_CAP)}.")
    out = out.head(SIGNAL_EXPLORER_ROW_CAP).reset_index(drop=True)
    out = out.assign(
        borrower=out["borrower_changed"].map({True: "changed", False: ""}),
        mtge1_doc=out["mtge1_doc_id"].map(acris_link),
        mtge2_doc=out["mtge2_doc_id"].map(acris_link),
    )
    cols = ["bbl", "mtge1_date", "mtge1_amt", "mtge2_date", "mtge2_amt", "days_between",
            "amt_increase", "borrower", "pairs_on_bbl", "bbls_same_signature",
            "review", "reason", "mtge1_doc", "mtge2_doc"]
    event = st.dataframe(
        with_badges(out[cols]), width="stretch", hide_index=True,
        column_config={"mtge1_doc": doc_link_column("Mortgage 1"), "mtge2_doc": doc_link_column("Mortgage 2")},
        on_select="rerun", selection_mode="single-row", key="serial_refi",
    )
    st.caption(
        "Select a row to record a review. When opening the documents, compare the mortgage cover "
        "pages: taxable vs total amount and any exemption code distinguish a consolidation from "
        "fresh borrowing, and a changed borrower between the two is flagged, not excluded."
    )
    selected = event.selection.rows
    if selected and selected[0] < len(out):
        r = out.iloc[selected[0]]
        st.divider()
        render_review_form(
            "serial_refinancing", r["bbl"], r["mtge1_doc_id"], r["mtge2_doc_id"],
            key=f"refi_{r['bbl']}_{r['mtge1_doc_id']}_{r['mtge2_doc_id']}",
            statuses=REVIEW_STATUSES, reasons=SERIAL_REFI_REASONS, reason_status="false_positive",
        )

# ============================================================
# ENTITY EXPLORER
# ============================================================

ENTITY_LIST_CAP = 500
ENTITY_REL_CAP = 2000
ENTITY_SORTS = {
    "Most properties": "n_properties",
    "Most ownership-role properties": "n_connecting_props",
    "Most sold": "n_sold",
    "Most bought": "n_bought",
}


@st.cache_data(ttl=300)
def table_columns(table):
    return list(run_query(f"SELECT * FROM {table} LIMIT 0").columns)


@st.cache_data(ttl=300)
def fetch_entity_filter_options():
    df = run_query("SELECT DISTINCT entity_kind, resolution_status FROM entities")
    kinds = sorted(df["entity_kind"].fillna("(none)").unique().tolist())
    statuses = sorted(df["resolution_status"].fillna("(none)").unique().tolist())
    return kinds, statuses


@st.cache_data(ttl=60)
def search_entities(q, kinds, statuses, min_props, both_sides, sort_label):
    order = ENTITY_SORTS[sort_label]
    return run_query(f"""
        SELECT e.entity_id, e.canonical_name, e.entity_kind, e.resolution_status,
            COUNT(DISTINCT er.bbl) AS n_properties,
            COUNT(DISTINCT er.bbl) FILTER (WHERE er.is_connecting) AS n_connecting_props,
            COUNT(DISTINCT er.bbl) FILTER (WHERE er.role = ANY(%(out_roles)s)) AS n_sold,
            COUNT(DISTINCT er.bbl) FILTER (WHERE er.role = ANY(%(in_roles)s)) AS n_bought,
            COUNT(*) AS n_relationships
        FROM entities e
        JOIN entity_relationships er ON er.entity_id = e.entity_id
        WHERE (%(q)s = '' OR e.canonical_name ILIKE %(pat)s OR e.entity_id::text = %(q)s)
        AND COALESCE(e.entity_kind, '(none)') = ANY(%(kinds)s)
        AND COALESCE(e.resolution_status, '(none)') = ANY(%(statuses)s)
        GROUP BY e.entity_id, e.canonical_name, e.entity_kind, e.resolution_status
        HAVING COUNT(DISTINCT er.bbl) >= %(min_props)s
        AND (NOT %(both)s OR (
                COUNT(DISTINCT er.bbl) FILTER (WHERE er.role = ANY(%(out_roles)s)) > 0
            AND COUNT(DISTINCT er.bbl) FILTER (WHERE er.role = ANY(%(in_roles)s)) > 0))
        ORDER BY {order} DESC, e.canonical_name
        LIMIT %(cap)s
    """, {
        "q": q.strip(), "pat": f"%{q.strip()}%", "kinds": list(kinds), "statuses": list(statuses),
        "min_props": int(min_props), "both": bool(both_sides), "cap": ENTITY_LIST_CAP,
        "out_roles": sorted(OUT_ROLES), "in_roles": sorted(IN_ROLES),
    })


@st.cache_data(ttl=60)
def fetch_entity_relationships(entity_id):
    doc_sel = ", er.document_id" if "document_id" in table_columns("entity_relationships") else ""
    return run_query(f"""
        SELECT er.bbl, p.street_number, p.street_name, p.latest_unit, er.role,
            er.relationship_class, er.is_connecting{doc_sel}, cp.case_id
        FROM entity_relationships er
        LEFT JOIN properties p ON p.bbl = er.bbl
        LEFT JOIN case_properties cp ON cp.bbl = er.bbl
        WHERE er.entity_id = %(id)s
        ORDER BY er.is_connecting DESC, er.bbl
        LIMIT %(cap)s
    """, {"id": int(entity_id), "cap": ENTITY_REL_CAP})


@st.cache_data(ttl=60)
def fetch_entity_cases(entity_id):
    return run_query("""
        SELECT ce.case_id, c.title, c.score, c.status
        FROM case_entities ce JOIN cases c ON c.case_id = ce.case_id
        WHERE ce.entity_id = %(id)s
        ORDER BY c.score DESC
    """, {"id": int(entity_id)})


@st.cache_data(ttl=60)
def fetch_counterparty_edges(entity_id, bbls, ownership_only):
    return run_query("""
        SELECT er.entity_id, e.canonical_name, e.entity_kind, er.bbl, er.role,
            er.relationship_class, er.is_connecting
        FROM entity_relationships er
        JOIN entities e ON e.entity_id = er.entity_id
        WHERE er.bbl = ANY(%(bbls)s) AND er.entity_id <> %(id)s
        AND (NOT %(own)s OR er.is_connecting)
    """, {"bbls": list(bbls), "id": int(entity_id), "own": bool(ownership_only)})


@st.cache_data(ttl=60)
def fetch_entity_totals(entity_ids):
    if not entity_ids:
        return pd.DataFrame(columns=["entity_id", "total_properties"])
    return run_query("""
        SELECT entity_id, COUNT(DISTINCT bbl) AS total_properties
        FROM entity_relationships WHERE entity_id = ANY(%(ids)s) GROUP BY entity_id
    """, {"ids": [int(i) for i in entity_ids]})


@st.cache_data(ttl=60)
def fetch_entity_name_variants(entity_id):
    return run_query("""
        SELECT name_norm, COUNT(DISTINCT document_id) AS n_documents
        FROM document_parties WHERE entity_id = %(id)s
        GROUP BY name_norm ORDER BY n_documents DESC, name_norm
    """, {"id": int(entity_id)})


def summarize_counterparties(rels_df, cp_df, totals_df):
    if cp_df.empty:
        return pd.DataFrame()
    center_out = set(rels_df.loc[rels_df["role"].isin(OUT_ROLES), "bbl"])
    center_in = set(rels_df.loc[rels_df["role"].isin(IN_ROLES), "bbl"])
    cp = cp_df.assign(is_out=cp_df["role"].isin(OUT_ROLES), is_in=cp_df["role"].isin(IN_ROLES))

    g = cp.groupby("entity_id")
    out = g.agg(
        canonical_name=("canonical_name", "first"), entity_kind=("entity_kind", "first"),
        shared_properties=("bbl", "nunique"),
        ownership_links=("is_connecting", "sum"),
        classes=("relationship_class", lambda x: ", ".join(sorted(set(map(str, x))))),
    )
    out["n_sold_to"] = cp[cp["is_in"] & cp["bbl"].isin(center_out)].groupby("entity_id")["bbl"].nunique()
    out["n_bought_from"] = cp[cp["is_out"] & cp["bbl"].isin(center_in)].groupby("entity_id")["bbl"].nunique()
    out[["n_sold_to", "n_bought_from"]] = out[["n_sold_to", "n_bought_from"]].fillna(0).astype(int)
    out = out.reset_index().merge(totals_df, how="left", on="entity_id")
    out["share_of_their_footprint"] = (out["shared_properties"] / out["total_properties"]).round(3)
    return out.sort_values(["shared_properties", "share_of_their_footprint"], ascending=False)


def _fit_on_load(page_html):
    _FIT_SNIPPET = """<script>
(function () {
  var done = false;
  var timers = [];queu
  function fitOnce() {
    if (done) return;
    done = true;
    timers.forEach(clearTimeout);
    timers = [];
    try {
      network.stopSimulation();
      network.fit({ animation: { duration: 500, easingFunction: "easeInOutQuad" } });
    } catch (e) {}
  }
  function cancel() {
    done = true;
    timers.forEach(clearTimeout);
    timers = [];
  }
  try {
    network.once("stabilized", fitOnce);
    network.on("dragStart", cancel);
    network.on("zoom", cancel);
    network.on("click", cancel);
  } catch (e) {}
  timers.push(setTimeout(fitOnce, 700));
  timers.push(setTimeout(fitOnce, 1800));
})();
</script>"""
    return page_html.replace("</body>", _FIT_SNIPPET + "</body>", 1)


def build_entity_ego_html(center, rels_df, cp_df):
    net = Network(height="600px", width="100%", directed=True, cdn_resources="in_line")
    net.barnes_hut(
        gravity=-8000,
        central_gravity=0.3,
        spring_length=200,
        spring_strength=0.05,
        damping=0.09,
        overlap=0.5,
    )
    net.set_options("""
   {
    "interaction": {
    "zoomView": true,
    "dragView": true,
    "hover": true
   },
    "physics": {
     "barnesHut": {
       "gravitationalConstant": -8000,
       "centralGravity": 0.3,
       "springLength": 200,
       "springConstant": 0.05,
       "damping": 0.09
    },
    "stabilization": {
      "enabled": true,
      "iterations": 500,
      "updateInterval": 25
    }
  }
}
""")

    # Freeze after the initial layout settles — prevents the runaway zoom.
    center_node = f"e{center['entity_id']}"
    net.add_node(center_node, label=str(center["canonical_name"]), shape="ellipse", color="#C44E52",
                size=30, title=f"{center['canonical_name']} ({center['entity_kind']}) -- selected entity")
    added = {center_node}

    addr = {}
    for _, r in rels_df.drop_duplicates("bbl").iterrows():
        label = f"{r['street_number'] if pd.notna(r['street_number']) else ''} " \
                f"{r['street_name'] if pd.notna(r['street_name']) else ''}".strip()
        addr[r["bbl"]] = label or r["bbl"]

    def add_prop(bbl):
        if bbl not in added:
            added.add(bbl)
            net.add_node(bbl, label=addr.get(bbl, bbl), shape="box", color="#4C72B0", title=f"BBL {bbl}")

    def add_edge(ent_node, bbl, role, rclass):
        style = RELATIONSHIP_STYLE.get(rclass, RELATIONSHIP_STYLE["OTHER_GENERIC"])
        kw = {"color": style["color"], "dashes": style["dashes"], "title": f"{role} ({rclass})"}
        if role in IN_ROLES:
            net.add_edge(ent_node, bbl, **kw)
        elif role in OUT_ROLES:
            net.add_edge(bbl, ent_node, **kw)
        else:
            net.add_edge(ent_node, bbl, arrows="", **kw)

    for _, r in rels_df.iterrows():
        add_prop(r["bbl"])
        add_edge(center_node, r["bbl"], r["role"], r["relationship_class"])
    for _, r in cp_df.iterrows():
        node = f"e{r['entity_id']}"
        if node not in added:
            added.add(node)
            net.add_node(node, label=str(r["canonical_name"]), shape="ellipse",
                        color="#DD8452" if r["is_connecting"] else "#8C8C8C",
                        title=f"{r['canonical_name']} ({r['entity_kind']})")
        add_prop(r["bbl"])
        add_edge(node, r["bbl"], r["role"], r["relationship_class"])
    return _fit_on_load(net.generate_html())


def render_entity_explorer():
    page_header(VIEW_META["Entity explorer"][0], "Entity explorer", VIEW_META["Entity explorer"][1])
    st.caption(
        "Entities are clusters produced by entity_resolution.py, not verified identities. "
        "Relationships here are descriptive; a prolific entity is as likely to be a legitimate "
        "investor or a lender as anything of interest."
    )
    kinds_all, statuses_all = fetch_entity_filter_options()

    q = st.sidebar.text_input("Name contains (or entity_id)", value="")
    kinds = st.sidebar.multiselect("Entity kind", kinds_all, default=kinds_all)
    statuses = st.sidebar.multiselect("Resolution status", statuses_all, default=statuses_all)
    min_props = st.sidebar.number_input("Minimum properties", min_value=1, value=3, step=1)
    both_sides = st.sidebar.checkbox("Both bought and sold properties", value=False)
    sort_label = st.sidebar.selectbox("Sort by", list(ENTITY_SORTS))

    results = search_entities(q, tuple(kinds), tuple(statuses), min_props, both_sides, sort_label)
    if results.empty:
        st.info("No entities match these filters.")
        return
    st.write(f"{len(results)} entities shown (capped at {ENTITY_LIST_CAP}; narrow the filters to see others).")
    event = st.dataframe(
        results, width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="entity_results",
    )
    selected = event.selection.rows
    if not selected or selected[0] >= len(results):
        st.caption("Select an entity to explore it.")
        return

    center = results.iloc[selected[0]]
    entity_id = int(center["entity_id"])
    st.divider()
    st.subheader(f"{center['canonical_name']}  (entity {entity_id})")
    st.write(
        f"{center['entity_kind']} - status: {center['resolution_status']} - "
        f"{int(center['n_properties'])} properties ({int(center['n_connecting_props'])} with an "
        f"ownership role; {int(center['n_sold'])} sold, {int(center['n_bought'])} bought) - "
        f"{int(center['n_relationships'])} relationships"
    )

    rels = fetch_entity_relationships(entity_id)
    if int(center["n_relationships"]) > len(rels):
        st.caption(f"Showing the first {len(rels)} of {int(center['n_relationships'])} relationships "
                "(ownership-role first).")

    max_props = st.sidebar.slider("Properties to expand (counterparties / network)", 5, 200, 50)
    own_only = st.sidebar.checkbox("Ownership-role counterparties only", value=True)
    expand_bbls = list(dict.fromkeys(rels["bbl"]))[:max_props]
    rels_x = rels[rels["bbl"].isin(expand_bbls)]
    cp_edges = fetch_counterparty_edges(entity_id, tuple(expand_bbls), own_only)

    tab_props, tab_cp, tab_net, tab_names, tab_cases = st.tabs(
        ["Properties", "Counterparties", "Network", "Name variants", "Cases"]
    )

    with tab_props:
        view = rels.assign(address=(
            rels["street_number"].fillna("").astype(str) + " " + rels["street_name"].fillna("")
        ).str.strip())
        cols = ["bbl", "address", "latest_unit", "role", "relationship_class", "is_connecting", "case_id"]
        cfg = {}
        if "document_id" in view.columns:
            view = view.assign(doc=view["document_id"].map(acris_link))
            cols.append("doc")
            cfg["doc"] = doc_link_column("Document")
        st.dataframe(view[cols], width="stretch", hide_index=True, column_config=cfg)
        st.caption("case_id is not stable across case_builder.py rebuilds -- note findings by BBL.")

    with tab_cp:
        ids = cp_edges["entity_id"].unique().tolist()
        totals = fetch_entity_totals(tuple(ids))
        summary = summarize_counterparties(rels_x, cp_edges, totals)
        if summary.empty:
            st.info("No other entities share these properties" +
                    (" with an ownership role." if own_only else "."))
        else:
            st.caption(
                f"Counterparties on this entity's first {len(expand_bbls)} properties. "
                "A high shared count with a small share_of_their_footprint is a hub (lender or "
                "servicer), not an association. n_sold_to / n_bought_from show opposite roles on "
                "the same property; without dates that cannot confirm a direct transfer."
            )
            st.dataframe(summary.drop(columns=["entity_id"]).head(200),
                        width="stretch", hide_index=True)

    with tab_net:
        if cp_edges.empty and rels_x.empty:
            st.info("Nothing to draw.")
        else:
            graph_legend("entity")
            st.caption(f"Showing the first {len(expand_bbls)} properties and the other entities on them. "
                    "Counterparties are never expanded outward.")
            st.iframe(build_entity_ego_html(center, rels_x, cp_edges), height=620)

    with tab_names:
        if "name_norm" not in table_columns("document_parties"):
            st.info("document_parties has no name_norm column, so merged name variants can't be listed.")
        else:
            names = fetch_entity_name_variants(entity_id)
            st.caption(
                f"{len(names)} distinct normalized name(s) merged into this entity. Several unrelated "
                "names, or sequentially-numbered LLCs, mean the cluster deserves doubt."
            )
            st.dataframe(names, width="stretch", hide_index=True)

    with tab_cases:
        cases = fetch_entity_cases(entity_id)
        if cases.empty:
            st.info("This entity is not linked to any case.")
        else:
            st.dataframe(cases, width="stretch", hide_index=True)
            st.caption("case_id is not stable across case_builder.py rebuilds.")


# ============================================================
# MAP VIEW
# ============================================================

PROPERTY_GEO_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "property_geo.csv"
MAP_POINT_CAP = 20000
REVIEW_PRECEDENCE = ["strong_candidate", "under_review", "unreviewed", "false_positive"]
REVIEW_COLORS = {
    "strong_candidate": [196, 78, 82], "under_review": [221, 132, 82],
    "unreviewed": [76, 114, 176], "false_positive": [150, 150, 150],
}
CASE_PALETTE = [[76, 114, 176], [221, 132, 82], [85, 168, 104], [196, 78, 82], [129, 114, 179],
                [147, 120, 96], [218, 139, 195], [140, 140, 140], [204, 185, 116], [100, 181, 205]]


def map_legend(layer, counts=None):
    if layer == "Cases":
        st.markdown(
            "<span style='color:#4C72B0'>●</span> Case properties "
            "&nbsp; <span style='color:#888'>━</span> Properties in the same case",
            unsafe_allow_html=True,
        )
        return

    labels = {
        "strong_candidate": "Strong candidate",
        "under_review": "Under review",
        "unreviewed": "Unreviewed",
        "false_positive": "False positive",
    }
    items = []
    for status in REVIEW_PRECEDENCE:
        rgb = REVIEW_COLORS[status]
        count = f" ({counts[status]:,})" if counts is not None and status in counts else ""
        items.append(
            f"<span style='color:rgb({rgb[0]},{rgb[1]},{rgb[2]})'>●</span> "
            f"{labels[status]}{count}"
        )
    st.markdown("&nbsp;&nbsp;".join(items), unsafe_allow_html=True)


@st.cache_data
def load_property_geo(path_str, mtime):
    return pd.read_csv(path_str, dtype={"bbl": str})


@st.cache_data(ttl=300)
def fetch_addresses(bbls):
    if not bbls:
        return pd.DataFrame(columns=["bbl", "street_number", "street_name"])
    return run_query(
        "SELECT bbl, street_number, street_name FROM properties WHERE bbl = ANY(%(b)s)",
        {"b": list(bbls)},
    )


@st.cache_data(ttl=60)
def fetch_case_property_points():
    return run_query("""
        SELECT cp.case_id, cp.bbl, c.score, c.title,
            (SELECT COUNT(*) FROM case_properties x WHERE x.case_id = cp.case_id) AS n_props
        FROM case_properties cp JOIN cases c ON c.case_id = cp.case_id
    """)


def aggregate_signals_by_bbl(df, mag_col):
    rank = {st_: i for i, st_ in enumerate(REVIEW_PRECEDENCE)}
    d = df.assign(_rank=df["review"].fillna("unreviewed").map(lambda x: rank.get(x, rank["unreviewed"])))
    g = d.groupby("bbl").agg(
        n_signals=("bbl", "size"), max_mag=(mag_col, "max"), _rank=("_rank", "min")
    ).reset_index()
    g["review"] = g["_rank"].map(lambda i: REVIEW_PRECEDENCE[i])
    return g.drop(columns="_rank")


def attach_geo(points, geo):
    out = points.merge(geo[["bbl", "lat", "lon", "precision"]], how="left", on="bbl")
    out["precision"] = out["precision"].fillna("unmapped")
    return out


def _fmt_mag(x):
    return "n/a" if pd.isna(x) else f"{x:,.2f}"


def split_for_map(points, addresses, mag_label):
    addr = {}
    for _, r in addresses.iterrows():
        label = f"{r['street_number'] if pd.notna(r['street_number']) else ''} " \
                f"{r['street_name'] if pd.notna(r['street_name']) else ''}".strip()
        addr[r["bbl"]] = html.escape(label)

    exact = points[points["precision"] == "lot"].copy()
    exact["tip"] = [
        f"<b>{r.bbl}</b> {addr.get(r.bbl, '')}<br/>exact lot<br/>{int(r.n_signals)} signal(s), "
        f"max {mag_label} {_fmt_mag(r.max_mag)}<br/>review: {r.review}"
        for r in exact.itertuples()
    ]
    exact["color"] = exact["review"].map(lambda x: REVIEW_COLORS[x] + [220])

    block = points[points["precision"] == "block"]
    rank = {st_: i for i, st_ in enumerate(REVIEW_PRECEDENCE)}
    if block.empty:
        return exact, pd.DataFrame(columns=["lat", "lon", "tip", "fill", "line", "radius", "n_properties"])
    block = block.assign(_rank=block["review"].map(rank))
    agg = block.groupby(["lat", "lon"]).agg(
        n_properties=("bbl", "nunique"), n_signals=("n_signals", "sum"),
        max_mag=("max_mag", "max"), _rank=("_rank", "min"),
        bbls=("bbl", lambda x: ", ".join(sorted(set(x))[:5]) + (" ..." if len(set(x)) > 5 else "")),
    ).reset_index()
    agg["review"] = agg["_rank"].map(lambda i: REVIEW_PRECEDENCE[i])
    agg["tip"] = [
        f"<b>Block-level (approximate)</b><br/>{r.n_properties} propert{'y' if r.n_properties == 1 else 'ies'} "
        f"on this block, {int(r.n_signals)} signal(s)<br/>max {mag_label} {_fmt_mag(r.max_mag)}<br/>"
        f"best review state: {r.review}<br/>{r.bbls}"
        for r in agg.itertuples()
    ]
    agg["line"] = agg["review"].map(lambda x: REVIEW_COLORS[x] + [230])
    agg["fill"] = agg["review"].map(lambda x: REVIEW_COLORS[x] + [45])
    agg["radius"] = agg["n_properties"].map(lambda n: 70 + 30 * math.sqrt(n))
    return exact, agg.drop(columns=["_rank"])


def build_case_lines(points):
    rows = []
    for case_id, grp in points[points["precision"].isin(["lot", "block"])].groupby("case_id"):
        pts = grp.drop_duplicates("bbl")[["lat", "lon", "color"]].values.tolist()
        for a, b in itertools.combinations(pts, 2):
            rows.append({"s_lat": a[0], "s_lon": a[1], "t_lat": b[0], "t_lon": b[1], "color": a[2][:3] + [150]})
    return pd.DataFrame(rows, columns=["s_lat", "s_lon", "t_lat", "t_lon", "color"])


def _initial_view(frames):
    pts = pd.concat([f[["lat", "lon"]] for f in frames if not f.empty], ignore_index=True)
    return pdk.ViewState(latitude=float(pts["lat"].mean()), longitude=float(pts["lon"].mean()), zoom=10)


def render_map_view():
    page_header(VIEW_META["Map view"][0], "Map view", VIEW_META["Map view"][1])
    if not PROPERTY_GEO_CSV.exists():
        st.warning(f"{PROPERTY_GEO_CSV.name} not found. Run pluto_geocode.py (from the scripts folder) first.")
        return
    mtime = PROPERTY_GEO_CSV.stat().st_mtime
    geo = load_property_geo(str(PROPERTY_GEO_CSV), mtime)

    layer = st.sidebar.radio("Layer", ["Over-mortgaging", "Rapid flips", "Serial refinancing", "Cases"])
    precision_mode = st.sidebar.radio(
        "Positions", ["Exact lot + block-level (approximate)", "Exact lot only"],
    )

    if layer == "Cases":
        _render_case_map(geo, precision_mode)
        return

    hide_fp = st.sidebar.checkbox("Hide signals already marked false_positive", value=False)
    if layer == "Over-mortgaging":
        df = fetch_all_over_mortgaging()
        df = df[df["category"] == "candidate"]
        df = add_review_columns(df, "over_mortgaging", "deed_document_id", "mtge_document_id")
        min_ratio = st.sidebar.number_input("Minimum mortgage-to-price ratio", 0.0, value=1.5, step=0.5)
        hide_mismatch = st.sidebar.checkbox("Hide unit/type mismatch ('verify') rows", value=False)
        df = df[df["mortgage_to_price_ratio"] >= min_ratio]
        if hide_mismatch:
            df = df[~df["unit_or_type_mismatch"].fillna(False).astype(bool)]
        mag_col, mag_label = "mortgage_to_price_ratio", "mortgage-to-price ratio"
    elif layer == "Rapid flips":
        df = fetch_all_flips()
        df = df[df["category"] == "candidate"]
        df = add_review_columns(df, "flip", "sale1_document_id", "sale2_document_id")
        min_pct = st.sidebar.number_input("Minimum recorded deed-amount increase (%)", 0.0, value=0.0, step=10.0)
        df = df[df["pct_price_increase"] >= min_pct]
        mag_col, mag_label = "pct_price_increase", "sale-price increase (%)"
    else:
        mt_serial = SERIAL_REFI_CSV.stat().st_mtime if SERIAL_REFI_CSV.exists() else None
        if mt_serial is None:
            st.warning(f"{SERIAL_REFI_CSV.name} not found. Run signals.py first.")
            return
        df = load_serial_refi(str(SERIAL_REFI_CSV), mt_serial)
        df = add_review_columns(df, "serial_refinancing", "mtge1_doc_id", "mtge2_doc_id")
        if st.sidebar.checkbox("Hide same-document rows (known artifact)", value=True):
            df = df[~df["same_doc"]]
        mag_col, mag_label = "amt_increase", "mortgage amount increase ($)"
    if hide_fp:
        df = df[df["review"].fillna("") != "false_positive"]
    if df.empty:
        st.info("No signals match these filters.")
        return

    by_bbl = aggregate_signals_by_bbl(df, mag_col)
    points = attach_geo(by_bbl, geo)
    _coverage_metrics(points)
    if precision_mode == "Exact lot only":
        points = points[points["precision"] == "lot"]
    points = points[points["precision"].isin(["lot", "block"])].head(MAP_POINT_CAP)
    if points.empty:
        st.info("No mapped properties for these filters.")
        return

    addresses = fetch_addresses(tuple(points["bbl"]))
    exact, block = split_for_map(points, addresses, mag_label)
    layers = []
    if not block.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=block, get_position="[lon, lat]", get_radius="radius",
            get_fill_color="fill", get_line_color="line", stroked=True, filled=True,
            line_width_min_pixels=2, radius_min_pixels=8, radius_max_pixels=40, pickable=True,
        ))
    if not exact.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=exact, get_position="[lon, lat]", get_radius=35,
            get_fill_color="color", get_line_color=[255, 255, 255], stroked=True,
            line_width_min_pixels=1, radius_min_pixels=4, radius_max_pixels=12, pickable=True,
        ))
    map_legend(layer, points["review"].value_counts().to_dict())
    st.pydeck_chart(
        pdk.Deck(layers=layers, initial_view_state=_initial_view([exact, block]),
                tooltip={"html": "{tip}", "style": {"fontSize": "12px"}}),
        width="stretch",
    )
    st.caption("Hover any point for details. Scroll to zoom, drag to pan.")
    table = points.merge(addresses, how="left", on="bbl")
    table["address"] = (table["street_number"].fillna("").astype(str) + " "
                        + table["street_name"].fillna("")).str.strip()
    magnitude_help = {
        "mortgage-to-price ratio": "Highest mortgage amount divided by recorded sale price among this property's signals. A ratio is not a property value.",
        "sale-price increase (%)": "Largest percent increase between the two recorded deed amounts; recorded amounts may need document review.",
        "mortgage amount increase ($)": "Largest difference between the two recorded mortgage amounts; this is not a property value or remaining loan balance.",
    }
    magnitude_title = {
        "mortgage-to-price ratio": "Largest mortgage / price ratio",
        "sale-price increase (%)": "Largest sale-price increase (%)",
        "mortgage amount increase ($)": "Largest mortgage amount increase ($)",
    }
    st.caption(
        "The table lists mapped properties only. Signal magnitude is layer-specific and is not a property-value estimate."
    )
    st.dataframe(
        with_badges(table.sort_values("max_mag", ascending=False)[
            ["bbl", "address", "precision", "n_signals", "max_mag", "review"]
        ]),
        width="stretch", hide_index=True,
        column_config={
            "max_mag": st.column_config.NumberColumn(
                magnitude_title[mag_label], help=magnitude_help[mag_label], format="%.2f"
            ),
            "n_signals": st.column_config.NumberColumn("Signals on property"),
        },
    )


def _coverage_metrics(points):
    n = len(points)
    by = points["precision"].value_counts()
    c1, c2, c3 = st.columns(3)
    c1.metric("Exact lot", f"{int(by.get('lot', 0)):,}")
    c2.metric("Block-level only", f"{int(by.get('block', 0)):,}")
    c3.metric("Unmapped", f"{int(by.get('unmapped', 0)):,}")
    if int(by.get("unmapped", 0)):
        st.caption(f"{int(by.get('unmapped', 0))} of {n} properties have no position and are not drawn.")


def _render_case_map(geo, precision_mode):
    pts = fetch_case_property_points()
    if pts.empty:
        st.info("No cases found.")
        return
    min_score = st.sidebar.number_input("Minimum case score", value=0, step=10)
    multi_only = st.sidebar.checkbox("Multi-property cases only", value=True)
    pts = pts[pts["score"] >= min_score]
    if multi_only:
        pts = pts[pts["n_props"] >= 2]
    if pts.empty:
        st.info("No cases match these filters.")
        return
    pts = attach_geo(pts, geo)
    _coverage_metrics(pts)
    if precision_mode == "Exact lot only":
        pts = pts[pts["precision"] == "lot"]
    pts = pts[pts["precision"].isin(["lot", "block"])].head(MAP_POINT_CAP)
    if pts.empty:
        st.info("No mapped properties for these filters.")
        return
    addresses = fetch_addresses(tuple(pts["bbl"]))
    addr = {}
    for _, r in addresses.iterrows():
        addr[r["bbl"]] = html.escape(
            f"{r['street_number'] if pd.notna(r['street_number']) else ''} "
            f"{r['street_name'] if pd.notna(r['street_name']) else ''}".strip()
        )
    pts = pts.assign(color=pts["case_id"].map(lambda i: CASE_PALETTE[int(i) % len(CASE_PALETTE)]))
    pts["tip"] = [
        f"<b>{r.bbl}</b> {addr.get(r.bbl, '')}<br/>{'exact lot' if r.precision == 'lot' else 'block-level (approximate)'}"
        f"<br/>case {int(r.case_id)} (id not stable across rebuilds), score {int(r.score)}, {int(r.n_props)} properties"
        for r in pts.itertuples()
    ]
    exact = pts[pts["precision"] == "lot"].assign(color=lambda d: d["color"].map(lambda c: c + [230]))
    block = pts[pts["precision"] == "block"].assign(
        line=lambda d: d["color"].map(lambda c: c + [230]), fill=lambda d: d["color"].map(lambda c: c + [45]),
    )
    layers = []
    lines = build_case_lines(pts.assign(color=pts["color"]))
    if not lines.empty:
        layers.append(pdk.Layer("LineLayer", data=lines, get_source_position="[s_lon, s_lat]",
                                get_target_position="[t_lon, t_lat]", get_color="color", get_width=2))
    if not block.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=block, get_position="[lon, lat]", get_radius=110,
            get_fill_color="fill", get_line_color="line", stroked=True, filled=True,
            line_width_min_pixels=2, radius_min_pixels=8, radius_max_pixels=30, pickable=True,
        ))
    if not exact.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=exact, get_position="[lon, lat]", get_radius=35,
            get_fill_color="color", get_line_color=[255, 255, 255], stroked=True,
            line_width_min_pixels=1, radius_min_pixels=4, radius_max_pixels=12, pickable=True,
        ))
    map_legend("Cases")
    st.pydeck_chart(
        pdk.Deck(layers=layers, initial_view_state=_initial_view([exact, block]),
                tooltip={"html": "{tip}", "style": {"fontSize": "12px"}}),
        width="stretch",
    )
    st.caption("Hover any point for details. Scroll to zoom, drag to pan.")


# ============================================================
# CASE QUEUE + CASE DETAIL
# ============================================================

def fetch_case_notes(case_id):
    return run_query("""
        SELECT author, note_text
        FROM case_notes
        WHERE case_id = %(case_id)s
    """, {"case_id": case_id})


def render_case_detail(case_id, cases_df):
    case_row = cases_df[cases_df["case_id"] == case_id].iloc[0]

    st.divider()
    st.header(f"Case #{case_id}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Score", f"{case_row['score']:.0f}")
    col2.metric("Properties", int(case_row["n_properties"]))
    col3.metric("Signals", int(case_row["n_signals"]))
    col4.metric("Entities", int(case_row["n_entities"]))

    st.write(f"**Status:** {case_row['status']}")
    if pd.notna(case_row["summary"]) and str(case_row["summary"]).strip():
        st.write(case_row["summary"])

    notes_df = fetch_case_notes(case_id)
    engine_notes = notes_df[notes_df["author"] == "corroboration_engine"]
    if not engine_notes.empty:
        with st.expander("Corroboration score breakdown", expanded=True):
            for note in engine_notes["note_text"]:
                st.code(note, language=None)

            other_notes = notes_df[notes_df["author"] != "corroboration_engine"]
    if not other_notes.empty:
        with st.expander("Analyst notes"):
            for _, row in other_notes.iterrows():
                st.markdown(f"**{row['author']}:** {row['note_text']}")

    tab_props, tab_entities, tab_signals, tab_timeline, tab_network = st.tabs(
        ["Properties", "Entities", "Signals", "Timeline", "Network"]
    )

    with tab_props:
        props_df = fetch_case_properties(case_id)
        if props_df.empty:
            st.caption("No properties linked to this case.")
        else:
            props_df = props_df.assign(
                address=(
                    props_df["street_number"].fillna("").astype(str) + " "
                    + props_df["street_name"].fillna("")
                ).str.strip()
            )
            st.dataframe(
                props_df[["bbl", "role_in_case", "address", "latest_unit", "latest_property_type"]],
                width="stretch", hide_index=True,
            )

    with tab_entities:
        entities_df = fetch_case_entities(case_id)
        if entities_df.empty:
            st.caption("No resolved entities linked to this case.")
        else:
            entities_df = entities_df.assign(
                role=entities_df["is_connecting"].map({True: "connecting", False: "evidence-only"}).fillna("evidence-only")
            )
            st.dataframe(
                entities_df[["canonical_name", "entity_kind", "resolution_status", "role"]],
                width="stretch", hide_index=True,
            )

    with tab_signals:
        flips_df = fetch_case_flips(case_id)
        mtg_df = fetch_case_over_mortgaging(case_id)

        if not flips_df.empty:
            st.subheader(f"Rapid flips ({len(flips_df)})")
            flips_df = add_review_columns(flips_df, "flip", "sale1_document_id", "sale2_document_id")
            display = flips_df.assign(
                mismatch=flips_df["unit_or_type_mismatch"].map({True: "verify", False: ""}),
                sale1_doc=flips_df["sale1_document_id"].map(acris_link),
                sale2_doc=flips_df["sale2_document_id"].map(acris_link),
            )
            st.dataframe(
                display[["bbl", "sale1_date", "sale1_amt", "sale2_date", "sale2_amt",
                        "pct_price_increase", "days_between", "category", "mismatch",
                        "review", "reason", "sale1_doc", "sale2_doc"]],
                width="stretch", hide_index=True,
                column_config={
                    "sale1_doc": doc_link_column("Sale 1 doc"),
                    "sale2_doc": doc_link_column("Sale 2 doc"),
                },
            )
        else:
            st.caption("No rapid-flip signals in this case.")

        if not mtg_df.empty:
            st.subheader(f"Over-mortgaging ({len(mtg_df)})")
            mtg_df = add_review_columns(mtg_df, "over_mortgaging", "deed_document_id", "mtge_document_id")
            display = mtg_df.assign(
                mismatch=mtg_df["unit_or_type_mismatch"].map({True: "verify", False: ""}),
                deed_doc=mtg_df["deed_document_id"].map(acris_link),
                mtge_doc=mtg_df["mtge_document_id"].map(acris_link),
            )
            st.dataframe(
                display[["bbl", "deed_date", "deed_amt", "mtge_date", "mtge_amt",
                        "mortgage_to_price_ratio", "days_between", "category", "mismatch",
                        "review", "reason", "deed_doc", "mtge_doc"]],
                width="stretch", hide_index=True,
                column_config={
                    "deed_doc": doc_link_column("Deed doc"),
                    "mtge_doc": doc_link_column("Mortgage doc"),
                },
            )
            st.caption(
                "Document links open ACRIS in your browser. On the mortgage cover page, check "
                "Taxable Mortgage Amount vs Mortgage Amount and any exemption code -- that is "
                "the only way to rule out a CEMA or tax-exempt financing."
            )
        else:
            st.caption("No over-mortgaging signals in this case.")

    with tab_timeline:
        events_df = fetch_case_events(case_id)
        if events_df.empty:
            st.caption("No timeline events recorded for this case.")
        else:
            events_df = events_df.assign(doc=events_df["document_id"].map(acris_link))
            st.dataframe(
                events_df[["event_date", "event_type", "doc", "description"]],
                width="stretch", hide_index=True,
                column_config={"doc": doc_link_column("Document")},
            )

    with tab_network:
        props_df = fetch_case_properties(case_id)
        edges_df = fetch_case_network_edges(case_id)
        if edges_df.empty:
            st.caption("No entity relationships to graph for this case.")
        else:
            connecting_entities = edges_df[edges_df["is_connecting"]].drop_duplicates("entity_id")
            show_wider = False
            if not connecting_entities.empty:
                show_wider = st.checkbox(
                    "Also show connecting entities' properties outside this case "
                    f"(capped at {WIDER_EXPANSION_LIMIT} per entity)",
                    key=f"wider_{case_id}",
                )
            wider_ids = connecting_entities["entity_id"].tolist() if show_wider else None
            graph_html = build_case_network_html(case_id, props_df, edges_df, wider_entities=wider_ids)
            graph_legend("case")
            st.iframe(graph_html, height=620)

    st.divider()
    st.subheader("Review a signal")
    st.caption(
        "Reviews are saved against the signal's document pair, not the case -- case IDs and "
        "the cases table are rebuilt from scratch on every case_builder.py run."
    )
    options = []
    for _, r in mtg_df.iterrows():
        options.append((
            "over_mortgaging", r["bbl"], r["deed_document_id"], r["mtge_document_id"],
            f"over-mortgaging -- {r['bbl']} -- {r['mortgage_to_price_ratio']:.2f}x",
        ))
    for _, r in flips_df.iterrows():
        options.append((
            "flip", r["bbl"], r["sale1_document_id"], r["sale2_document_id"],
            f"flip -- {r['bbl']} -- {r['pct_price_increase']:.2f}",
        ))
    if not options:
        st.caption("No signals in this case to review.")
    else:
        idx = st.selectbox(
            "Signal", options=list(range(len(options))),
            format_func=lambda i: options[i][4], key=f"review_pick_{case_id}",
        )
        stype, bbl, da, db, _label = options[idx]
        render_review_form(stype, bbl, da, db, key=f"case_{case_id}_{idx}")

# ============================================================
# CASE QUEUE
# ============================================================

def render_case_queue():
    page_header(VIEW_META["Case queue"][0], "Investigation queue", VIEW_META["Case queue"][1])
    st.caption(
        "Cases from case_builder.py, scored by corroboration_engine.py. Scores are "
        "explainable triage heuristics, not fraud probabilities."
    )

    cases_df = fetch_case_list()
    if cases_df.empty:
        st.warning("No cases found. Run etl_load.py, case_builder.py, then corroboration_engine.py first.")
        return

    st.sidebar.header("Filters")
    all_statuses = sorted(cases_df["status"].dropna().unique().tolist())
    status_filter = st.sidebar.multiselect("Status", options=all_statuses, default=all_statuses)
    min_score = st.sidebar.slider(
        "Minimum score",
        min_value=float(cases_df["score"].min()),
        max_value=float(cases_df["score"].max()),
        value=float(cases_df["score"].min()),
    )
    min_properties = st.sidebar.number_input("Minimum properties", min_value=1, value=1, step=1)

    filtered = cases_df[
        cases_df["status"].isin(status_filter)
        & (cases_df["score"] >= min_score)
        & (cases_df["n_properties"] >= min_properties)
    ].reset_index(drop=True)

    st.write(f"{len(filtered)} of {len(cases_df)} cases match current filters")

    display_cols = ["case_id", "score", "status", "n_properties", "n_signals", "n_entities", "title"]
    st.dataframe(
        filtered[display_cols], width="stretch", hide_index=True,
        column_config={
            "score": st.column_config.ProgressColumn(
                "Score", help="Explainable triage heuristic, not a fraud probability.",
                min_value=min(0.0, float(cases_df["score"].min())), max_value=float(cases_df["score"].max()),
                format="%.0f",
            ),
            "case_id": st.column_config.NumberColumn("Case", help="Changes on every rebuild; note findings by BBL."),
            "n_properties": st.column_config.NumberColumn("Properties"),
            "n_signals": st.column_config.NumberColumn("Signals"),
            "n_entities": st.column_config.NumberColumn("Entities"),
            "title": st.column_config.TextColumn("Summary", width="large"),
        },
    )

    if filtered.empty:
        return

    label_map = {
        cid: f"#{cid} (score {sc:.0f}) -- {t}"
        for cid, sc, t in zip(filtered["case_id"], filtered["score"], filtered["title"])
    }
    selected_case_id = st.selectbox(
        "Select a case to inspect",
        options=filtered["case_id"].tolist(),
        format_func=lambda cid: label_map[cid],
    )

    render_case_detail(selected_case_id, filtered)


# ============================================================
# UI: BRANDING, CSS, PAGE HEADERS, LEGENDS
# ============================================================

BRAND_NAME = "Phantom Equity"
BRAND_TAGLINE = "Real-estate anomaly intelligence"
VIEW_META = {
    "Home": ("\U0001F3E0", "Your starting point: what is here, how far along the review is, where to go next."),
    "Case queue": ("\U0001F4CB", "Scored cases ranked for triage, with properties, entities, signals, timeline and network."),
    "Signal explorer": ("\U0001F50E", "Every individual signal ranked by magnitude, independent of case score."),
    "Serial refinancing": ("\U0001F501", "Properties mortgaged again and again with no sale in between."),
    "Transfer signals": ("\U0001F4E7", "Inspect nominal chains, repeated transfers, CEMA proxies, and adjusted valuation residuals."),
    "Exclusion audit": ("\U0001F9EA", "Check what the party-overlap filter threw away, most suspicious near-misses first."),
    "Entity explorer": ("\U0001F578\ufe0f", "Who holds what, who they share properties with, and which names were merged."),
    "Map view": ("\U0001F5FA\ufe0f", "Where signals and cases sit, with honest marks for approximate positions."),
}
VIEW_ORDER = list(VIEW_META)
REVIEW_BADGE = {
    "strong_candidate": "\U0001F534 strong_candidate", "under_review": "\U0001F7E0 under_review",
    "unreviewed": "\U0001F535 unreviewed", "false_positive": "\u26AA false_positive",
    "wrongly_excluded": "\U0001F534 wrongly_excluded", "unclear": "\U0001F7E0 unclear",
    "correctly_excluded": "\u26AA correctly_excluded",
}
REVIEW_LEGEND = [
    ("strong_candidate", "Strong candidate", "Survived a manual document review. Still a candidate, never confirmed fraud."),
    ("under_review", "Under review", "Someone is looking at it right now."),
    ("unreviewed", "Unreviewed", "Not opened yet."),
    ("false_positive", "False positive", "Dismissed, with the reason on record."),
]

UI_CSS = """<style>
:root{--pe-accent:#4C72B0;--pe-warm:#DD8452;--pe-green:#55A868}
@keyframes pe-rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.pe-hero{padding:1.7rem 1.9rem;border-radius:18px;margin:0 0 1.2rem;color:#fff;
background:linear-gradient(120deg,#3f5f9a,#6f62b0 60%,#d27a4a);
box-shadow:0 8px 24px rgba(76,114,176,.24);animation:pe-rise .45s ease both}
.pe-hero h1{margin:.35rem 0 0;font-size:1.75rem;line-height:1.2;
letter-spacing:-.4px;color:#fff;padding:0;font-weight:700}
.pe-hero p{margin:.55rem 0 0;font-size:1rem;line-height:1.4;
color:#fff;opacity:.9;max-width:60ch}
.pe-pill{display:inline-block;padding:.18rem .7rem;border-radius:999px;
background:rgba(255,255,255,.16);font-size:.78rem;letter-spacing:.02em}
.pe-pulse{display:inline-block;width:8px;height:8px;border-radius:50%;
background:var(--pe-green);margin-right:.5rem}
.pe-page-header{display:flex;align-items:center;gap:.9rem;padding:.9rem 1.1rem;margin:0 0 1rem;
border-radius:16px;border:1px solid rgba(128,128,128,.22);
background:linear-gradient(90deg,rgba(76,114,176,.14),rgba(221,132,82,.07));
animation:pe-rise .4s ease both}
.pe-page-header .pe-emoji{font-size:2rem;line-height:1}
.pe-page-header h2{margin:0;padding:0;font-size:1.45rem;line-height:1.2}
.pe-page-header p{margin:.15rem 0 0;opacity:.78;font-size:.93rem}
.pe-legend{border:1px solid rgba(128,128,128,.28);border-radius:16px;padding:.9rem 1.1rem 1rem;
margin:.4rem 0 1rem;background:rgba(128,128,128,.06);animation:pe-rise .4s ease both}
.pe-legend-title{font-weight:800;letter-spacing:.07em;text-transform:uppercase;
font-size:.74rem;opacity:.8;margin-bottom:.6rem}
.pe-legend-head{font-weight:700;font-size:.8rem;opacity:.65;margin:.7rem 0 .4rem}
.pe-legend-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:.65rem 1.2rem}
.pe-legend-item{display:flex;align-items:flex-start;gap:.65rem;font-size:.9rem;line-height:1.28}
.pe-legend-item b{display:block}
.pe-legend-item .pe-d{display:block;opacity:.76;font-size:.84rem}
.pe-legend-note{margin-top:.75rem;padding-top:.6rem;border-top:1px dashed rgba(128,128,128,.35);
font-size:.84rem;opacity:.8}
.pe-sw{flex:0 0 auto;min-width:30px;height:22px;display:inline-flex;align-items:center;
justify-content:center;gap:2px}
.pe-dot{width:14px;height:14px;border-radius:50%;border:2px solid #fff;
box-shadow:0 0 0 1px rgba(0,0,0,.28)}
.pe-ring{width:20px;height:20px;border-radius:50%;border:3px solid}
.pe-box{width:18px;height:13px;border-radius:3px;box-shadow:0 0 0 1px rgba(0,0,0,.2)}
.pe-ell{width:20px;height:13px;border-radius:50%;box-shadow:0 0 0 1px rgba(0,0,0,.2)}
.pe-ln{width:30px;height:3px;border-radius:2px}
.pe-mini{width:10px;height:10px;border-radius:50%;box-shadow:0 0 0 1px rgba(0,0,0,.25)}
.pe-card-title{font-size:1.12rem;font-weight:700;margin-bottom:.25rem}
.pe-card-desc{opacity:.78;font-size:.9rem;min-height:3.1rem}
.pe-note{border-left:4px solid var(--pe-accent);padding:.35rem .85rem;margin:.2rem 0;
border-radius:4px;background:rgba(76,114,176,.07);font-size:.9rem;animation:pe-rise .5s ease both}
.pe-note b{display:block;margin-bottom:.15rem}
.pe-brand{padding:.2rem .2rem .8rem}
.pe-brand-title{font-size:1.3rem;font-weight:800;letter-spacing:-.3px}
.pe-brand-sub{font-size:.8rem;opacity:.75;margin-top:.15rem;display:flex;align-items:center}
div[data-testid="stMetric"]{border:1px solid rgba(128,128,128,.24);border-radius:16px;
padding:.75rem .95rem;background:rgba(128,128,128,.06);
transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease;
animation:pe-rise .45s ease both}
div[data-testid="stMetric"]:hover{transform:translateY(-3px);box-shadow:0 8px 20px rgba(0,0,0,.14);
border-color:var(--pe-accent)}
div[data-testid="stVerticalBlockBorderWrapper"]{transition:transform .18s ease,box-shadow .18s ease,
border-color .18s ease;border-radius:16px}
div[data-testid="stVerticalBlockBorderWrapper"]:hover{transform:translateY(-3px);
box-shadow:0 10px 24px rgba(0,0,0,.15)}
.stButton>button{border-radius:12px;transition:transform .15s ease,box-shadow .15s ease}
.stButton>button:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(76,114,176,.35)}
section[data-testid="stSidebar"] div[role="radiogroup"]>label{padding:.4rem .65rem;
border-radius:12px;margin-bottom:2px;transition:background .15s ease,transform .15s ease}
section[data-testid="stSidebar"] div[role="radiogroup"]>label:hover{background:rgba(76,114,176,.15);
transform:translateX(3px)}
section[data-testid="stSidebar"] div[role="radiogroup"]>label:has(input:checked){
background:rgba(76,114,176,.24);font-weight:700}
</style>"""


def inject_css():
    st.markdown(UI_CSS, unsafe_allow_html=True)


def page_header(emoji, title, subtitle):
    st.markdown(
        f'<div class="pe-page-header"><div class="pe-emoji">{emoji}</div>'
        f"<div><h2>{title}</h2><p>{subtitle}</p></div></div>",
        unsafe_allow_html=True,
    )


def _rgba(c, a=1.0):
    return f"rgba({c[0]},{c[1]},{c[2]},{a})"


def sw_dot(c):
    return f'<span class="pe-sw"><span class="pe-dot" style="background:{_rgba(c)}"></span></span>'


def sw_ring(c):
    return (f'<span class="pe-sw"><span class="pe-ring" style="border-color:{_rgba(c)};'
            f'background:{_rgba(c, .18)}"></span></span>')


def sw_box(color):
    return f'<span class="pe-sw"><span class="pe-box" style="background:{color}"></span></span>'


def sw_ellipse(color):
    return f'<span class="pe-sw"><span class="pe-ell" style="background:{color}"></span></span>'


def sw_palette(colors):
    dots = "".join(f'<span class="pe-mini" style="background:{_rgba(c)}"></span>' for c in colors)
    return f'<span class="pe-sw" style="min-width:52px">{dots}</span>'


_LINE_PATTERNS = {
    "solid": "{c}",
    "dash": "repeating-linear-gradient(90deg,{c} 0 7px,transparent 7px 11px)",
    "dot": "repeating-linear-gradient(90deg,{c} 0 2px,transparent 2px 6px)",
    "dashdot": "repeating-linear-gradient(90deg,{c} 0 6px,transparent 6px 9px)",
}


def sw_line(color, pattern="solid"):
    bg = _LINE_PATTERNS[pattern].format(c=color)
    return f'<span class="pe-sw"><span class="pe-ln" style="background:{bg}"></span></span>'


def legend(title, groups, note=""):
    body = ""
    for heading, items in groups:
        if heading:
            body += f'<div class="pe-legend-head">{heading}</div>'
        cells = "".join(
            f'<div class="pe-legend-item">{sw}<div><b>{label}</b><span class="pe-d">{desc}</span></div></div>'
            for sw, label, desc in items
        )
        body += f'<div class="pe-legend-grid">{cells}</div>'
    foot = f'<div class="pe-legend-note">{note}</div>' if note else ""
    st.markdown(f'<div class="pe-legend"><div class="pe-legend-title">{title}</div>{body}{foot}</div>',
                unsafe_allow_html=True)


def review_legend_items(counts=None):
    out = []
    for key, label, desc in REVIEW_LEGEND:
        shown = f"{label} \u00b7 {int(counts.get(key, 0)):,}" if counts is not None else label
        out.append((sw_dot(REVIEW_COLORS[key]), shown, desc))
    return out


def map_legend(layer, counts=None):
    ring_c, dot_c = (76, 114, 176), (76, 114, 176)
    if layer == "Cases":
        groups = [(None, [
            (sw_palette(CASE_PALETTE[:4]), "One colour per case", "Properties sharing a colour belong to the same case."),
            (sw_line("rgba(120,120,120,.85)"), "Line", "Joins the properties of a multi-property case."),
            (sw_dot(CASE_PALETTE[0]), "Solid dot", "Exact lot position."),
            (sw_ring(CASE_PALETTE[0]), "Hollow ring",
             "Block-level only: the block's average position, not the lot. A line to a ring ends at that average."),
        ])]
        note = "Case ids change on every rebuild. Note findings by BBL."
    else:
        groups = [
            ("Colour = review state" + (" (properties currently on the map)" if counts is not None else ""),
            review_legend_items(counts)),
            ("Shape = how precisely we know where it is", [
                (sw_dot(dot_c), "Solid dot", "Exact lot position from PLUTO."),
                (sw_ring(ring_c), "Hollow ring",
                "Block-level only (approximate). Ring size = properties on that block; hover for the list."),
            ]),
        ]
        note = "Dots cluster where transactions are dense, so a cluster alone is not evidence. Candidates are flags, never confirmed fraud."
    legend("Map key", groups, note)


def graph_legend(kind):
    nodes = []
    if kind == "entity":
        nodes.append((sw_ellipse("#C44E52"), "Selected entity", "The entity you picked in the list."))
    nodes.append((sw_box("#4C72B0"), "Property",
                "A property in this case." if kind == "case" else "A property the selected entity touches."))
    if kind == "case":
        nodes.append((sw_box("#D9D9D9"), "Property outside the case",
                    "Other properties an entity touches. Only shown when the toggle above is on."))
    nodes += [
        (sw_ellipse("#DD8452"), "Entity with an ownership role",
        "Buyer or seller on a deed. These links can join properties into one case."),
        (sw_ellipse("#8C8C8C"), "Evidence-only entity",
        "On a document without an ownership role, for example a lender. Context, never a connection."),
    ]
    edges = [
        (sw_line("#4C72B0", "solid"), "Ownership transfer", "A deed-type relationship."),
        (sw_line("#DD8452", "dash"), "Financing", "Mortgage-side relationship."),
        (sw_line("#8172B3", "dot"), "Assignment", "A mortgage assignment chain."),
        (sw_line("#55A868", "dashdot"), "Lease", "Leasehold, not a fee transfer."),
        (sw_line("#B0B0B0", "solid"), "Other", "Everything else."),
        ('<span class="pe-sw" style="font-size:1.2rem">\u279c</span>', "Arrow direction",
        "Into a property: the entity is on the buyer side. Out of a property to the entity: the seller side. No arrow: another role."),
    ]
    legend("Diagram key", [("Nodes", nodes), ("Lines", edges)],
           "Hover any node or line for its name, role and relationship class.")


def with_badges(df):
    if "review" in df.columns:
        df = df.assign(review=df["review"].map(lambda x: REVIEW_BADGE.get(x, x)))
    return df


def go_to(view_name):
    st.session_state["view"] = view_name


@st.cache_data(ttl=60)
def home_stats():
    def scalar(sql):
        try:
            return int(run_query(sql).iloc[0, 0])
        except Exception:
            return None

    stats = {
        "properties": scalar("SELECT COUNT(*) FROM properties"),
        "entities": scalar("SELECT COUNT(*) FROM entities"),
        "cases": scalar("SELECT COUNT(*) FROM cases"),
        "mtg": scalar("SELECT COUNT(*) FROM signal_over_mortgaging WHERE category = 'candidate'"),
        "flips": scalar("SELECT COUNT(*) FROM signal_flips WHERE category = 'candidate'"),
        "refi": None, "geo_exact": None, "geo_block": None, "reviews": {}, "audit_reviewed": 0,
    }
    try:
        stats["refi"] = len(pd.read_csv(SERIAL_REFI_CSV, usecols=["bbl"]))
    except Exception:
        pass
    try:
        geo = pd.read_csv(PROPERTY_GEO_CSV, usecols=["precision"])["precision"].value_counts()
        stats["geo_exact"], stats["geo_block"] = int(geo.get("lot", 0)), int(geo.get("block", 0))
    except Exception:
        pass
    try:
        rv = run_query("SELECT signal_type, status, COUNT(*) AS n FROM signal_reviews GROUP BY 1, 2")
        cand = rv[rv["signal_type"].isin(["over_mortgaging", "flip", "serial_refinancing"])]
        stats["reviews"] = cand.groupby("status")["n"].sum().astype(int).to_dict()
        stats["audit_reviewed"] = int(rv[rv["signal_type"] == "party_overlap_exclusion"]["n"].sum())
    except Exception:
        pass
    return stats


def _file_stamp(path):
    try:
        return f"{pd.Timestamp(path.stat().st_mtime, unit='s'):%Y-%m-%d %H:%M} UTC"
    except Exception:
        return "not found yet"


def render_home():
    stats = home_stats()
    hour = datetime.now().hour
    part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    name = os.environ.get("ANALYST_NAME", "").strip()
    greet = f"Good {part}, {html.escape(name)}" if name else f"Good {part}"

    st.markdown(
        f'<div class="pe-hero">'
        f'<span class="pe-pill">Analyst workbench</span>'
        f"<h1>{greet}</h1>"
        f"<p>{BRAND_NAME} surfaces property records that behave unusually and gives you "
        f"the tools to look closer: signals, entity networks, maps, and a review trail. "
        f"<b>Every flag is a lead to investigate, never a verdict.</b></p>"
        f"</div>",
        unsafe_allow_html=True,
    )

    def fmt(n):
        return "\u2014" if n is None else f"{n:,}"

    n_cand = sum(x for x in (stats["mtg"], stats["flips"], stats["refi"]) if x is not None)
    reviewed = sum(stats["reviews"].values())

    tiles = st.columns(3)
    tiles[0].metric("Cases", fmt(stats["cases"]), help="Cases are grouped properties/signals for triage; they are not findings of wrongdoing.")
    tiles[1].metric("Candidate signals", fmt(n_cand or None), help="Count of over-mortgaging, rapid-flip, and serial-refinancing rows in the latest signal outputs.")
    tiles[2].metric(
        "Reviewed",
        f"{reviewed:,}" if n_cand else "\u2014",
        delta=f"{reviewed / n_cand:.1%}" if n_cand else None,
        delta_color="off",
        help="Signals with a saved review status, including under review, strong candidate, and false positive.",
    )

    if n_cand:
        frac = min(1.0, reviewed / n_cand)
        st.progress(
            frac,
            text=f"{reviewed:,} of {n_cand:,} candidate signals reviewed ({frac:.1%})",
        )

    st.markdown("#### Where to start")

    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown(
                '<div class="pe-card-title">\U0001F4CB  Case queue</div>'
                '<div class="pe-card-desc">Scored cases ranked for triage, with '
                "properties, entities, signals, timeline, and network view.</div>",
                unsafe_allow_html=True,
            )
        with c2:
            st.write("")
            st.button(
                "Open case queue",
                key="open_Case queue",
                on_click=go_to,
                args=("Case queue",),
                width="stretch",
                type="primary",
            )

    secondary = [v for v in VIEW_ORDER if v not in ("Home", "Case queue")]
    for start in range(0, len(secondary), 3):
        row = secondary[start:start + 3]
        cols = st.columns(3)
        for col, v in zip(cols, row):
            with col:
                emoji, desc = VIEW_META[v]
                with st.container(border=True):
                    st.markdown(
                        f'<div class="pe-card-title">{emoji} {v}</div>'
                        f'<div class="pe-card-desc">{desc}</div>',
                        unsafe_allow_html=True,
                    )
                    st.button(
                        "Open",
                        key=f"open_{v}",
                        on_click=go_to,
                        args=(v,),
                        width="stretch",
                    )

    with st.expander("Read this first (methodology notes)", expanded=False):
        st.markdown(
            "**Candidates, not verdicts.** Several of the highest-ratio flags turned out "
            "to be ordinary once the documents were opened: self-transfers, debt "
            "consolidations, affordable-housing loans."
        )
        st.markdown(
            "**Positions have precision.** Condo units and lots PLUTO does not list only "
            "have a block-level position. The map draws those as hollow rings sized by "
            "how many properties share the block."
        )
        st.markdown(
            "**Hubs are not associates.** Lenders touch thousands of properties. The "
            "entity explorer shows how much of a counterparty's footprint overlaps, so "
            "you can tell a hub from a real association."
        )
        st.markdown(
            "**Case ids change.** Cases are rebuilt from scratch on every pipeline run. "
            "Note findings by BBL, not by case number."
        )

    key_counts = dict(stats["reviews"])
    key_counts["unreviewed"] = max(0, n_cand - reviewed)
    legend("Review states (used on every screen)", [(None, review_legend_items(key_counts))])
    st.caption(
        f"Signals last run: {_file_stamp(SERIAL_REFI_CSV)}  \u00b7  "
        f"Map positions: {_file_stamp(PROPERTY_GEO_CSV)}  \u00b7  "
        f"Exclusion audit verdicts saved: {stats['audit_reviewed']:,}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    st.set_page_config(
        page_title=f"{BRAND_NAME} -- Investigation Queue", page_icon="\U0001F3DB\ufe0f",
        layout="wide", initial_sidebar_state="expanded",
    )
    inject_css()
    ensure_reviews_table()
    st.sidebar.markdown(
        f'<div class="pe-brand"><div class="pe-brand-title">\U0001F3DB\ufe0f {BRAND_NAME}</div>'
        f'<div class="pe-brand-sub"><span class="pe-pulse"></span>{BRAND_TAGLINE}</div></div>',
        unsafe_allow_html=True,
    )
    st.sidebar.link_button("Search documents in ACRIS", ACRIS_SEARCH_URL, width="stretch")
    st.sidebar.caption("If a document link opens blank or errors, search its displayed document ID in ACRIS.")
    st.sidebar.caption("NAVIGATE")
    view = st.sidebar.radio(
        "View", VIEW_ORDER, key="view", label_visibility="collapsed",
        format_func=lambda v: f"{VIEW_META[v][0]}  {v}",
    )
    if view == "Home":
        render_home()
        return
    if view == "Map view":
        render_map_view()
        return
    if view == "Entity explorer":
        render_entity_explorer()
        return
    if view == "Serial refinancing":
        render_serial_refi()
        return
    if view == "Signal explorer":
        render_signal_explorer()
        return
    if view == "Transfer signals":
        render_transfer_signals()
        return
    if view == "Exclusion audit":
        render_exclusion_audit()
        return
    render_case_queue()


if __name__ == "__main__":
    main()
