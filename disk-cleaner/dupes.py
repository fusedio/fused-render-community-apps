"""Find byte-identical files under the home folder: a read-only report.

Nothing here changes a file. It answers "how much space is stored twice, and
where", the first half of what twinfold does. Folding the copies into clones
(APFS) or deduping them in place (btrfs/XFS FIDEDUPERANGE) is a later step;
`foldable` in the result says whether this filesystem could do it.

A home-folder scan outlives runPython's 60 s limit, so this runs like
clean_job.py: `action="start"` spawns a detached worker that writes progress to
`.cache/dupes-job.json`, the page polls `action="status"`, and the worker also
reports to fused-render's activity panel under the same job id.

Why not fused.fileIndex: the index ignores node_modules, .venv, site-packages
and .cache, which are exactly where most duplicate bytes live.

Steps: walk (one device, no symlinks) → group by size → hash the first 64 KB →
hash whole files that still match. Hashes only pick candidates for a future
fold, which must compare byte for byte; for a report, a 128-bit BLAKE2b match
is enough. Whole-file hashes are cached by (size, mtime, inode) so a rescan
only reads files that changed.
"""

import ctypes
import ctypes.util
import hashlib
import json
import os
import stat as statmod
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
JOB_FILE = os.path.join(CACHE_DIR, "dupes-job.json")
RESULT_FILE = os.path.join(CACHE_DIR, "dupes-result.json")
HASH_FILE = os.path.join(CACHE_DIR, "dupes-hashes.json")
CANCEL_FILE = os.path.join(CACHE_DIR, "dupes-job.cancel")
HASH_VERSION = 1
HEAD = 64 * 1024
MAX_SETS = 400          # sets kept in the result; totals still cover all of them
TERMINAL = ("done", "error", "cancelled")


class Cancelled(Exception):
    """Raised inside the scan when the user stops it. Not an OSError, so the
    per-file `except OSError` (unreadable file → skip it) can't swallow it."""

# Never look inside these (relative to the scan root, or by bundle suffix).
# Cloud folders hold placeholders that a read would download; libraries are
# managed by their apps; ~/Library is app state, not the user's files.
SKIP_TOP = {"Library", ".Trash", "Dropbox", "Google Drive", "iCloud Drive"}
SKIP_SUFFIX = (".photoslibrary", ".musiclibrary", ".tvlibrary", ".photolibrary")
SF_DATALESS = 0x40000000
UF_IMMUTABLE, SF_IMMUTABLE = 0x2, 0x20000


# ---- json files ------------------------------------------------------------

def _load(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


# ---- filesystem facts --------------------------------------------------------

def _fs_type(path: str) -> str:
    """Filesystem type name of the volume holding `path` ("apfs", "btrfs", ...)."""
    path = os.path.realpath(path)
    best, kind = "", "unknown"
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                # "/dev/disk3s5 on /System/Volumes/Data (apfs, local, ...)"
                try:
                    _, rest = line.split(" on ", 1)
                    mnt, opts = rest.rsplit(" (", 1)
                except ValueError:
                    continue
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, kind = mnt, opts.split(",")[0].strip(" )")
        else:
            with open("/proc/self/mounts") as fh:
                for line in fh:
                    parts = line.split()
                    mnt = parts[1].replace("\\040", " ")
                    if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                        best, kind = mnt, parts[2]
    except Exception:
        pass
    return kind


FOLD_METHOD = {"apfs": "APFS clones", "btrfs": "btrfs dedupe", "xfs": "XFS reflink dedupe"}


class _CloneInfo:
    """macOS only: which copies already share all their blocks (APFS clones).

    getattrlist with FSOPT_ATTR_CMN_EXTENDED returns, in bit order,
    ATTR_CMNEXT_PRIVATESIZE (off_t) then ATTR_CMNEXT_CLONEID (u_int64). Full
    clones of one file report the same clone id, so the number of distinct
    ids in a set is how many times its data is really stored.
    """

    class _AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                    ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                    ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                    ("forkattr", ctypes.c_uint32)]

    def __init__(self):
        self.ok = False
        if sys.platform != "darwin":
            return
        try:
            self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            self.req = self._AttrList(5, 0, 0, 0, 0, 0, 0x00000008 | 0x00000100)
            self.buf = ctypes.create_string_buffer(64)
            self.ok = True
        except OSError:
            pass

    def get(self, path: str):
        """(clone_id, private_bytes), or None when the call isn't available."""
        if not self.ok:
            return None
        rc = self.libc.getattrlist(path.encode(), ctypes.byref(self.req), self.buf,
                                   ctypes.c_size_t(64), ctypes.c_ulong(0x1 | 0x20))
        if rc != 0:
            return None
        private = int.from_bytes(self.buf.raw[4:12], "little", signed=True)
        clone_id = int.from_bytes(self.buf.raw[12:20], "little")
        return clone_id, private


# ---- scan --------------------------------------------------------------------

def _hash(path: str, limit=None, stop=None, progress=None) -> str:
    h = hashlib.blake2b(digest_size=16)
    left = limit
    with open(path, "rb", buffering=0) as fh:
        while True:
            n = 1 << 20 if left is None else min(1 << 20, left)
            if n <= 0:
                break
            chunk = fh.read(n)
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
            if progress:
                progress(len(chunk))
            if stop and stop():
                raise Cancelled
    return h.hexdigest()


def _walk(root: str, min_size: int, job: dict, stop):
    """Yield (path, stat) for regular files worth comparing."""
    dev = os.lstat(root).st_dev
    uid = os.getuid()
    stack = [root]
    while stack:
        if stop():
            raise Cancelled
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            job["skipped_dirs"] += 1
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if d == root and e.name in SKIP_TOP:
                            continue
                        if e.name.endswith(SKIP_SUFFIX) or e.name.startswith(("OneDrive", "Google Drive")):
                            continue
                        st = e.stat(follow_symlinks=False)
                        if st.st_dev == dev:
                            stack.append(e.path)
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                job["files_seen"] += 1
                if not statmod.S_ISREG(st.st_mode) or st.st_size < min_size:
                    continue
                flags = getattr(st, "st_flags", 0)
                # Same rules a fold would use: a hard link already shares its
                # data, and placeholders / locked / other people's files are
                # never touched, so they don't count as savings either.
                if st.st_nlink > 1 or st.st_uid != uid or flags & (SF_DATALESS | UF_IMMUTABLE | SF_IMMUTABLE):
                    continue
                yield e.path, st


def _scan(root: str, min_size: int, job: dict, lock, stop):
    def phase(name, detail=""):
        with lock:
            job["phase"], job["detail"] = name, detail

    # 1. list and group by size
    phase("listing", "Listing files…")
    by_size = {}
    for path, st in _walk(root, min_size, job, stop):
        by_size.setdefault(st.st_size, []).append((path, st))
        if job["files_seen"] % 2000 == 0:
            with lock:
                job["detail"] = "Listing files… %s seen" % format(job["files_seen"], ",")
    groups = [g for g in by_size.values() if len(g) > 1]
    with lock:
        job["candidates"] = sum(len(g) for g in groups)
        job["total"] = sum(st.st_size for g in groups for _, st in g)
        job["done"] = 0

    def advance(n):
        with lock:
            job["done"] += n

    # 2. head hash, 3. full hash (cached)
    cache = _load(HASH_FILE, {})
    if cache.get("v") != HASH_VERSION:
        cache = {"v": HASH_VERSION, "files": {}}
    known = cache["files"]
    fresh = {}
    sets = []
    for group in sorted(groups, key=lambda g: -g[0][1].st_size):
        size = group[0][1].st_size
        phase("comparing", "Comparing %s files of %s" % (len(group), _human(size)))
        heads = {}
        for path, st in group:
            sig = [st.st_size, st.st_mtime_ns, st.st_ino]
            hit = known.get(path)
            if hit and hit[:3] == sig:
                heads.setdefault(("full", hit[3]), []).append(path)
                fresh[path] = hit
                advance(size)
                continue
            try:
                heads.setdefault(("head", _hash(path, HEAD, stop, None)), []).append((path, sig))
            except OSError:
                advance(size)
        fulls = {}
        for (kind, key), items in heads.items():
            if kind == "full":
                for path in items:
                    fulls.setdefault(key, []).append(path)
        for (kind, key), items in heads.items():
            if kind != "head":
                continue
            if len(items) == 1 and not any(k == "full" for k, _ in heads):
                advance(size)
                continue
            for path, sig in items:
                try:
                    digest = _hash(path, None, stop, advance)
                except OSError:
                    continue
                fresh[path] = sig + [digest]
                fulls.setdefault(digest, []).append(path)
        for digest, paths in fulls.items():
            if len(paths) > 1:
                sets.append({"size": size, "hash": digest, "paths": sorted(paths)})
    _save(HASH_FILE, {"v": HASH_VERSION, "files": fresh})
    return sets


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def _summarise(root: str, sets: list, job: dict) -> dict:
    clones = _CloneInfo()
    folders = {}
    savings = shared = 0
    for s in sets:
        size, paths = s["size"], s["paths"]
        ids = [clones.get(p) for p in paths]
        if all(ids):
            stored = len({cid for cid, _ in ids})
        else:
            stored = len(paths)
        s["stored"] = stored
        s["savings"] = (stored - 1) * size
        savings += s["savings"]
        shared += (len(paths) - stored) * size
        # Credit the savings to the folders the extra copies live in.
        share = s["savings"] / (len(paths) - 1)
        for p in paths[1:]:
            rel = os.path.relpath(p, root)
            top = rel.split(os.sep, 1)[0] if os.sep in rel else "(files in %s)" % os.path.basename(root)
            folders[top] = folders.get(top, 0) + share
    sets.sort(key=lambda s: -s["savings"])
    fs = _fs_type(root)
    return {
        "root": root,
        "scanned_at": time.time(),
        "files_seen": job["files_seen"],
        "min_size": job["min_size"],
        "sets_total": len([s for s in sets if s["savings"] > 0]),
        "files_total": sum(len(s["paths"]) for s in sets),
        "savings": savings,
        "already_shared": shared,
        "clone_aware": clones.ok,
        "fs": fs,
        "foldable": fs in FOLD_METHOD,
        "fold_method": FOLD_METHOD.get(fs),
        "folders": sorted(({"name": k, "savings": round(v)} for k, v in folders.items() if v > 0),
                          key=lambda f: -f["savings"]),
        "sets": [s for s in sets if s["savings"] > 0][:MAX_SETS],
    }


# ---- job plumbing (same shape as clean_job.py) --------------------------------

def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _status():
    job = _load(JOB_FILE)
    if job is None:
        return {"state": "none"}
    if job["state"] == "running" and not _alive(job.get("pid")):
        job.update(state="error", detail="the duplicate scan exited unexpectedly")
        _save(JOB_FILE, job)
    return job


def main(action: str = "status", path: str = "~", min_size: int = 1048576, origin: str = ""):
    """status | start | cancel | result."""
    if action == "status":
        return _status()
    if action == "result":
        return _load(RESULT_FILE) or {"none": True}
    if action == "cancel":
        job = _status()
        if job.get("state") == "running":
            open(CANCEL_FILE, "w").close()
        return job
    if action != "start":
        raise RuntimeError("unknown action %r" % action)

    if _status().get("state") == "running":
        return _status()
    root = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(root):
        raise RuntimeError("not a folder: %s" % path)
    try:
        os.remove(CANCEL_FILE)
    except OSError:
        pass
    job = {
        "id": "disk-cleaner-dupes-" + uuid.uuid4().hex[:10],
        "title": "Disk Cleaner: finding duplicates",
        "state": "running", "phase": "listing", "detail": "Starting…",
        "root": root, "min_size": max(1, int(min_size)),
        "files_seen": 0, "skipped_dirs": 0, "candidates": 0, "done": 0, "total": 0,
        "origin": os.environ.get("FUSED_RENDER_ORIGIN") or origin,
        "started": time.time(),
    }
    _save(JOB_FILE, job)
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker"],
        cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    latest = _load(JOB_FILE) or job
    latest["pid"] = proc.pid
    _save(JOB_FILE, latest)
    return latest


def _post(job: dict) -> bool:
    """Best-effort report to the activity panel; returns cancel_requested."""
    origin = job.get("origin")
    if not origin:
        return False
    body = {"id": job["id"], "title": job["title"], "kind": "task", "unit": "bytes",
            "cancellable": job["state"] == "running", "state": job["state"],
            "done": job["done"], "detail": job["detail"]}
    if job["total"]:
        body["total"] = max(job["total"], job["done"])
    req = urllib.request.Request(
        origin.rstrip("/") + "/api/jobs", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Fused": "1"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            reply = json.loads(resp.read() or b"{}")
        return bool(reply.get("cancel_requested") or (reply.get("job") or {}).get("cancel_requested"))
    except Exception:
        return False


def _worker():
    job = _load(JOB_FILE)
    if not job or job.get("state") != "running":
        return
    job["pid"] = os.getpid()
    lock = threading.Lock()
    halt = threading.Event()
    finished = threading.Event()

    def stop():
        if os.path.exists(CANCEL_FILE):
            halt.set()
        return halt.is_set()

    def report():
        with lock:
            snap = dict(job)
        _save(JOB_FILE, snap)
        if _post(snap):
            halt.set()

    def heartbeat():
        while not finished.wait(1.0):
            report()

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        sets = _scan(job["root"], job["min_size"], job, lock, stop)
        with lock:
            job["phase"], job["detail"] = "summarising", "Checking which copies already share space…"
        result = _summarise(job["root"], sets, job)
        _save(RESULT_FILE, result)
        with lock:
            job["state"] = "done"
            job["detail"] = "%s stored more than once" % _human(result["savings"])
    except Cancelled:
        with lock:
            job["state"], job["detail"] = "cancelled", "Stopped"
    except Exception as err:  # noqa: BLE001 — must always reach a terminal state
        with lock:
            job["state"], job["detail"] = "error", "%s: %s" % (type(err).__name__, err)
    finally:
        finished.set()
        beat.join(timeout=5)
        job["finished"] = time.time()
        try:
            os.remove(CANCEL_FILE)
        except OSError:
            pass
        report()


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker()
