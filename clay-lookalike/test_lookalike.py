"""Tests for lookalike.py. Run from anywhere:
uv run --no-project --with pytest --with numpy --with pyarrow --with requests pytest <this file>
"""
import io
import os
import struct

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import lookalike as L


def polygon_wkb(pts):
    """Little-endian WKB polygon with one ring."""
    head = struct.pack("<BII", 1, 3, 1) + struct.pack("<I", len(pts))
    return head + b"".join(struct.pack("<dd", x, y) for x, y in pts)


def square(minx, miny, size=0.0016):
    return polygon_wkb([(minx, miny), (minx, miny + size), (minx + size, miny + size),
                        (minx + size, miny), (minx, miny)])


def parquet_bytes(geoms, embs):
    t = pa.table({"geometry": pa.array(geoms, pa.binary()),
                  "embeddings": pa.array(embs, pa.list_(pa.float32()))})
    buf = io.BytesIO()
    pq.write_table(t, buf)
    return buf.getvalue()


def write_index(root, emb, bounds=None):
    emb = np.asarray(emb, np.float32)
    if bounds is None:
        bounds = np.zeros((len(emb), 4), np.float32)
    os.makedirs(root, exist_ok=True)
    np.save(os.path.join(root, "emb.npy"), emb)
    np.asarray(bounds, np.float32).tofile(os.path.join(root, "bounds.bin"))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "CACHE", str(tmp_path))
    return str(tmp_path)


def test_wkb_bounds_of_little_endian_polygon():
    pts = [(-122.44, 37.81), (-122.44, 37.8114), (-122.4382, 37.8114), (-122.4382, 37.81), (-122.44, 37.81)]
    assert L.wkb_bounds(polygon_wkb(pts)) == pytest.approx((-122.44, 37.81, -122.4382, 37.8114))


def test_nominal_quarter_for_quad_13_ne():
    assert L.nominal_quarter("m_3712213_ne_10_060_20200524") == pytest.approx((-122.4375, 37.8125, -122.375, 37.875))


def test_nominal_quarter_for_quad_21_sw():
    assert L.nominal_quarter("m_3712221_sw_10_060_20200524") == pytest.approx((-122.5, 37.625, -122.4375, 37.6875))


def test_score_is_max_positive_minus_max_negative():
    emb = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [0.6, 0.8, 0]], np.float32)
    assert L.score(emb, pos=[0], neg=[1]) == pytest.approx([1.0, -1.0, 0.0, -0.2])


def test_score_with_two_positives_uses_the_closest():
    emb = np.array([[1, 0, 0], [0, 1, 0], [0.8, 0.6, 0]], np.float32)
    assert L.score(emb, pos=[0, 1], neg=[]) == pytest.approx([1.0, 1.0, 0.8])


def test_query_without_positives_returns_no_matches(cache):
    assert L.main(action="query", pos="", neg="") == {"top": []}


def test_query_with_out_of_range_id_returns_no_matches(cache):
    write_index(cache, np.eye(3))
    assert L.main(action="query", pos="999999") == {"top": []}


def test_query_returns_ranked_ids_excluding_selected(cache):
    write_index(cache, [[1, 0, 0], [0, 1, 0], [0.8, 0.6, 0], [0.6, 0.8, 0]])
    out = L.main(action="query", pos="0", neg="", k=10)
    assert [t[0] for t in out["top"]] == [2, 3, 1]
    assert out["top"][0][1] == pytest.approx(0.8, abs=1e-3)


def test_query_honours_k(cache):
    write_index(cache, np.eye(5))
    assert len(L.main(action="query", pos="0", k=2)["top"]) == 2


def test_status_reports_cached_scenes(cache):
    os.makedirs(os.path.join(cache, "scenes"))
    for s in L.SCENES[:2]:
        np.savez(os.path.join(cache, "scenes", s + ".npz"),
                 emb=np.zeros((1, 4), np.float32), bounds=np.zeros((1, 4), np.float32))
    st = L.main(action="status")
    assert (st["cached"], st["total"], st["ready"]) == (2, 19, False)
    assert st["missing"][0] == L.SCENES[2]


def test_build_refuses_until_all_scenes_cached(cache):
    with pytest.raises(RuntimeError):
        L.main(action="build")


def test_build_keeps_only_chips_centred_in_the_nominal_quarter_and_normalises(cache):
    os.makedirs(os.path.join(cache, "scenes"))
    for i, s in enumerate(L.SCENES):
        x0, y0, x1, y1 = L.nominal_quarter(s)
        inside = [x0 + 0.01, y0 + 0.01, x0 + 0.0116, y0 + 0.0116]
        outside = [x1 + 0.01, y1 + 0.01, x1 + 0.0116, y1 + 0.0116]
        np.savez(os.path.join(cache, "scenes", s + ".npz"),
                 emb=np.array([[3, 4, 0, 0], [0, 0, 5, 12]], np.float32),
                 bounds=np.array([inside, outside], np.float32))
    out = L.main(action="build")
    emb = np.load(os.path.join(cache, "emb.npy"))
    bounds = np.fromfile(os.path.join(cache, "bounds.bin"), np.float32).reshape(-1, 4)
    assert out["n"] == 19 and emb.shape == (19, 4) and bounds.shape == (19, 4)
    assert np.linalg.norm(emb, axis=1) == pytest.approx(np.ones(19))
    assert L.main(action="status")["ready"] is True


def test_fetch_parses_downloaded_parquet_into_npz(cache, monkeypatch):
    calls = []
    scene = L.SCENES[0]
    x0, y0, _, _ = L.nominal_quarter(scene)
    data = parquet_bytes([square(x0 + 0.01, y0 + 0.01)], [[1.0, 2.0, 2.0, 0.0]])
    monkeypatch.setattr(L, "_download", lambda url: calls.append(url) or data)
    out = L.main(action="fetch", scene=scene)
    z = np.load(os.path.join(cache, "scenes", scene + ".npz"))
    assert out == {"scene": scene, "n": 1, "downloaded": True}
    assert z["emb"].shape == (1, 4) and z["bounds"].shape == (1, 4)
    assert calls == [L.BASE_URL + scene + ".parquet"]


def test_fetch_skips_download_when_cached_npz_is_valid(cache, monkeypatch):
    scene = L.SCENES[0]
    os.makedirs(os.path.join(cache, "scenes"))
    np.savez(os.path.join(cache, "scenes", scene + ".npz"),
             emb=np.zeros((2, 4), np.float32), bounds=np.zeros((2, 4), np.float32))
    monkeypatch.setattr(L, "_download", lambda url: pytest.fail("should not download"))
    assert L.main(action="fetch", scene=scene) == {"scene": scene, "n": 2, "downloaded": False}


def test_fetch_redownloads_when_cached_npz_is_corrupt(cache, monkeypatch):
    scene = L.SCENES[0]
    os.makedirs(os.path.join(cache, "scenes"))
    with open(os.path.join(cache, "scenes", scene + ".npz"), "wb") as f:
        f.write(b"junk")
    x0, y0, _, _ = L.nominal_quarter(scene)
    data = parquet_bytes([square(x0 + 0.01, y0 + 0.01)], [[1.0, 0.0, 0.0, 0.0]])
    monkeypatch.setattr(L, "_download", lambda url: data)
    assert L.main(action="fetch", scene=scene)["downloaded"] is True


def test_fetch_rejects_unknown_scene(cache):
    with pytest.raises(ValueError):
        L.main(action="fetch", scene="../etc/passwd")
