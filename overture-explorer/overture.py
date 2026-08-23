"""runPython target for the Overture map explorer view.

Actions (dispatched via the `action` param):
  geocode : place name -> lat/lon + suggested bbox (Nominatim, cached)
  query   : bbox + theme/category/name-filter -> GeoJSON features + stats (DuckDB
            over Overture's public S3 parquet, file-pruned via the pinned release's
            STAC collections.parquet)
  story   : bbox -> cafes + cycleways, "% of cafes within 100 m of a bike path"

Everything expensive is disk-cached under ./.cache (fresh subprocess per call,
so an in-memory cache would never hit). Keys: (action args incl. rounded bbox).
"""

# /// script
# dependencies = ["duckdb", "numpy", "pandas<3", "pyproj", "requests", "shapely"]
# ///

import functools
import hashlib
import json
import math
import os
import sys

# Preferred Overture release. Overture serves only a rolling window of releases
# from its STAC catalog, so a hard pin eventually 404s; _resolve_release() falls
# back to the newest release the catalog still serves when this one is gone.
OVERTURE_RELEASE = "2026-08-19.0"
_STAC = "https://stac.overturemaps.org"
_CATALOG = f"{_STAC}/catalog.json"

_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))
_CACHE_DIR = os.path.join(_HERE, ".cache")

# theme key -> (overture theme dir, type/collection name)
_THEMES = {
    "places": ("places", "place"),
    "buildings": ("buildings", "building"),
    "transportation": ("transportation", "segment"),
}

# Max bbox span (degrees) per theme — keeps scans inside the 30 s budget.
_MAX_SPAN = {"places": 0.12, "buildings": 0.025, "transportation": 0.12}

# Feature caps sent to the browser.
_LIMITS = {"places": 4000, "buildings": 2500, "transportation": 4000}


def disk_cache(fn):
    """Memoize a JSON-returning function to disk, keyed by its args."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key_src = json.dumps([fn.__name__, args, kwargs], sort_keys=True, default=str)
        key = hashlib.sha256(key_src.encode()).hexdigest()[:16]
        path = os.path.join(_CACHE_DIR, f"{fn.__name__}_{key}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        result = fn(*args, **kwargs)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh)
        os.replace(tmp, path)
        return result

    return wrapper


# ── Release resolution ──────────────────────────────────────────────────────


def _collections_url(release):
    return f"{_STAC}/{release}/collections.parquet"


def _url_ok(url):
    import requests

    try:
        return requests.head(url, timeout=10, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def _available_releases():
    """Releases the STAC catalog currently serves, newest first."""
    import requests

    cat = requests.get(_CATALOG, timeout=10).json()
    rels = [link["href"].rstrip("/").split("/")[-2]
            for link in cat.get("links", []) if link.get("rel") == "child"]
    latest = cat.get("latest")
    if latest:
        rels = [latest] + [r for r in rels if r != latest]
    return rels


@functools.lru_cache(maxsize=1)
def _resolve_release():
    """A release whose collections.parquet is reachable: the pinned one if it
    still exists, else the newest the catalog serves. Cached per process."""
    if _url_ok(_collections_url(OVERTURE_RELEASE)):
        return OVERTURE_RELEASE
    for rel in _available_releases():
        if rel != OVERTURE_RELEASE and _url_ok(_collections_url(rel)):
            return rel
    raise RuntimeError("no reachable Overture STAC release found")


# ── DuckDB ────────────────────────────────────────────────────────────────


def _connect():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET enable_progress_bar=false;")
    return con


def _sq(s: str) -> str:
    """Escape a value for a single-quoted SQL literal."""
    return s.replace("'", "''")


@disk_cache
def _matching_files(type_name, release, w, s, e, n):
    """S3 parquet files of the given release whose bbox intersects ours."""
    con = _connect()
    rows = con.execute(
        f"""
        SELECT assets.aws.alternate.s3.href AS href
        FROM '{_collections_url(release)}'
        WHERE collection = '{type_name}'
          AND bbox.xmax >= {w} AND bbox.xmin <= {e}
          AND bbox.ymax >= {s} AND bbox.ymin <= {n}
        """
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def _clamp_bbox(theme, w, s, e, n):
    """Clamp bbox span to the per-theme cap, keeping the center. Returns
    (w, s, e, n, clamped?)."""
    max_span = _MAX_SPAN[theme]
    cx, cy = (w + e) / 2, (s + n) / 2
    dw, dh = e - w, n - s
    clamped = False
    if dw > max_span:
        w, e, clamped = cx - max_span / 2, cx + max_span / 2, True
    if dh > max_span:
        s, n, clamped = cy - max_span / 2, cy + max_span / 2, True
    return w, s, e, n, clamped


_SELECTS = {
    "places": "id, names.primary AS name, categories.primary AS category, "
    "confidence, NULL AS subtype, NULL AS class",
    "buildings": "id, names.primary AS name, NULL AS category, "
    "NULL AS confidence, subtype, class",
    "transportation": "id, names.primary AS name, NULL AS category, "
    "NULL AS confidence, subtype, class",
}


def _where(theme, category, search, w, s, e, n):
    filters = [
        f"bbox.xmin >= {w}", f"bbox.xmax <= {e}",
        f"bbox.ymin >= {s}", f"bbox.ymax <= {n}",
    ]
    if category:
        col = "categories.primary" if theme == "places" else "class"
        if category == "cafe" and theme == "places":
            filters.append("categories.primary IN ('cafe', 'coffee_shop')")
        else:
            filters.append(f"{col} = '{_sq(category)}'")
    if search:
        filters.append(f"names.primary ILIKE '%{_sq(search)}%'")
    return " AND ".join(filters)


@disk_cache
def _count(theme, category, search, release, w, s, e, n):
    """Exact feature count for the filter (no geometry decode) — its own
    runPython call so count + fetch each stay inside the 30 s budget."""
    files = _matching_files(_THEMES[theme][1], release, w, s, e, n)
    if not files:
        return {"total": 0}
    file_list = ", ".join(f"'{f}'" for f in files)
    total = _connect().execute(
        f"SELECT count(*) FROM read_parquet([{file_list}]) "
        f"WHERE {_where(theme, category, search, w, s, e, n)}"
    ).fetchone()[0]
    return {"total": int(total)}


@disk_cache
def _fetch(theme, category, search, release, w, s, e, n, k):
    """Fetch features, uniformly thinned by hash(id) % k when k > 1 — avoids
    the spatial bias of a bare LIMIT and the full-sort cost of ORDER BY."""
    files = _matching_files(_THEMES[theme][1], release, w, s, e, n)
    if not files:
        return {"features": []}
    file_list = ", ".join(f"'{f}'" for f in files)
    where = _where(theme, category, search, w, s, e, n)
    if k > 1:
        where += f" AND hash(id) % {int(k)} = 0"
    q = f"""
        SELECT {_SELECTS[theme]}, ST_AsGeoJSON(geometry) AS gj
        FROM read_parquet([{file_list}])
        WHERE {where}
        LIMIT {_LIMITS[theme]}
    """
    rows = _connect().execute(q).fetchall()
    feats = []
    for rid, name, cat, conf, subtype, klass, gj in rows:
        props = {"id": rid, "name": name, "category": cat, "subtype": subtype,
                 "class": klass}
        if conf is not None:
            props["confidence"] = round(float(conf), 3)
        feats.append({"type": "Feature", "geometry": json.loads(gj),
                      "properties": props})
    return {"features": feats}


def _run_query(theme, category, search, release, w, s, e, n):
    """count + fetch in one process (used by the story action, where the
    layers are expected to be disk-cache warm from prior count/fetch calls)."""
    total = _count(theme, category, search, release, w, s, e, n)["total"]
    k = max(1, -(-total // _LIMITS[theme]))
    feats = _fetch(theme, category, search, release, w, s, e, n, k)["features"]
    return {"features": feats, "capped": total > len(feats), "total": total}


# ── Actions ───────────────────────────────────────────────────────────────


@disk_cache
def _geocode(place):
    import requests

    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": "fused-render-overture-explorer"},
        timeout=10,
    )
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return {"ok": False, "error": f"Could not geocode {place!r}"}
    h = hits[0]
    s, n, w, e = (float(x) for x in h["boundingbox"])
    return {
        "ok": True,
        "lat": float(h["lat"]),
        "lon": float(h["lon"]),
        "display_name": h.get("display_name", place),
        "bbox": [w, s, e, n],
    }


def _norm_bbox(theme, w, s, e, n):
    """Normalize a viewport bbox to a CACHE-STABLE query bbox: snap the center
    to a 0.01° grid and always use the full per-theme span cap. Different
    window sizes / small pans over the same area then share one cache key
    (4-decimal rounding kept every resize cold: 16.5 s vs 0.6 s warm)."""
    w, s, e, n, clamped = _clamp_bbox(theme, w, s, e, n)
    cap = _MAX_SPAN[theme]
    cx = round(((w + e) / 2) / 0.01) * 0.01
    cy = round(((s + n) / 2) / 0.01) * 0.01
    w, e = round(cx - cap / 2, 4), round(cx + cap / 2, 4)
    s, n = round(cy - cap / 2, 4), round(cy + cap / 2, 4)
    return w, s, e, n, clamped


def _do_count(theme, category, search, w, s, e, n):
    w, s, e, n, clamped = _norm_bbox(theme, w, s, e, n)
    release = _resolve_release()
    total = _count(theme, category or "", search or "", release, w, s, e, n)["total"]
    k = max(1, -(-total // _LIMITS[theme]))
    print(f"count theme={theme} cat={category!r} search={search!r} "
          f"bbox=({w},{s},{e},{n}) -> total={total} k={k}")
    return {"ok": True, "total": total, "k": k, "clamped": clamped,
            "release": release}


def _do_fetch(theme, category, search, w, s, e, n, k):
    w, s, e, n, _ = _norm_bbox(theme, w, s, e, n)
    release = _resolve_release()
    feats = _fetch(theme, category or "", search or "", release, w, s, e, n, k)["features"]

    # Top categories / classes among returned features.
    counts = {}
    key = "category" if theme == "places" else "class"
    for f in feats:
        v = f["properties"].get(key) or f["properties"].get("subtype") or "(none)"
        counts[v] = counts.get(v, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]

    print(f"fetch theme={theme} cat={category!r} search={search!r} "
          f"bbox=({w},{s},{e},{n}) k={k} -> {len(feats)} feats")
    return {
        "ok": True,
        "release": release,
        "theme": theme,
        "count": len(feats),
        "bbox": [w, s, e, n],
        "top": [{"label": lbl, "n": v} for lbl, v in top],
        "geojson": {"type": "FeatureCollection", "features": feats},
    }


@disk_cache
def _story(release, w, s, e, n):
    from shapely.geometry import shape
    from shapely.ops import unary_union

    cafes = _run_query("places", "cafe", "", release, w, s, e, n)["features"]
    bikes = _run_query("transportation", "cycleway", "", release, w, s, e, n)["features"]
    near = 0
    if cafes and bikes:
        # Buffer cycleways by ~100 m in local-meter approximation.
        lat0 = (s + n) / 2
        m_per_deg = 111_320.0
        kx = math.cos(math.radians(lat0))

        def to_m(g):
            return _scale(shape(g), kx * m_per_deg, m_per_deg)

        buf = unary_union([to_m(b["geometry"]) for b in bikes]).buffer(100)
        flags = []
        for c in cafes:
            pt = to_m(c["geometry"])
            hit = buf.intersects(pt)
            flags.append(hit)
            near += hit
        for c, hit in zip(cafes, flags):
            c["properties"]["near_bike_path"] = bool(hit)
    pct = round(100.0 * near / len(cafes), 1) if cafes else 0.0
    print(f"story bbox=({w},{s},{e},{n}) cafes={len(cafes)} "
          f"cycleways={len(bikes)} near={near} ({pct}%)")
    return {
        "ok": True,
        "release": release,
        "cafes": {"type": "FeatureCollection", "features": cafes},
        "bikes": {"type": "FeatureCollection", "features": bikes},
        "n_cafes": len(cafes),
        "n_bikes": len(bikes),
        "n_near": int(near),
        "pct_near": pct,
    }


def _scale(geom, kx, ky):
    from shapely import affinity

    return affinity.scale(geom, xfact=kx, yfact=ky, origin=(0, 0))


def main(
    action: str = "query",
    place: str = "",
    theme: str = "places",
    category: str = "",
    search: str = "",
    west: float = -122.435,
    south: float = 37.765,
    east: float = -122.395,
    north: float = 37.795,
    k: int = 1,
) -> dict:
    if action == "geocode":
        return _geocode(place.strip())
    if action == "story":
        w, s, e, n, _ = _norm_bbox("places", west, south, east, north)
        return _story(_resolve_release(), w, s, e, n)
    if action in ("count", "fetch"):
        if theme not in _THEMES:
            raise ValueError(f"theme must be one of {sorted(_THEMES)}, got {theme!r}")
        args = (theme, category.strip(), search.strip(), west, south, east, north)
        return _do_count(*args) if action == "count" else _do_fetch(*args, k)
    raise ValueError(f"unknown action {action!r}")


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
