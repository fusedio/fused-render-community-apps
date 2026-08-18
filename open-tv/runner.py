"""Background runner for the channel health check (html-background-runner pattern).

The HTML calls main(action="start") which spawns a DETACHED worker process and
returns immediately, then polls main(action="status", run_id=..., since=N).
The worker appends one JSON line per event to
<paths.RUNS_DIR>/<run_id>/events.jsonl — the log is the source of truth.
"""

# The detached _worker (spawned via sys.executable = this entry's venv) imports
# healthcheck, which reads health.parquet via pyarrow.
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import paths

# The fused-render runner (app >= Jul 2026) exec()s the entry file without
# __file__; its preamble puts the script's directory at sys.path[0].
_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))

DIR = _HERE
RUNS_DIR = paths.RUNS_DIR


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- entry point

def main(action: str = "start", run_id: str = "", since: int = 0,
         job: str = "health", category: str = "sports", job_id: str = "") -> dict:
    if action == "start":
        return _start(job, category, job_id)
    if action == "status":
        return _status(run_id, int(since))
    if action == "cancel":
        return _cancel(run_id)
    if action == "list":
        return _list()
    return {"error": f"unknown action {action!r}"}


def _start(job: str = "health", category: str = "sports",
           job_id: str = "") -> dict:
    if job_id:
        if "/" in job_id or job_id.startswith("."):
            return {"error": "bad job_id"}
        run_id = job_id
    else:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(RUNS_DIR, run_id)
    if os.path.exists(run_dir):
        return {"error": f"run {run_id} already exists"}
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"job": job, "category": category}, f)
    subprocess.Popen(
        [sys.executable, os.path.join(_HERE, "runner.py"), "--worker", run_dir],
        stdout=open(os.path.join(run_dir, "worker.log"), "w"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=DIR,
    )
    return {"run_id": run_id}


def _read_events(run_dir):
    path = os.path.join(run_dir, "events.jsonl")
    events = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # half-written last line; next poll gets it
    return events


def _derive_state(events):
    state = {"total": 0, "finished": 0, "ok": 0, "failed": 0,
             "retry_total": 0, "retry_done": 0,
             "running": True, "summary": None}
    for e in events:
        t = e.get("type")
        if t == "run_start":
            state["total"] = e.get("msg", {}).get("total", 0)
        elif t == "job_end":
            state["finished"] += 1
            state["ok" if e.get("status") == "ok" else "failed"] += 1
        elif t == "retry_start":
            state["retry_total"] = e.get("msg", {}).get("total", 0)
        elif t == "job_retry":
            state["retry_done"] += 1
            if e.get("status") == "ok":
                state["ok"] += 1
        elif t == "run_end":
            state["running"] = False
            state["summary"] = e.get("msg")
    return state


def _status(run_id: str, since: int) -> dict:
    if not run_id or "/" in run_id or run_id.startswith("."):
        return {"error": "bad run_id"}
    run_dir = os.path.join(RUNS_DIR, run_id)
    if not os.path.isdir(run_dir):
        return {"error": f"no such run {run_id}"}
    events = _read_events(run_dir)
    return {"run_id": run_id, "state": _derive_state(events),
            "events": events[since:], "cursor": len(events)}


def _cancel(run_id: str) -> dict:
    if not run_id or "/" in run_id or run_id.startswith("."):
        return {"error": "bad run_id"}
    run_dir = os.path.join(RUNS_DIR, run_id)
    if not os.path.isdir(run_dir):
        return {"error": f"no such run {run_id}"}
    open(os.path.join(run_dir, "cancel"), "w").close()
    return {"cancelled": run_id}


def _list() -> dict:
    runs = []
    if os.path.isdir(RUNS_DIR):
        for rid in sorted(os.listdir(RUNS_DIR), reverse=True):
            d = os.path.join(RUNS_DIR, rid)
            if os.path.isdir(d):
                runs.append({"run_id": rid,
                             "state": _derive_state(_read_events(d))})
    return {"runs": runs}


# -------------------------------------------------------------------- worker

def _worker(run_dir: str):
    import healthcheck
    import thumbnails
    import channels

    with open(os.path.join(run_dir, "spec.json")) as f:
        spec = json.load(f)
    job_type = spec.get("job", "health")
    category = spec.get("category", "sports")

    log_path = os.path.join(run_dir, "events.jsonl")
    log_f = open(log_path, "a", encoding="utf-8")

    def emit(type_, job=None, status=None, msg=None, seconds=None,
             level="info"):
        row = {"ts": _now(), "type": type_, "job": job, "level": level,
               "status": status, "msg": msg, "seconds": seconds}
        log_f.write(json.dumps(row) + "\n")
        log_f.flush()

    cancel_flag = os.path.join(run_dir, "cancel")

    def cancelled():
        return os.path.exists(cancel_flag)

    if category == "favorites":
        import favorites
        chans = favorites.main()["channels"]
    else:
        chans = channels.main(category)["channels"]
    if job_type == "thumbs":
        # fail loudly rather than reporting every grab as a dead stream
        try:
            thumbnails._ffmpeg()
        except RuntimeError as e:
            emit("run_end", status="failed", msg={"error": str(e)})
            log_f.close()
            return
        # skip channels that have never responded in any health check
        recs = healthcheck._load_records()
        chans = sorted(
            (ch for ch in chans
             if not (r := recs.get(ch["url"])) or r["tries"] > r["fails"]),
            key=lambda ch: ch["name"].lower())
    emit("run_start", msg={"total": len(chans), "job": job_type})

    results = []
    concurrency = 128
    sem = asyncio.Semaphore(concurrency)

    async def one(ch):
        async with sem:
            if cancelled():
                return
            emit("job_start", job=ch["name"])
            t0 = time.monotonic()
            msg = None
            try:
                if job_type == "thumbs":
                    ok = await thumbnails.grab_async(
                        ch["url"],
                        log=lambda m: emit("log", job=ch["name"], msg=m))
                    if ok:
                        msg = {"url": ch["url"],
                               "thumb": thumbnails.thumb_data_uri(ch["url"])}
                else:
                    ok = await asyncio.to_thread(healthcheck._probe, ch["url"])
            except Exception:
                ok = False
            results.append((ch, ok))
            emit("job_end", job=ch["name"], status="ok" if ok else "failed",
                 msg=msg, seconds=round(time.monotonic() - t0, 2))

    async def run_all():
        from concurrent.futures import ThreadPoolExecutor
        # default to_thread pool caps at ~32 threads; match our concurrency
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=concurrency))
        await asyncio.gather(*(one(ch) for ch in chans))

    async def retry_thumbs(failed):
        sem2 = asyncio.Semaphore(32)

        async def again(ch):
            async with sem2:
                if cancelled():
                    return None
                ok = await thumbnails.grab_async(ch["url"], 30)
                msg = None
                if ok:
                    msg = {"url": ch["url"],
                           "thumb": thumbnails.thumb_data_uri(ch["url"])}
                emit("job_retry", job=ch["name"],
                     status="ok" if ok else "failed", msg=msg)
                return (ch, ok)

        emit("retry_start", msg={"total": len(failed)})
        return [r for r in await asyncio.gather(*(again(ch) for ch in failed))
                if r is not None]

    try:
        asyncio.run(run_all())
        if job_type == "health":
            summary = healthcheck.update_records(results)
        else:
            failed = [ch for ch, ok in results if not ok]
            retried = asyncio.run(retry_thumbs(failed)) if failed else []
            retry_ok = {ch["url"]: ok for ch, ok in retried}
            # a grabbed frame counts as responsive for the health stats too
            final = [(ch, retry_ok.get(ch["url"], ok)) for ch, ok in results]
            ok_n = sum(1 for _, ok in final if ok)
            summary = {"job": "thumbs", "checked": len(results), "grabbed": ok_n,
                       "health": healthcheck.update_records(final)}
        if cancelled():
            summary["cancelled"] = True
        emit("run_end", status="ok", msg=summary)
    except Exception as e:
        import traceback
        emit("run_end", status="failed",
             msg={"error": str(e), "traceback": traceback.format_exc()})
    finally:
        log_f.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2])
    else:
        print(json.dumps(main(*sys.argv[1:]), indent=2)[:1500])


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
