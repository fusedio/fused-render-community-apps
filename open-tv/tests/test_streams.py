"""Run: .venv/bin/python tests/test_streams.py  (plain asserts, no pytest)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import APP, Upstream  # noqa: E402

sys.path.insert(0, APP)
import streams  # noqa: E402


def test_classify_bytes():
    cb = streams.classify_bytes
    assert cb(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8\n") == "hls-master"
    assert cb(b"\xef\xbb\xbf#EXTM3U\n#EXTINF:2,\nseg.ts\n") == "hls-media"  # BOM
    assert cb(b"\r\n#EXTM3U\n#EXTINF:2,\nseg.ts\n") == "hls-media"
    ts = (b"\x47" + b"\x00" * 187) * 3
    assert cb(ts) == "ts"
    assert cb(b"\x00\x00\x00\x18ftypisom") == "mp4"
    assert cb(b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>') == "dash"
    assert cb(b"<!DOCTYPE html><html>") == "html"
    assert cb(b"  <html>") == "html"
    assert cb(b"") == "empty"
    assert cb(b"\x1f\x8b\x08\x00") == "gzip"
    assert cb(b"hello") == "other"


def test_pick_variant():
    master = ("#EXTM3U\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\naudio.m3u8\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=3000000,AVERAGE-BANDWIDTH=2500000,RESOLUTION=1280x720\n'
              "hi/index.m3u8\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=900000,CODECS="avc1.64001e,mp4a.40.2"\n'
              "../lo/index.m3u8\n")
    lo = streams.pick_variant(master, "http://h/x/master.m3u8")
    assert lo == "http://h/lo/index.m3u8", lo
    hi = streams.pick_variant(master, "http://h/x/master.m3u8", prefer="highest")
    assert hi == "http://h/x/hi/index.m3u8", hi
    audio_only = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS=\"mp4a.40.2\"\na.m3u8\n")
    assert streams.pick_variant(audio_only, "http://h/m.m3u8") == "http://h/a.m3u8"
    assert streams.pick_variant("#EXTM3U\n#EXTINF:2,\nseg.ts\n", "http://h/m.m3u8") is None


def test_rank_variants():
    master = ("#EXTM3U\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\naudio.m3u8\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
              "1080.m3u8\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="hvc1.1.6.L93.B0,mp4a.40.2"\n'
              "hevc720.m3u8\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"\n'
              "360.m3u8\n"
              '#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=960x540,CODECS="avc1.64001f,mp4a.40.2"\n'
              "540.m3u8\n")
    ranked = streams.rank_variants(master, "http://h/m.m3u8")
    assert [u.rsplit("/", 1)[1] for u in ranked] == [
        "540.m3u8", "360.m3u8", "1080.m3u8", "hevc720.m3u8", "audio.m3u8"], ranked
    assert streams.rank_variants("#EXTM3U\n#EXTINF:2,\nseg.ts\n", "http://h/m.m3u8") == []


def test_parse_media():
    live = ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:10\n"
            "#EXTINF:4.0,\nseg10.ts\n#EXTINF:4.0,\nseg11.ts\n")
    m = streams.parse_media(live)
    assert m["segments"] == ["seg10.ts", "seg11.ts"] and m["live"] and not m["key"]
    assert m["map"] is None and not m["byterange"] and m["target_duration"] == 4
    vod = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n#EXT-X-MAP:URI="init.mp4"\n'
           "#EXTINF:2,\n#EXT-X-BYTERANGE:1000@0\nseg.m4s\n#EXT-X-ENDLIST\n")
    m = streams.parse_media(vod)
    assert not m["live"] and m["key"] and m["map"] == "init.mp4" and m["byterange"]
    assert m["segments"] == ["seg.m4s"]


def test_fetch_and_classify_live():
    with Upstream() as up:
        f = streams.fetch(up.url("/gz/master.m3u8"), cap=4096)
        assert f.body.startswith(b"#EXTM3U"), f.body[:20]  # gzip decoded
        assert f.content_type.startswith("application/vnd.apple.mpegurl")
        r = streams.classify(up.url("/master.m3u8"))
        assert r["ok"] and r["kind"] == "hls-master", r
        r = streams.classify(up.url("/gz/vod/index.m3u8"))
        assert r["ok"] and r["kind"] == "hls-media", r
        r = streams.classify(up.url("/stream.ts"), timeout=5)
        assert r["ok"] and r["kind"] == "ts", r
        r = streams.classify(up.url("/page.html"))
        assert not r["ok"] and r["kind"] == "html", r
        r = streams.classify(up.url("/mpegurl-but-html.m3u8"))
        assert not r["ok"] and r["kind"] == "html", r
        r = streams.classify(up.url("/manifest.mpd"))
        assert r["ok"] and r["kind"] == "dash", r
        r = streams.classify(up.url("/empty.m3u8"))
        assert not r["ok"] and r["kind"] == "empty", r
        r = streams.classify(up.url("/missing.m3u8"))
        assert not r["ok"] and r["kind"] == "dead" and r["status"] == 404, r
        r = streams.classify(up.url("/hang"), timeout=1)
        assert not r["ok"] and r["kind"] == "dead" and "timed out" in r["error"], r
        r = streams.classify("http://127.0.0.1:1/x.m3u8", timeout=2)
        assert not r["ok"] and r["kind"] == "dead" and r["status"] == 0, r
        text, murl, note = streams.resolve_media(up.url("/master.m3u8"))
        assert murl == up.url("/vod/index.m3u8") and "#EXTINF" in text, (murl, note)


def test_fetch_total_deadline():
    with Upstream() as up:
        t0 = time.time()
        f = streams.fetch(up.url("/trickle"), cap=50_000_000, timeout=5, max_time=1.5)
        took = time.time() - t0
        assert 1.4 < took < 3, took
        assert f.truncated and 16384 <= len(f.body) < 500_000, len(f.body)
        f = streams.fetch(up.url("/clip.ts"), cap=50_000_000, timeout=5, max_time=10)
        assert not f.truncated and len(f.body) > 100_000


def test_ffmpeg_discovery_and_probe():
    ff = streams.find_ffmpeg()
    assert ff and os.path.exists(ff), ff
    with Upstream() as up:
        info = streams.probe_codecs(up.url("/clip.ts"), timeout=20)
        assert info["video"] == "h264" and info["audio"] == "aac", info
        info = streams.probe_codecs(up.url("/page.html"), timeout=20)
        assert info["video"] is None and info["error"], info


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL PASSED")
