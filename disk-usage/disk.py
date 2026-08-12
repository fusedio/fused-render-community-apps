"""Disk Space Visualizer & Cleaner backend. Stdlib only.

Actions (dispatched from index.html via fused.runPython):
  scan    — one directory level: subdir totals via `du -kxd1`, file sizes via scandir
  preview — metadata + text head for a file, or top entries for a dir
  delete  — move the path to ~/.Trash (never a hard rm)
"""
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

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


def main(action: str = "scan", path: str = "~/Desktop", refresh: str = "", **_) -> dict:
    path = os.path.expanduser(path)
    print(f"{action} {path}")
    if action == "scan":
        return _scan(path, refresh == "1")
    if action == "preview":
        return _preview(path)
    if action == "delete":
        return _delete(path)
    return {"error": f"unknown action: {action}"}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
