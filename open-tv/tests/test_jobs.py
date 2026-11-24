"""Run: .venv/bin/python tests/test_jobs.py — thumbnails, signal check,
health records and the detached worker, all against the local fixture."""
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="opentv-test-")
os.environ["OPEN_TV_CACHE_DIR"] = TMP  # before paths.py is imported

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import APP, MEDIA, Upstream  # noqa: E402

sys.path.insert(0, APP)
import healthcheck  # noqa: E402
import paths  # noqa: E402
import runner  # noqa: E402
import thumbnails  # noqa: E402

assert paths.CACHE_DIR == TMP, paths.CACHE_DIR


def _bad_playlist():
    with open(os.path.join(MEDIA, "bad.m3u8"), "w") as f:
        f.write("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nnope.ts\n#EXT-X-ENDLIST\n")


def test_grab():
    _bad_playlist()
    with Upstream() as up:
        t0 = time.time()
        r = thumbnails.grab(up.url("/master.m3u8"))
        assert r["ok"] and r["kind"] == "hls-master" and r["note"].startswith("variant"), r
        assert time.time() - t0 < 10
        assert os.path.exists(thumbnails.thumb_path(up.url("/master.m3u8")))
        assert thumbnails.thumb_data_uri(up.url("/master.m3u8")).startswith("data:image/jpeg;base64,")
        r = thumbnails.grab(up.url("/gz/vod/index.m3u8"))
        assert r["ok"] and r["kind"] == "hls-media", r
        r = thumbnails.grab(up.url("/stream.ts"))
        assert r["ok"] and r["kind"] == "ts" and r["note"] == "raw bytes", r
        r = thumbnails.grab(up.url("/clip.ts"))
        assert r["ok"], r
        r = thumbnails.grab(up.url("/page.html"))
        assert not r["ok"] and r["kind"] == "html" and "web page" in r["note"], r
        r = thumbnails.grab(up.url("/missing.m3u8"))
        assert not r["ok"] and r["kind"] == "dead" and r["note"] == "HTTP 404", r
        r = thumbnails.grab("http://127.0.0.1:1/x.m3u8")
        assert not r["ok"] and r["note"] == "connection refused", r
        r = thumbnails.grab(up.url("/bad.m3u8"))
        assert not r["ok"] and r["kind"] == "hls-media" and r["note"], r


def test_probe():
    _bad_playlist()
    with Upstream() as up:
        p = healthcheck.probe(up.url("/master.m3u8"))
        assert p == {"ok": True, "kind": "hls-master", "error": ""}, p
        p = healthcheck.probe(up.url("/gz/vod/index.m3u8"))
        assert p == {"ok": True, "kind": "hls-media", "error": ""}, p
        p = healthcheck.probe(up.url("/stream.ts"))
        assert p == {"ok": True, "kind": "ts", "error": ""}, p
        p = healthcheck.probe(up.url("/manifest.mpd"))
        assert p["ok"] and p["kind"] == "dash", p
        p = healthcheck.probe(up.url("/page.html"))
        assert not p["ok"] and p["kind"] == "html" and "web page" in p["error"], p
        p = healthcheck.probe(up.url("/mpegurl-but-html.m3u8"))
        assert not p["ok"] and p["kind"] == "html", p
        p = healthcheck.probe(up.url("/missing.m3u8"))
        assert p == {"ok": False, "kind": "dead", "error": "HTTP 404"}, p
        p = healthcheck.probe(up.url("/bad.m3u8"))
        assert not p["ok"] and p["kind"] == "hls-media" and p["error"] == "segment: HTTP 404", p
        t0 = time.time()
        p = healthcheck.probe(up.url("/hang"))
        assert not p["ok"] and p["error"] == "timed out" and time.time() - t0 < healthcheck.TIMEOUT + 3, p


def test_update_records_and_legacy_schema():
    import pyarrow as pa
    import pyarrow.parquet as pq
    legacy = pa.schema([f for f in healthcheck.SCHEMA if f.name not in ("kind", "last_error")])
    os.makedirs(healthcheck.CACHE_DIR, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([{
        "url": "http://legacy/x.m3u8", "name": "Legacy", "group": "Sports", "tries": 2,
        "fails": 1, "fail_pct": 50.0, "responsive_now": True, "last_checked": "2026-01-01T00:00:00+00:00",
    }], schema=legacy), healthcheck.PARQUET_PATH)
    recs = healthcheck._load_records()
    assert recs["http://legacy/x.m3u8"]["kind"] == "" and recs["http://legacy/x.m3u8"]["last_error"] == ""
    ch = lambda u, n: {"url": u, "name": n, "group": "Sports"}  # noqa: E731
    s = healthcheck.update_records([
        (ch("http://a/x.m3u8", "A"), {"ok": True, "kind": "hls-media", "error": ""}),
        (ch("http://b/x.m3u8", "B"), {"ok": False, "kind": "dead", "error": "HTTP 404"}),
        (ch("http://legacy/x.m3u8", "Legacy"), True),
    ])
    assert s["checked"] == 3 and s["responsive_now"] == 2 and s["health_pct"] == 66.7, s
    recs = healthcheck._load_records()
    assert recs["http://b/x.m3u8"]["last_error"] == "HTTP 404" and recs["http://b/x.m3u8"]["kind"] == "dead"
    assert recs["http://b/x.m3u8"]["fails"] == 1 and recs["http://b/x.m3u8"]["fail_pct"] == 100.0
    assert recs["http://legacy/x.m3u8"]["tries"] == 3 and recs["http://legacy/x.m3u8"]["responsive_now"]
    assert recs["http://a/x.m3u8"]["kind"] == "hls-media" and recs["http://a/x.m3u8"]["last_error"] == ""


def test_worker_end_to_end():
    import shutil
    shutil.rmtree(paths.RUNS_DIR, ignore_errors=True)
    if os.path.exists(healthcheck.PARQUET_PATH):
        os.remove(healthcheck.PARQUET_PATH)
    with Upstream() as up:
        pl = os.path.join(TMP, "playlists")
        os.makedirs(pl, exist_ok=True)
        entries = [("Good HLS", "/master.m3u8"), ("Gzip HLS", "/gz/vod/index.m3u8"),
                   ("Raw TS", "/stream.ts"), ("Web page", "/page.html"), ("Dead", "/missing.m3u8")]
        m3u = "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="" tvg-logo="" group-title="Sports",{n}\n{up.url(p)}\n'
            for n, p in entries)
        for name in ("sports.m3u", "all.m3u"):
            with open(os.path.join(pl, name), "w") as f:
                f.write(m3u)
        # thumbs skips every channel whose checks all failed (Dead, Web page)
        for job, total, ok in (("health", 5, 3), ("thumbs", 3, 3)):
            run_dir = os.path.join(paths.RUNS_DIR, f"test-{job}")
            os.makedirs(run_dir)
            with open(os.path.join(run_dir, "spec.json"), "w") as f:
                json.dump({"job": job, "category": "sports"}, f)
            t0 = time.time()
            runner._worker(run_dir)
            st = runner._status(f"test-{job}", 0)
            state = st["state"]
            assert not state["running"] and state["summary"], state
            assert (state["total"], state["finished"], state["ok"]) == (total, total, ok), (job, state)
            summ = state["summary"]
            if job == "health":
                assert summ["checked"] == 5 and summ["responsive_now"] == 3, summ
                kinds = {e["job"]: e["msg"]["kind"] for e in st["events"] if e["type"] == "job_end"}
                assert kinds == {"Good HLS": "hls-master", "Gzip HLS": "hls-media", "Raw TS": "ts",
                                 "Web page": "html", "Dead": "dead"}, kinds
                errs = {e["job"]: e["msg"]["error"] for e in st["events"] if e["type"] == "job_end"}
                assert errs["Dead"] == "HTTP 404" and "web page" in errs["Web page"], errs
            else:
                assert summ["grabbed"] == 3 and summ["checked"] == 3, summ
                assert summ["health"]["responsive_now"] == 3, summ["health"]
                ends = {e["job"]: e["msg"] for e in st["events"] if e["type"] == "job_end"}
                assert ends["Good HLS"]["thumb"].startswith("data:image/jpeg"), ends["Good HLS"].keys()
                assert ends["Raw TS"]["note"] == "raw bytes" and "Web page" not in ends, ends.keys()
            print(f"   {job}: {time.time() - t0:.1f}s")
        recs = healthcheck._load_records()
        assert recs[up.url("/missing.m3u8")]["fails"] == 1 and recs[up.url("/missing.m3u8")]["tries"] == 1
        assert recs[up.url("/master.m3u8")]["tries"] == 2 and recs[up.url("/master.m3u8")]["fails"] == 0


if __name__ == "__main__":
    only = sys.argv[1:]
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t0 = time.time()
            fn()
            print(f"ok {name} ({time.time() - t0:.1f}s)")
    print("ALL PASSED")
