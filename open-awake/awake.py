"""Open Awake daemon: owns a `caffeinate` child that keeps the Mac from sleeping.

Contract (fused-render background app): parse --status/--cache/--version, bind
127.0.0.1:0, publish {port, token, pid} atomically, then serve. Every route
needs the token (`?t=`). caffeinate is started with `-w <daemon pid>`, so it
exits with the daemon and can never be orphaned.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
SESSION = None  # {"start", "ends", "display", "proc"}
SERVER = None
CACHE = "."
VERSION = "0"
TOKEN = ""
MAX_LOG = 40


def _atomic_write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _log_path():
    return os.path.join(CACHE, "sessions.json")


def _read_log():
    try:
        with open(_log_path()) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _close_locked(reason):
    """Record the running session and clear it. Caller holds LOCK."""
    global SESSION
    s = SESSION
    if not s:
        return
    SESSION = None
    entry = {
        "start": s["start"],
        "end": time.time(),
        "planned": s["planned"],
        "display": s["display"],
        "reason": reason,
    }
    try:
        os.makedirs(CACHE, exist_ok=True)
        _atomic_write(_log_path(), (_read_log() + [entry])[-MAX_LOG:])
    except OSError:
        pass


def _start(minutes, display):
    exe = shutil.which("caffeinate") or "/usr/bin/caffeinate"
    if not os.path.exists(exe):
        raise RuntimeError("caffeinate not found: Open Awake needs macOS")
    secs = max(0, int(minutes * 60))
    # -i idle sleep, -m disk, -s system sleep on AC, -d display (optional)
    cmd = [exe, "-i", "-m", "-s", "-w", str(os.getpid())]
    if display:
        cmd.append("-d")
    if secs:
        cmd += ["-t", str(secs)]
    with LOCK:
        if SESSION:
            SESSION["proc"].terminate()
            _close_locked("replaced")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        now = time.time()
        globals()["SESSION"] = {
            "start": now, "ends": now + secs if secs else None,
            "planned": secs, "display": bool(display), "proc": proc,
        }


def _stop():
    with LOCK:
        if SESSION:
            SESSION["proc"].terminate()
            try:
                SESSION["proc"].wait(2)
            except subprocess.TimeoutExpired:
                SESSION["proc"].kill()
            _close_locked("stopped")


def _snapshot():
    with LOCK:
        s = SESSION
        # caffeinate -t pauses while the Mac sleeps, so enforce the wall-clock end too
        if s and s["proc"].poll() is None and s["ends"] and time.time() >= s["ends"]:
            s["proc"].terminate()
            try:
                s["proc"].wait(2)
            except subprocess.TimeoutExpired:
                s["proc"].kill()
        if s and s["proc"].poll() is not None:
            _close_locked("expired" if s["ends"] else "ended")
            s = None
        out = {"active": bool(s), "now": time.time(), "log": _read_log()[::-1]}
        if s:
            out.update(start=s["start"], ends=s["ends"], display=s["display"])
        return out


def _watch():
    while True:
        time.sleep(1)
        _snapshot()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        url = urlparse(self.path)
        if parse_qs(url.query).get("t", [""])[0] != TOKEN:
            return self._reply(403, {"error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._reply(400, {"error": "bad json"})
        route = url.path.rstrip("/")
        try:
            if route == "/ping":
                return self._reply(200, {"ok": True, "version": VERSION})
            if route == "/quit":
                self._reply(200, {"ok": True})
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                return
            if route == "/start":
                _start(float(body.get("minutes") or 0), bool(body.get("display", True)))
            elif route == "/stop":
                _stop()
            elif route != "/state":
                return self._reply(404, {"error": "not found"})
            return self._reply(200, _snapshot())
        except (RuntimeError, ValueError, OSError) as e:
            return self._reply(500, {"error": str(e)})

    do_GET = do_POST = _handle


def main():
    global SERVER, CACHE, VERSION, TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--version", required=True)
    a = ap.parse_args()
    CACHE, VERSION, TOKEN = a.cache, a.version, secrets.token_urlsafe(24)
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _atomic_write(a.status, {"port": SERVER.server_address[1], "token": TOKEN, "pid": os.getpid()})
    threading.Thread(target=_watch, daemon=True).start()
    try:
        SERVER.serve_forever()
    finally:
        _stop()


if __name__ == "__main__":
    sys.exit(main())
