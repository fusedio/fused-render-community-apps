"""Live polyfill backend for basics/hierarchy.html (draw -> hexagonify).

Same dual-backend pattern as ../h3_ingest.py (kept standalone on purpose:
the deploy bundler only ships files beside the page). Prefers the pure-python
`h3` package when importable (hosted serve runtime); the local app's bundled
interpreter has no python h3, so local runs use DuckDB's community h3
extension — same reference H3 library underneath.

  actions
  -------
  env                     -> runtime diagnostics
  polyfill  ring= res=    -> covering H3 cells for a drawn polygon:
                             per-cell {id, boundary}, exact total hex area,
                             the polygon's own area, and coverage ratio.
  tree      cells=[hex ids]  (or lat= lng= res= to bootstrap a root)
                          -> batch descent data for hierarchy_zoom.html:
                             per cell {id, res, pentagon, lat, lng,
                             boundary [[lng,lat]...], children [{id,
                             pentagon, boundary}]}. One call per level:
                             the page prefetches all visible cells' children
                             before the user clicks them.
  node      lat= lng= res=  (or cell=)
                          -> the parent/children mismatch anywhere on Earth:
                             parent + children boundaries projected to local
                             meters (azimuthal equidistant around the cell
                             center, pole/antimeridian safe) plus real
                             leak/gap/area/twist stats computed by convex
                             clipping (pure python, no shapely needed).

`ring` is a list of [lng, lat] vertices (open or closed, either works).
Areas: hex side is the exact spherical cell area from the H3 library;
polygon side is a shoelace in a local equirectangular projection (accurate
to ~1e-5 at the few-hundred-meter scale the page draws at).
"""

import json
import time

_con = None
_use_py = None
_h3m = None


def _duck():
    """duckdb connection with the community h3 extension loaded."""
    global _con
    if _con is None:
        import duckdb
        con = duckdb.connect()
        try:
            # sandboxes may have no $HOME; duckdb needs one for extensions
            con.sql("SET home_directory='/tmp';")
        except Exception:
            pass
        try:
            con.sql("LOAD h3;")
        except Exception:
            con.sql("INSTALL h3 FROM community; LOAD h3;")
        _con = con
    return _con


def _h3py():
    global _use_py
    if _use_py is None:
        try:
            import h3  # noqa: F401
            _use_py = True
        except Exception:
            _duck()
            _use_py = False
    return _use_py


def _h3():
    global _h3m
    if _h3m is None:
        try:
            import h3.api.numpy_int as m
        except Exception:
            import h3.api.basic_int as m
        _h3m = m
    return _h3m


def _parse_wkt_ring(wkt):
    pts = wkt[wkt.index("((") + 2: wkt.index("))")].split(", ")
    ring = [[round(float(x), 6), round(float(y), 6)]
            for x, y in (p.split(" ") for p in pts)]
    if len(ring) > 1 and ring[0] == ring[-1]:  # WKT closes the ring; we keep it open
        ring.pop()
    return ring


def _ring_wkt(ring):
    return "POLYGON((" + ", ".join(f"{x} {y}" for x, y in ring) + "))"


def _poly_area_m2(ring):
    """Shoelace in a local equirectangular projection centered on the ring."""
    import math
    lat0 = sum(p[1] for p in ring) / len(ring)
    lng0 = sum(p[0] for p in ring) / len(ring)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110574.0
    pts = [((p[0] - lng0) * kx, (p[1] - lat0) * ky) for p in ring]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _polyfill(ring, res):
    """Covering cells for a [lng,lat] ring -> (ids, boundaries, hex_m2)."""
    if _h3py():
        import h3 as h3s  # string api is fine here: a few hundred cells max
        latlng = [(p[1], p[0]) for p in ring]
        cells = sorted(h3s.polygon_to_cells(h3s.LatLngPoly(latlng), int(res)))
        bounds = [
            [[round(lng, 6), round(lat, 6)]
             for lat, lng in h3s.cell_to_boundary(c)]
            for c in cells
        ]
        hex_m2 = sum(h3s.cell_area(c, unit="m^2") for c in cells)
        return [h3s.str_to_int(c) for c in cells], bounds, hex_m2
    con = _duck()
    wkt = _ring_wkt(ring)
    rows = con.sql(
        f"""SELECT x, h3_cell_to_boundary_wkt(x), h3_cell_area(x, 'm^2')
            FROM (SELECT UNNEST(h3_polygon_wkt_to_cells('{wkt}', {int(res)})) AS x)
            ORDER BY x"""
    ).fetchall()
    ids = [r[0] for r in rows]
    bounds = [_parse_wkt_ring(r[1]) for r in rows]
    hex_m2 = sum(r[2] for r in rows)
    return ids, bounds, hex_m2


EARTH_R = 6371007.180918475  # h3's authalic radius, meters


def _aeqd(lat0, lng0):
    """Azimuthal equidistant projection centered on (lat0, lng0): accurate for
    cell-sized extents anywhere on Earth, including poles and the antimeridian."""
    import math
    la0 = math.radians(lat0)
    lo0 = math.radians(lng0)
    sla0, cla0 = math.sin(la0), math.cos(la0)

    def fwd(lat, lng):
        la, lo = math.radians(lat), math.radians(lng)
        d = lo - lo0
        c = math.acos(max(-1.0, min(1.0, sla0*math.sin(la) + cla0*math.cos(la)*math.cos(d))))
        if c < 1e-12:
            return (0.0, 0.0)
        az = math.atan2(math.sin(d)*math.cos(la),
                        cla0*math.sin(la) - sla0*math.cos(la)*math.cos(d))
        return (EARTH_R*c*math.sin(az), EARTH_R*c*math.cos(az))
    return fwd


def _shoelace(pts):
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1*y2 - x2*y1
    return s / 2.0  # signed; CCW positive


def _clip_convex(subject, clip):
    """Sutherland-Hodgman: subject polygon clipped by a convex CCW polygon.
    Open rings of (x,y). H3 cells are convex to within float noise."""
    out = list(subject)
    n = len(clip)
    for i in range(n):
        if not out:
            return []
        ax, ay = clip[i]
        bx, by = clip[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        inside = [(ex*(p[1]-ay) - ey*(p[0]-ax)) >= 0 for p in out]
        nxt = []
        for j in range(len(out)):
            k = (j + 1) % len(out)
            p, q = out[j], out[k]
            if inside[j]:
                nxt.append(p)
            if inside[j] != inside[k]:
                dx, dy = q[0]-p[0], q[1]-p[1]
                denom = ey*dx - ex*dy
                if abs(denom) > 1e-12:
                    t = (ex*(p[1]-ay) - ey*(p[0]-ax)) / denom
                    nxt.append((p[0]+dx*t, p[1]+dy*t))
        out = nxt
    return out


def _boundaries_duck(cell_ids):
    values = ",".join(f"({c}::UBIGINT)" for c in cell_ids)
    rows = _duck().sql(
        f"SELECT h3_cell_to_boundary_wkt(x) FROM (VALUES {values}) t(x)"
    ).fetchall()
    return [_parse_wkt_ring(r[0]) for r in rows]


def _cell_geo(cell_int):
    """boundary [[lng,lat]...], children ids, child boundaries, is_pentagon,
    resolution, center lat, center lng — via whichever h3 backend exists."""
    if _h3py():
        h3m = _h3()
        b = [[lng, lat] for lat, lng in h3m.cell_to_boundary(cell_int)]
        kids = sorted(int(k) for k in h3m.cell_to_children(cell_int))
        kb = [[[lng, lat] for lat, lng in h3m.cell_to_boundary(k)] for k in kids]
        la, lo = h3m.cell_to_latlng(cell_int)
        return b, kids, kb, bool(h3m.is_pentagon(cell_int)), \
            int(h3m.get_resolution(cell_int)), la, lo
    con = _duck()
    row = con.sql(
        f"""SELECT h3_cell_to_boundary_wkt({cell_int}::UBIGINT),
                   h3_is_pentagon({cell_int}::UBIGINT),
                   h3_get_resolution({cell_int}::UBIGINT),
                   h3_cell_to_lat({cell_int}::UBIGINT),
                   h3_cell_to_lng({cell_int}::UBIGINT)"""
    ).fetchone()
    r = int(row[2])
    kids = sorted(x[0] for x in con.sql(
        f"SELECT UNNEST(h3_cell_to_children({cell_int}::UBIGINT, {r + 1}))"
    ).fetchall())
    return _parse_wkt_ring(row[0]), kids, _boundaries_duck(kids), \
        bool(row[1]), r, float(row[3]), float(row[4])


def _is_pent_many(cell_ints):
    if not cell_ints:
        return []
    if _h3py():
        h3m = _h3()
        return [bool(h3m.is_pentagon(c)) for c in cell_ints]
    values = ",".join(f"({c}::UBIGINT)" for c in cell_ints)
    rows = _duck().sql(
        f"SELECT h3_is_pentagon(x) FROM (VALUES {values}) t(x)"
    ).fetchall()
    return [bool(r[0]) for r in rows]


def _tree(cells=None, lat=None, lng=None, res=5):
    """Batch node data for the zoomable-descent page (hierarchy_zoom.html)."""
    if cells:
        if isinstance(cells, str):
            cells = json.loads(cells)
        cell_ints = [int(c, 16) for c in cells]
    elif _h3py():
        cell_ints = [int(_h3().latlng_to_cell(float(lat), float(lng), int(res)))]
    else:
        cell_ints = [_duck().sql(
            f"SELECT h3_latlng_to_cell({float(lat)}, {float(lng)}, {int(res)})"
        ).fetchone()[0]]
    rnd = lambda ring: [[round(x, 7), round(y, 7)] for x, y in ring]  # noqa: E731
    nodes = []
    for ci in cell_ints:
        b, kids, kb, pent, r, cla, clo = _cell_geo(ci)
        kp = _is_pent_many(kids)
        nodes.append({
            "id": format(ci, "x"), "res": r, "pentagon": pent,
            "lat": round(cla, 7), "lng": round(clo, 7),
            "boundary": rnd(b),
            "children": [{"id": format(k, "x"), "pentagon": p, "boundary": rnd(x)}
                         for k, p, x in zip(kids, kp, kb)],
        })
    return {"nodes": nodes}


def _orient_deg(pts, n):
    """Circular-mean vertex azimuth mod (360/n) around the centroid."""
    import math
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    s = c = 0.0
    for x, y in pts:
        a = math.atan2(y - cy, x - cx) * n
        s += math.sin(a)
        c += math.cos(a)
    return math.degrees(math.atan2(s, c)) / n


def _node(cell=None, lat=None, lng=None, res=8):
    if cell:
        cell_int = int(cell, 16)
    elif _h3py():
        cell_int = int(_h3().latlng_to_cell(float(lat), float(lng), int(res)))
    else:
        cell_int = _duck().sql(
            f"SELECT h3_latlng_to_cell({float(lat)}, {float(lng)}, {int(res)})"
        ).fetchone()[0]
    b_ll, kids, kb_ll, pent, res_of, cla, clo = _cell_geo(cell_int)
    fwd = _aeqd(cla, clo)
    par = [fwd(p[1], p[0]) for p in b_ll]
    if _shoelace(par) < 0:                     # clipping expects CCW
        par = par[::-1]
    kxy = [[fwd(p[1], p[0]) for p in kb] for kb in kb_ll]
    par_a = abs(_shoelace(par))
    tot = sum(abs(_shoelace(k)) for k in kxy)
    # Cells near pentagons are slightly NON-convex (7-10 vertices), which breaks
    # Sutherland-Hodgman as the clip polygon. H3 cells are star-shaped from the
    # centroid, so fan-triangulate the parent (an exact partition) and clip each
    # child against each triangle — every clip is then truly convex.
    pcx = sum(p[0] for p in par) / len(par)
    pcy = sum(p[1] for p in par) / len(par)
    tris = []
    for i in range(len(par)):
        t = [(pcx, pcy), par[i], par[(i + 1) % len(par)]]
        if _shoelace(t) < 0:
            t = t[::-1]
        tris.append(t)
    inside = 0.0
    for k in kxy:
        for t in tris:
            c = _clip_convex(k, t)
            if c:
                inside += abs(_shoelace(c))
    # twist vs the central child (nearest centroid) — 6-vertex cells only
    twist = None
    central = min(kxy, key=lambda k: (sum(p[0] for p in k) / len(k))**2 +
                                     (sum(p[1] for p in k) / len(k))**2)
    if not pent and len(par) == 6 and len(central) == 6:
        d = (_orient_deg(central, 6) - _orient_deg(par, 6)) % 60
        if d > 30:
            d -= 60
        twist = round(d, 2)
    rnd = lambda pts: [[round(x, 2), round(y, 2)] for x, y in pts]  # noqa: E731
    return {
        "cell": format(cell_int, "x"), "res": res_of,
        "lat": round(cla, 6), "lng": round(clo, 6),
        "pentagon": pent, "n_children": len(kids),
        "xy": rnd(par),
        "children": [{"id": format(k, "x"), "xy": rnd(x)}
                     for k, x in zip(kids, kxy)],
        "stats": {
            "parent_m2": round(par_a, 1),
            "children_sum_m2": round(tot, 1),
            "leak_pct": round((tot - inside) / tot * 100, 3),
            "gap_pct": round((par_a - inside) / par_a * 100, 3),
            "area_ratio": round(tot / par_a, 4),
            "twist_deg": twist,
        },
    }


def main(
    action: str = "polyfill",
    ring=None,
    res: int = 9,
    lat=None,
    lng=None,
    cell: str = "",
    cells=None,
):
    import platform
    t0 = time.monotonic()
    res = max(3, min(12, int(res)))

    out = {}
    if action == "env":
        import importlib
        out = {"python": platform.python_version()}
        for mod in ("duckdb", "h3"):
            try:
                m = importlib.import_module(mod)
                out[mod] = getattr(m, "__version__", "?")
            except Exception as e:
                out[mod] = f"IMPORT FAIL: {type(e).__name__}: {e}"
        out["h3_mode"] = "python-h3" if _h3py() else "duckdb-ext"

    elif action == "polyfill":
        if isinstance(ring, str):
            ring = json.loads(ring)
        if not ring or len(ring) < 3:
            raise ValueError("ring must have at least 3 [lng,lat] vertices")
        ring = [[float(x), float(y)] for x, y in ring]
        if ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        ids, bounds, hex_m2 = _polyfill(ring, res)
        poly_m2 = _poly_area_m2(ring[:-1])
        out = {
            "res": res,
            "n_cells": len(ids),
            "cells": [{"id": format(c, "x"), "boundary": b}
                      for c, b in zip(ids, bounds)],
            "hex_area_m2": round(hex_m2, 1),
            "poly_area_m2": round(poly_m2, 1),
            "coverage": round(hex_m2 / poly_m2, 4) if poly_m2 > 0 else None,
            "h3_mode": "python-h3" if _h3py() else "duckdb-ext",
        }

    elif action == "tree":
        out = _tree(cells=cells, lat=lat, lng=lng, res=res)
        out["h3_mode"] = "python-h3" if _h3py() else "duckdb-ext"

    elif action == "node":
        out = _node(cell=cell or None, lat=lat, lng=lng, res=res)
        out["h3_mode"] = "python-h3" if _h3py() else "duckdb-ext"

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
