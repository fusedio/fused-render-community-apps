"""Local upstream for the tests: a tiny HTTP server that serves every kind of
thing a public IPTV URL turns out to be (HLS, gzip-compressed HLS, a raw
never-ending MPEG-TS stream, an HTML page, 404s, a hang) plus ffmpeg-made
media files. Stdlib only; ffmpeg is needed once to build the media."""
import gzip
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
if APP not in sys.path:
    sys.path.insert(0, APP)

MEDIA = os.path.join(HERE, "_media")  # generated, gitignored


def build_media():
    """clip.ts (6 s, h264+aac), vod/index.m3u8 (2 s segments), master.m3u8."""
    import streams
    ffmpeg = streams.find_ffmpeg()
    assert ffmpeg, "ffmpeg is required to build the test media"
    os.makedirs(os.path.join(MEDIA, "vod"), exist_ok=True)
    clip = os.path.join(MEDIA, "clip.ts")
    if not os.path.exists(clip):
        subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", clip],
                       check=True, timeout=120)
    index = os.path.join(MEDIA, "vod", "index.m3u8")
    if not os.path.exists(index):
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", clip, "-c", "copy",
                        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                        "-hls_segment_filename", os.path.join(MEDIA, "vod", "seg%03d.ts"),
                        index], check=True, timeout=120)
    with open(os.path.join(MEDIA, "master.m3u8"), "w") as f:
        f.write("#EXTM3U\n#EXT-X-VERSION:3\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="mp4a.40.2"\naudio/index.m3u8\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=320x180,CODECS="avc1.64001e,mp4a.40.2"\n'
                "vod/index.m3u8\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"\n'
                "missing/index.m3u8\n")
    return MEDIA


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/page.html":
            return self._send(200, b"<!DOCTYPE html><html><body>not a stream</body></html>", "text/html")
        if path == "/mpegurl-but-html.m3u8":  # lying content-type
            return self._send(200, b"<html><body>login</body></html>", "application/vnd.apple.mpegurl")
        if path == "/empty.m3u8":
            return self._send(200, b"", "text/plain")
        if path == "/manifest.mpd":
            return self._send(200, b'<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"></MPD>',
                              "application/dash+xml")
        if path == "/hang":
            time.sleep(20)
            return self._send(200, b"late", "text/plain")
        if path == "/trickle":  # 16 KB every 250 ms, forever
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                for _ in range(400):
                    self.wfile.write(b"\x47" + b"\x00" * 16383)
                    self.wfile.flush()
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        if path == "/stream.ts":  # endless raw MPEG-TS, no Content-Length
            self.send_response(200)
            self.send_header("Content-Type", "video/mpeg")
            self.end_headers()
            data = open(os.path.join(MEDIA, "clip.ts"), "rb").read()
            try:
                for _ in range(200):
                    self.wfile.write(data)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        if path.startswith("/gz/"):  # gzip-compressed, as a few CDNs serve playlists
            fp = os.path.join(MEDIA, path[4:])
            if not os.path.isfile(fp):
                return self._send(404, b"nope", "text/plain")
            body = gzip.compress(open(fp, "rb").read())
            return self._send(200, body, "application/vnd.apple.mpegurl",
                              [("Content-Encoding", "gzip")])
        fp = os.path.normpath(os.path.join(MEDIA, path.lstrip("/")))
        if not fp.startswith(MEDIA) or not os.path.isfile(fp):
            return self._send(404, b"not found", "text/plain")
        data = open(fp, "rb").read()
        ctype = "application/vnd.apple.mpegurl" if fp.endswith(".m3u8") else "video/mp2t"
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split("-")
            a = int(a); b = int(b) if b else len(data) - 1
            part = data[a:b + 1]
            return self._send(206, part, ctype, [("Content-Range", f"bytes {a}-{b}/{len(data)}"),
                                                  ("Accept-Ranges", "bytes")])
        return self._send(200, data, ctype, [("Accept-Ranges", "bytes")])


class Upstream:
    """with Upstream() as up: up.url('/vod/index.m3u8')"""

    def __enter__(self):
        build_media()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"
