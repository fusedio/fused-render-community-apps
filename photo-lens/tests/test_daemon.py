import http.client
import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import piexif
import pytest
from PIL import Image

from lens import config, metadata, tags, validate
from lens.daemon import _LOOPBACK_HOST, _LOOPBACK_ORIGIN, LensServer
from tests.conftest import (FakeFaceModel, band_of, face_photo,
                            write_video)


class FakeEmbedder:
    dim = 4
    key = "fake"

    def embed_images(self, imgs):
        return np.full((len(imgs), 4), 0.5, dtype=np.float16)

    def embed_text(self, text):
        return np.full((4,), 0.5, dtype=np.float16)


class SlowEmbedder:
    """Simulates a real embedding model: one batch call per flush, each
    taking noticeably longer than a status/read request should have to
    wait for."""
    dim = 4
    key = "slow"

    def embed_images(self, imgs):
        time.sleep(0.5)
        return np.full((len(imgs), 4), 0.5, dtype=np.float16)

    def embed_text(self, text):
        return np.full((4,), 0.5, dtype=np.float16)


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
        return r.status, r.headers, r.read()


def _raw(port, path, headers=None, method="GET", body=None):
    """Full control over the request line and headers — including Host, which
    urllib would otherwise fill in for us."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def _post_json(port, path, obj, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    status, headers, body = _raw(port, path, h, method="POST",
                                 body=json.dumps(obj).encode())
    return status, json.loads(body or b"{}")


def test_daemon_endpoints(cache_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Ubud", "Bali", "ID"))
    root = tmp_path / "photos"
    root.mkdir()
    photo = root / "a.jpg"
    Image.new("RGB", (64, 64), "green").save(photo, "JPEG")
    exif = {"GPS": {
        piexif.GPSIFD.GPSLatitudeRef: b"S",
        piexif.GPSIFD.GPSLatitude: [(8, 1), (24, 1), (0, 1)],
        piexif.GPSIFD.GPSLongitudeRef: b"E",
        piexif.GPSIFD.GPSLongitude: [(115, 1), (6, 1), (0, 1)],
    }}
    piexif.insert(piexif.dump(exif), str(photo))

    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(), port=0)
    srv.index_now()                       # synchronous index for the test
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.port

    status, headers, body = _get(port, "/status")
    assert status == 200
    # no Origin on the request, so nothing to reflect
    assert "Access-Control-Allow-Origin" not in headers
    st = json.loads(body)
    assert st["photos"] == 1 and st["indexing"] is False
    assert st["model_loaded"] is True         # FakeEmbedder has no lazy load

    _, _, body = _get(port, "/query?q=ubud")
    res = json.loads(body)
    assert res["parsed"]["places"] == ["Ubud"]
    assert res["total"] == 1
    items = res["groups"][0]["items"]
    assert len(items) == 1 and items[0]["place_city"] == "Ubud"

    pid = items[0]["id"]
    status, headers, body = _get(port, f"/thumb/{pid}?s=512")
    assert status == 200 and headers["Content-Type"] == "image/webp"
    assert body[:4] == b"RIFF"

    _, _, body = _get(port, f"/meta/{pid}")
    meta = json.loads(body)
    assert meta["place_city"] == "Ubud" and isinstance(meta["raw_exif"], dict)

    srv.shutdown()


def test_status_during_index_is_nonblocking(cache_dir, tmp_path, monkeypatch):
    """/status must return promptly with indexing:true while a reindex is
    running — Store-level per-call locking (not a single lock spanning the
    whole index run) is what makes this possible."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Ubud", "Bali", "ID"))
    root = tmp_path / "photos"
    root.mkdir()
    for name in ("a.jpg", "b.jpg"):
        Image.new("RGB", (64, 64), "green").save(root / name, "JPEG")

    srv = LensServer(cache_dir, roots=[str(root)], embedder=SlowEmbedder(), port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.port

    assert srv.start_reindex() is True

    t0 = time.monotonic()
    status, _, body = _get(port, "/status")
    elapsed = time.monotonic() - t0
    st = json.loads(body)
    assert status == 200
    assert elapsed < 1.0, f"/status took {elapsed:.2f}s while indexing"
    assert st["indexing"] is True

    srv._index_thread.join(timeout=10)

    _, _, body = _get(port, "/status")
    st = json.loads(body)
    assert st["indexing"] is False
    assert st["photos"] == 2

    srv.shutdown()


class CountingEmbedder(FakeEmbedder):
    """Records every embed_text call so warm-up can be observed."""

    def __init__(self):
        self.text_calls = []

    def embed_text(self, text):
        self.text_calls.append(text)
        return super().embed_text(text)


def _camera_photo(path, colour="blue"):
    """A file the indexer will classify as a photograph.

    /query defaults to `scope=photos` (metadata.is_photo), so a corpus of
    EXIF-less blank JPEGs is — correctly — filtered out of every result. A
    capture timestamp is the minimum that makes one a photo."""
    Image.new("RGB", (64, 64), colour).save(path, "JPEG")
    piexif.insert(piexif.dump({"Exif": {
        piexif.ExifIFD.DateTimeOriginal: b"2025:07:01 10:00:00"}}), str(path))
    return path


def _one_photo_root(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    _camera_photo(root / "a.jpg")
    return root


def test_background_reindex_warms_text_encoder(cache_dir, tmp_path):
    """A daemon that stays up to serve queries warms the text encoder, so the
    first semantic query doesn't pay its one-off setup cost."""
    root = _one_photo_root(tmp_path)
    emb = CountingEmbedder()
    srv = LensServer(cache_dir, roots=[str(root)], embedder=emb, port=0)
    assert srv.start_reindex() is True
    srv._index_thread.join(timeout=10)
    assert emb.text_calls, "background reindex did not warm the text encoder"
    srv.shutdown()


def test_synchronous_index_does_not_warm_text_encoder(cache_dir, tmp_path):
    """The one-shot/synchronous path would just block the caller on a warm-up
    it never benefits from, so it stays off unless asked for."""
    root = _one_photo_root(tmp_path)
    emb = CountingEmbedder()
    srv = LensServer(cache_dir, roots=[str(root)], embedder=emb, port=0)
    srv.index_now()
    assert emb.text_calls == []
    assert srv.status()["last_index"]["added"] == 1

    srv.index_now(warm=True)            # opt in explicitly
    assert emb.text_calls
    srv.shutdown()


def test_text_warmup_failure_does_not_fail_the_index_run(cache_dir, tmp_path):
    """A broken text encoder must not be reported as an index failure."""

    class BadTextEmbedder(FakeEmbedder):
        def embed_text(self, text):
            raise RuntimeError("text tower exploded")

    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGB", (64, 64), "blue").save(root / "a.jpg", "JPEG")

    srv = LensServer(cache_dir, roots=[str(root)], embedder=BadTextEmbedder(), port=0)
    srv.index_now(warm=True)
    st = srv.status()
    assert "error" not in st["last_index"], st["last_index"]
    assert st["last_index"]["added"] == 1
    assert st["photos"] == 1
    srv.shutdown()


def test_shutdown_without_serving_does_not_hang(cache_dir):
    """shutdown() must be safe on a server that was never serve_forever()'d.

    Run it on a worker so the deadlock this guards against fails the test
    instead of hanging the whole suite."""
    srv = LensServer(cache_dir, roots=[], embedder=FakeEmbedder(), port=0)
    t = threading.Thread(target=srv.shutdown, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "shutdown() deadlocked on a never-served server"


def _photo_with_gps(path, lat_dms, lat_ref, lon_dms, lon_ref, colour="green"):
    Image.new("RGB", (64, 64), colour).save(path, "JPEG")
    exif = {"GPS": {
        piexif.GPSIFD.GPSLatitudeRef: lat_ref,
        piexif.GPSIFD.GPSLatitude: lat_dms,
        piexif.GPSIFD.GPSLongitudeRef: lon_ref,
        piexif.GPSIFD.GPSLongitude: lon_dms,
    }}
    piexif.insert(piexif.dump(exif), str(path))


def test_query_matches_region_not_just_city(cache_dir, tmp_path, monkeypatch):
    """"bali" is a region: real reverse_geocoder returns admin1="Bali" for
    -8.4/115.1 while the nearest city is the hamlet "Tua". The query must
    still find those photos, and must not drag in the Mumbai ones."""
    def fake_geocode(lat, lon):
        if lat < 0:
            return "Tua", "Bali", "ID"
        return "Mumbai", "Maharashtra", "IN"

    monkeypatch.setattr(metadata, "geocode", fake_geocode)
    root = tmp_path / "photos"
    root.mkdir()
    # -8.4 / 115.1  (Bali)
    _photo_with_gps(root / "bali.jpg", [(8, 1), (24, 1), (0, 1)], b"S",
                    [(115, 1), (6, 1), (0, 1)], b"E")
    # 19.07 / 72.88 (Mumbai)
    _photo_with_gps(root / "home.jpg", [(19, 1), (4, 1), (0, 1)], b"N",
                    [(72, 1), (52, 1), (0, 1)], b"E", colour="red")

    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(), port=0)
    srv.index_now()

    assert "Bali" in srv.known_places()
    assert "Tua" in srv.known_places()

    res = srv.run_query("bali")
    assert res["parsed"]["places"] == ["Bali"]
    names = sorted(r["path"].rsplit("/", 1)[-1] for r in res["groups"][0]["items"])
    assert names == ["bali.jpg"], names

    # the city name still works, and so does the region for the other photo
    assert [r["path"].rsplit("/", 1)[-1]
            for r in srv.run_query("tua")["groups"][0]["items"]] == ["bali.jpg"]
    assert [r["path"].rsplit("/", 1)[-1]
            for r in srv.run_query("maharashtra")["groups"][0]["items"]] == ["home.jpg"]
    srv.shutdown()


# ── access control (C3) ────────────────────────────────────────────────────
def _served(cache_dir, tmp_path, monkeypatch, embedder=None):
    """A running daemon with one indexed photo."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Ubud", "Bali", "ID"))
    root = _one_photo_root(tmp_path)
    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=embedder or FakeEmbedder(), port=0)
    srv.index_now()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_loopback_origin_is_reflected(cache_dir, tmp_path, monkeypatch):
    """The fused-render view is served from loopback and must keep working."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    for origin in ("http://127.0.0.1:8765", "http://localhost:8765",
                   "http://localhost"):
        status, headers, _ = _raw(srv.port, "/status", {"Origin": origin})
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == origin, origin
        assert headers.get("Vary") == "Origin"
    srv.shutdown()


def test_foreign_origin_gets_no_cors_header(cache_dir, tmp_path, monkeypatch):
    """`Access-Control-Allow-Origin: *` let any page the user visits read
    their photo paths, GPS coordinates and image bytes off this daemon. With
    no header the browser refuses to hand the response to that page."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    for origin in ("https://evil.example", "http://127.0.0.1.evil.example",
                   "http://notlocalhost", "null"):
        status, headers, _ = _raw(srv.port, "/status", {"Origin": origin})
        assert status == 200                        # served, just not readable
        assert "Access-Control-Allow-Origin" not in headers, origin
    # and the same for the endpoints that carry the actual pixels/metadata
    pid = srv.run_query("")["groups"][0]["items"][0]["id"]
    for path in (f"/thumb/{pid}", f"/meta/{pid}", "/query?q=ubud"):
        _, headers, _ = _raw(srv.port, path, {"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in headers, path
    srv.shutdown()


def test_preflight_follows_the_same_origin_rule(cache_dir, tmp_path, monkeypatch):
    srv = _served(cache_dir, tmp_path, monkeypatch)
    _, headers, _ = _raw(srv.port, "/reindex",
                         {"Origin": "http://localhost:8765"}, method="OPTIONS")
    assert headers.get("Access-Control-Allow-Origin") == "http://localhost:8765"
    _, headers, _ = _raw(srv.port, "/reindex",
                         {"Origin": "https://evil.example"}, method="OPTIONS")
    assert "Access-Control-Allow-Origin" not in headers
    srv.shutdown()


def test_non_loopback_host_is_refused(cache_dir, tmp_path, monkeypatch):
    """DNS rebinding resolves an attacker's hostname to 127.0.0.1, which makes
    their page same-origin with us and bypasses CORS entirely — but the Host
    header still names them."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    pid = srv.run_query("")["groups"][0]["items"][0]["id"]
    for method, path in (("GET", "/status"), ("GET", f"/thumb/{pid}"),
                         ("GET", f"/meta/{pid}"), ("GET", "/query?q=ubud"),
                         ("POST", "/reindex"), ("OPTIONS", "/reindex")):
        status, _, body = _raw(srv.port, path, {"Host": "evil.example"},
                               method=method)
        assert status == 403, (method, path, status)
        assert b"forbidden" in body, (method, path)

    # loopback hosts, with and without a port, still get through
    for host in ("127.0.0.1", f"127.0.0.1:{srv.port}", "localhost",
                 f"localhost:{srv.port}"):
        status, _, _ = _raw(srv.port, "/status", {"Host": host})
        assert status == 200, host
    srv.shutdown()


# ── query vocabulary + totals ──────────────────────────────────────────────
def test_country_codes_are_not_query_vocabulary(cache_dir, tmp_path, monkeypatch):
    """"NO" (Norway) turned "no dogs" into a Norway filter, and "ID"
    (Indonesia) made "id card" a Bali search. Two-letter codes are out."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Oslo", "Oslo", "NO"))
    root = _one_photo_root(tmp_path)
    _photo_with_gps(root / "oslo.jpg", [(59, 1), (55, 1), (0, 1)], b"N",
                    [(10, 1), (45, 1), (0, 1)], b"E")
    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(), port=0)
    srv.index_now()

    assert "Oslo" in srv.known_places()
    assert "NO" not in srv.known_places()

    res = srv.run_query("no dogs")
    assert res["parsed"]["places"] == []
    assert res["parsed"]["residual"] == "no dogs"

    # a query that is nothing but a country code can only mean the place
    assert "NO" in srv.known_places("no")
    assert srv.run_query("no")["parsed"]["places"] == ["NO"]

    # and a real place name is untouched
    assert srv.run_query("oslo")["parsed"]["places"] == ["Oslo"]
    srv.shutdown()


def test_query_reports_total_beyond_the_limit(cache_dir, tmp_path, monkeypatch):
    """The view renders "showing N of TOTAL", so the daemon has to report the
    size of the match set, not of the slice it returned."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(5):
        _camera_photo(root / f"p{i}.jpg", "green")
    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(), port=0)
    srv.index_now()

    res = srv.run_query("", limit=2)
    assert res["total"] == 5 and res["limit"] == 2
    assert len(res["groups"][0]["items"]) == 2

    res = srv.run_query("", limit=100)
    assert res["total"] == 5
    assert len(res["groups"][0]["items"]) == 5

    # a semantic query counts what survived the relevance floor, pre-slice
    res = srv.run_query("sunset", limit=2)
    assert res["parsed"]["residual"] == "sunset"
    assert res["total"] == 5                  # FakeEmbedder scores everything alike
    assert len(res["groups"][0]["items"]) == 2
    srv.shutdown()


class SplitEmbedder:
    """Scores the first `n_far` images it is ever shown as unrelated (0.0) and
    every one after that as a perfect match (1.0).

    The indexer walks `sorted(todo)`, so naming the unrelated files "far_*" and
    the matching ones "near_*" puts them in that order — counted across calls,
    because the indexer embeds in batches of 16."""

    dim = 2
    key = "split"

    def __init__(self, n_far):
        self.n_far = n_far
        self.seen = 0

    def embed_images(self, imgs):
        out = []
        for _ in imgs:
            out.append([0.0, 1.0] if self.seen < self.n_far else [1.0, 0.0])
            self.seen += 1
        return np.array(out, dtype=np.float16)

    def embed_text(self, text):
        return np.array([1.0, 0.0], dtype=np.float16)


def _split_corpus(tmp_path, n_far, n_near):
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(n_far):
        _camera_photo(root / f"far_{i:02d}.jpg", "green")
    for i in range(n_near):
        _camera_photo(root / f"near_{i:02d}.jpg", "green")
    return root


def test_relevance_cut_drops_weak_semantic_matches(cache_dir, tmp_path,
                                                   monkeypatch):
    """Photos close to the query text and photos orthogonal to it: the far ones
    are not results, so they must not pad out the grid.

    The cut is proportional (query.RELEVANCE_RATIO), and it never trims below
    query.MIN_KEEP rows — hence a dozen matches here, not one."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = _split_corpus(tmp_path, n_far=5, n_near=12)

    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SplitEmbedder(n_far=5), port=0)
    srv.index_now()

    res = srv.run_query("sunset")
    names = sorted(r["path"].rsplit("/", 1)[-1]
                   for r in res["groups"][0]["items"])
    assert names == [f"near_{i:02d}.jpg" for i in range(12)]
    assert res["total"] == 12
    # the far ones are still catalogued — the cut is about this query, not the
    # library
    assert srv.status()["photos"] == 17
    srv.shutdown()


def test_weak_signal_query_offers_the_closest_matches(cache_dir, tmp_path,
                                                     monkeypatch):
    """Below MIN_KEEP results the proportional cut stands down: "the closest
    few, ranked" beats "no matches" when the library is small or the query is
    vague, and the score is on every row for the caller to judge."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = _split_corpus(tmp_path, n_far=2, n_near=1)

    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SplitEmbedder(n_far=2), port=0)
    srv.index_now()

    res = srv.run_query("sunset")
    items = res["groups"][0]["items"]
    assert len(items) == 3                        # fewer than MIN_KEEP exist
    assert items[0]["path"].endswith("near_00.jpg")     # best hit still first
    assert items[0]["score"] > items[-1]["score"]
    srv.shutdown()


# ── photos vs. everything ──────────────────────────────────────────────────
def _mixed_root(tmp_path):
    """One photograph and two files software made — the shape of a real home
    folder, where the graphics outnumber the photos."""
    root = tmp_path / "lib"
    root.mkdir()
    _camera_photo(root / "shot.jpg", "green")
    Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(root / "overlay.png", "PNG")
    Image.new("RGB", (64, 64), "white").save(root / "export.jpg", "JPEG")
    return root


def _names(res):
    return sorted(r["path"].rsplit("/", 1)[-1]
                  for g in res["groups"] for r in g["items"])


def test_query_scope_defaults_to_photographs(cache_dir, tmp_path, monkeypatch):
    """Search is only useful on a real library if it answers about photos by
    default: the PNG assets and EXIF-less exports around them are the majority
    of the files and none of the intent."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    srv = LensServer(cache_dir, roots=[str(_mixed_root(tmp_path))],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()

    res = srv.run_query("")
    assert _names(res) == ["shot.jpg"]
    assert res["scope"] == "photos" and res["total"] == 1

    res = srv.run_query("", scope="all")
    assert _names(res) == ["export.jpg", "overlay.png", "shot.jpg"]
    assert res["scope"] == "all" and res["total"] == 3
    srv.shutdown()


def test_status_reports_both_scopes(cache_dir, tmp_path, monkeypatch):
    """The view labels its toggle "Photos N / All M", so both counts come from
    the one /status it already polls."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    srv = LensServer(cache_dir, roots=[str(_mixed_root(tmp_path))],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()
    st = srv.status()
    assert st["photos_scope"] == 1
    assert st["all_scope"] == 3
    assert st["photos"] == 3            # unchanged meaning: files catalogued
    srv.shutdown()


def test_scope_over_http_defaults_safely(cache_dir, tmp_path, monkeypatch):
    """Only a scope this daemon offers is honoured. A missing, misspelled or
    hostile value gets the default rather than a 400 — a stale deep link, or one
    written by a newer view, should still search."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    srv = LensServer(cache_dir, roots=[str(_mixed_root(tmp_path))],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    _, _, body = _get(srv.port, "/query?q=&scope=all")
    assert json.loads(body)["total"] == 3

    for q in ("/query?q=", "/query?q=&scope=photos", "/query?q=&scope=ALL",
              "/query?q=&scope=everything", "/query?q=&scope="):
        status, _, body = _get(srv.port, q)
        res = json.loads(body)
        assert status == 200 and res["scope"] == "photos", q
        assert res["total"] == 1, q
    srv.shutdown()


def test_reindex_refuses_a_foreign_origin(cache_dir, tmp_path, monkeypatch):
    """POST /reindex is a CORS-simple request, so the browser sends it without
    a preflight: withholding the response header keeps a foreign page from
    reading the reply but not from firing the rescan blind."""
    srv = _served(cache_dir, tmp_path, monkeypatch)

    status, _, body = _raw(srv.port, "/reindex",
                           {"Origin": "https://evil.example"}, method="POST")
    assert status == 403 and b"forbidden" in body

    # the view's own origin, and a browser-less client, still work
    status, _, body = _raw(srv.port, "/reindex",
                           {"Origin": "http://127.0.0.1:8765"}, method="POST")
    assert status == 200 and b"started" in body
    srv._index_thread.join(timeout=10)
    status, _, body = _raw(srv.port, "/reindex", method="POST")
    assert status == 200 and b"started" in body
    srv._index_thread.join(timeout=10)
    srv.shutdown()


def test_loopback_patterns_are_fully_anchored():
    """`$` also matches just before a trailing newline, so `^...$` would have
    accepted a header value with something smuggled onto a second line."""
    assert _LOOPBACK_ORIGIN.fullmatch("http://localhost:8765")
    assert _LOOPBACK_ORIGIN.fullmatch("https://127.0.0.1")
    assert not _LOOPBACK_ORIGIN.fullmatch("http://localhost:8765\n")
    assert not _LOOPBACK_ORIGIN.fullmatch("http://localhost\nhttps://evil.example")
    assert not _LOOPBACK_ORIGIN.fullmatch("http://localhost.evil.example")
    assert _LOOPBACK_HOST.fullmatch("127.0.0.1:8877")
    assert not _LOOPBACK_HOST.fullmatch("127.0.0.1:8877\n")
    assert not _LOOPBACK_HOST.fullmatch("evil.example")


# ── roots management (folders from the UI) ─────────────────────────────────
def _corpus(dirpath, names=("a.jpg",)):
    dirpath.mkdir(parents=True, exist_ok=True)
    for n in names:
        Image.new("RGB", (32, 32), "green").save(dirpath / n, "JPEG")
    return dirpath


def test_roots_get_reports_each_folder_and_whether_it_exists(cache_dir, tmp_path,
                                                             monkeypatch):
    """`exists` is what lets the view distinguish a folder the user should
    remove from a drive that simply isn't plugged in right now."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    gone = str(tmp_path / "unplugged")
    config.save_config({**config.load_config(cache_dir),
                        "roots": [str(tmp_path / "photos"), gone]}, cache_dir)

    _, _, body = _get(srv.port, "/roots")
    roots = json.loads(body)["roots"]
    assert [r["path"] for r in roots] == [str(tmp_path / "photos"), gone]
    assert [r["exists"] for r in roots] == [True, False]
    srv.shutdown()


def test_add_root_persists_and_indexes_it(cache_dir, tmp_path, monkeypatch):
    """The whole point of the UI flow: a folder added from the view is in the
    config and in the library, with no CLI and no restart."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    assert srv.status()["photos"] == 1

    second = _corpus(tmp_path / "second", ("b.jpg", "c.jpg"))
    status, out = _post_json(srv.port, "/roots", {"path": str(second)})

    assert status == 200
    assert out["reindexing"] is True
    assert str(second) in [r["path"] for r in out["roots"]]
    assert str(second) in config.load_config(cache_dir)["roots"]

    srv._index_thread.join(timeout=20)
    assert srv.status()["photos"] == 3
    assert srv.store.get_photo(str(second / "b.jpg")) is not None

    assert out["changed"] is True
    # adding the same folder twice is not two entries, and not a second scan:
    # re-adding what is already there must not cost a pass over the library
    status, out = _post_json(srv.port, "/roots", {"path": str(second)})
    assert status == 200
    assert [r["path"] for r in out["roots"]].count(str(second)) == 1
    assert out["changed"] is False and out["reindexing"] is False
    assert srv._indexing is False
    srv.shutdown()


def test_add_root_expands_and_resolves_the_path(cache_dir, tmp_path, monkeypatch):
    """Stored in one spelling, so `~/x` and `/Users/me/x` are one root and the
    remove button can find what the add button wrote."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    second = _corpus(tmp_path / "second")
    status, out = _post_json(srv.port, "/roots",
                             {"path": str(tmp_path / "." / "second") + "/"})
    assert status == 200
    assert str(second) in [r["path"] for r in out["roots"]]
    srv._index_thread.join(timeout=20)

    status, out = _post_json(srv.port, "/roots/remove", {"path": str(second)})
    assert status == 200
    assert str(second) not in [r["path"] for r in out["roots"]]
    srv._index_thread.join(timeout=20)
    srv.shutdown()


def test_add_root_rejects_a_non_directory(cache_dir, tmp_path, monkeypatch):
    srv = _served(cache_dir, tmp_path, monkeypatch)
    before = config.load_config(cache_dir)["roots"]
    a_file = tmp_path / "photos" / "a.jpg"
    for bad in (str(a_file), str(tmp_path / "nope")):
        status, out = _post_json(srv.port, "/roots", {"path": bad})
        assert status == 400, bad
        assert out["error"] == "not a directory"
    assert config.load_config(cache_dir)["roots"] == before
    srv.shutdown()


def test_add_root_rejects_a_body_without_a_usable_path(cache_dir, tmp_path,
                                                      monkeypatch):
    srv = _served(cache_dir, tmp_path, monkeypatch)
    for body in ({}, {"path": ""}, {"path": "   "}, {"path": 7},
                 {"path": None}, ["/tmp"]):
        status, out = _post_json(srv.port, "/roots", body)
        assert status == 400, body
        assert out["error"] == "path required"
    for raw in (b"", b"not json", b'{"path": '):
        status, _, resp = _raw(srv.port, "/roots",
                               {"Content-Type": "application/json"},
                               method="POST", body=raw)
        assert status == 400, raw
        assert b"path required" in resp
    # and a body far too large to be one path is not read into memory
    status, _, resp = _raw(srv.port, "/roots",
                           {"Content-Type": "application/json"},
                           method="POST", body=b"x" * (70 * 1024))
    assert status == 400 and b"path required" in resp
    srv.shutdown()


def test_remove_root_prunes_its_photos(cache_dir, tmp_path, monkeypatch):
    """Removing a folder in the UI has to empty it out of the library — the
    files are untouched on disk, so only the reindex's config-aware pruning
    makes them leave."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    second = _corpus(tmp_path / "second", ("b.jpg",))
    _post_json(srv.port, "/roots", {"path": str(second)})
    srv._index_thread.join(timeout=20)
    assert srv.status()["photos"] == 2

    status, out = _post_json(srv.port, "/roots/remove", {"path": str(second)})
    assert status == 200
    assert out["reindexing"] is True
    assert [r["path"] for r in out["roots"]] == [str(tmp_path / "photos")]
    assert config.load_config(cache_dir)["roots"] == [str(tmp_path / "photos")]

    srv._index_thread.join(timeout=20)
    assert srv.status()["photos"] == 1
    assert srv.store.get_photo(str(second / "b.jpg")) is None
    assert (second / "b.jpg").exists(), "the file itself must not be touched"
    srv.shutdown()


def test_remove_root_that_is_not_configured_is_a_no_op(cache_dir, tmp_path,
                                                       monkeypatch):
    """Nothing changed, so nothing is rescanned — the view says so quietly."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    status, out = _post_json(srv.port, "/roots/remove",
                             {"path": str(tmp_path / "never-added")})
    assert status == 200
    assert [r["path"] for r in out["roots"]] == [str(tmp_path / "photos")]
    assert out["changed"] is False and out["reindexing"] is False
    assert srv._index_thread is None, "a no-op started an index run"
    assert srv.status()["photos"] == 1
    srv.shutdown()


def test_roots_edit_during_an_index_defers_the_rescan(cache_dir, tmp_path,
                                                      monkeypatch):
    """A run already in flight started with the old roots, so it cannot pick
    the change up. Say so (`reindexing: false`) instead of pretending — the
    view still has ↻."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = _corpus(tmp_path / "photos", ("a.jpg", "b.jpg"))
    srv = LensServer(cache_dir, roots=[str(root)], embedder=SlowEmbedder(), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    assert srv.start_reindex() is True

    second = _corpus(tmp_path / "second", ("c.jpg",))
    status, out = _post_json(srv.port, "/roots", {"path": str(second)})
    assert status == 200
    assert out["reindexing"] is False           # already running
    assert str(second) in [r["path"] for r in out["roots"]]

    srv._index_thread.join(timeout=30)
    assert srv.status()["photos"] == 2          # the new folder wasn't scanned

    assert srv.start_reindex() is True          # what ↻ does
    srv._index_thread.join(timeout=30)
    assert srv.status()["photos"] == 3
    srv.shutdown()


def test_index_run_rereads_roots_from_config(cache_dir, tmp_path, monkeypatch):
    """Roots are not captured at construction: the config file is the source of
    truth for every run, which is what lets the view edit them at runtime."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    first = _corpus(tmp_path / "first")
    srv = LensServer(cache_dir, roots=[str(first)], embedder=FakeEmbedder(), port=0)
    srv.index_now()
    assert srv.status()["photos"] == 1

    second = _corpus(tmp_path / "second", ("b.jpg", "c.jpg"))
    config.save_config({**config.load_config(cache_dir),
                        "roots": [str(first), str(second)]}, cache_dir)
    assert srv.current_roots() == [str(first), str(second)]

    srv.index_now()
    assert srv.status()["photos"] == 3
    srv.shutdown()


def test_fs_dirs_browses_subdirectories_only(cache_dir, tmp_path, monkeypatch):
    srv = _served(cache_dir, tmp_path, monkeypatch)
    base = tmp_path / "browse"
    for sub in ("Zeta", "alpha", ".hidden"):
        (base / sub).mkdir(parents=True)
    (base / "a-file.jpg").write_bytes(b"x")

    _, _, body = _get(srv.port, "/fs/dirs?path=" + urllib.parse.quote(str(base)))
    out = json.loads(body)
    assert out["path"] == str(base)
    assert out["parent"] == str(tmp_path)
    # subdirectories only, no files, no hidden entries, case-insensitive sort
    assert [d["name"] for d in out["dirs"]] == ["alpha", "Zeta"]
    assert [d["path"] for d in out["dirs"]] == [str(base / "alpha"),
                                                str(base / "Zeta")]

    # no param → the home directory, the browser's starting point
    _, _, body = _get(srv.port, "/fs/dirs")
    assert json.loads(body)["path"] == str(Path.home())

    # "/" is the top: no parent to climb to
    _, _, body = _get(srv.port, "/fs/dirs?path=/")
    assert json.loads(body)["parent"] is None

    for bad in (str(base / "a-file.jpg"), str(tmp_path / "nope")):
        status, _, resp = _raw(srv.port, "/fs/dirs?path="
                               + urllib.parse.quote(bad))
        assert status == 400, bad
        assert b"not a directory" in resp
    srv.shutdown()


def test_roots_endpoints_refuse_foreign_origins_and_hosts(cache_dir, tmp_path,
                                                          monkeypatch):
    """Same posture as every other endpoint: these read the filesystem and
    rewrite the library, so a foreign page must not reach them — through CORS
    (no reflected header) or around it (DNS rebinding, caught by Host)."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    second = _corpus(tmp_path / "second")
    before = config.load_config(cache_dir)["roots"]

    for path, body in (("/roots", {"path": str(second)}),
                       ("/roots/remove", {"path": str(tmp_path / "photos")})):
        status, out = _post_json(srv.port, path, body,
                                 {"Origin": "https://evil.example"})
        assert status == 403 and out["error"] == "forbidden", path
    assert config.load_config(cache_dir)["roots"] == before, "an edit got through"

    for method, path in (("GET", "/roots"), ("GET", "/fs/dirs"),
                         ("POST", "/roots"), ("POST", "/roots/remove")):
        status, _, resp = _raw(srv.port, path, {"Host": "evil.example"},
                               method=method)
        assert status == 403, (method, path)
        assert b"forbidden" in resp
    assert config.load_config(cache_dir)["roots"] == before, "an edit got through"

    # a foreign page cannot read the folder listing either
    for path in ("/roots", "/fs/dirs"):
        _, headers, _ = _raw(srv.port, path, {"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in headers, path

    # the view's own origin still works, end to end
    status, out = _post_json(srv.port, "/roots", {"path": str(second)},
                             {"Origin": "http://127.0.0.1:8765"})
    assert status == 200 and str(second) in [r["path"] for r in out["roots"]]
    srv._index_thread.join(timeout=20)
    srv.shutdown()


def test_preflight_allows_the_json_content_type(cache_dir, tmp_path, monkeypatch):
    """POST /roots sends application/json, which is not a CORS-simple content
    type: without Allow-Headers the browser fails the preflight and the view
    can never add a folder."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    _, headers, _ = _raw(srv.port, "/roots",
                         {"Origin": "http://127.0.0.1:8765",
                          "Access-Control-Request-Method": "POST",
                          "Access-Control-Request-Headers": "content-type"},
                         method="OPTIONS")
    assert headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8765"
    assert "POST" in headers.get("Access-Control-Allow-Methods", "")
    assert "Content-Type" in headers.get("Access-Control-Allow-Headers", "")
    srv.shutdown()


# ── big-root guardrails ────────────────────────────────────────────────────
def test_add_root_refuses_the_filesystem_root(cache_dir, tmp_path, monkeypatch):
    """"/" is never what someone means, and it is the one root whose scan can't
    be waited out."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    before = config.load_config(cache_dir)["roots"]
    for spelling in ("/", "//", "/.."):
        status, out = _post_json(srv.port, "/roots", {"path": spelling})
        assert status == 400, spelling
        assert out["error"] == "root too broad", spelling
        assert "filesystem" in out["message"]
    assert config.load_config(cache_dir)["roots"] == before
    assert srv._index_thread is None
    srv.shutdown()


def test_add_root_asks_before_indexing_the_home_directory(cache_dir, tmp_path,
                                                          monkeypatch):
    """The home directory is allowed — it is the obvious thing to point lens at
    — but its first scan is long enough that it must not start on a mis-click."""
    home = _corpus(tmp_path / "home", ("h.jpg",))
    monkeypatch.setenv("HOME", str(home))
    srv = _served(cache_dir, tmp_path, monkeypatch)

    status, out = _post_json(srv.port, "/roots", {"path": str(home)})
    assert status == 400
    assert out["error"] == "confirm_home"
    assert "home folder" in out["message"]
    assert str(home) not in config.load_config(cache_dir)["roots"]
    assert srv._index_thread is None, "the refused add started a scan anyway"

    # "~" is the same folder, so it gets the same question
    status, out = _post_json(srv.port, "/roots", {"path": "~"})
    assert status == 400 and out["error"] == "confirm_home"

    # confirmed, it goes in and is indexed like any other folder
    status, out = _post_json(srv.port, "/roots", {"path": str(home),
                                                 "confirm": True})
    assert status == 200 and out["changed"] is True
    assert str(home) in [r["path"] for r in out["roots"]]
    srv._index_thread.join(timeout=20)
    assert srv.store.get_photo(str(home / "h.jpg")) is not None

    # a subfolder of home is not home: no question
    sub = _corpus(home / "Pictures", ("p.jpg",))
    status, out = _post_json(srv.port, "/roots", {"path": str(sub)})
    assert status == 200 and out["changed"] is True
    srv._index_thread.join(timeout=20)
    srv.shutdown()


def test_unusable_paths_are_refused_not_crashed(cache_dir, tmp_path, monkeypatch):
    """A null byte makes the OS reject the name outright, which used to surface
    as a 500 from the resolve() call."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    for path in ("/tmp/a\x00b", "\x00"):
        for endpoint in ("/roots", "/roots/remove"):
            status, out = _post_json(srv.port, endpoint, {"path": path})
            assert status == 400, (endpoint, path)
            assert out["error"] == "invalid path", (endpoint, out)

    status, _, resp = _raw(srv.port, "/fs/dirs?path=" + urllib.parse.quote(
        "/tmp/a\x00b", safe=""))
    assert status == 400 and b"not a directory" in resp
    srv.shutdown()


# ── the confidence horizon, end to end ─────────────────────────────────────
def test_query_embeds_a_caption_not_the_bare_residual(cache_dir, tmp_path,
                                                      monkeypatch):
    """The text tower sees a sentence (query.TEXT_PROMPT); the view still sees
    the words the user typed."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    emb = CountingEmbedder()
    srv = LensServer(cache_dir, roots=[str(_one_photo_root(tmp_path))],
                     embedder=emb, port=0)
    srv.index_now()
    emb.text_calls.clear()

    res = srv.run_query("beach sunset")
    assert emb.text_calls == ["a photo of a beach sunset"]
    assert res["parsed"]["residual"] == "beach sunset"    # echoed bare
    srv.shutdown()


def test_query_reports_where_the_answers_stop(cache_dir, tmp_path, monkeypatch):
    """Three matches padded out to MIN_KEEP is the shape that made search look
    wrong. The rows still come back — the daemon now says which of them are
    actually matches."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = _split_corpus(tmp_path, n_far=5, n_near=3)
    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SplitEmbedder(n_far=5), port=0)
    srv.index_now()

    res = srv.run_query("sunset")
    items = res["groups"][0]["items"]
    assert res["strong"] == 3
    assert res["strong_cutoff"] == items[0]["score"] * 0.5
    assert len(items) == 8                     # the padding is still served
    # the split is positional: rows are ranked, so the strong ones lead
    assert all(i["path"].rsplit("/", 1)[-1].startswith("near_")
               for i in items[:3])
    assert all(i["score"] < res["strong_cutoff"] for i in items[3:])
    srv.shutdown()


def test_query_admits_when_nothing_is_a_strong_match(cache_dir, tmp_path,
                                                     monkeypatch):
    """Gibberish. Every photo is orthogonal to the query, so there is no top
    score to take a fraction of — `strong: 0` is what lets the view say so
    instead of dressing up a dozen unrelated frames as results."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = _split_corpus(tmp_path, n_far=6, n_near=0)
    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SplitEmbedder(n_far=6), port=0)
    srv.index_now()

    res = srv.run_query("zzzqqqxx purple dinosaur spaceship")
    assert res["strong"] == 0
    assert res["strong_cutoff"] is None
    assert len(res["groups"][0]["items"]) == 6      # still offered, not hidden
    srv.shutdown()


def test_a_query_with_no_semantic_part_has_no_horizon(cache_dir, tmp_path,
                                                      monkeypatch):
    """Nothing was ranked, so there is no confidence to grade: every row is a
    filter match. Null, not zero — zero would read as "no matches"."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    srv = LensServer(cache_dir, roots=[str(_one_photo_root(tmp_path))],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()

    res = srv.run_query("")
    assert res["strong"] is None and res["strong_cutoff"] is None
    assert len(res["groups"][0]["items"]) == 1
    srv.shutdown()


def test_query_reports_how_much_it_searched(cache_dir, tmp_path, monkeypatch):
    """`strong` only means something against the size of what was ranked. The
    view needs both to tell "3 of 86 matched" from "84 of 86 matched", which is
    a bar that separated nothing — the one thing about query quality this
    daemon can honestly measure."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = _split_corpus(tmp_path, n_far=5, n_near=3)
    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SplitEmbedder(n_far=5), port=0)
    srv.index_now()

    assert srv.run_query("sunset")["searched"] == 8      # every photo ranked
    # nothing was ranked, so there is no denominator to report
    assert srv.run_query("")["searched"] is None
    srv.shutdown()


def test_query_pages_through_one_ranking(cache_dir, tmp_path, monkeypatch):
    """`offset` slices the same ordered match set, so the view appends a page
    instead of re-rendering — and `total` keeps describing the whole set, not
    the page."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(7):
        _camera_photo(root / f"p{i}.jpg", "green")
    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(), port=0)
    srv.index_now()

    whole = [i["id"] for i in srv.run_query("")["groups"][0]["items"]]
    assert len(whole) == 7

    seen, offset = [], 0
    while True:
        res = srv.run_query("", limit=3, offset=offset)
        assert res["total"] == 7 and res["offset"] == offset
        page = [i["id"] for i in res["groups"][0]["items"]]
        if not page:
            break
        assert len(page) <= 3
        seen += page
        offset += 3
    assert seen == whole                      # every row once, in rank order

    # past the end is an empty page, not an error and not a wrap-around
    assert srv.run_query("", limit=3, offset=99)["groups"][0]["items"] == []
    # a negative offset cannot slice backwards off the tail
    assert [i["id"] for i in
            srv.run_query("", limit=3, offset=-5)["groups"][0]["items"]] == whole[:3]
    srv.shutdown()


def test_offset_is_validated_like_every_other_parameter(cache_dir, tmp_path,
                                                        monkeypatch):
    srv = _served(cache_dir, tmp_path, monkeypatch)
    status, _, body = _raw(srv.port, "/query?q=&offset=nonsense")
    assert status == 400 and b"bad parameter" in body
    srv.shutdown()


def test_status_reports_index_progress(cache_dir, tmp_path, monkeypatch):
    """The view drives a real progress bar off this. It exists only while a run
    is in flight — a fraction left standing after the run would be a lie about
    work that is finished."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(6):
        _camera_photo(root / f"p{i}.jpg", "green")
    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SlowEmbedder(), port=0)
    assert srv.status()["progress"] is None          # nothing running

    seen = []
    srv.start_reindex()
    for _ in range(200):
        s = srv.status()
        if not s["indexing"]:
            break
        if s["progress"]:
            seen.append((s["progress"]["done"], s["progress"]["total"]))
        time.sleep(0.02)
    srv._index_thread.join(timeout=30)

    assert seen, "no progress was ever reported during a run"
    assert all(0 <= d <= t or t == 0 for d, t in seen), seen
    assert seen[-1][1] == 6                          # total is the work to do
    assert srv.status()["progress"] is None          # and it is gone after
    srv.shutdown()


def test_progress_reports_elapsed_and_eta(cache_dir, tmp_path, monkeypatch):
    """The status line's "so far / about this much left" comes from these
    two fields: `elapsed_s` is real time since the run started, `eta_s` a
    rate-based projection off however much of the work is done."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(8):
        _camera_photo(root / f"p{i}.jpg", "green")
    srv = LensServer(cache_dir, roots=[str(root)],
                     embedder=SlowEmbedder(), port=0)
    assert srv.status()["progress"] is None

    seen = []
    srv.start_reindex()
    for _ in range(300):
        s = srv.status()
        if not s["indexing"]:
            break
        if s["progress"]:
            seen.append(s["progress"])
        time.sleep(0.02)
    srv._index_thread.join(timeout=30)

    assert seen, "no progress was ever reported during a run"
    assert all("elapsed_s" in p and "eta_s" in p for p in seen)
    # elapsed_s only grows, and is a real duration once the run has started —
    # never negative, and (with a SlowEmbedder in the mix) not stuck at zero
    assert all(p["elapsed_s"] is None or p["elapsed_s"] >= 0 for p in seen)
    assert any(p["elapsed_s"] and p["elapsed_s"] > 0 for p in seen)
    # eta_s is only ever offered once there is a rate to project from —
    # "0 done of N" has no rate, so it must not invent one
    zero_done = [p for p in seen if p["done"] == 0]
    assert all(p["eta_s"] is None for p in zero_done)
    assert srv.status()["progress"] is None
    srv.shutdown()


def test_roots_carry_what_each_folder_contributed(cache_dir, tmp_path,
                                                  monkeypatch):
    """A list of paths cannot answer the question the settings panel is opened
    with — is this the folder my photos are in? The counts are the answer, and
    they add up to the totals in the header."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    for i in range(3):
        _camera_photo(a / f"p{i}.jpg", "green")
    _camera_photo(b / "one.jpg", "blue")
    Image.new("RGB", (8, 8), "white").save(b / "graphic.png", "PNG")

    srv = LensServer(cache_dir, roots=[str(a), str(b)],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()

    by_path = {r["path"]: r for r in srv.roots_payload()["roots"]}
    assert by_path[str(a)]["photos"] == 3 and by_path[str(a)]["images"] == 3
    assert by_path[str(b)]["photos"] == 1 and by_path[str(b)]["images"] == 2
    st = srv.status()
    assert sum(r["photos"] for r in by_path.values()) == st["photos_scope"]
    assert sum(r["images"] for r in by_path.values()) == st["all_scope"]
    srv.shutdown()


def test_a_nested_root_does_not_count_its_files_twice(cache_dir, tmp_path,
                                                      monkeypatch):
    """A file under both ~/Pictures and ~ belongs to the folder that names it
    most precisely; counting it under both would put more photos in the panel
    than exist in the library."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    _camera_photo(outer / "o.jpg", "green")
    _camera_photo(inner / "i.jpg", "blue")

    srv = LensServer(cache_dir, roots=[str(outer), str(inner)],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()
    by_path = {r["path"]: r for r in srv.roots_payload()["roots"]}
    assert by_path[str(outer)]["photos"] == 1
    assert by_path[str(inner)]["photos"] == 1
    assert sum(r["photos"] for r in by_path.values()) == srv.status()["photos_scope"]
    srv.shutdown()


def _gps_photo(path, when, lat, lon):
    Image.new("RGB", (64, 64), "green").save(path, "JPEG")
    def dms(v):
        v = abs(v)
        d = int(v); m = int((v - d) * 60); sec = int(((v - d) * 60 - m) * 6000)
        return [(d, 1), (m, 1), (sec, 100)]
    piexif.insert(piexif.dump({
        "Exif": {piexif.ExifIFD.DateTimeOriginal: when.encode()},
        "GPS": {piexif.GPSIFD.GPSLatitudeRef: b"S",
                piexif.GPSIFD.GPSLatitude: dms(lat),
                piexif.GPSIFD.GPSLongitudeRef: b"E",
                piexif.GPSIFD.GPSLongitude: dms(lon)},
    }), str(path))
    return path


def test_trip_groups_carry_their_own_dates(cache_dir, tmp_path, monkeypatch):
    """Two stays in one place produced two headings reading the same thing.
    The dates are what tells them apart, so they travel with the group."""
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (("Ubud" if lon < 118 else "Faraway"),
                                          "Bali", "ID"))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(4):                                  # home
        _gps_photo(root / f"h{i}.jpg", f"2025:07:0{i + 1} 10:00:00", 8.5, 115.26)
    for i in range(3):                                  # 500km away, days later
        _gps_photo(root / f"t{i}.jpg", f"2025:07:1{i} 10:00:00", 8.5, 120.5)

    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(), port=0)
    srv.index_now()

    groups = srv.run_query("trips")["groups"]
    assert groups, "no trip was detected in a corpus built to contain one"
    g = groups[0]
    assert g["trip"] and g["start"] and g["end"]
    assert g["start"] <= g["end"]
    assert all(g["start"] <= i["taken_at"] <= g["end"] for i in g["items"])
    srv.shutdown()


def test_limit_is_a_page_size_not_a_suggestion(cache_dir, tmp_path, monkeypatch):
    """A page size bounds the work one request can ask for. Zero and negative
    slice nonsense out of the ranking; unbounded hands back the whole library.
    The view never asks for either, so neither is worth serving."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    for bad in ("0", "-1", "2001", "999999", "nonsense"):
        status, _, body = _raw(srv.port, "/query?q=&limit=" + bad)
        assert status == 400, (bad, status)
        assert b"bad parameter" in body, bad
    for good in ("1", "200", "2000"):
        status, _, _ = _raw(srv.port, "/query?q=&limit=" + good)
        assert status == 200, good
    # absent is the default, not a rejection — and parse_qs drops a blank
    # value, so "limit=" is absent rather than a malformed number
    assert _raw(srv.port, "/query?q=")[0] == 200
    assert _raw(srv.port, "/query?q=&limit=")[0] == 200
    srv.shutdown()


# ── trips as a browsable thing ─────────────────────────────────────────────
def _trip_root(tmp_path, n_away=3):
    """Four photos at home and `n_away` some 575km away days later — the gap in
    time plus the distance is what compute_trips turns into one trip."""
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(4):
        _gps_photo(root / f"h{i}.jpg", f"2025:07:0{i + 1} 10:00:00", 8.5, 115.26)
    for i in range(n_away):
        _gps_photo(root / f"t{i}.jpg", f"2025:07:1{i} 10:00:00", 8.5, 120.5)
    return root


def _trip_server(cache_dir, tmp_path, monkeypatch, n_away=3):
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (("Ubud" if lon < 118 else "Faraway"),
                                          "Bali", "ID"))
    srv = LensServer(cache_dir, roots=[str(_trip_root(tmp_path, n_away))],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _trips(port):
    status, _, body = _get(port, "/trips")
    assert status == 200
    return json.loads(body)["trips"]


def test_trips_endpoint_carries_what_a_card_needs(cache_dir, tmp_path,
                                                 monkeypatch):
    """A trip card has to stand on its own: a name, its dates, how many photos
    it holds and which one to show. Without the count and the cover the view
    would need a query per trip just to draw the list."""
    srv = _trip_server(cache_dir, tmp_path, monkeypatch)
    trips = _trips(srv.port)

    assert len(trips) == 1
    t = trips[0]
    assert set(t) == {"id", "name", "start", "end", "place", "count", "cover_id"}
    assert t["place"] == "Faraway" and t["name"] == "Faraway · Jul 2025"
    assert t["start"] <= t["end"]
    assert t["count"] == 3                       # the away photos, and only those

    inside = srv.store.query_photos("trip_id = ?", [t["id"]])
    assert len(inside) == t["count"]
    assert t["cover_id"] == min(inside, key=lambda r: r["taken_at"])["id"]

    # and the cover is a photo the view can actually draw
    status, headers, body = _get(srv.port, f"/thumb/{t['cover_id']}")
    assert status == 200 and headers["Content-Type"] == "image/webp"
    assert body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    srv.shutdown()


def test_trips_endpoint_is_empty_when_nothing_is_a_trip(cache_dir, tmp_path,
                                                        monkeypatch):
    """Most libraries hold no trip at all (no GPS, or never left home). That is
    an empty list, not an error and not a missing key."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    _, _, body = _get(srv.port, "/trips")
    assert json.loads(body) == {"trips": []}
    srv.shutdown()


def test_query_can_be_narrowed_to_one_trip(cache_dir, tmp_path, monkeypatch):
    """Opening a trip is a query, so it composes with every other filter: a
    search typed while a trip is open searches inside that trip."""
    srv = _trip_server(cache_dir, tmp_path, monkeypatch)
    tid = _trips(srv.port)[0]["id"]

    _, _, body = _get(srv.port, "/query?q=")
    everything = json.loads(body)
    assert everything["total"] == 7 and everything["trip"] is None

    _, _, body = _get(srv.port, f"/query?q=&trip={tid}")
    res = json.loads(body)
    # echoed back so the view can tell "this page is the trip I asked for" from
    # "this page is everything", without trusting that its own URL and the
    # response it is holding are the same generation
    assert res["trip"] == tid
    assert res["total"] == 3
    assert _names(res) == ["t0.jpg", "t1.jpg", "t2.jpg"]
    assert {i["trip_id"] for i in res["groups"][0]["items"]} == {tid}

    # composed with a place filter: inside the trip, and outside it
    _, _, body = _get(srv.port, f"/query?q=faraway&trip={tid}")
    assert json.loads(body)["total"] == 3
    _, _, body = _get(srv.port, f"/query?q=ubud&trip={tid}")
    inside_home = json.loads(body)
    assert inside_home["total"] == 0              # the home photos are not in it
    assert json.loads(_get(srv.port, "/query?q=ubud")[2])["total"] == 4

    # ...and with the scope toggle, which is a different clause again
    _, _, body = _get(srv.port, f"/query?q=&trip={tid}&scope=all")
    res = json.loads(body)
    assert res["scope"] == "all" and res["trip"] == tid and res["total"] == 3

    # A trip id the view is holding can go stale under it: the photos were
    # reassigned by a reindex, or nothing ended up in this trip at all. An empty
    # page is the answer — a 404 would leave the view unable to draw the page it
    # is already on.
    srv.store.replace_trips(srv.store.get_trips(), {})
    _, _, body = _get(srv.port, f"/query?q=&trip={tid}")
    res = json.loads(body)
    assert res["total"] == 0 and res["trip"] == tid
    assert [i for g in res["groups"] for i in g["items"]] == []
    # and so is a trip id that never existed
    assert json.loads(_get(srv.port, "/query?q=&trip=99999")[2])["total"] == 0
    srv.shutdown()


def test_a_non_numeric_trip_is_a_bad_parameter(cache_dir, tmp_path, monkeypatch):
    """Present-but-not-a-number is a link the view never wrote, and guessing
    which trip was meant would be worse than saying so."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    for bad in ("nonsense", "1.5", "1,2", "-", "0x1", "1%202"):
        status, _, body = _raw(srv.port, "/query?q=&trip=" + bad)
        assert status == 400, bad
        assert b"bad parameter" in body, bad

    # absent is "every trip" — and parse_qs drops a blank value, so "trip=" is
    # absent rather than a malformed number
    for path in ("/query?q=", "/query?q=&trip="):
        status, _, body = _raw(srv.port, path)
        assert status == 200, path
        assert json.loads(body)["trip"] is None, path
    srv.shutdown()


# ── auto-tags ──────────────────────────────────────────────────────────────
_TAGLESS_ROW = {"path": "/p/not-embedded.jpg", "sha1": "beefbeef", "size": 1,
                "mtime": 1.0, "width": 8, "height": 8, "format": "JPEG",
                "taken_at": "2025-07-01T10:00:00", "raw_exif": "{}",
                "error": None, "is_photo": 1}


def test_tags_endpoint_has_three_different_answers(cache_dir, tmp_path,
                                                   monkeypatch):
    """Nothing in EXIF says what is *in* a picture; the vector the library
    already holds does. Three outcomes, and they must not be conflated: chips
    for a photo that has a vector, `[]` for one that does not — which the panel
    says out loud rather than leaving a gap where chips should be — and 404 for
    a photo that isn't there.

    FakeEmbedder's constant vector scores every label alike, so what is pinned
    here is the shape and the cap; the ranking itself is tested against
    distinguishable vectors in tests/test_tags.py."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    pid = srv.run_query("")["groups"][0]["items"][0]["id"]

    status, _, body = _get(srv.port, f"/tags/{pid}")
    assert status == 200
    out = json.loads(body)["tags"]
    assert len(out) == tags.TOP_K
    assert all(set(t) == {"label", "score"} for t in out)
    assert all(t["label"] in tags.VOCAB for t in out)
    assert len({t["label"] for t in out}) == len(out)      # no chip drawn twice
    scores = [t["score"] for t in out]
    assert scores == sorted(scores, reverse=True)
    # the same photo asked twice is the same answer, in the same order
    assert json.loads(_get(srv.port, f"/tags/{pid}")[2])["tags"] == out

    # catalogued, but never embedded
    tagless = srv.store.upsert_photo(dict(_TAGLESS_ROW))
    status, _, body = _get(srv.port, f"/tags/{tagless}")
    assert status == 200 and json.loads(body) == {"tags": []}

    # no such photo at all
    status, _, body = _raw(srv.port, "/tags/999999")
    assert status == 404 and b"no such photo" in body
    assert _raw(srv.port, "/tags/abc")[0] == 404          # not even a photo id
    srv.shutdown()


def test_tags_are_computed_once_per_photo(cache_dir, tmp_path, monkeypatch):
    """Labels are a pure function of a stored vector and a fixed vocabulary, so
    the only thing that can change them is a re-index — and the panel is opened
    and closed repeatedly on the same photo."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    pid = srv.run_query("")["groups"][0]["items"][0]["id"]
    first = json.loads(_get(srv.port, f"/tags/{pid}")[2])["tags"]

    # reaching into the private cache on purpose: "did not recompute" is not
    # visible in the response, so the only way to see the cache carry the second
    # request is to make recomputation impossible
    assert pid in srv._tag_cache

    class Exploding:
        def top(self, vec, *a, **kw):
            raise AssertionError("recomputed tags for a photo already answered")

    srv._tags = Exploding()
    assert json.loads(_get(srv.port, f"/tags/{pid}")[2])["tags"] == first
    srv.shutdown()


def test_an_unembedded_photo_is_not_cached_as_having_no_tags(cache_dir, tmp_path,
                                                            monkeypatch):
    """`[]` must not be remembered, because it is not an answer about the photo.

    It says "there is no vector for this yet", which is a fact about the index —
    and the index moves. Cached alongside the real answers, it meant that opening
    the details panel on a photo *while a reindex was still running* pinned
    "nothing to describe it with" onto that photo for the life of the process,
    however long after the run actually embedded it. Only a real answer is a pure
    function of a stored vector, and only a real answer is cacheable."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    pid = srv.store.upsert_photo(dict(_TAGLESS_ROW))
    assert srv.tags_for(pid) == []
    assert pid not in srv._tag_cache, "cached the absence of a vector"

    # now give it one, exactly as an index run would, and ask again
    ids, mat = srv.store.load_embeddings()
    srv.store.save_embeddings(
        np.append(ids, np.int64(pid)),
        np.vstack([mat, np.full((1, mat.shape[1]), 0.5, dtype=np.float16)]))
    out = srv.tags_for(pid)
    assert out and len(out) == tags.TOP_K
    assert srv._tag_cache[pid] == out        # ...and *that* is worth keeping
    srv.shutdown()


# ── the audit, over HTTP ───────────────────────────────────────────────────
def test_validate_endpoint_serves_the_whole_audit(cache_dir, tmp_path,
                                                  monkeypatch):
    """The page draws a ring off `composite` and a row per check, so all four
    travel together with the library they were measured on. What each check
    actually measures is pinned in tests/test_validate.py."""
    srv = _trip_server(cache_dir, tmp_path, monkeypatch)
    status, _, body = _get(srv.port, "/validate")
    assert status == 200
    out = json.loads(body)

    assert set(out["checks"]) == set(validate.CHECKS)
    assert out["composite"] == 1.0               # nothing is wrong with it yet
    assert all(c["pass"] is True for c in out["checks"].values()), out["checks"]
    assert out["library"] == {"images": 7, "photos": 7, "videos": 0, "trips": 1,
                             "faces": srv.store.face_counts()}
    assert isinstance(out["elapsed_ms"], int)
    srv.shutdown()


def test_runs_endpoint_serves_recent_index_history(cache_dir, tmp_path,
                                                    monkeypatch):
    """explain.html's performance panel reads this rather than trusting a
    page that could say anything: the real duration/stages/rate/mem_peak_gb
    off however many runs this daemon has actually made, oldest first."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _camera_photo(root / "a.jpg")
    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(),
                     port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    status, _, body = _get(srv.port, "/runs")
    assert status == 200 and json.loads(body)["runs"] == []   # nothing yet

    srv.index_now()
    srv.index_now()
    status, _, body = _get(srv.port, "/runs")
    out = json.loads(body)["runs"]
    assert len(out) == 2
    for run in out:
        assert set(run) >= {"at", "duration_s", "stages", "rate",
                            "mem_peak_gb", "embedded", "errors", "error"}
        assert run["error"] is None

    # limit is honoured and capped
    srv.index_now()
    status, _, body = _get(srv.port, "/runs?limit=1")
    assert len(json.loads(body)["runs"]) == 1
    srv.shutdown()


def test_the_new_get_routes_follow_the_same_loopback_rules(cache_dir, tmp_path,
                                                           monkeypatch):
    """/trips, /validate and /tags hand out place names, file paths and a map of
    the whole library, exactly the material the two guards exist for: a foreign
    page must not read them through CORS, and a rebound hostname must not reach
    them around it."""
    srv = _trip_server(cache_dir, tmp_path, monkeypatch)
    pid = srv.run_query("")["groups"][0]["items"][0]["id"]

    for path in ("/trips", "/validate", f"/tags/{pid}", "/runs"):
        status, _, resp = _raw(srv.port, path, {"Host": "evil.example"})
        assert status == 403, path
        assert b"forbidden" in resp, path

        status, headers, _ = _raw(srv.port, path,
                                  {"Origin": "https://evil.example"})
        assert status == 200, path                  # served, just not readable
        assert "Access-Control-Allow-Origin" not in headers, path

        status, headers, _ = _raw(srv.port, path,
                                  {"Origin": "http://localhost:8765"})
        assert status == 200, path
        assert headers.get("Access-Control-Allow-Origin") == "http://localhost:8765"
        assert headers.get("Vary") == "Origin", path
    srv.shutdown()


# ── /status as the "how it works" page's source of facts ───────────────────
class NamedEmbedder(FakeEmbedder):
    """A real embedder knows which weights it loaded; /status is where the view
    shows them."""
    model_id = "google/siglip-so400m-patch14-384"


def test_status_names_the_model_the_matrix_and_the_cache(cache_dir, tmp_path,
                                                         monkeypatch):
    """The claim "nothing leaves your computer" is made checkable rather than
    asserted: the weights actually loaded, the shape of the matrix on disk, and
    where on this machine the library lives — all cheap enough to ride along on
    the 10s poll."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        _camera_photo(root / f"p{i}.jpg")
    srv = LensServer(cache_dir, roots=[str(root)], embedder=NamedEmbedder(), port=0)
    srv.index_now()

    st = srv.status()
    assert st["model_id"] == NamedEmbedder.model_id
    assert st["embeddings"] == {"count": 3, "dims": NamedEmbedder.dim}
    assert st["cache"] == str(cache_dir)
    assert Path(st["cache"]).is_dir()

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    over_http = json.loads(_get(srv.port, "/status")[2])
    assert over_http["model_id"] == NamedEmbedder.model_id
    assert over_http["embeddings"] == {"count": 3, "dims": NamedEmbedder.dim}
    assert over_http["cache"] == str(cache_dir)
    srv.shutdown()

    # an embedder that cannot name its weights reports null rather than dropping
    # the key the view reads
    plain = LensServer(cache_dir, roots=[], embedder=FakeEmbedder(), port=0)
    assert plain.status()["model_id"] is None
    plain.shutdown()


# ── Apple Photos (settings + status) ───────────────────────────────────────
def _fake_apple(monkeypatch, tmp_path, n=2):
    """A .photoslibrary bundle with `n` originals and a fake osxphotos reporting
    them. See tests/test_apple_photos.py for why the real library is never in the
    loop: it needs macOS, a real database and a privacy permission."""
    from test_apple_photos import FakePhoto, install_fake
    bundle = tmp_path / "Photos Library.photoslibrary" / "originals"
    bundle.mkdir(parents=True)
    photos = []
    for i in range(n):
        p = bundle / f"IMG_{i}.jpg"
        Image.new("RGB", (32, 32), "green").save(p, "JPEG")
        photos.append(FakePhoto(f"u{i}", path=str(p), albums=["Bali 2025"],
                                title=f"Shot {i}", favorite=(i == 0),
                                persons=["Ana"],
                                date=datetime(2025, 7, 1 + i, 10)))
    photos.append(FakePhoto("u-cloud"))           # offloaded original
    install_fake(monkeypatch, photos)
    return bundle


def test_apple_section_is_off_and_empty_until_it_is_switched_on(cache_dir,
                                                               tmp_path,
                                                               monkeypatch):
    _fake_apple(monkeypatch, tmp_path)
    srv = _served(cache_dir, tmp_path, monkeypatch)

    for payload in (json.loads(_get(srv.port, "/roots")[2])["apple"],
                    json.loads(_get(srv.port, "/status")[2])["apple"]):
        assert payload == {"enabled": False, "rows": 0, "last": None}
    srv.shutdown()


def test_switching_apple_on_saves_the_config_and_reindexes(cache_dir, tmp_path,
                                                           monkeypatch):
    """The settings panel's whole flow: one POST, and the library has the photos
    in it — no CLI, no restart, the same contract an added folder has."""
    bundle = _fake_apple(monkeypatch, tmp_path)
    srv = _served(cache_dir, tmp_path, monkeypatch)

    status, out = _post_json(srv.port, "/config", {"apple_photos": True})

    assert status == 200
    assert out["changed"] is True and out["reindexing"] is True
    assert config.load_config(cache_dir)["apple_photos"] is True
    srv._index_thread.join(timeout=20)

    st = json.loads(_get(srv.port, "/status")[2])
    assert st["photos"] == 3                       # one folder photo + two Apple
    assert st["apple"]["enabled"] is True
    assert st["apple"]["rows"] == 2
    assert st["apple"]["last"]["found"] == 3       # the offloaded one included
    assert st["apple"]["last"]["offloaded"] == 1
    assert st["apple"]["last"]["error"] is None
    assert st["apple"]["last"]["at"]
    assert srv.store.get_photo(str(bundle / "IMG_0.jpg")) is not None

    # and an album name is a search
    res = json.loads(_get(srv.port, "/query?q=" + urllib.parse.quote("bali 2025"))[2])
    assert res["parsed"]["albums"] == ["Bali 2025"]
    assert res["total"] == 2
    srv.shutdown()


def test_switching_apple_off_again_prunes_it(cache_dir, tmp_path, monkeypatch):
    _fake_apple(monkeypatch, tmp_path)
    srv = _served(cache_dir, tmp_path, monkeypatch)
    _post_json(srv.port, "/config", {"apple_photos": True})
    srv._index_thread.join(timeout=20)
    assert srv.status()["apple"]["rows"] == 2

    status, out = _post_json(srv.port, "/config", {"apple_photos": False})
    assert status == 200 and out["changed"] is True and out["reindexing"] is True
    srv._index_thread.join(timeout=20)

    st = srv.status()
    assert st["photos"] == 1                       # the folder photo, untouched
    assert st["apple"] == {"enabled": False, "rows": 0, "last": None}
    srv.shutdown()


def test_setting_apple_to_what_it_already_is_starts_no_scan(cache_dir, tmp_path,
                                                            monkeypatch):
    """Same rule as re-adding a folder: a no-op must not cost a pass over the
    whole library."""
    _fake_apple(monkeypatch, tmp_path)
    srv = _served(cache_dir, tmp_path, monkeypatch)

    status, out = _post_json(srv.port, "/config", {"apple_photos": False})
    assert status == 200
    assert out["changed"] is False and out["reindexing"] is False
    assert srv._indexing is False
    srv.shutdown()


def test_a_blocked_photos_library_is_reported_not_fatal(cache_dir, tmp_path,
                                                        monkeypatch):
    """macOS refuses the library until Full Disk Access is granted, and the whole
    index run must survive it: folders still get scanned, and the panel gets a
    sentence the user can act on."""
    from test_apple_photos import install_fake
    install_fake(monkeypatch, [], raises=PermissionError(1, "Operation not permitted"))
    srv = _served(cache_dir, tmp_path, monkeypatch)

    _post_json(srv.port, "/config", {"apple_photos": True})
    srv._index_thread.join(timeout=20)

    st = json.loads(_get(srv.port, "/status")[2])
    assert st["photos"] == 1                     # the folder was still scanned
    assert st["last_index"].get("error") is None  # the run did not fail
    assert st["apple"]["enabled"] is True and st["apple"]["rows"] == 0
    assert "Full Disk Access" in st["apple"]["last"]["error"]
    srv.shutdown()


def test_config_post_takes_only_the_setting_it_offers(cache_dir, tmp_path,
                                                      monkeypatch):
    """Not a "merge this JSON into the config" endpoint: that would let any
    loopback page rewrite `roots` — bypassing every check in add_root — or the
    model, or the port."""
    srv = _served(cache_dir, tmp_path, monkeypatch)
    before = config.load_config(cache_dir)

    for body in ({}, {"roots": ["/etc"]}, {"model": "evil"}, {"port": 1}):
        status, out = _post_json(srv.port, "/config", body)
        assert status == 400 and out["error"] == "nothing to set"
    for body in ({"apple_photos": "yes"}, {"apple_photos": 1},
                 {"apple_photos": None}):
        status, out = _post_json(srv.port, "/config", body)
        assert status == 400 and "true or false" in out["error"]

    assert config.load_config(cache_dir) == before
    srv.shutdown()


def test_config_post_sets_the_memory_limit(cache_dir, tmp_path, monkeypatch):
    """The settings panel's memory-limit input, same shape as the Apple
    Photos toggle: one POST, and the next index run picks it up — no
    restart, no rescan (a limit change is not a change to what is in the
    library)."""
    srv = _served(cache_dir, tmp_path, monkeypatch)

    status, out = _post_json(srv.port, "/config", {"max_index_memory_gb": 12})
    assert status == 200
    assert out == {"max_index_memory_gb": 12.0}
    assert config.load_config(cache_dir)["max_index_memory_gb"] == 12.0
    assert srv._indexing is False        # no scan started by this edit

    for bad in (0, -1, "12", True, None):
        status, out = _post_json(srv.port, "/config",
                                 {"max_index_memory_gb": bad})
        assert status == 400 and "positive number" in out["error"]
    srv.shutdown()


def test_config_post_refuses_a_foreign_origin(cache_dir, tmp_path, monkeypatch):
    """A POST with a CORS-simple content type reaches the handler with no
    preflight, so withholding the response header is not the defence — refusing
    the Origin is."""
    srv = _served(cache_dir, tmp_path, monkeypatch)

    status, out = _post_json(srv.port, "/config", {"apple_photos": True},
                             headers={"Origin": "https://evil.example"})
    assert status == 403 and out["error"] == "forbidden"

    status, _, _ = _raw(srv.port, "/config", {"Host": "evil.example"},
                        method="POST", body=b'{"apple_photos": true}')
    assert status == 403
    assert config.load_config(cache_dir)["apple_photos"] is False
    srv.shutdown()


# ── videos ─────────────────────────────────────────────────────────────────
def _video_root(tmp_path):
    """A photograph, a graphic and two videos — the three things the scope toggle
    has to be able to tell apart."""
    root = tmp_path / "lib"
    root.mkdir()
    _camera_photo(root / "shot.jpg", "green")
    Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(root / "overlay.png", "PNG")
    write_video(root / "clip.mp4", seconds=2.0, fps=10, size=32)
    write_video(root / "rec.webm", codec="libvpx", seconds=1.0, fps=8, size=32)
    return root


def _video_server(cache_dir, tmp_path, monkeypatch, serve=False):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    srv = LensServer(cache_dir, roots=[str(_video_root(tmp_path))],
                     embedder=FakeEmbedder(), port=0)
    srv.index_now()
    if serve:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_videos_scope_holds_the_videos_and_nothing_else(cache_dir, tmp_path,
                                                           monkeypatch):
    """Three scopes, and the photographs' one is the default. A video is not a
    photograph — the page would otherwise open on a grid where every third tile
    wants pressing play — and it is not a graphic either, so it needs its own."""
    srv = _video_server(cache_dir, tmp_path, monkeypatch)

    assert _names(srv.run_query("", scope="photos")) == ["shot.jpg"]
    assert _names(srv.run_query("", scope="videos")) == ["clip.mp4", "rec.webm"]
    assert _names(srv.run_query("", scope="all")) == [
        "clip.mp4", "overlay.png", "rec.webm", "shot.jpg"]
    assert srv.run_query("", scope="videos")["scope"] == "videos"
    srv.shutdown()


def test_a_video_ranks_through_the_same_path_as_a_photograph(cache_dir, tmp_path,
                                                            monkeypatch):
    """No special case below run_query: a video row carries one vector like a
    photograph's, pooled from its frames, so a semantic query scores it with the
    same dot product and it can outrank a still."""
    srv = _video_server(cache_dir, tmp_path, monkeypatch)
    res = srv.run_query("something", scope="all")
    scored = [r for g in res["groups"] for r in g["items"]]
    assert all(r["score"] is not None for r in scored)
    assert res["searched"] == 4 and res["strong"] is not None
    srv.shutdown()


def test_items_carry_what_a_video_tile_needs_to_draw_itself(cache_dir, tmp_path,
                                                           monkeypatch):
    """A card has to know whether to draw a play glyph and a running time before
    it has fetched anything else, so both ride along with every item — one
    nullable float against a /meta request per tile."""
    srv = _video_server(cache_dir, tmp_path, monkeypatch)
    items = {r["path"].rsplit("/", 1)[-1]: r
             for g in srv.run_query("", scope="all")["groups"] for r in g["items"]}

    assert items["clip.mp4"]["kind"] == "video"
    assert items["clip.mp4"]["duration_s"] == pytest.approx(2.0, abs=0.15)
    assert items["shot.jpg"]["kind"] == "image"
    assert items["shot.jpg"]["duration_s"] is None
    srv.shutdown()


def test_status_reports_all_three_scopes(cache_dir, tmp_path, monkeypatch):
    """The toggle labels its three buttons off the one /status it already polls,
    and the three do not add up to the library: "all" also holds the graphic."""
    srv = _video_server(cache_dir, tmp_path, monkeypatch)
    st = srv.status()
    assert st["photos_scope"] == 1
    assert st["videos_scope"] == 2
    assert st["all_scope"] == 4
    assert st["photos"] == 4              # unchanged meaning: files catalogued
    srv.shutdown()


def test_a_video_scope_deep_link_is_honoured_over_http(cache_dir, tmp_path,
                                                       monkeypatch):
    srv = _video_server(cache_dir, tmp_path, monkeypatch, serve=True)
    status, _, body = _get(srv.port, "/query?q=&scope=videos")
    res = json.loads(body)
    assert status == 200 and res["scope"] == "videos" and res["total"] == 2
    # ...and a scope this daemon does not offer still lands on the photographs
    _, _, body = _get(srv.port, "/query?q=&scope=video")
    assert json.loads(body)["scope"] == "photos"
    srv.shutdown()


def test_a_video_serves_a_thumbnail_at_both_sizes(cache_dir, tmp_path, monkeypatch):
    """The grid asks for 512, which the index already wrote; the lightbox asks for
    2048, which nothing has rendered yet — so the route decodes one frame for it,
    and it has to be the same frame the tile is showing (thumbs.ensure_video_thumb).
    """
    srv = _video_server(cache_dir, tmp_path, monkeypatch, serve=True)
    row = srv.store.get_photo(str(tmp_path / "lib" / "clip.mp4"))

    small = _raw(srv.port, f"/thumb/{row['id']}?s=512")
    large = _raw(srv.port, f"/thumb/{row['id']}?s=2048")
    assert small[0] == 200 and small[1]["Content-Type"] == "image/webp"
    assert large[0] == 200 and large[1]["Content-Type"] == "image/webp"

    import io
    with Image.open(io.BytesIO(small[2])) as a, Image.open(io.BytesIO(large[2])) as b:
        assert band_of(a) == band_of(b), "the tile and the lightbox disagree"
    srv.shutdown()


def test_a_videos_details_are_answered_by_one_request(cache_dir, tmp_path,
                                                     monkeypatch):
    """A movie container has no EXIF to dump, so the whole probe travels under
    `_video` in raw_exif — the panel needs the duration, the dimensions, the
    codec and the date, and asks for them once."""
    srv = _video_server(cache_dir, tmp_path, monkeypatch, serve=True)
    row = srv.store.get_photo(str(tmp_path / "lib" / "clip.mp4"))

    status, _, body = _get(srv.port, f"/meta/{row['id']}")
    meta = json.loads(body)
    assert status == 200
    assert meta["kind"] == "video"
    assert meta["duration_s"] == pytest.approx(2.0, abs=0.15)
    assert meta["format"] == "MP4"
    assert meta["raw_exif"]["_video"]["codec"] == "h264"
    srv.shutdown()


def test_a_video_is_described_by_the_same_tag_vocabulary(cache_dir, tmp_path,
                                                        monkeypatch):
    """The chips in the details panel are the closest labels to this row's own
    vector, and a video has one of those like anything else — so "looks like"
    works on a clip without a line of special-case code."""
    srv = _video_server(cache_dir, tmp_path, monkeypatch)
    row = srv.store.get_photo(str(tmp_path / "lib" / "clip.mp4"))
    out = srv.tags_for(row["id"])
    assert out and len(out) == tags.TOP_K      # non-empty: the row has a vector
    assert all(t["label"] in tags.VOCAB for t in out)
    # ...and `[]` would have meant something else entirely: "no vector yet",
    # which is what the panel says out loud instead of leaving a gap
    assert srv.tags_for(10 ** 6) is None
    srv.shutdown()


# ── people ─────────────────────────────────────────────────────────────────
# Identity is colour: `face_photo` writes bands of it and FakeFaceModel reads one
# face per band, with a vector per person (see tests/conftest.py).
def _people_server(cache_dir, tmp_path, monkeypatch, cast=None):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    cast = cast or {"ana": 3, "ben": 3}
    for name, n in cast.items():
        for i in range(n):
            face_photo(root / f"{name}{i}.jpg", [name])
    face_photo(root / "beach.jpg", [])
    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(),
                     port=0, face_model=FakeFaceModel())
    srv.index_now()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _people(port):
    status, _, body = _get(port, "/people")
    assert status == 200
    return json.loads(body)["people"]


def test_people_endpoint_carries_what_a_card_needs(cache_dir, tmp_path,
                                                   monkeypatch):
    """A person card stands on its own: a face to show, a name or the absence of
    one, and the two counts — a group shot is one photograph and several faces,
    so both numbers are real."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch,
                         {"ana": 4, "ben": 3})
    people = _people(srv.port)

    assert len(people) == 2
    assert set(people[0]) == {"id", "name", "face_count", "photo_count",
                              "cover_face_id"}
    # most-photographed first: that order answers "who is in my library?"
    assert [p["photo_count"] for p in people] == [4, 3]
    assert all(p["name"] is None for p in people)      # nobody has been named

    # ...and the cover renders, cropped out of the thumbnail lens already had
    status, headers, body = _get(srv.port,
                                 f"/people/{people[0]['cover_face_id']}/face.webp")
    assert status == 200 and headers["Content-Type"] == "image/webp"
    assert body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    srv.shutdown()


def test_a_face_crop_is_bounded_in_size_and_404s_for_nobody(cache_dir, tmp_path,
                                                            monkeypatch):
    """A size in a URL is a request for work, so it is snapped to the two sizes
    the view asks for rather than honoured — and an id nobody has is a 404, not a
    traceback."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch)
    fid = _people(srv.port)[0]["cover_face_id"]

    for s in ("200", "400", "99999", ""):
        status, _, _ = _get(srv.port, f"/people/{fid}/face.webp?s={s}")
        assert status == 200, s
    assert _raw(srv.port, f"/people/{fid}/face.webp?s=abc")[0] == 400
    status, _, body = _raw(srv.port, "/people/999999/face.webp")
    assert status == 404 and b"no such face" in body
    srv.shutdown()


def test_query_can_be_narrowed_to_one_person(cache_dir, tmp_path, monkeypatch):
    """Opening a person is a query like any other, so it composes: the response
    echoes the person back, and a search typed inside it searches within it."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch, {"ana": 3, "ben": 4})
    people = _people(srv.port)
    ben = people[0]["id"]

    _, _, body = _get(srv.port, f"/query?q=&person={ben}")
    out = json.loads(body)
    assert out["person"] == ben
    assert out["total"] == 4
    ids = {it["id"] for it in out["groups"][0]["items"]}
    faces = srv.store.faces_for_photos(ids)
    assert all(any(f["cluster_id"] == ben for f in faces[pid]) for pid in ids)

    # a person nobody has is an empty answer, not a 500
    _, _, body = _get(srv.port, "/query?q=&person=99999")
    assert json.loads(body)["total"] == 0
    # ...and a person id that is not a number is a link this view never wrote
    assert _raw(srv.port, "/query?q=&person=nonsense")[0] == 400
    srv.shutdown()


def test_renaming_a_person_makes_the_name_searchable(cache_dir, tmp_path,
                                                     monkeypatch):
    """The point of a name is not the label on the card — it is that "photos of
    Ana" starts working, through the same vocabulary that matches album names."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch, {"ana": 3, "ben": 4})
    people = _people(srv.port)
    ana = people[1]["id"]

    status, out = _post_json(srv.port, f"/people/{ana}/rename",
                             {"name": "  Ana Costa  "})
    assert status == 200
    assert out["person"] == {"id": ana, "name": "Ana Costa"}   # trimmed

    _, _, body = _get(srv.port, "/query?q=" + urllib.parse.quote("photos of ana costa"))
    res = json.loads(body)
    assert res["parsed"]["people"] == ["Ana Costa"]
    assert res["total"] == 3 and res["parsed"]["residual"] == ""

    # the name is on the card too, and clearing it is a real operation
    assert [p["name"] for p in _people(srv.port) if p["id"] == ana] == ["Ana Costa"]
    status, out = _post_json(srv.port, f"/people/{ana}/rename", {"name": ""})
    assert status == 200 and out["person"]["name"] is None
    _, _, body = _get(srv.port, "/query?q=" + urllib.parse.quote("photos of ana costa"))
    assert json.loads(body)["parsed"]["people"] == []
    srv.shutdown()


def test_rename_refuses_what_it_cannot_store(cache_dir, tmp_path, monkeypatch):
    srv = _people_server(cache_dir, tmp_path, monkeypatch)
    pid = _people(srv.port)[0]["id"]

    status, out = _post_json(srv.port, f"/people/{pid}/rename", {})
    assert status == 400 and out["error"] == "name required"
    status, out = _post_json(srv.port, f"/people/{pid}/rename", {"name": 7})
    assert status == 400 and out["error"] == "bad name"
    status, out = _post_json(srv.port, "/people/99999/rename", {"name": "Ana"})
    assert status == 404
    # a name is a label and a phrase, not a document
    status, out = _post_json(srv.port, f"/people/{pid}/rename",
                             {"name": "A" * 500})
    assert status == 200 and len(out["person"]["name"]) == 80
    srv.shutdown()


def test_merging_two_people_keeps_the_name_and_both_sets_of_photographs(
        cache_dir, tmp_path, monkeypatch):
    """The same person found twice is the failure mode this feature has to make
    cheap to fix: one press, and their photographs are in one grid under one
    name — and it survives the next index run's re-clustering."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch, {"ana": 3, "ben": 3})
    people = _people(srv.port)
    keep, absorb = people[0]["id"], people[1]["id"]
    _post_json(srv.port, f"/people/{absorb}/rename", {"name": "Ana"})

    status, out = _post_json(srv.port, "/people/merge",
                             {"keep": keep, "absorb": absorb})
    assert status == 200
    assert out["person"]["id"] == keep
    assert out["person"]["photo_count"] == 6
    # the name came across rather than being lost with the row it was on
    assert out["person"]["name"] == "Ana"
    assert [p["id"] for p in _people(srv.port)] == [keep]

    _, _, body = _get(srv.port, f"/query?q=&person={keep}")
    assert json.loads(body)["total"] == 6

    # ...and re-clustering does not split them apart again
    srv.index_now()
    assert [(p["id"], p["name"]) for p in _people(srv.port)] == [(keep, "Ana")]
    _, _, body = _get(srv.port, "/query?q=" + urllib.parse.quote("photos of ana"))
    assert json.loads(body)["total"] == 6
    srv.shutdown()


def test_merge_refuses_ids_it_cannot_merge(cache_dir, tmp_path, monkeypatch):
    srv = _people_server(cache_dir, tmp_path, monkeypatch)
    people = _people(srv.port)
    a, b = people[0]["id"], people[1]["id"]

    for body in ({}, {"keep": a}, {"keep": a, "absorb": "x"}):
        status, out = _post_json(srv.port, "/people/merge", body)
        assert status == 400, body
    status, _ = _post_json(srv.port, "/people/merge", {"keep": a, "absorb": a})
    assert status == 404                       # merging somebody into themselves
    status, _ = _post_json(srv.port, "/people/merge", {"keep": a, "absorb": 99999})
    assert status == 404
    assert len(_people(srv.port)) == 2         # nothing half-applied
    srv.shutdown()


def test_people_writes_are_refused_from_a_foreign_page(cache_dir, tmp_path,
                                                       monkeypatch):
    """Same guard as every other POST: a page on the internet must not be able to
    rename or merge the people in somebody's photo library through their own
    browser. A request with no Origin at all is the CLI, and is fine."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch)
    people = _people(srv.port)
    pid = people[0]["id"]

    hostile = {"Origin": "https://evil.example"}
    status, _ = _post_json(srv.port, f"/people/{pid}/rename", {"name": "Hacked"},
                           hostile)
    assert status == 403
    status, _ = _post_json(srv.port, "/people/merge",
                           {"keep": people[0]["id"], "absorb": people[1]["id"]},
                           hostile)
    assert status == 403
    assert all(p["name"] is None for p in _people(srv.port))
    assert len(_people(srv.port)) == 2

    # ...and the same requests from the view's own origin are allowed
    ours = {"Origin": f"http://127.0.0.1:{srv.port}"}
    status, _ = _post_json(srv.port, f"/people/{pid}/rename", {"name": "Ana"},
                           ours)
    assert status == 200
    srv.shutdown()


def test_meta_says_who_is_in_the_photograph(cache_dir, tmp_path, monkeypatch):
    """The details panel draws these as chips beside the camera and the exposure,
    so they ride along on the request that already reads the row — and a face
    lens has seen nowhere else still travels, with no person, because "somebody is
    in this photo" is a true and useful thing to show."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch)
    pid = _people(srv.port)[0]["id"]
    _post_json(srv.port, f"/people/{pid}/rename", {"name": "Ana"})

    _, _, body = _get(srv.port, f"/query?q=&person={pid}")
    photo = json.loads(body)["groups"][0]["items"][0]
    _, _, body = _get(srv.port, f"/meta/{photo['id']}")
    meta = json.loads(body)

    assert len(meta["people"]) == 1
    face = meta["people"][0]
    assert face["person_id"] == pid and face["name"] == "Ana"
    assert face["prob"] > 0.9 and len(face["bbox"]) == 4

    # a photograph with nobody in it says so with an empty list, not a missing key
    _, _, body = _get(srv.port, "/query?q=")
    beach = next(it for it in json.loads(body)["groups"][0]["items"]
                 if it["path"].endswith("beach.jpg"))
    _, _, body = _get(srv.port, f"/meta/{beach['id']}")
    assert json.loads(body)["people"] == []
    srv.shutdown()


def test_status_counts_faces_against_how_much_has_been_looked_at(cache_dir,
                                                                 tmp_path,
                                                                 monkeypatch):
    """"3 people" means nothing without a denominator: the face sweep runs after
    the photographs are searchable, so the view has to be able to say "so far"."""
    srv = _people_server(cache_dir, tmp_path, monkeypatch)
    st = srv.status()
    assert st["faces"] == {"faces": 6, "clustered": 6, "people": 2, "named": 0,
                           "scanned": 7, "eligible": 7}
    srv.shutdown()


def test_progress_says_which_sweep_it_is_reporting(cache_dir, tmp_path,
                                                    monkeypatch):
    """Two sweeps over one library, so a bar that fills twice has to be able to
    say why (see indexer.STAGE_FACES)."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(6):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    srv = LensServer(cache_dir, roots=[str(root)], embedder=SlowEmbedder(),
                     port=0, face_model=FakeFaceModel())
    seen = []
    t = threading.Thread(target=srv.index_now, daemon=True)
    t.start()
    while t.is_alive():
        p = srv.status()["progress"]
        if p:
            seen.append(p["stage"])
        time.sleep(0.05)
    t.join()
    assert seen and set(seen) <= {"index", "faces"}
    assert srv.status()["progress"] is None
    srv.shutdown()
