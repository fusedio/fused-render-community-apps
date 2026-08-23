#!/usr/bin/env python
"""One index run, in its own process, reporting to fused-render's job manager.

Why a separate process at all. `views/lens_api.py` is reached through
`fused.runPython`, which is a fresh subprocess with a 60s cap — and an index
run is minutes: a walk of every root, a thumbnail and a vector per new file,
then the face sweep. It also has to survive the page that started it, because
the page is a tab: a user who starts a scan and then opens a photograph, or
switches to another view, has not asked to cancel their scan. So `op=reindex`
does not *do* the run, it `Popen`s this file with `start_new_session=True` and
returns — and everything the UI needs afterwards it learns from two places this
worker writes rather than from the process it spawned:

  * `<cache>/index_run.json` — the lock AND the progress record, one file
    (see `_write_run`). `op=status` reads it to answer `indexing`/`progress`,
    which is what drives the header spinner and the 2px progress line.
  * `POST <origin>/api/jobs` — the row in fused-render's own download manager,
    so a run is visible from anywhere in the app and cancellable from there.
    The reply to each tick carries `cancel_requested`, which is the only way
    this process is told to stop (SPEC BG-4).

The embedding does NOT happen here either: `lens.embed.ApiEmbedder` calls
fused-render's resident so400m over HTTP, so this worker holds no model and no
torch — one copy of a 4.55GB tower on the machine, in the process that already
owns it. `lens/indexer.py` is unchanged and unaware; the embedder it is handed
takes decoded PIL images exactly as the old one did.

Run it by hand for a narrowed scan (which is also how it is tested):

    FUSED_RENDER_ORIGIN=http://127.0.0.1:2477 \
      .venv/bin/python scripts/index_worker.py --root /tmp/scratch-photos
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lens import config                                        # noqa: E402
from lens.embed import (ApiEmbedder, EmbedApiError,             # noqa: E402
                        EmbedCancelled, api_origin)
from lens.indexer import STAGE_INDEX, _append_run_history, index_once  # noqa: E402
from lens.store import Store                                   # noqa: E402

# The lock and the progress record, in one file. One rather than two because
# they answer the same question — "is a run live, and where is it up to" — and
# two files can disagree: a lock without a progress file is a run the header
# cannot describe, and a progress file without a lock is a row that lies.
RUN_FILE = "index_run.json"

# Where this worker's stdout goes when `op=reindex` starts it. Named here
# because both sides need the same spelling and only one of them should own it.
LOG_FILE = "index.log"

# The job id, and it is deliberately CONSTANT rather than per-run. A page that
# is closed and reopened must re-attach to the row that already exists instead
# of adding a second one — and there is only ever one index run on a machine
# (the lock below enforces it), so a stable id is not a collision, it is the
# correct identity.
JOB_ID = "lens:index"

# A heartbeat this old means the process that wrote it is gone without saying
# so — killed, crashed, or its machine rebooted mid-run. Generous, because the
# gaps between two ticks are not all short: the first embed of a cold run waits
# on a multi-gigabyte model load, and a single 4K video's decode is seconds. The
# heartbeat thread writes every HEARTBEAT_S regardless of what the run is doing,
# so this only has to be longer than a stall in that *thread*.
STALE_AFTER_S = 90

# How often the record is rewritten and the job row posted. ~1/s is what the
# manager polls at, and it is also what bounds this worker's own cost: a report
# is a localhost POST, and one per embedded photo would have been thousands.
HEARTBEAT_S = 1.0


class Cancelled(Exception):
    """The manager's ✕ was pressed. Raised out of the progress callback.

    Raised rather than flagged-and-returned because `index_once` has no
    "stop now" parameter and should not grow one for this: the progress
    callback is already called once per file, from a point in that loop
    *outside* its per-file `try` (see the `if progress:` at the end of
    `index_once`'s main loop), so an exception from here unwinds the run
    cleanly instead of being recorded as "this photograph is corrupt".

    What is lost by unwinding: the vectors computed since the last checkpoint
    (CHECKPOINT_EVERY images) and the trips/faces passes. Nothing is left
    inconsistent — a catalogued row with no vector is exactly what the next
    run's `todo` picks up again — which is what makes this a safe way to stop.
    """


# ── the run record (lock + progress) ──────────────────────────────────────
class Report:
    """Everything the outside world can learn about this run, and the one
    writer of it.

    A small class rather than module globals because there are two threads: the
    run itself, which only ever *sets* counters (and must never block on a
    socket to do it), and a heartbeat thread that turns those counters into a
    file write and an HTTP post. The lock is over the numbers, not the IO.
    """

    def __init__(self, cache: Path, origin: str, job_id: str, title: str):
        self.cache = Path(cache)
        self.origin = (origin or "").rstrip("/")
        self.job_id = job_id
        self.title = title
        # Re-entrant on purpose: the SIGTERM handler calls `finish`, and a
        # signal is delivered to the main thread at an arbitrary statement —
        # including one inside `progress`, which already holds this. A plain
        # Lock deadlocks the worker there; an RLock lets the handler through.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.cancelled = threading.Event()
        self.started_at = time.time()
        self._t0 = time.monotonic()
        self.done = 0
        self.total = 0
        self.stage = STAGE_INDEX
        self.detail = "scanning folders…"
        self.state = "running"
        self.error = None
        self._thread = None

    # -- what the run tells us (cheap, no IO) --
    def progress(self, done, total, stage=STAGE_INDEX):
        """`index_once`'s progress callback — and the cancellation check.

        The check lives here because this is the one function the run calls
        often and from a place it is safe to raise from. Polling `/api/jobs`
        from the run's own thread instead would put a network round trip in
        front of every photograph."""
        with self._lock:
            self.done, self.total, self.stage = int(done), int(total), str(stage)
        if self.cancelled.is_set():
            raise Cancelled()

    def note(self, detail: str):
        """A sentence for the row while there is no fraction to show — a model
        load, chiefly, which is otherwise thirty seconds of an empty bar."""
        with self._lock:
            self.detail = str(detail)[:200]

    # -- what the world reads --
    def _snapshot(self) -> dict:
        with self._lock:
            done, total, stage = self.done, self.total, self.stage
            detail, state, error = self.detail, self.state, self.error
        # The stage speaks for itself once there is a fraction, and it has to:
        # an index run is two sweeps over the library, and a bar that fills,
        # resets and fills again reads as a bug unless the row says which sweep
        # it is watching (lens.html's `indexingLabel` makes the same point about
        # the header). `detail` set by hand — a model load — outlives this only
        # while there is nothing counted yet.
        if state == "running" and total:
            detail = ("finding people" if stage == "faces" else "indexing")
        elapsed = round(time.monotonic() - self._t0, 1)
        # Rate-based and only once there is a rate: `elapsed × remaining/done`.
        # Absent until a file is done, because "0s left" on the first tick is a
        # worse answer than no answer (daemon._progress_payload said the same).
        eta = (round(elapsed * (total - done) / done, 1)
               if done and total and done <= total else None)
        return {"pid": os.getpid(), "job_id": self.job_id, "state": state,
                "stage": stage, "done": done, "total": total,
                "detail": detail, "error": error,
                "started_at": self.started_at, "updated_at": time.time(),
                "elapsed_s": elapsed, "eta_s": eta}

    def _write_run(self, record: dict):
        """The record, atomically. `op=status` reads this file on every poll
        (~1/s while a run is live), so a reader must never catch it half
        written — same tmp-then-replace rule as config.save_config."""
        path = self.cache / RUN_FILE
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(record))
            os.replace(tmp, path)
        except OSError as exc:
            print(f"lens: could not write {path}: {exc}", flush=True)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _post_job(self, record: dict):
        """One tick to the job manager, and the cancel flag off its reply.

        Failure is swallowed on purpose: an index run must not die because the
        server it is reporting to restarted. The run file above is the record
        `op=status` actually reads, so a page keeps its spinner either way —
        what is lost is only the manager's row and the ability to cancel from
        it, which is a degradation, not a corruption.
        """
        if not self.origin:
            return
        body = {
            "id": self.job_id,
            "title": self.title,
            "kind": "task",
            "state": record["state"],
            "detail": record["detail"],
            "cancellable": True,
        }
        # A total of 0 is a bar drawn at 0% for a run that has not finished
        # walking yet; omitting both is the indeterminate sweep, which is the
        # honest shape for "counting the files".
        if record["total"]:
            body["done"] = record["done"]
            body["total"] = record["total"]
            body["unit"] = "images"
        if record["error"]:
            body["message"] = str(record["error"])[:400]
        request = urllib.request.Request(
            f"{self.origin}/api/jobs", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Fused": "1"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                reply = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"lens: job report failed: {exc}", flush=True)
            return
        if isinstance(reply, dict) and reply.get("cancel_requested"):
            if not self.cancelled.is_set():
                print("lens: cancel requested — stopping after this file",
                      flush=True)
            self.cancelled.set()

    # -- the heartbeat --
    def tick(self):
        record = self._snapshot()
        self._write_run(record)
        self._post_job(record)

    def start(self):
        self.tick()                     # the lock exists before the run does
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(HEARTBEAT_S):
            self.tick()

    def finish(self, state: str, detail: str, error=None):
        """The terminal report, and the reason this is a method rather than
        three lines at the end of `main`.

        A row whose reporter stops posting goes "stalled" in the manager, and a
        run file left saying `running` makes `op=status` show a spinner over a
        process that no longer exists. Both are the same bug — a lie that
        outlives the run — so every exit path calls this exactly once, and the
        heartbeat is stopped first so it cannot overwrite the terminal state
        with one more `running`."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=HEARTBEAT_S * 3)
        with self._lock:
            self.state, self.detail, self.error = state, detail, error
        self.tick()


# ── the lock ──────────────────────────────────────────────────────────────
def _alive(pid: int) -> bool:
    """Whether that pid is still a process we could signal.

    Signal 0 is the standard "does it exist and may I touch it" probe.
    `PermissionError` counts as alive: something is there under another user,
    and "I may not signal it" is not evidence it stopped."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def read_run(cache) -> dict | None:
    """The run record, or None if there isn't one this file can parse.

    Unparseable is treated as absent rather than raised on, for the same reason
    `config.load_config` does: this file is written by a process that can be
    killed mid-write, and one truncated record must not be able to wedge every
    future run out of starting."""
    path = Path(cache) / RUN_FILE
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    return record if isinstance(record, dict) else None


def live_run(cache) -> dict | None:
    """The record of a run that is genuinely still going, or None.

    Three ways a `running` record is NOT a live run, and all three have to be
    treated as free rather than blocking: the process is gone (crashed, killed,
    machine rebooted), the heartbeat has stopped (STALE_AFTER_S), or the record
    is already terminal. Without the first two a single hard kill would refuse
    every subsequent scan forever, with nothing in the UI to explain it."""
    record = read_run(cache)
    if not record or record.get("state") != "running":
        return None
    if not _alive(int(record.get("pid") or 0)):
        return None
    updated = float(record.get("updated_at") or 0)
    if time.time() - updated > STALE_AFTER_S:
        return None
    return record


# ── the run ───────────────────────────────────────────────────────────────
def _install_stop_signals(report: "Report"):
    """Route SIGTERM/SIGINT into the report's cancel flag.

    Best-effort: `signal.signal` refuses outside the main thread, and a worker
    that cannot install a handler is still a worker — it just has to be killed
    harder."""
    def handler(signum, frame):
        print(f"lens: signal {signum} — stopping after this file", flush=True)
        report.cancelled.set()
        # A signal that arrives before the run has started embedding has no
        # progress callback to unwind through, so the flag alone would be
        # ignored until the first file. `finish` is safe from a handler (it is
        # a file write and a POST) and leaves the record terminal rather than
        # `running` — which is what a killed worker owes the page.
        if report.state == "running" and report.done == 0:
            report.finish("cancelled", "cancelled before it started")
            os._exit(1)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def run(cache: Path, roots, origin: str, job_id: str, apple=None) -> int:
    # Resolved once, here, so the embedder and the job reporter can never end
    # up talking to two different servers.
    origin = (origin or api_origin() or "").rstrip("/")
    store = Store(cache)
    shape = store.embedding_shape()
    report = Report(cache, origin, job_id,
                    "Indexing photos" if len(roots) != 1
                    else f"Indexing {Path(roots[0]).name}")
    # The lock is claimed and the signal handlers installed BEFORE anything
    # slow, and that order is the fix for a real failure: the embedder check
    # below can take thirty seconds on a cold model, and until the record
    # exists a second ↻ press sees no live run and starts a second worker. A
    # SIGTERM in that same window killed the process outright, leaving an empty
    # log and no record of why — which is exactly the "did nothing" a user
    # cannot debug.
    # The lock, the signal handlers and the heartbeat all come up BEFORE
    # anything slow, and that order is two fixes rather than one:
    #
    #   * until the record exists a second ↻ press sees no live run and starts a
    #     second worker, and a SIGTERM in that window killed the process
    #     outright — an empty log and no record of why, which is exactly the
    #     "it did nothing" a user cannot debug;
    #   * the model check below is the longest wait in the whole run on a cold
    #     machine (4.55GB to fetch and load), and it is the wait most likely to
    #     be cancelled. A row that only appears afterwards cannot be cancelled
    #     during it, which would be the worst ✕ in the app.
    _install_stop_signals(report)
    report.note("checking the embedding model…")
    report.start()

    try:
        # The embedder is built — and asked to prove itself — before a single
        # row is touched. That order matters: `indexer.flush` records an embed
        # failure *on the rows of the batch it was for*, so a service that is
        # simply not there would not fail the run, it would flag every
        # photograph in the library as unreadable. Failing here costs nothing
        # and says why.
        embedder = ApiEmbedder(config.load_config(cache).get("model", "siglip2"),
                               origin=origin,
                               # `shape[1]` is 0 on an empty store, which is not
                               # a mismatch — it is a store with no opinion yet.
                               expect_dim=shape[1] or None,
                               progress=report.note,
                               should_stop=report.cancelled.is_set)
        embedder.load()
    except EmbedCancelled as exc:
        store.close()
        print(f"lens: {exc}", flush=True)
        report.finish("cancelled", "cancelled before the scan started")
        return 1
    except (EmbedApiError, ValueError) as exc:
        store.close()
        print(f"lens: {exc}", flush=True)
        # Reported as a terminal row rather than a silent exit: the page has
        # already been told a run started, and this is how it finds out it did
        # not get one.
        report.finish("error", "could not reach the embedding model", str(exc))
        return 2

    report.note("scanning folders…")
    stats, state, detail, error = None, "done", "", None
    try:
        stats = index_once(store, roots, embedder, cache,
                           progress=report.progress, apple=apple)
    except (Cancelled, EmbedCancelled):
        # `EmbedCancelled` too: a cancel pressed while a mid-run batch was
        # waiting on a re-load surfaces as that, and it means the same thing.
        state, detail = "cancelled", "cancelled — partial progress kept"
        # `index_once` writes the history line itself on the paths it returns
        # from; an unwound run never reaches it, so the line is written here
        # instead. Without it `op=status`'s `last_index` would keep describing
        # a run from days ago as if nothing had happened since.
        _append_run_history(cache, {"error": "cancelled by the user",
                                    "embedded": 0, "errors": 0,
                                    "duration_s": round(
                                        time.time() - report.started_at, 3)})
        print("lens: cancelled", flush=True)
    except (EmbedApiError, OSError, RuntimeError, ValueError) as exc:
        state, detail, error = "error", "indexing failed", str(exc)
        _append_run_history(cache, {"error": str(exc)[:300],
                                    "embedded": 0, "errors": 0,
                                    "duration_s": round(
                                        time.time() - report.started_at, 3)})
        print(f"lens: index failed: {exc}", flush=True)
    else:
        if report.cancelled.is_set():
            # A cancel that arrived during the FACE sweep does not unwind the
            # run: `index_once` wraps that whole pass in its own `except
            # Exception` (a missing face model must not fail an index that has
            # just made photographs searchable), so our Cancelled is swallowed
            # there and the run returns normally. It really did stop early, so
            # the row has to say cancelled rather than done — the flag is the
            # only witness left.
            state, detail = "cancelled", "cancelled — partial progress kept"
            print("lens: cancelled during the face pass", flush=True)
        else:
            embedded = stats.get("embedded", 0)
            detail = (f"{embedded} embedded, {stats.get('added', 0)} new"
                      if not stats.get("error") else stats["error"])
            if stats.get("error"):
                # A run that stopped on the memory guard finished *safely* and
                # saved its work — it is not an error row, it is a done row
                # with something to say (see indexer's aborted-run branch).
                error = stats["error"]
        print(f"lens: {json.dumps(stats)}", flush=True)
    finally:
        report.finish(state, detail, error)
        try:
            store.close()
        except Exception:
            pass
    return 0 if state == "done" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="index_worker",
        description="Run one lens index pass, reporting to fused-render.")
    ap.add_argument("--root", action="append", default=[],
                    help="scan only this folder (repeatable). Default: the "
                         "roots in config.json.")
    ap.add_argument("--origin", default=None,
                    help="fused-render's origin. Default: FUSED_RENDER_ORIGIN.")
    ap.add_argument("--job-id", default=JOB_ID,
                    help=f"job manager row id (default {JOB_ID}).")
    ap.add_argument("--force", action="store_true",
                    help="start even if another worker holds the lock.")
    args = ap.parse_args(argv)

    cache = config.cache_dir()
    held = None if args.force else live_run(cache)
    if held is not None:
        # The one refusal that is a *fact* rather than a limitation, and the
        # message says which run it is losing to. Exit 3 so `op=reindex` can
        # tell "already running" from "failed to start".
        print(f"lens: an index run is already going (pid {held.get('pid')}, "
              f"{held.get('done')}/{held.get('total')})", flush=True)
        return 3

    cfg = config.load_config(cache)
    if args.root:
        roots = [config.normalize_root(r) for r in args.root]
    else:
        roots = list(cfg["roots"])
    if not roots:
        print("lens: no folders configured — add one from the view's ⚙ menu",
              flush=True)
        return 4
    # A narrowed run never touches the Photos library. Apple ingest is
    # library-wide by nature — it has no notion of a root — so honouring the
    # config flag under `--root` would turn "index this one scratch folder"
    # into a full library sync, which is precisely what a narrowed run is for
    # avoiding (and what makes this file testable at all).
    apple = False if args.root else None

    try:
        return run(cache, roots, args.origin or None, args.job_id, apple=apple)
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
