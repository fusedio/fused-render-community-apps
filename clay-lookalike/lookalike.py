"""Clay lookalike — data layer.

Fetches the Clay v1.5 NAIP embeddings that cover San Francisco (19 scene
parquet files published by Clay on Source Cooperative), caches them as
numpy arrays under .fused/cache/v1/, and answers similarity queries.

main(action=...) actions:
  status                     cache state
  fetch   scene=<name>       download + parse one scene into scenes/<name>.npz
  build                      concatenate scenes into emb.npy, bounds.bin, meta.json
  query   pos=1,2 neg=3 k=N  ranked [id, score] of the chips most like the positives
"""
import io
import json
import os
import re
import struct

import numpy as np

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(APP_DIR, ".fused", "cache", "v1")
BASE_URL = "https://data.source.coop/clay/clay-v1-5-naip-2/ca/2020/60cm/rgbir_cog/37122/"

# NAIP 2020 quarter quads 12-14 and 20-22 of the 37122 cell: San Francisco,
# the Golden Gate and the western edge of the bay.
SCENES = [
    "m_3712212_ne_10_060_20200524", "m_3712212_nw_10_060_20200524", "m_3712212_se_10_060_20200524",
    "m_3712213_ne_10_060_20200524", "m_3712213_nw_10_060_20200524", "m_3712213_se_10_060_20200524",
    "m_3712213_sw_10_060_20200524",
    "m_3712214_ne_10_060_20200524", "m_3712214_nw_10_060_20200524", "m_3712214_se_10_060_20200524",
    "m_3712214_sw_10_060_20200524",
    "m_3712220_ne_10_060_20200524",
    "m_3712221_ne_10_060_20200524", "m_3712221_nw_10_060_20200524", "m_3712221_se_10_060_20200524",
    "m_3712221_sw_10_060_20200524",
    "m_3712222_ne_10_060_20200524", "m_3712222_nw_10_060_20200524", "m_3712222_sw_10_060_20200524",
]

_SCENE_RE = re.compile(r"^m_(\d{2})(\d{3})(\d{2})_([ns])([ew])_")


def wkb_bounds(b: bytes) -> tuple:
    """(minx, miny, maxx, maxy) of a WKB polygon's outer ring."""
    bo = "<" if b[0] == 1 else ">"
    (typ,) = struct.unpack(bo + "I", b[1:5])
    if typ & 0xFF != 3:
        raise ValueError(f"not a polygon: wkb type {typ}")
    off = 5
    if typ & 0x20000000:  # EWKB with SRID
        off += 4
    (npts,) = struct.unpack(bo + "I", b[off + 4:off + 8])
    pts = struct.unpack(bo + "d" * (2 * npts), b[off + 8:off + 8 + 16 * npts])
    xs, ys = pts[0::2], pts[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def nominal_quarter(scene: str) -> tuple:
    """(minx, miny, maxx, maxy) of the quarter quad a NAIP scene name stands for."""
    m = _SCENE_RE.match(scene)
    if not m:
        raise ValueError(f"not a NAIP scene name: {scene}")
    lat_deg, lon_deg, quad = int(m.group(1)), int(m.group(2)), int(m.group(3))
    row, col = (quad - 1) // 8, (quad - 1) % 8
    x0 = -(lon_deg + 1) + col * 0.125
    y1 = lat_deg + 1 - row * 0.125
    x1, y0 = x0 + 0.125, y1 - 0.125
    if m.group(4) == "n":
        y0 = y1 - 0.0625
    else:
        y1 = y0 + 0.0625
    if m.group(5) == "e":
        x0 = x1 - 0.0625
    else:
        x1 = x0 + 0.0625
    return (x0, y0, x1, y1)


def score(emb: np.ndarray, pos: list, neg: list) -> np.ndarray:
    """Max cosine to any positive minus max cosine to any negative (rows are unit vectors)."""
    s = (emb @ emb[pos].T).max(axis=1)
    if neg:
        s = s - (emb @ emb[neg].T).max(axis=1)
    return s.astype(np.float32)


def _download(url: str) -> bytes:
    import requests

    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def _parse(data: bytes):
    """Parquet bytes -> (unit embeddings float32 N×D, bounds float32 N×4)."""
    import pyarrow.parquet as pq

    t = pq.read_table(io.BytesIO(data))
    col = t.column("embeddings").combine_chunks()
    n = len(col)
    flat = col.flatten().to_numpy(zero_copy_only=False).astype(np.float32)
    emb = flat.reshape(n, -1)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    bounds = np.array([wkb_bounds(g) for g in t.column("geometry").to_pylist()], np.float32)
    return emb, bounds


def _scene_path(scene: str) -> str:
    if scene not in SCENES:
        raise ValueError(f"unknown scene: {scene}")
    return os.path.join(CACHE, "scenes", scene + ".npz")


def _load_scene(scene: str):
    """(emb, bounds) from the cache, or None when missing or unreadable.

    Any failure to read the file counts as a miss (truncated or empty zip,
    bad pickle, wrong keys or shapes); `fetch` then downloads it again.
    """
    path = _scene_path(scene)
    try:
        with np.load(path) as z:
            emb, bounds = z["emb"], z["bounds"]
        if emb.ndim != 2 or bounds.shape != (len(emb), 4):
            return None
        return emb, bounds
    except Exception:
        return None


def _replace(path: str, write):
    """Atomic write: write(tmp_path) then os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp" + os.path.splitext(path)[1]
    write(tmp)
    os.replace(tmp, path)


def _ids(s: str, n: int) -> list:
    out = []
    for part in s.split(","):
        part = part.strip()
        if part.isdigit() and int(part) < n:
            out.append(int(part))
    return sorted(set(out))


def _status() -> dict:
    missing = [s for s in SCENES if _load_scene(s) is None]
    ready = all(os.path.exists(os.path.join(CACHE, f)) for f in ("emb.npy", "bounds.bin", "meta.json"))
    n = None
    if ready:
        try:
            with open(os.path.join(CACHE, "meta.json")) as f:
                n = int(json.load(f)["n"])
        except Exception:  # unreadable index metadata: rebuild on the next fetch
            ready, n = False, None
    return {"ready": ready, "cached": len(SCENES) - len(missing), "total": len(SCENES),
            "missing": missing, "n": n}


def _fetch(scene: str) -> dict:
    path = _scene_path(scene)
    cached = _load_scene(scene)
    if cached is not None:
        return {"scene": scene, "n": int(len(cached[0])), "downloaded": False}
    emb, bounds = _parse(_download(BASE_URL + scene + ".parquet"))
    _replace(path, lambda p: np.savez(p, emb=emb, bounds=bounds))
    return {"scene": scene, "n": int(len(emb)), "downloaded": True}


def _build() -> dict:
    embs, bnds, scenes = [], [], []
    for scene in SCENES:
        loaded = _load_scene(scene)
        if loaded is None:
            raise RuntimeError(f"scene not cached yet: {scene}")
        emb, bounds = loaded
        x0, y0, x1, y1 = nominal_quarter(scene)
        cx, cy = (bounds[:, 0] + bounds[:, 2]) / 2, (bounds[:, 1] + bounds[:, 3]) / 2
        keep = (cx >= x0) & (cx < x1) & (cy >= y0) & (cy < y1)
        embs.append(emb[keep])
        bnds.append(bounds[keep])
        scenes.append({"name": scene, "n": int(keep.sum())})
    emb = np.concatenate(embs).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    bounds = np.concatenate(bnds).astype(np.float32)
    _replace(os.path.join(CACHE, "emb.npy"), lambda p: np.save(p, emb))
    _replace(os.path.join(CACHE, "bounds.bin"), lambda p: bounds.tofile(p))
    meta = {"version": 1, "n": int(len(emb)), "dim": int(emb.shape[1]), "scenes": scenes}
    _replace(os.path.join(CACHE, "meta.json"), lambda p: json.dump(meta, open(p, "w")))
    return {"n": meta["n"], "scenes": len(scenes)}


def _query(pos: str, neg: str, k: int) -> dict:
    if not pos.strip():
        return {"top": []}
    emb = np.load(os.path.join(CACHE, "emb.npy"), mmap_mode="r")
    n = len(emb)
    pos_ids, neg_ids = _ids(pos, n), _ids(neg, n)
    if not pos_ids:
        return {"top": []}
    s = score(emb, pos_ids, neg_ids)
    s[pos_ids + neg_ids] = -np.inf
    k = max(1, min(int(k), n))
    top = np.argpartition(-s, k - 1)[:k]
    top = top[np.argsort(-s[top])]
    return {"top": [[int(i), round(float(s[i]), 4)] for i in top if np.isfinite(s[i])]}


def main(action: str = "status", scene: str = "", pos: str = "", neg: str = "", k: int = 1500) -> dict:
    if action == "status":
        return _status()
    if action == "fetch":
        return _fetch(scene)
    if action == "build":
        return _build()
    if action == "query":
        return _query(pos, neg, k)
    raise ValueError(f"unknown action: {action}")
