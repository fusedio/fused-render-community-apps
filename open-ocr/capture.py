from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = APP_DIR / ".fused" / "cache" / "scratch"

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _run(job_id: str, path: Path, process: "subprocess.Popen") -> None:
    process.wait()
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job.get("state") == "cancelled":
            return
        if path.exists() and path.stat().st_size > 0:
            job.update(state="done", path=str(path))
        else:
            job.update(state="cancelled")


def start() -> dict:
    """Hand off to macOS's own interactive screenshot picker (`screencapture -i`)
    -- the same tool behind Cmd+Shift+4: drag a region, or press space to click
    any window from any app. Blocks in a background thread until the person
    finishes or presses Escape."""
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    job_id = f"shot-{int(time.time() * 1000)}"
    path = SCRATCH_DIR / f"{job_id}.png"
    process = subprocess.Popen(["screencapture", "-i", str(path)])
    with _lock:
        _jobs[job_id] = {"state": "running", "path": "", "pid": process.pid}
    threading.Thread(target=_run, args=(job_id, path, process), daemon=True).start()
    return {"job_id": job_id}


def poll(job_id: str) -> dict:
    with _lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return {"state": "unknown", "path": ""}
    return {"state": job["state"], "path": job.get("path", "")}


def cancel(job_id: str) -> dict:
    pid = None
    with _lock:
        job = _jobs.get(job_id)
        if job and job.get("state") == "running":
            job["state"] = "cancelled"
            pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"state": "cancelled"}
