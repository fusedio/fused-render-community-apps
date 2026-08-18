"""Grab one video frame per channel as a thumbnail (ffmpeg), saved to ~/.fused-render/cache/OpenTV/."""
import asyncio
import base64
import hashlib
import os
import subprocess
import urllib.parse

import paths

PROXY_BASE = "http://127.0.0.1:8787/proxy?url="

DIR = os.path.dirname(os.path.abspath(__file__))
THUMBS_DIR = paths.CACHE_DIR

FFMPEG = "/opt/homebrew/bin/ffmpeg"
TIMEOUT = 15
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def thumb_path(url: str) -> str:
    return os.path.join(THUMBS_DIR, hashlib.md5(url.encode()).hexdigest() + ".jpg")


def thumb_data_uri(url: str) -> str:
    """Base64 data URI of the saved thumbnail, or "" if none exists."""
    path = thumb_path(url)
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()


def _grab_from(src: str, out: str, timeout: int) -> bool:
    tmp = out + ".tmp.jpg"
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-user_agent", UA,
           "-i", src,
           "-frames:v", "1", "-vf", "scale=160:-2", "-q:v", "5",
           tmp]
    try:
        subprocess.run(cmd, timeout=timeout, capture_output=True)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return True
    except subprocess.TimeoutExpired:
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return False


def grab(url: str, timeout: int = TIMEOUT) -> bool:
    """Capture one frame; falls back to the local proxy like the player does."""
    os.makedirs(THUMBS_DIR, exist_ok=True)
    out = thumb_path(url)
    if _grab_from(url, out, timeout):
        return True
    proxied = PROXY_BASE + urllib.parse.quote(url, safe="")
    return _grab_from(proxied, out, max(timeout * 2, 45))


def _ffmpeg_cmd(src: str, tmp: str) -> list:
    return [FFMPEG, "-y", "-loglevel", "error",
            "-user_agent", UA,
            "-i", src,
            "-frames:v", "1", "-vf", "scale=160:-2", "-q:v", "5",
            tmp]


async def _grab_from_async(src: str, out: str, timeout: int) -> bool:
    tmp = out + ".tmp.jpg"
    proc = await asyncio.create_subprocess_exec(
        *_ffmpeg_cmd(src, tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), timeout)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out)
            return True
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return False


async def grab_async(url: str, timeout: int = TIMEOUT, log=None) -> bool:
    """True-async grab (no thread pool); falls back to the local proxy."""
    os.makedirs(THUMBS_DIR, exist_ok=True)
    out = thumb_path(url)
    if await _grab_from_async(url, out, timeout):
        return True
    if log:
        log("direct grab failed, retrying via proxy")
    proxied = PROXY_BASE + urllib.parse.quote(url, safe="")
    return await _grab_from_async(proxied, out, max(timeout * 2, 45))
