"""Disk Space Visualizer & Cleaner backend. Stdlib only.

Actions (dispatched from index.html via fused.runPython):
  scan    — one directory level: subdir totals via `du -kxd1`, file sizes via scandir
  preview — metadata + text head for a file, or top entries for a dir
  delete  — move the path to ~/.Trash (never a hard rm)

du/pwd are POSIX-only, so on Windows main() routes to the *_win versions below.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

if os.name != "nt":
    import pwd

TRASH = os.path.expanduser("~/.Trash")
CACHE = os.path.expanduser("~/.cache/disk_viz_scan.json")
CACHE_TTL = 300  # seconds

# Never allow deleting these (or anything shallower than depth 3).
PROTECTED = {
    "/", "/System", "/Library", "/Applications", "/Users", "/private",
    "/usr", "/bin", "/sbin", "/etc", "/var", "/opt", "/Volumes",
    os.path.expanduser("~"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Library"),
    TRASH,
}


def _cache_load() -> dict:
    try:
        with open(CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_save(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def _du(path: str) -> int:
    """Recursive size of one subtree via a dedicated du process."""
    try:
        out = subprocess.run(["du", "-skx", path],
                             capture_output=True, text=True, timeout=300)
        return int(out.stdout.split("\t")[0]) * 1024
    except (ValueError, IndexError, subprocess.TimeoutExpired):
        return 0


def _scan(path: str, refresh: bool = False) -> dict:
    path = os.path.realpath(path)
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}"}

    cache = _cache_load()
    hit = cache.get(path)
    if hit and not refresh and time.time() - hit["ts"] < CACHE_TTL:
        hit["result"]["cached"] = True
        return hit["result"]

    try:
        entries = list(os.scandir(path))
    except PermissionError:
        return {"error": f"permission denied: {path}"}

    dirs, children = [], []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                dirs.append(e)
            elif e.is_file(follow_symlinks=False):
                children.append({
                    "name": e.name, "path": e.path, "dir": False,
                    "size": e.stat(follow_symlinks=False).st_size,
                })
        except OSError:
            continue

    # One du process per subdir, in parallel — saturates SSD instead of
    # walking the whole tree single-threaded.
    workers = min(16, max(4, (os.cpu_count() or 4)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        sizes = list(ex.map(lambda d: _du(d.path), dirs))
    for e, size in zip(dirs, sizes):
        children.append({"name": e.name, "path": e.path, "dir": True, "size": size})

    children.sort(key=lambda c: -c["size"])
    total = sum(c["size"] for c in children)
    disk = shutil.disk_usage(path)
    result = {
        "path": path,
        "total": total,
        "children": children[:400],
        "truncated": max(0, len(children) - 400),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
    }
    cache[path] = {"ts": time.time(), "result": result}
    _cache_save(cache)
    return result


TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".html", ".css", ".sh",
            ".yml", ".yaml", ".toml", ".csv", ".log", ".xml", ".ini", ".cfg", ".sql"}


def _preview(path: str) -> dict:
    path = os.path.realpath(path)
    if not os.path.exists(path):
        return {"error": f"missing: {path}"}
    st = os.lstat(path)
    info = {
        "path": path,
        "size": st.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        "owner": pwd.getpwuid(st.st_uid).pw_name,
        "mode": stat.filemode(st.st_mode),
        "dir": os.path.isdir(path),
    }
    if info["dir"]:
        try:
            names = sorted(os.listdir(path))
            info["entries"] = names[:50]
            info["entry_count"] = len(names)
        except PermissionError:
            info["entries"] = []
            info["entry_count"] = -1
    elif os.path.splitext(path)[1].lower() in TEXT_EXT and st.st_size < 5_000_000:
        try:
            with open(path, "r", errors="replace") as f:
                info["head"] = f.read(4000)
        except OSError:
            pass
    return info


def _delete(path: str) -> dict:
    path = os.path.realpath(path)
    if path in PROTECTED or len([p for p in path.split("/") if p]) < 3:
        return {"error": f"refusing to delete protected/shallow path: {path}"}
    if not os.path.exists(path):
        return {"error": f"missing: {path}"}
    os.makedirs(TRASH, exist_ok=True)
    dest = os.path.join(TRASH, os.path.basename(path))
    if os.path.exists(dest):
        dest += time.strftime("-%H%M%S")
    freed = _preview(path)["size"] if not os.path.isdir(path) else None
    shutil.move(path, dest)
    # Sizes changed everywhere above this path — drop stale cache entries.
    cache = _cache_load()
    stale = [k for k in cache
             if k == path or k.startswith(path + "/") or path.startswith(k + "/")]
    for k in stale:
        del cache[k]
    _cache_save(cache)
    return {"ok": True, "trashed_to": dest, "freed": freed}


# --- Windows backend --------------------------------------------------------
# scan reads sizes from the fused-render file index (no du); preview/delete are
# plain filesystem ops. Paths come back POSIX-style so index.html is unchanged.

def _posix(p: str) -> str:
    return p.replace("\\", "/")


def _win_path(p: str) -> str:
    # Undo two things before realpath: a surrounding "" pair from an Explorer
    # paste, and a breadcrumb's leading slash ("/C:/…" would go drive-relative).
    p = p.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1]
    return re.sub(r"^[\\/]+([A-Za-z]:)", r"\1", p)


def _index_store_dir() -> str:
    # Mirrors fused_render's home_dir()/_branch.sanitize() to find the store.
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    ref = (os.environ.get("FUSED_RENDER_BRANCH") or "").lower()
    if ref and ref not in ("main", "master", "head"):
        collapsed = re.sub(r"[^a-z0-9]+", "-", ref).strip("-")[:12].rstrip("-")
        if collapsed:
            home = os.path.join(home, "branches", collapsed)
    return os.path.join(home, "index")


def _index_connect():
    """duckdb connection with `files`/`dirs` views, or None if no index yet.
    Follows the manifest (never globs files/*.parquet — stale generations linger
    on disk and would double-count)."""
    import duckdb
    d = _index_store_dir()
    try:
        with open(os.path.join(d, "partitions.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None
    parts = [os.path.join(d, "files", p["file"]) for p in manifest.get("partitions") or []]
    dirs = os.path.join(d, "dirs.parquet")
    if not parts or not os.path.exists(dirs):
        return None  # files without dirs.parquet = index still building; not ready
    con = duckdb.connect()
    con.read_parquet(parts).create_view("files")
    con.read_parquet(dirs).create_view("dirs")
    return con


def _scan_win(path: str, refresh: bool = False) -> dict:
    """One directory level, with recursive per-child sizes aggregated from the
    index's `files` rows (no du walk, so it stays instant on huge trees)."""
    path = os.path.realpath(path)
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}"}
    p = _posix(path)

    con = _index_connect()
    if con is None:
        return {"error": "no file index yet — run a scan in fused-render, then retry"}

    prefix = p + "/"
    rel = len(prefix) + 1  # 1-based substr start of the path tail after "<p>/"
    rows = con.execute(
        "SELECT split_part(substr(path, ?), '/', 1) AS seg, "
        "       sum(size) AS total, "
        "       bool_or(substr(path, ?) LIKE '%/%') AS is_dir "
        "FROM files WHERE starts_with(path, ?) "
        "GROUP BY seg ORDER BY total DESC",
        [rel, rel, prefix]).fetchall()

    if not rows:
        known = con.execute("SELECT 1 FROM dirs WHERE dir = ? LIMIT 1", [p]).fetchone()
        if not known:
            return {"error": f"{p} isn't in the file index yet — rescan in fused-render to include it"}

    # Show the top 400 (size-sorted); lexists-stat only those to drop rows whose
    # entry is already gone (e.g. just trashed — the index lags until fsevents).
    head, tail = rows[:400], rows[400:]
    children = [{"name": seg, "path": prefix + seg, "dir": bool(is_dir), "size": int(sz or 0)}
                for seg, sz, is_dir in head
                if os.path.lexists(prefix + seg)]
    total = sum(c["size"] for c in children) + sum(int(sz or 0) for _, sz, _ in tail)
    disk = shutil.disk_usage(path)
    return {
        "path": p,
        "total": total,
        "children": children,
        "truncated": len(tail),
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
    }


def _preview_win(path: str) -> dict:
    path = os.path.realpath(path)
    if not os.path.exists(path):
        return {"error": f"missing: {path}"}
    st = os.lstat(path)
    info = {
        "path": _posix(path),
        "size": st.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        "owner": "—",  # no stdlib owner-by-name on Windows
        "mode": stat.filemode(st.st_mode),
        "dir": os.path.isdir(path),
    }
    if info["dir"]:
        try:
            names = sorted(os.listdir(path))
            info["entries"] = names[:50]
            info["entry_count"] = len(names)
        except PermissionError:
            info["entries"] = []
            info["entry_count"] = -1
    elif os.path.splitext(path)[1].lower() in TEXT_EXT and st.st_size < 5_000_000:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                info["head"] = f.read(4000)
        except OSError:
            pass
    return info


if os.name == "nt":  # only _delete_win reads these; don't build them on POSIX
    _HOME_WIN = os.path.expanduser("~")
    TRASH_WIN = os.path.join(_HOME_WIN, ".Trash")
    PROTECTED_WIN = {
        os.path.normpath(p).lower()
        for p in filter(None, [
            os.environ.get("SystemDrive", "C:") + os.sep,
            os.environ.get("windir"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramData"),
            os.path.dirname(_HOME_WIN),
            _HOME_WIN,
            os.path.join(_HOME_WIN, "Desktop"),
            os.path.join(_HOME_WIN, "Documents"),
            os.path.join(_HOME_WIN, "Downloads"),
            TRASH_WIN,
        ])
    }


def _delete_win(path: str) -> dict:
    path = os.path.realpath(path)
    norm = os.path.normpath(path)
    _, tail = os.path.splitdrive(norm)
    if norm.lower() in PROTECTED_WIN or len([p for p in tail.split(os.sep) if p]) < 3:
        return {"error": f"refusing to delete protected/shallow path: {path}"}
    if not os.path.exists(path):
        return {"error": f"missing: {path}"}
    os.makedirs(TRASH_WIN, exist_ok=True)
    dest = os.path.join(TRASH_WIN, os.path.basename(path))
    if os.path.exists(dest):
        dest += time.strftime("-%H%M%S")
    freed = os.lstat(path).st_size if not os.path.isdir(path) else None
    shutil.move(path, dest)
    return {"ok": True, "trashed_to": _posix(dest), "freed": freed}


def main(action: str = "scan", path: str = "~/Desktop", refresh: str = "", **_) -> dict:
    win = os.name == "nt"
    if win:
        path = _win_path(path)  # strip quotes/leading-slash before ~ expansion
    path = os.path.expanduser(path)
    print(f"{action} {path}")
    scan_, preview_, delete_ = (
        (_scan_win, _preview_win, _delete_win) if win else (_scan, _preview, _delete)
    )
    if action == "scan":
        return scan_(path, refresh == "1")
    if action == "preview":
        return preview_(path)
    if action == "delete":
        return delete_(path)
    return {"error": f"unknown action: {action}"}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
