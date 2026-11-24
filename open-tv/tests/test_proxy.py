"""Run: .venv/bin/python tests/test_proxy.py — exercises proxy.py in-process
against the local upstream fixture (no public internet needed)."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse

os.environ["OPEN_TV_CACHE_DIR"] = tempfile.mkdtemp(prefix="opentv-test-")  # before paths.py loads

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import APP, MEDIA, Upstream  # noqa: E402

sys.path.insert(0, APP)
import proxy  # noqa: E402


class Proxy:
    def __enter__(self):
        self.server = proxy.Server(("127.0.0.1", 0), proxy.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        return self

    def __exit__(self, *a):
        proxy.stop_all_bridges()
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, headers=None, read=True, timeout=40):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r, (r.read() if read else None), c


def q(url):
    return urllib.parse.quote(url, safe="")


def test_playlist_rewrite_and_gzip():
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/proxy?url=" + q(up.url("/gz/vod/index.m3u8")))
        assert r.status == 200 and body.startswith(b"#EXTM3U"), body[:60]
        assert r.getheader("Access-Control-Allow-Origin") == "*"
        assert r.getheader("Content-Type", "").startswith("application/vnd.apple.mpegurl")
        assert b"/proxy?url=http%3A%2F%2F127.0.0.1" in body and b"seg000.ts" in body, body[:300]
        r, body, _ = px.get("/proxy?url=" + q(up.url("/master.m3u8")))
        assert r.status == 200 and b"%2Fvod%2Findex.m3u8" in body, body[:300]
        # a playlist served as HTML/other is passed through untouched
        r, body, _ = px.get("/proxy?url=" + q(up.url("/mpegurl-but-html.m3u8")))
        assert r.status == 200 and body.startswith(b"<html>")


def test_segment_passthrough_and_range():
    with Upstream() as up, Proxy() as px:
        size = os.path.getsize(os.path.join(MEDIA, "vod", "seg000.ts"))
        r, body, _ = px.get("/proxy?url=" + q(up.url("/vod/seg000.ts")))
        assert r.status == 200 and len(body) == size and body[0] == 0x47, (r.status, len(body), size)
        assert r.getheader("Content-Length") == str(size)
        r, body, _ = px.get("/proxy?url=" + q(up.url("/vod/seg000.ts")), {"Range": "bytes=0-99"})
        assert r.status == 206 and len(body) == 100, (r.status, len(body))
        assert (r.getheader("Content-Range") or "").startswith("bytes 0-99/")


def test_upstream_errors():
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/proxy?url=" + q(up.url("/missing.m3u8")))
        assert r.status == 404 and r.getheader("Access-Control-Allow-Origin") == "*", r.status
        r, body, _ = px.get("/proxy?url=" + q("http://127.0.0.1:1/x.m3u8"))
        assert r.status == 502, r.status
        r, body, _ = px.get("/proxy?url=ftp%3A%2F%2Fx")
        assert r.status == 400
        t0 = time.time()
        r, body, _ = px.get("/proxy?url=" + q(up.url("/hang")))
        assert r.status == 504 and time.time() - t0 < proxy.UPSTREAM_TIMEOUT + 4, (r.status, body)
        r, body, _ = px.get("/nope")
        assert r.status == 404


def test_endless_stream_is_streamed_and_bounded():
    with Upstream() as up, Proxy() as px:
        base = threading.active_count()
        r, _, c = px.get("/proxy?url=" + q(up.url("/stream.ts")), read=False)
        assert r.status == 200 and r.getheader("Transfer-Encoding") == "chunked", r.getheaders()
        assert r.getheader("Content-Type") == "video/mpeg"
        data = r.read(200_000)
        assert len(data) == 200_000 and data[0] == 0x47
        c.close()  # client gives up: both the proxy's and the upstream's threads must wind down
        deadline = time.time() + 10
        while time.time() < deadline and threading.active_count() > base:
            time.sleep(0.2)
        assert threading.active_count() <= base, (threading.active_count(), base)


def test_check():
    with Upstream() as up, Proxy() as px:
        for path, kind, ok in [("/master.m3u8", "hls-master", True), ("/page.html", "html", False),
                               ("/stream.ts", "ts", True), ("/missing", "dead", False),
                               ("/manifest.mpd", "dash", True)]:
            r, body, _ = px.get("/check?url=" + q(up.url(path)))
            j = json.loads(body)
            assert r.status == 200 and j["kind"] == kind and j["ok"] == ok, (path, j)
        r, body, _ = px.get("/check?deep=1&url=" + q(up.url("/master.m3u8")))
        j = json.loads(body)
        assert j["ok"] and j["segments"] >= 1 and j["variant"].endswith("/vod/index.m3u8") and j["live"] is False, j
        r, body, _ = px.get("/check?deep=1&url=" + q(up.url("/gz/vod/index.m3u8")))
        j = json.loads(body)
        assert j["ok"] and j["kind"] == "hls-media" and j["segments"] >= 1, j
        r, body, _ = px.get("/check?url=nonsense")
        assert r.status == 400


def _poll(px, bid, want, limit=35):
    deadline = time.time() + limit
    while time.time() < deadline:
        r, body, _ = px.get("/bridge/status?id=" + bid)
        j = json.loads(body)
        if want(j):
            return j
        time.sleep(0.5)
    raise AssertionError(f"bridge never reached wanted state: {j}")


def test_bridge_copy_endless_ts():
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/bridge/start?mode=copy&url=" + q(up.url("/stream.ts")))
        j = json.loads(body)
        assert r.status == 200 and j["id"] and j["playlist"].startswith("/bridge/"), j
        bid = j["id"]
        st = _poll(px, bid, lambda s: s["segments"] >= 2 or s["state"] in ("failed", "ended"))
        assert st["state"] == "running" and st["playlist_ready"], st
        r, body, _ = px.get(j["playlist"])
        assert r.status == 200 and body.startswith(b"#EXTM3U") and b"seg" in body, body[:200]
        assert r.getheader("Access-Control-Allow-Origin") == "*"
        assert r.getheader("Content-Type", "").startswith("application/vnd.apple.mpegurl")
        seg = next(l for l in body.decode().splitlines() if l and not l.startswith("#"))
        r, sbody, _ = px.get(f"/bridge/{bid}/{seg}")
        assert r.status == 200 and sbody[0] == 0x47 and r.getheader("Content-Type") == "video/mp2t"
        r, body, _ = px.get(f"/bridge/{bid}/../../etc/passwd")
        assert r.status in (400, 404)
        r, body, _ = px.get("/bridge/stop?id=" + bid)
        assert json.loads(body)["ok"]
        st = _poll(px, bid, lambda s: not s["running"], 10)
        assert st["state"] == "stopped", st


def test_bridge_auto_probe_and_failure():
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/bridge/start?mode=auto&url=" + q(up.url("/page.html")))
        bid = json.loads(body)["id"]
        st = _poll(px, bid, lambda s: s["state"] in ("failed", "ended", "running"))
        assert st["state"] == "failed" and st["error"], st
        r, body, _ = px.get("/bridge/start?mode=auto&url=" + q(up.url("/clip.ts")))
        bid = json.loads(body)["id"]
        st = _poll(px, bid, lambda s: s["segments"] >= 1 or s["state"] == "failed")
        assert st["state"] in ("running", "ended") and st["codecs"]["video"] == "h264", st
        assert st["video"] == "copy" and st["audio"] == "copy", st
        st = _poll(px, bid, lambda s: s["state"] == "ended", 30)  # 6 s clip finishes
        r, body, _ = px.get(f"/bridge/{bid}/index.m3u8")
        assert b"#EXT-X-ENDLIST" in body


def test_bridge_master_skips_dead_variant():
    # master.m3u8's best-looking variant (720p) is a dead link; the bridge must
    # move on to the one that answers.
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/bridge/start?mode=auto&url=" + q(up.url("/master.m3u8")))
        bid = json.loads(body)["id"]
        st = _poll(px, bid, lambda s: s["segments"] >= 1 or s["state"] == "failed", 40)
        assert st["state"] in ("running", "ended") and st["source"].endswith("/vod/index.m3u8"), st
        assert st["video"] == "copy" and st["codecs"]["video"] == "h264", st


def test_bridge_vp9():
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/bridge/start?mode=auto&target=vp9&url=" + q(up.url("/stream.ts")))
        j = json.loads(body)
        assert j["target"] == "vp9", j
        st = _poll(px, j["id"], lambda s: s["segments"] >= 2 or s["state"] in ("failed", "ended"), 60)
        assert st["state"] == "running" and st["video"] == "libvpx-vp9" and st["audio"] == "libopus", st
        r, body, _ = px.get(j["playlist"])
        assert b'#EXT-X-MAP:URI="init.mp4"' in body and b".m4s" in body, body[:300]
        r, ibody, _ = px.get(f"/bridge/{j['id']}/init.mp4")
        assert r.status == 200 and ibody[4:8] == b"ftyp" and r.getheader("Content-Type") == "video/mp4"
        seg = next(l for l in body.decode().splitlines() if l and not l.startswith("#"))
        r, sbody, _ = px.get(f"/bridge/{j['id']}/{seg}")
        assert r.status == 200 and sbody[4:8] in (b"styp", b"moof") and b"moof" in sbody[:4096], sbody[:16]
        px.get("/bridge/stop?id=" + j["id"])
        r, body, _ = px.get("/bridge/start?target=av1&url=" + q(up.url("/stream.ts")))
        assert r.status == 400


def test_bridge_transcode():
    with Upstream() as up, Proxy() as px:
        r, body, _ = px.get("/bridge/start?mode=transcode&url=" + q(up.url("/clip.ts")))
        bid = json.loads(body)["id"]
        st = _poll(px, bid, lambda s: s["segments"] >= 1 or s["state"] == "failed", 40)
        assert st["state"] in ("running", "ended") and st["video"] not in ("copy", None), st
        assert st["audio"] == "aac", st


if __name__ == "__main__":
    only = sys.argv[1:]
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and (not only or name in only):
            t0 = time.time()
            fn()
            print(f"ok {name} ({time.time() - t0:.1f}s)")
    print("ALL PASSED")
