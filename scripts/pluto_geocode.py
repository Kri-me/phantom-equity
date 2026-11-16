"""
pluto_geocode.py -- BBL -> latitude/longitude lookup from NYC PLUTO, for the map view.

SOURCE: NYC Open Data, "Primary Land Use Tax Lot Output (PLUTO)", Socrata dataset 64uk-42ks.
PLUTO's latitude/longitude is the approximate location of the tax lot (a centroid constrained
to fall inside the lot), not a surveyed point.

THE CONDO PROBLEM (why there is a fallback): PLUTO has ONE record per condominium COMPLEX,
not one per condo unit. ACRIS records unit sales under the unit's own tax lot (typically
lots 1001-6999), and those unit BBLs do not exist in PLUTO -- the complex appears once, under
its billing lot. A straight join would silently drop every condo-unit property, and condo
units are a large share of the flagged ones (e.g. 3-2325-1020, unit 3K at 127 Kent Avenue).
So each project BBL is resolved in two tiers, and the tier is recorded:
    precision = "lot"    exact BBL found in PLUTO
    precision = "block"  BBL not in PLUTO; falls back to the mean position of the PLUTO lots
                         on the same borough+block (good to roughly block scale, NOT lot scale)
    (no row)             neither found -- left unmapped, never guessed
The map view must show the precision, and must not present "block" points as exact.

BBL FORMAT: PLUTO stores bbl as a 10-digit number (borough 1 digit + block 5 + lot 4, e.g.
1000477501, sometimes with a trailing ".00000000"). This project uses 'B-BBBB-LL' unpadded
(e.g. 4-1457-31). normalize_bbl() converts the former to the latter.

OUTPUT (data/processed/):
    property_geo.csv     one row per project BBL that could be located: bbl, lat, lon, precision
Reads the project's BBLs from the Postgres `properties` table (same connection env vars as
case_builder.py). If the database isn't reachable it still writes pluto_lots_geo.csv (every PLUTO
lot) so the lookup can be built later.

USAGE (from the scripts folder, inside the activated venv):
    python pluto_geocode.py
Optional: set SOCRATA_APP_TOKEN in .env to avoid unauthenticated rate limits.
If the download is interrupted, just re-run it: finished pages are cached in data/raw/pluto_pages
and skipped. Use `python pluto_geocode.py --refresh` to discard the cache and re-download.
Needs only pandas, plus psycopg2 and python-dotenv (already used by the other scripts).
"""

import gzip
import http.client
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "processed"

DATASET_ID = "64uk-42ks"
DATASET_URL = f"https://data.cityofnewyork.us/resource/{DATASET_ID}.json"
PAGE_SIZE = 20000          # smaller pages: each request is quick, progress is visible, a retry is cheap
PAUSE_SECONDS = 0.5
CONNECT_TIMEOUT = 20       # a stuck TCP/TLS handshake is abandoned quickly; a fresh try usually works
READ_TIMEOUT = 60          # seconds of silence mid-response before the request is abandoned and retried
MAX_ATTEMPTS = 5
# Every downloaded page is saved here, so an interrupted or failed run resumes where it stopped.
# Delete the folder (or run with --refresh) to force a fresh download of the current PLUTO version.
CACHE_DIR = ROOT / "data" / "raw" / "pluto_pages"

# PLUTO has a small share of records with coordinates outside the city; drop anything outside
# a box slightly wider than NYC rather than plotting it in the ocean or New Jersey.
LAT_MIN, LAT_MAX = 40.45, 40.95
LON_MIN, LON_MAX = -74.30, -73.65


def normalize_bbl(raw):
    """PLUTO bbl ('1000477501' or '1000477501.00000000') -> 'B-BBBB-LL' unpadded. None if invalid."""
    try:
        n = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    s = f"{n:010d}"
    if len(s) != 10 or s[0] not in "12345":
        return None
    return f"{s[0]}-{int(s[1:6])}-{int(s[6:])}"


def block_key(bbl):
    """'4-1457-31' -> '4-1457' (borough+block, ignoring lot)."""
    return bbl.rsplit("-", 1)[0]


_conn = None  # one keep-alive connection reused across pages: no new TLS handshake per page


def _reset_conn():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None


GEO_SELECT = "bbl,latitude,longitude"
GEO_WHERE = "latitude IS NOT NULL AND longitude IS NOT NULL"


def fetch_page(offset, select=GEO_SELECT, where=GEO_WHERE):
    """One page of PLUTO. The defaults fetch coordinates; pluto_attributes.py passes its own
    select/where so both scripts share one connection-handling and retry implementation."""
    global _conn
    parsed = urllib.parse.urlparse(DATASET_URL)
    params = {"$select": select, "$order": "bbl", "$limit": PAGE_SIZE, "$offset": offset}
    if where:
        params["$where"] = where
    target = (parsed.path or "/") + "?" + urllib.parse.urlencode(params)
    headers = {"Accept-Encoding": "gzip"}  # JSON compresses very well; matters on a slow link
    token = os.environ.get("SOCRATA_APP_TOKEN")
    if token:
        headers["X-App-Token"] = token

    attempt = 0
    while True:
        reused = _conn is not None and _conn.sock is not None
        try:
            if _conn is None:
                cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                _conn = cls(parsed.netloc, timeout=CONNECT_TIMEOUT)
            if _conn.sock is None:
                _conn.timeout = CONNECT_TIMEOUT
                _conn.connect()
                _conn.sock.settimeout(READ_TIMEOUT)
            _conn.request("GET", target, headers=headers)
            resp = _conn.getresponse()
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} {resp.reason}")
            if resp.getheader("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # stalls, resets, 5xx: reconnect and retry the same page
            _reset_conn()
            if reused:
                continue  # a keep-alive connection the server dropped: reconnect now, not a real failure
            attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise
            wait = 2 ** attempt
            print(f"  attempt {attempt}/{MAX_ATTEMPTS} failed at row {offset:,} ({exc}); "
                  f"reconnecting in {wait}s")
            time.sleep(wait)


def fetch_all_pluto(refresh=False, select=GEO_SELECT, where=GEO_WHERE, cache_tag=""):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if refresh:
        for f in CACHE_DIR.glob(f"{cache_tag}page_*.json"):
            f.unlink()
    rows, offset = [], 0
    while True:
        cache_file = CACHE_DIR / f"{cache_tag}page_{PAGE_SIZE}_{offset}.json"
        if cache_file.exists():
            page, source = json.loads(cache_file.read_text(encoding="utf-8")), "cached"
        else:
            print(f"  requesting rows {offset:,}+ (gives up and retries by itself if silent for {READ_TIMEOUT}s) ...")
            page, source = fetch_page(offset, select, where), "downloaded"
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(page), encoding="utf-8")
            os.replace(tmp, cache_file)  # atomic: a Ctrl+C can never leave a half-written page
        rows.extend(page)
        print(f"  {source}: {len(rows):,} PLUTO rows so far")
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        if source == "downloaded":
            time.sleep(PAUSE_SECONDS)
    return rows


def build_lot_table(rows):
    """Raw Socrata rows -> DataFrame[bbl, lat, lon], normalized, bounds-checked, de-duplicated."""
    df = pd.DataFrame(rows, columns=["bbl", "latitude", "longitude"])
    df["bbl"] = df["bbl"].map(normalize_bbl)
    df["lat"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["lon"] = pd.to_numeric(df["longitude"], errors="coerce")
    n_raw = len(df)
    df = df.dropna(subset=["bbl", "lat", "lon"])
    in_box = df["lat"].between(LAT_MIN, LAT_MAX) & df["lon"].between(LON_MIN, LON_MAX)
    n_out = int((~in_box).sum())
    df = df[in_box].drop_duplicates("bbl")
    print(f"  {n_raw:,} raw -> {len(df):,} usable lots ({n_out:,} outside NYC bounds dropped)")
    return df[["bbl", "lat", "lon"]]


def build_block_centroids(lots):
    keys = lots["bbl"].map(block_key)
    return lots.assign(_k=keys).groupby("_k")[["lat", "lon"]].mean().round(6)


def resolve(bbls, lots, centroids):
    """Resolve each project BBL: exact lot, else block centroid, else unmapped (omitted)."""
    lot_idx = lots.set_index("bbl")
    out = []
    for bbl in bbls:
        if bbl in lot_idx.index:
            r = lot_idx.loc[bbl]
            out.append({"bbl": bbl, "lat": float(r["lat"]), "lon": float(r["lon"]), "precision": "lot"})
        elif block_key(bbl) in centroids.index:
            r = centroids.loc[block_key(bbl)]
            out.append({"bbl": bbl, "lat": float(r["lat"]), "lon": float(r["lon"]), "precision": "block"})
    return pd.DataFrame(out, columns=["bbl", "lat", "lon", "precision"])


def project_bbls():
    """BBLs from the properties table, or None if the database can't be reached."""
    try:
        import psycopg2
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=ROOT / ".env", override=True)
        conn = psycopg2.connect(
            host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ.get("PGDATABASE", "phantom_equity"),
            user=os.environ.get("PGUSER", "postgres"), password=os.environ.get("PGPASSWORD", ""),
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT bbl FROM properties")
            bbls = [r[0] for r in cur.fetchall()]
        conn.close()
        return bbls
    except Exception as exc:
        print(f"Could not read project BBLs from Postgres ({exc}).")
        return None


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading PLUTO lat/lon (dataset 64uk-42ks)...")
    lots = build_lot_table(fetch_all_pluto(refresh="--refresh" in sys.argv))
    lots.to_csv(OUT_DIR / "pluto_lots_geo.csv", index=False)
    centroids = build_block_centroids(lots)

    bbls = project_bbls()
    if bbls is None:
        print("Wrote pluto_lots_geo.csv only. Fix the database connection and re-run for property_geo.csv.")
        return
    geo = resolve(bbls, lots, centroids)
    geo.to_csv(OUT_DIR / "property_geo.csv", index=False)

    n = len(bbls)
    by = geo["precision"].value_counts()
    n_lot, n_block = int(by.get("lot", 0)), int(by.get("block", 0))
    print(f"\nProject properties: {n:,}")
    print(f"  located at exact lot : {n_lot:,} ({n_lot / n:.1%})")
    print(f"  located at block only: {n_block:,} ({n_block / n:.1%})  <- condo units and other non-PLUTO lots")
    print(f"  unmapped             : {n - len(geo):,} ({(n - len(geo)) / n:.1%})")
    print(f"Wrote {OUT_DIR / 'property_geo.csv'}")


if __name__ == "__main__":
    main()
