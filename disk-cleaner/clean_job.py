"""Run a clean as a detached background job, so a 30 GB delete survives the
page being closed or navigated away from.

`runPython` kills `main()` at 60 s and the page frame dies on navigation, so
neither can own a long delete. Instead `action="start"` validates the request
(through clean.validate — the same narrow, id-only rules), writes a job file
and spawns this script as a detached worker. The worker empties the selected
locations with no time budget and reports progress two ways:

* to `.cache/clean-job.json`, which the page polls via `action="status"`;
* to fused-render's download manager (POST /api/jobs), under the same job id
  the page uses for `fused.trackJob`, so the row keeps moving — and its ✕
  keeps working — after the page is gone.

Cancel stops between items (a single item mid-rmtree is always finished).
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__":
    # catalog.CACHE_DIR is relative to the app folder, like under runPython.
    os.chdir(HERE)
    sys.path.insert(0, HERE)

import catalog  # noqa: E402
import clean  # noqa: E402

JOB_FILE = os.path.join(catalog.CACHE_DIR, "clean-job.json")
CANCEL_FILE = os.path.join(catalog.CACHE_DIR, "clean-job.cancel")
TERMINAL = ("done", "error", "cancelled")


def _read():
    try:
        with open(JOB_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(job: dict):
    os.makedirs(catalog.CACHE_DIR, exist_ok=True)
    tmp = JOB_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(job, fh)
    os.replace(tmp, JOB_FILE)


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A finished child of ours that nobody reaped is a zombie, not alive.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _title(mode: str) -> str:
    return "Disk Cleaner: moving to Trash" if mode == "trash" else "Disk Cleaner: deleting"


def _status():
    job = _read()
    if job is None:
        return {"state": "none"}
    if job["state"] == "running" and not _alive(job.get("pid")):
        job.update(state="error", detail="the cleanup worker exited unexpectedly")
        _write(job)
    return job


def main(action: str = "status", ids: str = "", mode: str = "trash",
         confirm: bool = False, total: float = 0, origin: str = ""):
    if action == "status":
        return _status()

    if action == "cancel":
        job = _status()
        if job.get("state") == "running":
            open(CANCEL_FILE, "w").close()
        return job

    if action == "ack":
        # The page has shown this job's result banner; don't show it again.
        job = _status()
        if job.get("state") in TERMINAL:
            job["acked"] = True
            _write(job)
        return job

    if action != "start":
        raise RuntimeError("unknown action %r" % action)

    current = _status()
    if current.get("state") == "running":
        raise RuntimeError("a cleanup is already running")
    wanted = clean.validate(ids, mode, confirm)

    try:
        os.remove(CANCEL_FILE)
    except OSError:
        pass
    job = {
        "id": "disk-cleaner-clean-" + uuid.uuid4().hex[:10],
        "title": _title(mode),
        "ids": wanted,
        "mode": mode,
        "state": "running",
        "total": int(total or 0),
        "freed": 0,
        "removed": 0,
        "detail": "Starting…",
        "origin": os.environ.get("FUSED_RENDER_ORIGIN") or origin,
        "started": time.time(),
        "result": None,
        "acked": False,
    }
    _write(job)
    # Its own session and no inherited pipes: runPython waits for EOF on the
    # stdout it captures, and must not take the worker down when it returns.
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker"],
        cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    job["pid"] = proc.pid
    # The worker may already have written progress; only add the pid.
    latest = _read() or job
    latest["pid"] = proc.pid
    _write(latest)
    return latest


# ---- worker ---------------------------------------------------------------

class _Progress:
    """Hooks clean._clean_dir_contents calls per item."""

    def __init__(self, job: dict):
        self.job = job
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.where = ""

    def entry(self, name):
        self.where = name

    def begin(self, item):
        with self.lock:
            self.job["detail"] = "%s · %s" % (self.where, item)

    def add(self, size):
        with self.lock:
            self.job["freed"] += size

    def removed(self):
        with self.lock:
            self.job["removed"] += 1

    def stopped(self):
        if os.path.exists(CANCEL_FILE):
            self.stop.set()
        return self.stop.is_set()


def _post(job: dict):
    """Best-effort report to the download manager; returns cancel_requested."""
    origin = job.get("origin")
    if not origin:
        return False
    body = {
        "id": job["id"], "title": job["title"], "kind": "task", "unit": "bytes",
        "cancellable": job["state"] == "running", "state": job["state"],
        "done": job["freed"], "detail": job["detail"],
    }
    if job["total"]:
        body["total"] = max(job["total"], job["freed"])
    req = urllib.request.Request(
        origin.rstrip("/") + "/api/jobs", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Fused": "1"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            reply = json.loads(resp.read() or b"{}")
        return bool(reply.get("cancel_requested") or
                    (reply.get("job") or {}).get("cancel_requested"))
    except Exception:
        return False


def _report(progress: _Progress):
    with progress.lock:
        snap = dict(progress.job)
    _write(snap)
    if _post(snap):
        progress.stop.set()


def _worker():
    job = _read()
    if not job or job.get("state") != "running":
        return
    job["pid"] = os.getpid()
    progress = _Progress(job)
    finished = threading.Event()

    def heartbeat():
        # Also covers one huge item: sizing + rmtree of a 20 GB folder is a
        # single step with no per-item callback for minutes.
        while not finished.wait(1.0):
            _report(progress)

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        result = clean.run(job["ids"], job["mode"], float("inf"), progress)
        with progress.lock:
            job["result"] = result
            job["freed"] = result["freed"]
            job["removed"] = result["removed"]
            if progress.stop.is_set() and result["remaining"]:
                job["state"] = "cancelled"
                job["detail"] = "Stopped — %d item(s) left" % result["remaining"]
            else:
                job["state"] = "done"
                job["detail"] = "%d item(s) removed" % result["removed"]
    except Exception as err:  # noqa: BLE001 — must always reach a terminal state
        with progress.lock:
            job["state"] = "error"
            job["detail"] = "%s: %s" % (type(err).__name__, err)
    finally:
        finished.set()
        beat.join(timeout=5)
        job["finished"] = time.time()
        try:
            os.remove(CANCEL_FILE)
        except OSError:
            pass
        _report(progress)


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker()
