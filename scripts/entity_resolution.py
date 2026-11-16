"""
Entity resolution for the real-estate-fraud-detection project.

Takes the party-level output of acris_pipeline.py (data/processed/acris_parties_analyzed.csv)
and produces entity clusters: groups of name variants that likely refer to the
same real-world entity or person, so downstream shell-network analysis operates
on merged identities instead of raw, unmerged strings.

Two separate resolution tracks, because they're different problems:
  1. LLC / Corp / company name matching -- fuzzy string matching on normalized
     company names, blocked by address/zip to keep comparisons tractable.
  2. Trustee matching -- "X AS TRUSTEE [OF] Y" records are matched on the
     PERSON (X), not the trust name (Y), since the same person often controls
     several differently-named trusts.

This produces CANDIDATE clusters, not verified identities. Every cluster
should be spot-checked -- fuzzy matching produces false merges (two
different "JOHNSON LLC"s in different zips are not necessarily related, if
the address blocking key is loose) and false splits (a company that changed
its registered address between filings won't block together at all).

Requires: pip install rapidfuzz pandas
"""

import pandas as pd
import re
from rapidfuzz import fuzz

# --- Name normalization ---

SUFFIX_MAP = {
    r"\bL\.?L\.?C\.?\b": "LLC",
    r"\bL\.?P\.?\b": "LP",
    r"\bINC\.?\b": "INC",
    r"\bCORP\.?ORATION\b": "CORP",
    r"\bCORP\.?\b": "CORP",
    r"\bCO\.?\b": "CO",
    r"\bCOMPANY\b": "CO",
}

def normalize_entity_name(name):
    n = str(name).upper().strip()
    n = re.sub(r"[.,'\"]", "", n)
    for pattern, canonical in SUFFIX_MAP.items():
        n = re.sub(pattern, canonical, n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# --- Trustee extraction ---
# Observed patterns: "X AS TRUSTEE Y", "X AS TRUSTEE OF Y", "X TRUSTEE"
TRUSTEE_PATTERN = re.compile(
    r"^(?P<person>.+?)\s+AS\s+TRUSTEE(?:\s+OF)?\s*(?P<trust>.*)$", re.IGNORECASE
)
TRUSTEE_PATTERN_SIMPLE = re.compile(r"^(?P<person>.+?)\s+TRUSTEE$", re.IGNORECASE)

def extract_trustee(name):
    n = str(name).strip()
    m = TRUSTEE_PATTERN.match(n)
    if m:
        return m.group("person").strip().upper(), (m.group("trust").strip().upper() or None)
    m = TRUSTEE_PATTERN_SIMPLE.match(n)
    if m:
        return m.group("person").strip().upper(), None
    return None, None


# --- Blocking + clustering ---

class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def extract_numeric_tokens(name):
    """Numbers in a real-estate entity name (street number, series #, unit #) are usually
    the single most distinguishing token. Two names differing in ANY numeric token are
    treated as different entities, no matter how similar the rest of the string is."""
    return frozenset(re.findall(r"\d+", str(name)))


def cluster_llc_names(df, name_col="name_norm2", zip_col="zip", threshold=88):
    """Block by zip, fuzzy-match within block, union-find into clusters.
    Never merges two names with different numeric tokens (see extract_numeric_tokens)."""
    df["_numtok"] = df[name_col].apply(extract_numeric_tokens)
    uf = UnionFind(df[name_col].unique())
    for _, block in df.groupby(df[zip_col].fillna("UNKNOWN")):
        names = block[[name_col, "_numtok"]].drop_duplicates(name_col).values.tolist()
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                name_i, num_i = names[i]
                name_j, num_j = names[j]
                if num_i != num_j:
                    continue  # different numeric token(s) -> never the same entity
                score = fuzz.token_sort_ratio(name_i, name_j)
                if score >= threshold:
                    uf.union(name_i, name_j)
    df["entity_cluster_id"] = df[name_col].apply(lambda n: uf.find(n))
    return df


def cluster_trustees(df, name_col="name_norm2", zip_col="zip", threshold=90):
    """
    Cluster trustee-flagged records the SAME conservative way as LLC names:
    full normalized string, blocked by zip, numeric-token guard applied.

    We deliberately do NOT try to extract a standalone "person" key (e.g. just
    the surname) to cluster on. ACRIS's individual-name formatting is
    inconsistent enough across records -- "LASTNAME AS TRUSTEE FIRSTNAME",
    "LASTNAME, TRUSTEE, FIRSTNAME MI", "FULL NAME AS TRUSTEE" all appear in the
    same sample -- that a naive surname-only key produces false merges (e.g.
    two different people who happen to share a surname getting merged into
    "the same person"). Clustering on the full string is more conservative:
    it will under-merge (miss that "SMITH AS TRUSTEE JOHN" and "JOHN SMITH
    TRUSTEE" are the same person) rather than over-merge (falsely claiming two
    different people are one). For a fraud tool, under-merging is the safer
    failure mode.
    """
    return cluster_llc_names(df, name_col=name_col, zip_col=zip_col, threshold=threshold).rename(
        columns={"entity_cluster_id": "trustee_cluster_id"}
    )


def resolve(parties_analyzed):
    df = parties_analyzed.copy()
    entities = df[df["is_entity"]].copy()

    # Informational only -- NOT used for clustering (see cluster_trustees docstring
    # for why a surname-only key is unsafe given ACRIS's inconsistent name formats).
    entities["trustee_person_guess"], entities["trust_name_guess"] = zip(
        *entities["name"].apply(extract_trustee)
    )
    entities["is_trustee_record"] = entities["name"].str.contains("TRUSTEE", case=False, na=False)

    non_trustee = entities[~entities["is_trustee_record"]].copy()
    trustee = entities[entities["is_trustee_record"]].copy()

    non_trustee["name_norm2"] = non_trustee["name"].apply(normalize_entity_name)
    non_trustee = cluster_llc_names(non_trustee)

    if not trustee.empty:
        trustee["name_norm2"] = trustee["name"].apply(normalize_entity_name)
        trustee = cluster_trustees(trustee)

    print(f"\n{len(non_trustee)} non-trustee entity rows -> "
          f"{non_trustee['entity_cluster_id'].nunique()} clusters")
    multi = non_trustee.groupby("entity_cluster_id")["name_norm2"].nunique()
    multi_clusters = multi[multi > 1]
    print(f"{len(multi_clusters)} clusters merge 2+ distinct raw name spellings:")
    for cid in multi_clusters.index:
        variants = non_trustee.loc[non_trustee["entity_cluster_id"] == cid, "name"].unique()
        print(f"  cluster {cid[:30]!r}: {list(variants)}")

    if not trustee.empty:
        print(f"\n{len(trustee)} trustee-flagged records -> "
              f"{trustee['trustee_cluster_id'].nunique()} clusters (conservative: exact/near-exact "
              f"full-name matches only; will under-merge rather than risk false person-merges)")
        multi_t = trustee.groupby("trustee_cluster_id")["name_norm2"].nunique()
        multi_t_clusters = multi_t[multi_t > 1]
        print(f"{len(multi_t_clusters)} clusters merge 2+ distinct raw spellings:")
        for cid in multi_t_clusters.index:
            variants = trustee.loc[trustee["trustee_cluster_id"] == cid, "name"].unique()
            print(f"  {list(variants)}")

    review_rows = []
    for cid in multi_clusters.index:
        variants = non_trustee.loc[non_trustee["entity_cluster_id"] == cid, "name"].unique()
        review_rows.append({"cluster_type": "entity", "cluster_id": cid, "variants": " | ".join(variants)})

    if not trustee.empty:
        multi_t_all = trustee.groupby("trustee_cluster_id")["name_norm2"].nunique()
        for cid in multi_t_all[multi_t_all > 1].index:
            variants = trustee.loc[trustee["trustee_cluster_id"] == cid, "name"].unique()
            review_rows.append({"cluster_type": "trustee", "cluster_id": cid, "variants": " | ".join(variants)})

    if review_rows:
        import os
        review_df = pd.DataFrame(review_rows)
        review_path = os.path.join("..", "data", "processed", "clusters_needing_review.csv")
        review_df.to_csv(review_path, index=False)
        print(f"\n{len(review_df)} multi-variant clusters written to {review_path} for manual verification. "
              f"These are CANDIDATE merges, not confirmed -- at least 2 known false merges "
              f"(different first names, same surname) have been observed at this threshold. "
              f"Spot-check before treating any cluster here as one confirmed entity.")

    return non_trustee, trustee


def main():
    import os
    processed_path = os.path.join("..", "data", "processed", "acris_parties_analyzed.csv")
    df = pd.read_csv(processed_path)
    non_trustee, trustee = resolve(df)

    out_dir = os.path.join("..", "data", "processed")
    non_trustee.to_csv(os.path.join(out_dir, "entities_resolved.csv"), index=False)
    if not trustee.empty:
        trustee.to_csv(os.path.join(out_dir, "trustees_resolved.csv"), index=False)
    print(f"\nWrote {out_dir}/entities_resolved.csv"
          + (f" and {out_dir}/trustees_resolved.csv" if not trustee.empty else ""))


if __name__ == "__main__":
    main()
