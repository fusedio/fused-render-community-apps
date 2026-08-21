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


def _pid_alive(pid):
    """Whether `pid` currently names a live process. A cold start on Windows
    (AV scanning a freshly spawned python.exe, importing torch/sentence-
    transformers) can legitimately take well over a minute while still alive,
    so staleness must be judged by this, not by how long the lock has existed."""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _lock_pid():
    try:
        with open(SPAWN_LOCK, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _acquire_spawn_lock():
    """Atomic single-spawner lock: two racing serve.py calls (page reloads) must
    not both launch a server. Only the winner spawns; the loser just waits for
    health. A lock is reclaimed only when its holder's PID is no longer alive —
    not merely old — so a slow-but-live spawn is never mistaken for a crashed
    one and yanked out from under it."""
    try:
        if os.path.exists(SPAWN_LOCK) and not _pid_alive(_lock_pid()):
            os.remove(SPAWN_LOCK)
    except OSError:
        pass
    try:
        fd = os.open(SPAWN_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return None
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def _release_spawn_lock(fd):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    # Only remove it if it still names OUR pid — a lock some other process has
    # since (re)acquired at this same path must not be deleted out from under it.
    if _lock_pid() != os.getpid():
        return
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
