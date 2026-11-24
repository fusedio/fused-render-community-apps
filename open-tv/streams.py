"""Stream inspection helpers shared by proxy.py (the daemon), healthcheck.py
and thumbnails.py: fetch a URL the way a browser would, work out what it
actually serves (HLS, raw MPEG-TS, DASH, an HTML page, nothing), pick a
variant out of a master playlist, and find/probe ffmpeg.

Stdlib only — the proxy daemon runs in the folder venv with just pyarrow.
"""
import http.client
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Public IPTV hosts have expired/self-signed certs all the time; verifying
# would only turn playable streams into "dead" ones.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_TIMEOUT = 5

# What a URL turned out to serve. hls.js plays BROWSER_KINDS directly; the
# other ALIVE_KINDS are real media too, but need the ffmpeg bridge.
BROWSER_KINDS = frozenset({"hls-master", "hls-media"})
ALIVE_KINDS = BROWSER_KINDS | frozenset({"ts", "mp4", "dash"})
KIND_LABEL = {
    "hls-master": "HLS", "hls-media": "HLS", "ts": "raw MPEG-TS", "mp4": "MP4",
    "dash": "DASH", "html": "web page", "empty": "empty response",
    "gzip": "gzip blob", "other": "unknown data", "dead": "no response",
}


class NotPlaylist(Exception):
    def __init__(self, kind, detail=""):
        super().__init__(detail or f"not an HLS playlist ({KIND_LABEL.get(kind, kind)})")
        self.kind = kind


class Fetched:
    __slots__ = ("status", "headers", "body", "final_url", "content_type",
                 "cors", "truncated")

    def __init__(self, status, headers, body, final_url, truncated):
        self.status = status
        self.headers = headers
        self.body = body
        self.final_url = final_url
        self.truncated = truncated
        self.content_type = (headers.get("Content-Type") or "").strip()
        self.cors = (headers.get("Access-Control-Allow-Origin") or "").strip()


def iri_to_uri(url: str) -> str:
    """Percent-encode the non-ASCII / space characters playlists sometimes
    carry; urllib raises on them otherwise. Existing %XX escapes survive."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")


def open_url(url: str, timeout: float = DEFAULT_TIMEOUT, headers: dict = None,
             rng: str = None):
    """urlopen with a browser UA and lenient TLS. Raises urllib.error.HTTPError
    (4xx/5xx), urllib.error.URLError / TimeoutError (connection problems)."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    if rng:
        h["Range"] = rng
    req = urllib.request.Request(iri_to_uri(url), headers=h)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def make_decoder(encoding: str):
    """Incremental gzip/deflate decoder for a Content-Encoding, or None."""
    enc = (encoding or "").strip().lower()
    if enc in ("gzip", "x-gzip", "deflate"):
        return zlib.decompressobj(32 + zlib.MAX_WBITS)  # 32: auto-detect header
    return None


def decode_body(raw: bytes, encoding: str) -> bytes:
    """Decompress a (possibly truncated) response body. Lenient: a server that
    sends gzip bytes without saying so is decoded too; undecodable → as-is."""
    dec = make_decoder(encoding)
    if dec is None and raw[:2] == b"\x1f\x8b":
        dec = make_decoder("gzip")
    if dec is None:
        return raw
    try:
        return dec.decompress(raw)
    except zlib.error:
        return raw


def fetch(url: str, cap: int = 2_000_000, timeout: float = DEFAULT_TIMEOUT,
          headers: dict = None, max_time: float = None) -> Fetched:
    """GET up to `cap` bytes (so an endless stream can't hang us), decoded.
    `timeout` bounds each read; `max_time` bounds the whole download — a
    server trickling bytes stops being waited for and whatever arrived is
    returned, marked truncated."""
    t0 = time.monotonic()
    with open_url(url, timeout=timeout, headers=headers) as r:
        if max_time is None:
            raw = r.read(cap + 1)
            truncated = len(raw) > cap
        else:
            chunks, got, truncated = [], 0, False
            while got <= cap:
                if time.monotonic() - t0 > max_time:
                    truncated = True
                    break
                chunk = r.read1(min(65536, cap + 1 - got))
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            raw = b"".join(chunks)
            truncated = truncated or len(raw) > cap
        body = decode_body(raw[:cap], r.headers.get("Content-Encoding"))
        return Fetched(r.status, r.headers, body, r.geturl(), truncated)


def strip_bom(b: bytes) -> bytes:
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    return b.lstrip(b" \t\r\n")


def decode_text(body: bytes) -> str:
    return strip_bom(body).decode("utf-8", "replace")


def classify_bytes(head: bytes, content_type: str = "") -> str:
    """What the first bytes of a response are (see ALIVE_KINDS)."""
    s = strip_bom(head)
    if s.startswith(b"#EXTM3U"):
        return "hls-master" if b"#EXT-X-STREAM-INF" in s else "hls-media"
    if s.startswith(b"\x1f\x8b"):
        return "gzip"
    if len(s) >= 189 and s[0] == 0x47 and s[188] == 0x47:
        return "ts"
    if s[4:8] in (b"ftyp", b"moov", b"styp", b"moof"):
        return "mp4"
    probe = s[:4096]
    if b"<MPD" in probe:
        return "dash"
    low = probe[:512].lower()
    if low.startswith(b"<") or b"<html" in low or b"<!doctype" in low:
        return "html"
    if not s:
        return "empty"
    return "other"


def describe_error(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(reason, ssl.SSLError):
        return "TLS handshake failed"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, ConnectionResetError):
        return "connection reset"
    if isinstance(reason, http.client.RemoteDisconnected):
        return "server closed the connection"
    if isinstance(reason, http.client.IncompleteRead):
        return "server sent a truncated response"
    if isinstance(reason, OSError) and reason.strerror:
        return reason.strerror
    return f"{type(reason).__name__}: {reason}"[:100]


def classify(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """One small GET: is anything there, and what is it? Never raises."""
    t0 = time.monotonic()
    out = {"url": url, "ok": False, "kind": "dead", "status": 0, "content_type": "",
           "cors": "", "final_url": url, "error": ""}
    try:
        f = fetch(url, cap=4096, timeout=timeout)
    except urllib.error.HTTPError as e:
        out.update(status=e.code, error=f"HTTP {e.code}")
    except Exception as e:  # URLError, TimeoutError, ssl, http.client, ValueError
        out["error"] = describe_error(e)
    else:
        kind = classify_bytes(f.body, f.content_type)
        out.update(kind=kind, status=f.status, content_type=f.content_type,
                   cors=f.cors, final_url=f.final_url, ok=kind in ALIVE_KINDS)
        if not out["ok"]:
            out["error"] = f"serves {KIND_LABEL.get(kind, kind)}, not a stream"
    out["label"] = KIND_LABEL.get(out["kind"], out["kind"])
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


# ------------------------------------------------------------ HLS playlists

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
VIDEO_CODECS = ("avc", "hvc", "hev", "vp0", "av01", "mp4v", "dvh", "dva", "hvt")


def parse_attrs(line: str) -> dict:
    payload = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip('"') for k, v in ATTR_RE.findall(payload)}


def _digits(s: str) -> int:
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else 0


def iter_variants(text: str):
    """(has_video, bandwidth, uri) per #EXT-X-STREAM-INF entry."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attrs(line)
        bw = _digits(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH"))
        codecs = attrs.get("CODECS", "").lower()
        has_video = ("RESOLUTION" in attrs or not codecs
                     or any(c in codecs for c in VIDEO_CODECS))
        uri = next((l.strip() for l in lines[i + 1:]
                    if l.strip() and not l.startswith("#")), None)
        if uri:
            yield has_video, bw, uri


def pick_variant(text: str, base_url: str, prefer: str = "lowest"):
    """Absolute URL of the cheapest (or richest) variant with video, or None
    if `text` is not a master playlist."""
    variants = list(iter_variants(text))
    if not variants:
        return None
    pool = [v for v in variants if v[0]] or variants
    chosen = (min if prefer == "lowest" else max)(pool, key=lambda v: v[1])
    return urllib.parse.urljoin(base_url, chosen[2])


def rank_variants(text: str, base_url: str) -> list:
    """Variant URLs of a master playlist in the order the ffmpeg bridge should
    try them: video before audio-only, H.264 (copyable) before anything else,
    at most 720p-ish before bigger, then the highest bandwidth first."""
    lines = text.splitlines()
    ranked = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attrs(line)
        bw = _digits(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH"))
        codecs = attrs.get("CODECS", "").lower()
        has_video = ("RESOLUTION" in attrs or not codecs
                     or any(c in codecs for c in VIDEO_CODECS))
        avc = "avc" in codecs or not codecs
        width = _digits(attrs.get("RESOLUTION", "").split("x")[0]) if "x" in attrs.get("RESOLUTION", "") else 0
        uri = next((l.strip() for l in lines[i + 1:]
                    if l.strip() and not l.startswith("#")), None)
        if uri:
            ranked.append(((not has_video, not avc, width > 1280, -bw),
                           urllib.parse.urljoin(base_url, uri)))
    ranked.sort(key=lambda r: r[0])
    out = []
    for _, url in ranked:
        if url not in out:
            out.append(url)
    return out


def parse_media(text: str) -> dict:
    segs, key, map_uri, byterange, target, live = [], False, None, False, None, True
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if s.startswith("#EXT-X-KEY") and "METHOD=NONE" not in s:
                key = True
            elif s.startswith("#EXT-X-MAP"):
                m = re.search(r'URI="([^"]+)"', s)
                map_uri = m.group(1) if m else map_uri
            elif s.startswith("#EXT-X-BYTERANGE"):
                byterange = True
            elif s.startswith("#EXT-X-TARGETDURATION"):
                try:
                    target = float(s.split(":", 1)[1])
                except ValueError:
                    pass
            elif s.startswith("#EXT-X-ENDLIST"):
                live = False
            continue
        segs.append(s)
    return {"segments": segs, "live": live, "key": key, "map": map_uri,
            "byterange": byterange, "target_duration": target}


def resolve_media(url: str, timeout: float = DEFAULT_TIMEOUT, prefer: str = "lowest"):
    """Fetch `url`; if it is a master playlist, follow it to one media
    playlist. Returns (text, media_url, "media" | "variant").
    Raises NotPlaylist, or whatever fetch() raises."""
    f = fetch(url, cap=4_000_000, timeout=timeout)
    text = decode_text(f.body)
    if not text.startswith("#EXTM3U"):
        raise NotPlaylist(classify_bytes(f.body, f.content_type))
    if "#EXT-X-STREAM-INF" not in text:
        return text, f.final_url, "media"
    vurl = pick_variant(text, f.final_url, prefer)
    if not vurl:
        raise NotPlaylist("hls-master", "master playlist lists no variants")
    f2 = fetch(vurl, cap=4_000_000, timeout=timeout)
    text2 = decode_text(f2.body)
    if not text2.startswith("#EXTM3U"):
        raise NotPlaylist(classify_bytes(f2.body, f2.content_type),
                          "variant playlist is not a playlist")
    return text2, f2.final_url, "variant"


# ------------------------------------------------------------------ ffmpeg

_FFMPEG_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def find_ffmpeg(name: str = "ffmpeg"):
    """Absolute path to ffmpeg (or ffprobe), or None. Looks beyond PATH
    because the daemon and the detached worker inherit a slim PATH."""
    exe = shutil.which(name)
    if exe:
        return exe
    for d in _FFMPEG_DIRS:
        p = os.path.join(os.path.expanduser(d), name)
        if os.access(p, os.X_OK):
            return p
    return None


_STREAM_RE = re.compile(
    r"Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?: (Video|Audio): ([A-Za-z0-9_]+)")


def probe_codecs(url: str, timeout: float = 12, ffmpeg: str = None) -> dict:
    """{'video': 'h264'|..., 'audio': 'aac'|..., 'error': str} via ffmpeg
    itself (ffprobe is often installed elsewhere than ffmpeg)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return {"video": None, "audio": None, "error": "ffmpeg not found"}
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-user_agent", UA,
           "-rw_timeout", str(int(timeout * 1_000_000)),
           "-probesize", "3000000", "-analyzeduration", "3000000",
           "-i", url, "-t", "0.1", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 3)
        err = p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return {"video": None, "audio": None, "error": "ffmpeg timed out probing the stream"}
    video = audio = None
    for kind, codec in _STREAM_RE.findall(err):
        if kind == "Video" and video is None:
            video = codec.lower()
        elif kind == "Audio" and audio is None:
            audio = codec.lower()
    error = ""
    if video is None and audio is None:
        lines = [l for l in err.strip().splitlines() if l.strip()]
        error = (lines[-1] if lines else "ffmpeg found no streams")[:160]
    return {"video": video, "audio": audio, "error": error}


_encoder_cache = {}


def h264_encoder(ffmpeg: str = None):
    """Best H.264 encoder this ffmpeg has: VideoToolbox on macOS, else x264."""
    ffmpeg = ffmpeg or find_ffmpeg()
    if ffmpeg in _encoder_cache:
        return _encoder_cache[ffmpeg]
    enc = None
    if ffmpeg:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                 capture_output=True, timeout=15).stdout.decode()
            for cand in ("h264_videotoolbox", "libx264", "h264_nvenc", "h264_qsv"):
                if re.search(rf"\b{cand}\b", out):
                    enc = cand
                    break
        except (OSError, subprocess.TimeoutExpired):
            pass
    _encoder_cache[ffmpeg] = enc
    return enc
