"""Live H3 ingestion backend for the hex style lab.

Kept standalone on purpose: the deploy bundler only ships files beside the
page, so a runPython target must live under the page's own folder.

Runs on the FusedRender built-in executor (bundled interpreter: duckdb,
pandas, pyarrow — no PEP 723 there), so all H3 math goes through DuckDB's
community h3 extension. That is not a workaround: it is the same engine and
the same functions (h3_latlng_to_cell, h3_cell_to_parent) the real
world-scale Overture_Hexify pipeline uses.

Every number shown on the page is computed here, for real, on this machine:
the hex assignment (same centroid rule as the production pipeline), the
multi-resolution rollup, and the parquet byte counts (actual pyarrow files
written to memory, zstd-compressed, both sides measured the same way).

  actions
  -------
  meta                          -> dataset counts/areas + python version
  hexify  dataset= res=        -> per-hex {id, boundary, area, cnt} + totals + ms
  assign  dataset= res= n=     -> per-building centroid -> hex id (animation sample)
  sizes   dataset= res=        -> real parquet bytes: geometry table vs hex table
  diff    dataset= res=        -> per-hex before/after across the two releases
                                  (dataset = "assam" or "ams")
  raster_hexify dataset= res=  -> pixels -> hex cells with avg/min/max/cnt metrics,
                                  member pixel indices, and real parquet bytes
                                  (dataset = a RASTERS key, e.g. "fuji")

The local client passes data_dir (absolute path to the page's data/ folder,
derived from the page URL). On a hosted deploy no data_dir is sent; bundle v2
lands every bundled file at its real page-relative path under the project root,
so the data files are read beside this script (data/<name>).
"""

import json
import struct
import time

DATASETS = {
    "ams": "ams_2025-05-21-0.json",
    "assam_new": "assam_2025-05-21-0.json",
    "assam_old": "assam_2025-04-23-0.json",
    # extra places for the hex style lab (same release, same extraction UDF)
    "nyc": "nyc_2025-05-21-0.json",
    "venice": "venice_2025-05-21-0.json",
    "barcelona": "barcelona_2025-05-21-0.json",
    # previous release of the Amsterdam patch (for the compare tab)
    "ams_old": "ams_2025-04-23-0.json",
    # a Japanese neighborhood that densified between the two releases
    "japan_old": "kurashiki_2025-04-23-0.json",
    "japan_new": "kurashiki_2025-05-21-0.json",
}
# release pairs the diff action can compare (old, new)
DIFF_PAIRS = {"japan": ("japan_old", "japan_new"),
              "assam": ("assam_old", "assam_new"),
              "ams": ("ams_old", "ams")}
# real raster extracts (elevation grids) for the raster->hex tab
RASTERS = {"canyon": "elev_canyon_terrarium.json"}
_cache = {}
_con = None
_use_py = None


def _duck():
    """A duckdb connection with the community h3 extension loaded (cached
    on disk after the first call, so only the very first run needs network)."""
    global _con
    if _con is None:
        import duckdb
        con = duckdb.connect()
        try:
            # sandboxes (e.g. the hosted serve plane) may have no $HOME, and
            # duckdb refuses to resolve its extension dir without one
            con.sql("SET home_directory='/tmp';")
        except Exception:
            pass
        try:
            con.sql("LOAD h3;")
        except Exception:
            con.sql("INSTALL h3 FROM community; LOAD h3;")
        _con = con                      # only cache a connection that HAS h3
    return _con


def _h3py():
    """Prefer the pure-python `h3` package when it's importable (the hosted
    serve runtime ships it, and each hosted request is a fresh process — the
    duckdb route would pay a ~3.5 s `INSTALL h3 FROM community` network fetch
    every call). The local app's bundled interpreter has no python h3, so
    local runs keep the duckdb community extension. Same cells, same math —
    both wrap the reference H3 library."""
    global _use_py
    if _use_py is None:
        try:
            import h3  # noqa: F401
            _use_py = True
        except Exception:
            _duck()                     # no python h3 -> duckdb must work
            _use_py = False
    return _use_py


_h3m = None


def _h3():
    """The int-based h3 API: cells as plain ints, no hex-string round-trips
    (the string API costs ~2x per call, which matters at 16k pixels/req)."""
    global _h3m
    if _h3m is None:
        try:
            import h3.api.numpy_int as m
        except Exception:
            import h3.api.basic_int as m
        _h3m = m
    return _h3m


def _data_path(data_dir, fname):
    """Local runs pass an absolute data_dir. Hosted (bundle v2) every bundled file
    lands at its real page-relative path under the project root, so data/ sits
    beside this script — read it there. No asset_path: it anchors under an assets/
    prefix the fused-render bundle doesn't use."""
    if data_dir:
        return f"{data_dir}/{fname}"
    import os
    base = os.path.dirname(os.path.abspath(__file__)) \
        if "__file__" in globals() else os.getcwd()
    return os.path.join(base, "data", fname)


def _load(data_dir, dataset):
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    key = (data_dir, dataset)
    if key not in _cache:
        with open(_data_path(data_dir, DATASETS[dataset])) as f:
            _cache[key] = json.load(f)
    return _cache[key]


def _hexify(buildings, res):
    """Centroid rule, exactly like the world-scale pipeline: the whole
    building's area lands in the one hex that contains its middle point."""
    if not buildings:
        return {}
    if _h3py():
        h3m = _h3()
        r = int(res)
        cells = {}
        for b in buildings:
            c = int(h3m.latlng_to_cell(b["cy"], b["cx"], r))
            m = cells.setdefault(c, [0.0, 0])
            m[0] += b["area"]
            m[1] += 1
        return {c: cells[c] for c in sorted(cells)}
    import pandas as pd
    df = pd.DataFrame(
        {"cy": [b["cy"] for b in buildings],
         "cx": [b["cx"] for b in buildings],
         "area": [b["area"] for b in buildings]}
    )
    rows = _duck().sql(
        f"""SELECT h3_latlng_to_cell(cy, cx, {int(res)}) AS hex,
                   SUM(area) AS area, COUNT(1) AS cnt
            FROM df GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    return {r[0]: [r[1], r[2]] for r in rows}


def _parse_wkt(wkt):
    pts = wkt[wkt.index("((") + 2: wkt.index("))")].split(", ")
    return [[round(float(x), 6), round(float(y), 6)]
            for x, y in (p.split(" ") for p in pts)]


def _boundaries(cell_ids):
    """[[lng, lat], ...] per cell, one batched query (cells are trusted ints)."""
    if not cell_ids:
        return []
    if _h3py():
        h3m = _h3()
        return [
            [[round(lng, 6), round(lat, 6)]
             for lat, lng in h3m.cell_to_boundary(int(c))]
            for c in cell_ids
        ]
    values = ",".join(f"({c}::UBIGINT)" for c in cell_ids)
    rows = _duck().sql(
        f"SELECT h3_cell_to_boundary_wkt(x) FROM (VALUES {values}) t(x)"
    ).fetchall()
    return [_parse_wkt(r[0]) for r in rows]


def _boundary(cell):
    return _boundaries([cell])[0]


def _cells_out(cells):
    ids = list(cells)
    bounds = _boundaries(ids)
    return [
        {"id": format(c, "x"), "boundary": b,
         "area": round(cells[c][0]), "cnt": int(cells[c][1])}
        for c, b in zip(ids, bounds)
    ]


def _load_raster(data_dir, dataset):
    if dataset not in RASTERS:
        raise ValueError(f"unknown raster {dataset!r}")
    key = (data_dir, "raster:" + dataset)
    if key not in _cache:
        with open(_data_path(data_dir, RASTERS[dataset])) as f:
            _cache[key] = json.load(f)
    return _cache[key]


def _wkb_polygon(ring):
    """Minimal WKB so the geometry side of the size comparison is a fair,
    standard binary encoding (what real GeoParquet stores)."""
    out = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(ring))
    for x, y in ring:
        out += struct.pack("<dd", x, y)
    return out


def _parquet_bytes(table):
    import io

    import pyarrow.parquet as pq
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getbuffer().nbytes


def main(
    action: str = "meta",
    dataset: str = "ams",
    res: int = 9,
    n: int = 400,
    data_dir: str = "",
):
    import platform
    t0 = time.monotonic()
    res = max(3, min(12, int(res)))
    n = int(n)

    out = {}
    if action == "env":
        # diagnostics: what does this runtime actually have? (the hosted serve
        # plane hides tracebacks, so failures are opaque without this)
        import importlib
        out = {"python": platform.python_version()}
        for mod in ("duckdb", "h3", "pandas", "pyarrow"):
            try:
                m = importlib.import_module(mod)
                out[mod] = getattr(m, "__version__", "?")
            except Exception as e:
                out[mod] = f"IMPORT FAIL: {type(e).__name__}: {e}"
        try:
            _duck()
            out["duckdb_h3_ext"] = "ok"
        except Exception as e:
            out["duckdb_h3_ext"] = f"FAIL: {type(e).__name__}: {e}"
        try:
            out["h3_mode"] = "python-h3" if _h3py() else "duckdb-ext"
        except Exception as e:
            out["h3_mode"] = f"NEITHER: {type(e).__name__}: {e}"

    elif action == "meta":
        for name in DATASETS:
            d = _load(data_dir, name)
            out[name] = {
                "n": d["n"],
                "bbox": d["bbox"],
                "release": d["release"],
                "total_area_m2": round(sum(b["area"] for b in d["buildings"])),
            }

    elif action == "hexify":
        d = _load(data_dir, dataset)
        cells = _hexify(d["buildings"], res)
        out = {
            "res": res,
            "cells": _cells_out(cells),
            "n_cells": len(cells),
            "n_buildings": d["n"],
            "total_area_m2": round(sum(a for a, _ in cells.values())),
        }

    elif action == "scene":
        # everything the buildings tab needs in ONE round-trip (each runPython
        # call costs ~1s of process spawn locally and more hosted, so the old
        # assign-then-hexify pair doubled the first paint time).
        d = _load(data_dir, dataset)
        bld = d["buildings"]
        cells = _hexify(bld, res)
        if _h3py():
            h3m = _h3()
            rr = int(res)
            acells = [int(h3m.latlng_to_cell(b["cy"], b["cx"], rr)) for b in bld]
        else:
            import pandas as pd
            df = pd.DataFrame({"cy": [b["cy"] for b in bld],
                               "cx": [b["cx"] for b in bld]})
            acells = [r[0] for r in _duck().sql(
                f"SELECT h3_latlng_to_cell(cy, cx, {int(res)}) FROM df"
            ).fetchall()]
        out = {
            "res": res,
            "cells": _cells_out(cells),
            "n_cells": len(cells),
            "n_buildings": d["n"],
            "total_area_m2": round(sum(a for a, _ in cells.values())),
            # cell id per building, aligned with the extract's building order
            # (the client already has cx/cy/area from the data file)
            "assign_cells": [format(c, "x") for c in acells],
        }

    elif action == "assign":
        d = _load(data_dir, dataset)
        step = max(1, len(d["buildings"]) // n)
        sample = d["buildings"][::step][:n]
        cells = []
        if sample and _h3py():
            h3m = _h3()
            cells = [int(h3m.latlng_to_cell(b["cy"], b["cx"], res))
                     for b in sample]
        elif sample:
            import pandas as pd
            df = pd.DataFrame({"cy": [b["cy"] for b in sample],
                               "cx": [b["cx"] for b in sample]})
            cells = [r[0] for r in _duck().sql(
                f"SELECT h3_latlng_to_cell(cy, cx, {res}) FROM df"
            ).fetchall()]
        out = {
            "res": res,
            "assignments": [
                {"cx": b["cx"], "cy": b["cy"], "area": b["area"], "cell": format(cell, "x")}
                for b, cell in zip(sample, cells)
            ],
        }

    elif action == "sizes":
        import pyarrow as pa
        d = _load(data_dir, dataset)
        geom = pa.table({
            "geometry": pa.array([_wkb_polygon(b["ring"]) for b in d["buildings"]], pa.binary()),
            "area_m2": pa.array([b["area"] for b in d["buildings"]], pa.float64()),
        })
        cells = _hexify(d["buildings"], res)
        ids = sorted(cells)  # sorted by hex id = how the real files are filed
        hexes = pa.table({
            "hex": pa.array(ids, pa.uint64()),
            "area_m2": pa.array([int(cells[c][0]) for c in ids], pa.uint64()),
            "cnt": pa.array([int(cells[c][1]) for c in ids], pa.uint32()),
        })
        out = {
            "res": res,
            "n_buildings": d["n"],
            "n_cells": len(ids),
            "geometry_parquet_bytes": _parquet_bytes(geom),
            "hex_parquet_bytes": _parquet_bytes(hexes),
        }

    elif action == "raster_hexify":
        import pyarrow as pa
        r = _load_raster(data_dir, dataset)
        nx, ny = r["nx"], r["ny"]
        x0, y0, x1, y1 = r["bbox"]
        # pixel centers, row-major from the north row (matches the extract)
        lngs = [x0 + (i % nx + 0.5) / nx * (x1 - x0) for i in range(nx * ny)]
        lats = [y1 - (i // nx + 0.5) / ny * (y1 - y0) for i in range(nx * ny)]
        if _h3py():
            h3m = _h3()
            rr = int(res)
            agg = {}
            for i, (la, lo, e2) in enumerate(zip(lats, lngs, r["elev"])):
                c = int(h3m.latlng_to_cell(la, lo, rr))
                m = agg.setdefault(c, [0, 0, 10**9, -(10**9), []])
                m[0] += e2
                m[1] += 1
                m[2] = min(m[2], e2)
                m[3] = max(m[3], e2)
                m[4].append(i)
            rows = [(c, round(m[0] / m[1]), m[2], m[3], m[1], m[4])
                    for c, m in sorted(agg.items())]
        else:
            import pandas as pd
            df = pd.DataFrame({"cy": lats, "cx": lngs, "elev": r["elev"],
                               "idx": range(nx * ny)})
            rows = _duck().sql(
                f"""SELECT h3_latlng_to_cell(cy, cx, {int(res)}) AS hex,
                           ROUND(AVG(elev)) AS avg, MIN(elev) AS mn, MAX(elev) AS mx,
                           COUNT(1) AS cnt, LIST(idx ORDER BY idx) AS px
                    FROM df GROUP BY 1 ORDER BY 1"""
            ).fetchall()
        ids = [rw[0] for rw in rows]
        bounds = _boundaries(ids)
        # real parquet bytes, both sides zstd: the raster grid (int16 values,
        # what a raster actually stores per pixel) vs the hex metric table
        grid = pa.table({"elev": pa.array(r["elev"], pa.int16())})
        hexes = pa.table({
            "hex": pa.array(ids, pa.uint64()),
            "elev_avg": pa.array([int(rw[1]) for rw in rows], pa.int16()),
            "elev_min": pa.array([int(rw[2]) for rw in rows], pa.int16()),
            "elev_max": pa.array([int(rw[3]) for rw in rows], pa.int16()),
            "cnt": pa.array([int(rw[4]) for rw in rows], pa.uint16()),
        })
        out = {
            "res": res, "nx": nx, "ny": ny, "n_pixels": nx * ny,
            "n_cells": len(ids),
            "raster_parquet_bytes": _parquet_bytes(grid),
            "hex_parquet_bytes": _parquet_bytes(hexes),
            "cells": [
                {"id": format(c, "x"), "boundary": bd,
                 "avg": int(rw[1]), "min": int(rw[2]), "max": int(rw[3]),
                 "cnt": int(rw[4]), "px": [int(i) for i in rw[5]]}
                for c, bd, rw in zip(ids, bounds, rows)
            ],
        }

    elif action == "diff":
        pair = DIFF_PAIRS.get(dataset if dataset in DIFF_PAIRS else "assam")
        a = _hexify(_load(data_dir, pair[0])["buildings"], res)
        b = _hexify(_load(data_dir, pair[1])["buildings"], res)
        merged = {}
        for c, (area, cnt) in a.items():
            merged[c] = [area, cnt, 0.0, 0]
        for c, (area, cnt) in b.items():
            m = merged.setdefault(c, [0.0, 0, 0.0, 0])
            m[2], m[3] = area, cnt
        ids = list(merged)
        bounds = _boundaries(ids)
        out = {
            "res": res,
            "cells": [
                {"id": format(c, "x"), "boundary": bd,
                 "area_old": round(merged[c][0]), "cnt_old": int(merged[c][1]),
                 "area_new": round(merged[c][2]), "cnt_new": int(merged[c][3])}
                for c, bd in zip(ids, bounds)
            ],
        }

    else:
        raise ValueError(f"unknown action {action!r}")

    out["ms"] = round((time.monotonic() - t0) * 1000)
    out["python"] = platform.python_version()
    return out


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except Exception:
    pass
