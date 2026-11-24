"""Local stream proxy + ffmpeg bridge for OpenTV. Stdlib only.

Run by hand:  python3 proxy.py              (listens on http://127.0.0.1:8787)
              python3 proxy.py --port 8797  (another port, e.g. for tests)

Normally you don't: pyproject.toml declares this file as the folder's
fused-render background daemon, and index.html starts it when a stream needs
it (fused.daemon.start()). In that mode fused-render runs it as
`proxy.py --status <path> --cache <dir> --version <str>`; it then also binds a
token-guarded control port (:0) for /ping and /quit and publishes
{port, token, pid} to the status file. The stream proxy itself stays on 8787,
unauthenticated, because hls.js fetches it with plain GETs.

Endpoints (GET, CORS-open, 127.0.0.1 only):

  /proxy?url=U         Fetch U server-side — browser User-Agent, lenient TLS,
                       gzip decoded, Range passed through, upstream status
                       passed through. Playlists are rewritten so every URI
                       (variants, segments, keys, init maps) goes through
                       /proxy too; anything else is streamed through in
                       chunks with a size cap, so an endless raw stream can
                       neither hang a thread nor eat memory.
  /check?url=U[&deep=1]
                       The signal check: one small GET → what U serves
                       (hls-master, hls-media, ts, dash, mp4, html, empty,
                       dead …), HTTP status, CORS, and a plain-English error.
                       deep=1 also follows an HLS master to a media playlist
                       and counts its segments.
  /bridge/start?url=U&mode=auto|copy|transcode
                       Start ffmpeg turning U into a local HLS stream hls.js
                       can play: raw MPEG-TS, DASH, HEVC / AC-3 / MP2 codecs.
                       auto probes the codecs first and copies H.264 / AAC,
                       transcoding only what the browser can't decode.
  /bridge/status?id=X  {state, segments, playlist_ready, error, codecs, …}
  /bridge/X/<file>     The generated playlist and segments.
  /bridge/stop?id=X    Stop ffmpeg. A bridge nobody has asked for in 30 s
                       stops by itself.

Failures and bridge events are logged to <cache dir>/proxy.log.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import paths  # noqa: E402
import streams  # noqa: E402

PORT = 8787
UPSTREAM_TIMEOUT = 8            # seconds to connect / per read
PLAYLIST_CAP = 16 * 1024 * 1024  # a playlist bigger than this is not one
STREAM_CAP = 256 * 1024 * 1024   # per request; stops a runaway raw stream
CHUNK = 64 * 1024
BRIDGE_DIR = os.path.join(paths.CACHE_DIR, "bridge")
BRIDGE_IDLE = 30                 # seconds without a request → ffmpeg stopped
BRIDGE_MAX = 2                   # concurrent ffmpeg bridges
LOG_PATH = os.path.join(paths.CACHE_DIR, "proxy.log")

_log_lock = threading.Lock()


def log(msg: str):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    with _log_lock:
        try:
            os.makedirs(paths.CACHE_DIR, exist_ok=True)
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


def proxied(url: str) -> str:
    return "/proxy?url=" + urllib.parse.quote(url, safe="")


def rewrite_playlist(text: str, base_url: str) -> str:
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#") or stripped.startswith("EXT"):
            # some servers emit malformed tags missing the leading '#'
            out.append(URI_ATTR_RE.sub(
                lambda m: 'URI="%s"' % proxied(urllib.parse.urljoin(base_url, m.group(1))),
                line))
        else:
            out.append(proxied(urllib.parse.urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"


def _url_param(qs) -> str:
    url = (qs.get("url") or [""])[0].strip()
    return url if url.startswith(("http://", "https://")) else ""


def _read_at_least(resp, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = resp.read1(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


# ------------------------------------------------------------------ bridge

class Bridge:
    """One ffmpeg process turning a stream into local HLS under BRIDGE_DIR/<id>."""

    def __init__(self, url: str, mode: str, target: str = "h264"):
        self.id = secrets.token_hex(6)
        self.url = url
        self.source = url         # the variant ffmpeg actually reads
        self.mode = mode
        self.target = target      # "h264" (copy when possible) or "vp9" (always transcode)
        self.dir = os.path.join(BRIDGE_DIR, self.id)
        self.state = "starting"   # starting → probing → running → ended | failed | stopped
        self.error = ""
        self.codecs = {}
        self.video = None         # "copy" or the encoder used
        self.audio = None         # "copy" or "aac"
        self.proc = None
        self.created = self.last_access = time.monotonic()
        self._stop = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.state in ("starting", "probing", "running")

    def start(self):
        os.makedirs(self.dir, exist_ok=True)
        threading.Thread(target=self._run, daemon=True, name=f"bridge-{self.id}").start()

    def _fail(self, msg: str):
        self.error = msg
        self.state = "failed"
        log(f"bridge {self.id} failed: {msg}")

    def _run(self):
        try:
            ffmpeg = streams.find_ffmpeg()
            if not ffmpeg:
                return self._fail("ffmpeg not found — install it (macOS: brew install ffmpeg) and try again")
            # A master playlist: feed ffmpeg one variant (preferably H.264,
            # so it can be copied) instead of letting it open every variant,
            # and skip variants that turn out to be dead.
            sources = [self.url]
            try:
                f = streams.fetch(self.url, cap=4_000_000, timeout=6)
                text = streams.decode_text(f.body)
                if text.startswith("#EXTM3U") and "#EXT-X-STREAM-INF" in text:
                    sources = streams.rank_variants(text, f.final_url)[:3] or [self.url]
            except Exception:
                pass
            has_video = True
            if self.target == "vp9":
                return self._run_vp9(ffmpeg, sources[0])
            if self.mode == "copy":
                vcopy = acopy = True
                self.source = sources[0]
            elif self.mode == "transcode":
                vcopy = acopy = False
                self.source = sources[0]
            else:
                self.state = "probing"
                info = None
                for src in sources:
                    info = streams.probe_codecs(src, timeout=12, ffmpeg=ffmpeg)
                    if self._stop:
                        return
                    if info["video"] or info["audio"]:
                        self.source = src
                        break
                self.codecs = {"video": info["video"], "audio": info["audio"]}
                if not info["video"] and not info["audio"]:
                    return self._fail("ffmpeg can't read this stream: "
                                      + (info["error"] or "no audio or video found"))
                has_video = info["video"] is not None
                vcopy = info["video"] == "h264"
                acopy = info["audio"] in (None, "aac")
            venc = None
            if has_video and not vcopy:
                venc = streams.h264_encoder(ffmpeg)
                if not venc:
                    return self._fail("this ffmpeg has no H.264 encoder (needs h264_videotoolbox or libx264)")
            self.video = ("copy" if vcopy else venc) if has_video else None
            self.audio = "copy" if acopy else "aac"
            cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
                   "-user_agent", streams.UA, "-rw_timeout", "15000000",
                   "-reconnect", "1", "-reconnect_streamed", "1",
                   "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
                   "-fflags", "+genpts+discardcorrupt",
                   "-probesize", "3000000", "-analyzeduration", "3000000",
                   "-i", self.source, "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn"]
            if has_video:
                if vcopy:
                    cmd += ["-c:v", "copy"]
                else:
                    cmd += ["-c:v", venc, "-pix_fmt", "yuv420p",
                            "-vf", "scale='min(1280,iw)':-2", "-g", "50",
                            "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k"]
                    cmd += (["-realtime", "1"] if venc == "h264_videotoolbox"
                            else ["-preset", "veryfast", "-tune", "zerolatency"])
            cmd += (["-c:a", "copy"] if acopy
                    else ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000"])
            cmd += ["-f", "hls", "-hls_time", "2", "-hls_list_size", "12",
                    "-hls_flags", "delete_segments+independent_segments+temp_file",
                    "-hls_segment_type", "mpegts",
                    "-hls_segment_filename", os.path.join(self.dir, "seg%06d.ts"),
                    os.path.join(self.dir, "index.m3u8")]
            self._spawn(cmd)
        except Exception as e:  # never let a bridge thread die silently
            self._fail(f"{type(e).__name__}: {e}")

    def _run_vp9(self, ffmpeg: str, src: str):
        """VP9 + Opus in fragmented MP4: the one combination a Chromium built
        without proprietary codecs can still play through hls.js."""
        self.source = src
        self.video, self.audio = "libvpx-vp9", "libopus"
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
               "-user_agent", streams.UA, "-rw_timeout", "15000000",
               "-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
               "-fflags", "+genpts+discardcorrupt",
               "-probesize", "3000000", "-analyzeduration", "3000000",
               "-i", src, "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn",
               "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
               "-row-mt", "1", "-threads", "4", "-pix_fmt", "yuv420p",
               "-vf", "scale='min(1280,iw)':-2",
               "-g", "30", "-force_key_frames", "expr:gte(t,n_forced*1)",
               "-b:v", "1800k", "-maxrate", "2200k", "-bufsize", "4400k",
               "-c:a", "libopus", "-b:a", "96k", "-ac", "2", "-ar", "48000",
               "-f", "hls", "-hls_time", "1", "-hls_list_size", "16",
               "-hls_flags", "delete_segments+independent_segments+temp_file",
               "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
               "-hls_segment_filename", os.path.join(self.dir, "seg%06d.m4s"),
               os.path.join(self.dir, "index.m3u8")]
        self._spawn(cmd)

    def _spawn(self, cmd: list):
        logf = open(os.path.join(self.dir, "ffmpeg.log"), "wb")
        with self._lock:
            if self._stop:
                logf.close()
                return
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=logf, start_new_session=True)
            self.state = "running"
        log(f"bridge {self.id} ffmpeg started video={self.video} audio={self.audio} {self.source}")
        rc = self.proc.wait()
        logf.close()
        if self._stop:
            self.state = "stopped"
        elif rc == 0:
            self.state = "ended"
            log(f"bridge {self.id} stream ended")
        else:
            self._fail(f"ffmpeg exited with code {rc}: {self.log_tail(1) or 'no details'}")

    def touch(self):
        self.last_access = time.monotonic()

    def log_tail(self, n: int = 3) -> str:
        try:
            with open(os.path.join(self.dir, "ffmpeg.log"), "rb") as f:
                lines = f.read()[-4000:].decode("utf-8", "replace").strip().splitlines()
            return " | ".join(l.strip() for l in lines[-n:] if l.strip())[:400]
        except OSError:
            return ""

    def segments(self) -> int:
        try:
            return sum(1 for f in os.listdir(self.dir)
                       if f.startswith("seg") and f.endswith((".ts", ".m4s")))
        except OSError:
            return 0

    def playlist_ready(self) -> bool:
        try:
            with open(os.path.join(self.dir, "index.m3u8"), "rb") as f:
                return b"#EXTINF" in f.read()
        except OSError:
            return False

    def stop(self):
        with self._lock:
            self._stop = True
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except OSError:
                pass
        if self.running:
            self.state = "stopped"

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def status(self) -> dict:
        return {"id": self.id, "state": self.state, "running": self.running,
                "segments": self.segments(), "playlist_ready": self.playlist_ready(),
                "error": self.error, "codecs": self.codecs, "video": self.video,
                "audio": self.audio, "mode": self.mode, "log_tail": self.log_tail(),
                "source": self.source, "target": self.target,
                "playlist": f"/bridge/{self.id}/index.m3u8",
                "age": round(time.monotonic() - self.created, 1)}


BRIDGES: dict = {}
_bridges_lock = threading.Lock()
_reaper_started = False


def _reaper():
    while True:
        time.sleep(5)
        now = time.monotonic()
        for bid, b in list(BRIDGES.items()):
            if b.running and now - b.last_access > BRIDGE_IDLE:
                log(f"bridge {bid} idle, stopping")
                b.stop()
            elif not b.running and now - b.last_access > BRIDGE_IDLE * 4:
                b.cleanup()
                with _bridges_lock:
                    BRIDGES.pop(bid, None)


def start_bridge(url: str, mode: str, target: str = "h264") -> Bridge:
    global _reaper_started
    with _bridges_lock:
        if not _reaper_started:
            threading.Thread(target=_reaper, daemon=True, name="bridge-reaper").start()
            _reaper_started = True
        running = sorted((b for b in BRIDGES.values() if b.running), key=lambda b: b.created)
        for old in running[:max(0, len(running) - (BRIDGE_MAX - 1))]:
            old.stop()
        b = Bridge(url, mode, target)
        BRIDGES[b.id] = b
    b.start()
    return b


def stop_all_bridges():
    with _bridges_lock:
        items = list(BRIDGES.values())
        BRIDGES.clear()
    for b in items:
        b.stop()
        b.cleanup()


# ----------------------------------------------------------------- servers

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128   # the default 5 drops connections under a burst

    def handle_error(self, request, client_address):
        # A player dropping a keep-alive connection is not worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    BRIDGE_FILE_RE = re.compile(r"index\.m3u8|init\.mp4|seg\d+\.(ts|m4s)")

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Length, Content-Range, Accept-Ranges, Content-Type")

    def _send(self, status, body: bytes, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        try:
            if path == "/proxy":
                self._proxy(qs)
            elif path == "/check":
                self._check(qs)
            elif path == "/bridge/start":
                self._bridge_start(qs)
            elif path == "/bridge/status":
                self._bridge_status(qs)
            elif path == "/bridge/stop":
                self._bridge_stop(qs)
            elif path.startswith("/bridge/"):
                self._bridge_file(path)
            elif path in ("/", "/health"):
                self._json(200, {"ok": True, "pid": os.getpid(),
                                 "bridges": len([b for b in BRIDGES.values() if b.running])})
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player went away mid-response
        except Exception as e:
            log(f"ERR {path}: {type(e).__name__}: {e}")
            try:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    # /proxy ---------------------------------------------------------------

    def _proxy(self, qs):
        url = _url_param(qs)
        if not url:
            return self._json(400, {"error": "bad url"})
        rng = self.headers.get("Range")
        try:
            resp = streams.open_url(url, timeout=UPSTREAM_TIMEOUT, rng=rng)
        except urllib.error.HTTPError as e:
            log(f"proxy HTTP {e.code} {url}")
            return self._send(e.code if 400 <= e.code < 600 else 502,
                              f"upstream answered HTTP {e.code}".encode())
        except Exception as e:
            msg = streams.describe_error(e)
            log(f"proxy FAIL {msg} {url}")
            return self._send(504 if msg == "timed out" else 502, f"upstream: {msg}".encode())
        with resp:
            enc = resp.headers.get("Content-Encoding", "")
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            head_raw = _read_at_least(resp, 1024)
            head = streams.decode_body(head_raw, enc)
            if streams.strip_bom(head).startswith(b"#EXTM3U"):
                raw = head_raw + resp.read(PLAYLIST_CAP)
                text = streams.decode_text(streams.decode_body(raw, enc))
                body = rewrite_playlist(text, resp.geturl()).encode()
                return self._send(200, body, "application/vnd.apple.mpegurl",
                                  {"Cache-Control": "no-store"})
            dec = streams.make_decoder(enc)
            if dec is None and head_raw[:2] == b"\x1f\x8b":
                dec = streams.make_decoder("gzip")
            length = resp.headers.get("Content-Length")
            chunked = dec is not None or not length
            self.send_response(resp.status)
            self._cors()
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            for h in ("Content-Range", "Accept-Ranges"):
                if resp.headers.get(h):
                    self.send_header(h, resp.headers[h])
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", length)
            self.end_headers()
            sent = 0

            def push(chunk):
                nonlocal sent
                if not chunk:
                    return
                if chunked:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                sent += len(chunk)

            push(dec.decompress(head_raw) if dec else head_raw)
            while sent < STREAM_CAP:
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                push(dec.decompress(chunk) if dec else chunk)
            if dec:
                push(dec.flush())
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
            elif sent < int(length):
                self.close_connection = True  # upstream cut short / capped
            self.wfile.flush()

    # /check ---------------------------------------------------------------

    def _check(self, qs):
        url = _url_param(qs)
        if not url:
            return self._json(400, {"error": "bad url"})
        deep = (qs.get("deep") or ["0"])[0] in ("1", "true")
        try:
            timeout = min(20.0, max(1.0, float((qs.get("timeout") or [UPSTREAM_TIMEOUT])[0])))
        except ValueError:
            timeout = UPSTREAM_TIMEOUT
        r = streams.classify(url, timeout=timeout)
        if deep and r["ok"] and r["kind"] in streams.BROWSER_KINDS:
            try:
                text, murl, _ = streams.resolve_media(url, timeout=timeout)
                m = streams.parse_media(text)
                r.update(variant=murl, segments=len(m["segments"]), live=m["live"],
                         encrypted=m["key"], fmp4=bool(m["map"]))
                if not m["segments"]:
                    r.update(ok=False, error="playlist lists no segments")
            except streams.NotPlaylist as e:
                r.update(ok=False, error=str(e))
            except urllib.error.HTTPError as e:
                r.update(ok=False, error=f"variant playlist: HTTP {e.code}")
            except Exception as e:
                r.update(ok=False, error="variant playlist: " + streams.describe_error(e))
        log(f"check {r['kind']} {'ok' if r['ok'] else 'FAIL ' + r['error']} {r['ms']}ms {url}")
        self._json(200, r)

    # /bridge --------------------------------------------------------------

    def _bridge_start(self, qs):
        url = _url_param(qs)
        if not url:
            return self._json(400, {"error": "bad url"})
        mode = (qs.get("mode") or ["auto"])[0]
        target = (qs.get("target") or ["h264"])[0]
        if mode not in ("auto", "copy", "transcode"):
            return self._json(400, {"error": "mode must be auto, copy or transcode"})
        if target not in ("h264", "vp9"):
            return self._json(400, {"error": "target must be h264 or vp9"})
        b = start_bridge(url, mode, target)
        log(f"bridge {b.id} start mode={mode} target={target} {url}")
        self._json(200, {"id": b.id, "playlist": f"/bridge/{b.id}/index.m3u8",
                         "mode": mode, "target": target})

    def _bridge_status(self, qs):
        b = BRIDGES.get((qs.get("id") or [""])[0])
        if not b:
            return self._json(404, {"error": "no such bridge"})
        b.touch()
        self._json(200, b.status())

    def _bridge_stop(self, qs):
        b = BRIDGES.get((qs.get("id") or [""])[0])
        if not b:
            return self._json(404, {"error": "no such bridge"})
        b.stop()
        log(f"bridge {b.id} stopped by player")
        self._json(200, {"ok": True, "state": b.state})

    def _bridge_file(self, path):
        parts = path.split("/")
        if len(parts) != 4 or not self.BRIDGE_FILE_RE.fullmatch(parts[3]):
            return self._json(404, {"error": "not found"})
        b = BRIDGES.get(parts[2])
        if not b:
            return self._json(404, {"error": "no such bridge"})
        b.touch()
        try:
            with open(os.path.join(b.dir, parts[3]), "rb") as f:
                data = f.read()
        except OSError:
            return self._json(404, {"error": "not ready"})
        ctype = ("application/vnd.apple.mpegurl" if parts[3].endswith(".m3u8")
                 else "video/mp4" if parts[3].endswith((".mp4", ".m4s"))
                 else "video/mp2t")
        self._send(200, data, ctype, {"Cache-Control": "no-store"})


class ControlHandler(BaseHTTPRequestHandler):
    """fused-render daemon contract: token-guarded /ping and /quit."""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        token = (urllib.parse.parse_qs(parsed.query).get("t") or [""])[0]
        if not token or not secrets.compare_digest(token, self.server.token):
            self._json(403, {"error": "forbidden"})
            return
        if parsed.path == "/ping":
            self._json(200, {"ok": True, "version": self.server.version,
                             "pid": os.getpid(), "hls_port": self.server.hls_port,
                             "hls_bound": self.server.hls_bound})
        elif parsed.path == "/quit":
            self._json(200, {"ok": True})
            stop_all_bridges()
            # shutdown() from the handler thread would deadlock the server.
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json(404, {"error": "not found"})

    do_POST = do_GET


def _write_status(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status")
    ap.add_argument("--cache")
    ap.add_argument("--version", default="dev")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    shutil.rmtree(BRIDGE_DIR, ignore_errors=True)  # bridges never outlive a proxy
    log(f"proxy starting pid={os.getpid()} port={args.port} managed={bool(args.status)}")

    if not args.status:
        print(f"stream proxy on http://127.0.0.1:{args.port}/proxy?url=...")
        try:
            Server(("127.0.0.1", args.port), Handler).serve_forever()
        finally:
            stop_all_bridges()
        return

    # Managed by fused-render. Bind the proxy port first so a failure (e.g. a
    # hand-started proxy already on 8787) is known before we publish.
    hls_bound = True
    try:
        hls = Server(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"could not bind proxy port {args.port}: {e}")
        log(f"could not bind proxy port {args.port}: {e}")
        hls, hls_bound = None, False

    control = Server(("127.0.0.1", 0), ControlHandler)
    control.token = secrets.token_urlsafe(32)
    control.version = args.version
    control.hls_bound = hls_bound
    control.hls_port = args.port
    _write_status(args.status, {"port": control.server_address[1],
                                "token": control.token, "pid": os.getpid()})
    if hls is not None:
        threading.Thread(target=hls.serve_forever, daemon=True).start()
    try:
        control.serve_forever(poll_interval=0.25)
    finally:
        stop_all_bridges()
        if hls is not None:
            hls.shutdown()
            hls.server_close()
        control.server_close()


if __name__ == "__main__":
    main()
