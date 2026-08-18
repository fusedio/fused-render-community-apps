"""Launcher: ensure the DocChat embedding server (ragserver.py) is running.

Called from the page via fused.runPython. Idempotent — if a server for the
requested model is already up it just reports it; otherwise it spawns one
DETACHED (and with no console window on Windows, per house rule) so it outlives
this 60s call and stays warm across questions. The server writes its chosen free
port to `.ragserver.json`; first launch downloads the model, so `ready` may be
false for a bit — the page polls /health (via this launcher) until it flips true.
"""

import http.client
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rag_common as rc

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
PORT = int(os.environ.get("RAG_PORT", "8271"))
SIDECAR = os.path.join(rc.HERE, ".ragserver.json")
SPAWN_LOCK = os.path.join(rc.HERE, ".ragserver.spawn")


def _acquire_spawn_lock():
    """Atomic single-spawner lock: two racing serve.py calls (page reloads) must
    not both launch a server. Only the winner spawns; the loser just waits for
    health. Stale locks (crashed spawner) older than 60s are reclaimed."""
    try:
        if os.path.exists(SPAWN_LOCK) and time.time() - os.path.getmtime(SPAWN_LOCK) > 60:
            os.remove(SPAWN_LOCK)
    except OSError:
        pass
    try:
        return os.open(SPAWN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return None


def _release_spawn_lock(fd):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(SPAWN_LOCK)
    except OSError:
        pass


def _sidecar_pid():
    try:
        with open(SIDECAR, "r", encoding="utf-8") as f:
            return json.load(f).get("pid")
    except Exception:
        return None


def _sidecar_token():
    """The running server's auth token, read off disk (not over HTTP) so the page
    can gate its fetches without the token ever crossing the open HTTP surface."""
    try:
        with open(SIDECAR, "r", encoding="utf-8") as f:
            return json.load(f).get("token", "")
    except Exception:
        return ""


def _kill(pid):
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(int(pid), 9)
    except Exception:
        pass


def _health(port, timeout=1.5):
    try:
        c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
        c.request("GET", "/health")
        r = c.getresponse()
        if r.status == 200:
            return json.loads(r.read())
    except Exception:
        return None
    return None


def _spawn(model):
    """Launch the server detached and, on Windows, with NO console window ever
    (house rule) — plus a break-away so the engine tearing down this runPython
    child doesn't take the server with it."""
    env = dict(os.environ, RAG_MODEL=model)
    args = [sys.executable, os.path.join(rc.HERE, "ragserver.py")]
    common = dict(cwd=rc.HERE, env=env, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name != "nt":
        subprocess.Popen(args, start_new_session=True, **common)
        return
    # CREATE_NO_WINDOW (no console popup, house rule) + break away from the engine's
    # job so the server outlives this runPython call. NOT DETACHED_PROCESS — combining
    # it with CREATE_NO_WINDOW is contradictory and can hang a concurrently-started
    # interpreter at startup.
    flags = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        subprocess.Popen(args, creationflags=flags, **common)   # still windowless


def _status(started, h):
    return {"ok": True, "started": started, "port": PORT, "token": _sidecar_token(),
            "ready": bool(h.get("ready")), "stage": h.get("stage"), "model": h.get("model"),
            "device": h.get("device"), "dim": h.get("dim"), "models_dir": h.get("models_dir")}


def main(model: str = "", restart: bool = False):
    desired = model or os.environ.get("RAG_MODEL") or DEFAULT_MODEL

    h = _health(PORT)
    if h and h.get("model") == desired and not restart:
        return _status(False, h)           # already serving the requested model

    if h and (restart or h.get("model") != desired):
        _kill(_sidecar_pid())              # switching model -> stop the old server, free the port
        for _ in range(25):
            time.sleep(0.2)
            if not _health(PORT):
                break

    fd = _acquire_spawn_lock()             # only the lock winner launches a server
    if fd is not None:
        _spawn(desired)
    try:
        for _ in range(120):               # ~24s to bind the fixed port + answer /health
            time.sleep(0.2)
            h = _health(PORT)
            if h and h.get("model") == desired:
                return _status(fd is not None, h)
        return {"ok": False, "error": "The embedding server did not come up on port " + str(PORT) +
                ". It may still be installing, or the port is used by another app (set RAG_PORT)."}
    finally:
        _release_spawn_lock(fd)
