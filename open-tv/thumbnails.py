"""Grab one video frame per channel as a thumbnail, using ffmpeg.

Thumbnails are written as <md5(url)>.jpg under paths.CACHE_DIR.

Fast path: resolve the HLS playlist in Python (master → cheapest variant with
video → newest segment), download at most SEGMENT_CAP bytes of that segment
and decode its first keyframe from a pipe. ffmpeg never probes a master
playlist or opens every variant, bandwidth per grab is bounded, and a dead
URL costs one failed GET — no ffmpeg process at all.

Fallbacks, in order: ffmpeg on the media playlist URL (encrypted, byte-range
or otherwise unusual playlists), ffmpeg on the bytes already fetched (raw
MPEG-TS / MP4 served directly), ffmpeg on the URL itself (DASH, anything else).
ffmpeg must be on PATH or in a usual place (`brew install ffmpeg`,
`apt install ffmpeg`, ...); _ffmpeg() raises a clear error when it is missing.
"""
import base64
import hashlib
import os
import subprocess
import urllib.error
import urllib.parse

import paths
import streams

THUMBS_DIR = paths.CACHE_DIR

TIMEOUT = 12            # ffmpeg wall clock per attempt, seconds
FETCH_TIMEOUT = 5       # per HTTP request
SEGMENT_CAP = 1_500_000  # bytes of one segment: enough for the first keyframe
SEGMENT_TIME = 10        # seconds for that download; a trickle gets decoded as-is
UA = streams.UA

_OUT = ["-frames:v", "1", "-vf", "scale=160:-2", "-q:v", "5"]
# Protocol options (-user_agent, -rw_timeout) only exist for URL inputs; ffmpeg
# aborts with "Option not found" if they are given for a pipe. Likewise the
# hls demuxer options only exist when the input really is an HLS playlist.
# -skip_frame nokey is a decoder option: it must come before -i too.
_PIPE_IN = ["-skip_frame", "nokey", "-probesize", "2000000", "-analyzeduration", "2000000"]
_URL_IN = _PIPE_IN + ["-user_agent", UA, "-rw_timeout", "8000000"]
_HLS_IN = _URL_IN + ["-allowed_extensions", "ALL", "-extension_picky", "0",
                     "-live_start_index", "-1"]


def _ffmpeg() -> str:
    """Absolute path to ffmpeg, or a clear error if it isn't installed."""
    exe = streams.find_ffmpeg()
    if not exe:
        raise RuntimeError(
            "ffmpeg not found on PATH — OpenTV needs it to grab channel "
            "thumbnails. Install it (macOS: `brew install ffmpeg`, "
            "Debian/Ubuntu: `sudo apt install ffmpeg`) and reopen the app.")
    return exe


def thumb_path(url: str) -> str:
    return os.path.join(THUMBS_DIR, hashlib.md5(url.encode()).hexdigest() + ".jpg")


def thumb_data_uri(url: str) -> str:
    """Base64 data URI of the saved thumbnail, or "" if none exists."""
    path = thumb_path(url)
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def _decode(in_args: list, out: str, timeout: float, stdin: bytes = None):
    """Run ffmpeg to write one frame to `out`. Returns (ok, error_text)."""
    tmp = out + ".tmp.jpg"
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", *in_args, *_OUT, tmp]
    if stdin is None:
        cmd.insert(1, "-nostdin")
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return True, ""
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, (err[-1].strip() if err else "ffmpeg produced no frame")[:160]
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _fetch(url: str, cap: int, max_time: float = None):
    """fetch() with every failure folded into a short reason string."""
    try:
        return streams.fetch(url, cap=cap, timeout=FETCH_TIMEOUT, max_time=max_time), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, streams.describe_error(e)


def _grab_hls(f, out: str, timeout: float) -> dict:
    text, murl, note = streams.decode_text(f.body), f.final_url, "media playlist"
    if "#EXT-X-STREAM-INF" in text:
        vurl = streams.pick_variant(text, f.final_url)
        if not vurl:
            return {"ok": False, "note": "master playlist lists no variants"}
        f2, err = _fetch(vurl, 4_000_000)
        if f2 is None:
            return {"ok": False, "note": f"variant playlist: {err}"}
        text, murl, note = streams.decode_text(f2.body), f2.final_url, "variant"
        if not text.startswith("#EXTM3U"):
            return {"ok": False, "note": "variant playlist is not a playlist"}
    m = streams.parse_media(text)
    if not m["segments"]:
        return {"ok": False, "note": "playlist lists no segments"}
    seg_err = ""
    if not (m["key"] or m["byterange"]):
        seg = m["segments"][-1] if m["live"] else m["segments"][0]
        data = b""
        if m["map"]:
            fm, seg_err = _fetch(urllib.parse.urljoin(murl, m["map"]), 2_000_000)
            data = fm.body if fm else b""
        if not seg_err:
            fs, seg_err = _fetch(urllib.parse.urljoin(murl, seg), SEGMENT_CAP, SEGMENT_TIME)
            if fs:
                data += fs.body
        if data and not seg_err:
            ok, err = _decode(_PIPE_IN + ["-i", "pipe:0"], out, timeout, stdin=data)
            if ok:
                return {"ok": True, "note": f"{note}, segment"}
            seg_err = err or "no decodable frame in segment"
        elif seg_err:
            seg_err = f"segment: {seg_err}"
    # Keys, byte ranges, odd containers: let ffmpeg's own HLS demuxer handle it.
    ok, err = _decode(_HLS_IN + ["-i", murl], out, timeout)
    if ok:
        return {"ok": True, "note": f"{note}, ffmpeg"}
    return {"ok": False, "note": err or seg_err or "ffmpeg produced no frame"}


def grab(url: str, timeout: float = TIMEOUT) -> dict:
    """Capture one frame of `url` to thumb_path(url).
    Returns {"ok": bool, "kind": str, "note": str} — note says which path
    worked, or why none did. Never raises for a bad stream (only for a
    missing ffmpeg)."""
    os.makedirs(THUMBS_DIR, exist_ok=True)
    out = thumb_path(url)
    f, err = _fetch(url, 8192)
    if f is None:
        return {"ok": False, "kind": "dead", "note": err}
    kind = streams.classify_bytes(f.body, f.content_type)
    if kind in streams.BROWSER_KINDS:
        if f.truncated:  # a long media playlist: fetch the whole thing
            f, err = _fetch(url, 4_000_000)
            if f is None:
                return {"ok": False, "kind": kind, "note": err}
        return {"kind": kind, **_grab_hls(f, out, timeout)}
    if kind not in streams.ALIVE_KINDS:
        return {"ok": False, "kind": kind,
                "note": f"serves {streams.KIND_LABEL.get(kind, kind)}, not a stream"}
    if kind in ("ts", "mp4"):
        fb, err = _fetch(url, SEGMENT_CAP * 2, SEGMENT_TIME)
        if fb is not None:
            ok, err = _decode(_PIPE_IN + ["-i", "pipe:0"], out, timeout, stdin=fb.body)
            if ok:
                return {"ok": True, "kind": kind, "note": "raw bytes"}
    ok, err2 = _decode(_URL_IN + ["-i", url], out, timeout)
    if ok:
        return {"ok": True, "kind": kind, "note": "ffmpeg on url"}
    return {"ok": False, "kind": kind, "note": err2 or err or "ffmpeg produced no frame"}
