"""Tiny localhost range server for cog_range_viewer — serves LOCAL files over
HTTP with real Range + CORS support, so the browser-side COG demo can make the
exact same range requests against a file on disk as it does against S3.

Same two-role pattern as the geotiff template's tile_server.py:
  1. runPython entrypoint  main(action="ensure")  -> {"port": N}
  2. the daemon itself     python3 range_server.py --serve

Stdlib only — runs on the system python3, no venv needed.
Endpoints:
  /ping                    -> {"ok": true, "version": ...}
  /raw?file=<abs path>     -> the file bytes; honours Range (206 + Content-Range),
                              HEAD, and OPTIONS preflight; CORS * with
                              Content-Range/Content-Length exposed (geotiff.js
                              needs them cross-origin).
  /quit                    -> daemon exits
"""

import json
import os
import sys
import time

STATE = os.path.expanduser("~/.cache/fused-render-cog-range/daemon.json")
IDLE_EXIT_S = 2 * 60 * 60


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "range_server.py")


def _version():
    try:
        return str(os.path.getmtime(_me()))
    except OSError:
        return "0"


def _python3():
    """System python3 — the daemon is stdlib-only, no bundle/venv needed."""
    import shutil
    return shutil.which("python3") or "/usr/bin/python3"


def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


def main(action: str = "ensure", self_path: str = ""):
    """runPython entrypoint: make sure the daemon runs, return {port}."""
    import subprocess
    me = self_path or _me()
    version = _version() if not self_path else str(os.path.getmtime(me))
    try:
        with open(STATE) as f:
            st = json.load(f)
        if _alive(st.get("port"), version):
            return {"port": st["port"], "reused": True}
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit", timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    log = os.path.join(os.path.dirname(STATE), "daemon.log")
    denv = {k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONHOME")}
    with open(log, "ab") as lf:
        subprocess.Popen([_python3(), me, "--serve"],
                         stdout=lf, stderr=lf, env=denv,
                         start_new_session=True, cwd=os.path.dirname(me))
    for _ in range(100):
        time.sleep(0.05)
        try:
            with open(STATE) as f:
                st = json.load(f)
            if st.get("version") == version and _alive(st.get("port"), version):
                return {"port": st["port"], "reused": False}
        except (OSError, ValueError):
            continue
    return {"error": f"daemon did not start — see {log}"}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ daemon
def _serve():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs, unquote

    VERSION = _version()
    last_hit = [time.time()]

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Range")
            self.send_header("Access-Control-Expose-Headers",
                             "Content-Range, Content-Length, Accept-Ranges")
            self.send_header("Access-Control-Max-Age", "3600")

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _file_head(self):
            q = parse_qs(urlparse(self.path).query)
            path = unquote(q.get("file", [""])[0])
            if not path or not os.path.isfile(path):
                return None, None
            return path, os.path.getsize(path)

        def _range(self, size):
            h = self.headers.get("Range")
            if not h or not h.startswith("bytes="):
                return None
            part = h[6:].split(",")[0].strip()
            a, _, b = part.partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = min(int(b), size - 1) if (a and b) else (size - 1 if a else size - 1)
            return (start, end) if start <= end < size or start < size else None

        def do_HEAD(self):
            self._respond(head=True)

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            if u.path == "/ping":
                return self._json({"ok": True, "version": VERSION})
            if u.path == "/quit":
                self._json({"ok": True})
                os._exit(0)
            if u.path == "/raw":
                return self._respond(head=False)
            self._json({"error": "unknown endpoint"}, 404)

        def _respond(self, head):
            path, size = self._file_head()
            if path is None:
                return self._json({"error": "file not found"}, 404)
            rng = self._range(size)
            if rng:
                start, end = rng
                self.send_response(206)
                self._cors()
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if head:
                    return
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = f.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if head:
                    return
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"port": port, "version": VERSION, "pid": os.getpid()}, f)

    import threading

    def reaper():
        while True:
            time.sleep(60)
            if time.time() - last_hit[0] > IDLE_EXIT_S:
                os._exit(0)

    threading.Thread(target=reaper, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    _serve()
