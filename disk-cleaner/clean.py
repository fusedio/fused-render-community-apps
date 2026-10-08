"""Delete the contents of selected cache directories.

This is the only destructive file here, so it is deliberately narrow:

* it accepts catalog **ids**, never paths — nothing outside catalog.CATALOG can
  be targeted, and the page cannot smuggle in a path;
* it refuses to run without `confirm=true`;
* it empties a catalog directory's *contents* and always keeps the directory
  itself, so no app loses a path it expects to exist;
* every target is re-checked against the hard rules in `_guard` immediately
  before anything is removed.

`mode="trash"` moves items to the OS trash (reversible; space comes back when
the trash is emptied) — macOS's flat `~/.Trash`, or a proper freedesktop.org
XDG trash (`~/.local/share/Trash/{files,info}`, with a `.trashinfo` sidecar
per item) on Linux, so file managers there recognize what landed in it.
`mode="delete"` unlinks them permanently on either OS.
"""

import os
import shutil
import time

import catalog

# Never touch these, whatever a catalog entry claims to be.
FORBIDDEN = {
    "/",
    catalog.HOME,
    os.path.join(catalog.HOME, "Library"),
    os.path.join(catalog.HOME, "Documents"),
    os.path.join(catalog.HOME, "Desktop"),
    os.path.join(catalog.HOME, "Downloads"),
    os.path.join(catalog.HOME, "Pictures"),
    os.path.join(catalog.HOME, "Movies"),
    os.path.join(catalog.HOME, "Music"),
}

if catalog.OS == "Linux":
    XDG_TRASH = os.path.join(catalog.HOME, ".local", "share", "Trash")
    TRASH_FILES = os.path.join(XDG_TRASH, "files")
    TRASH_INFO = os.path.join(XDG_TRASH, "info")
else:
    TRASH_FILES = os.path.join(catalog.HOME, ".Trash")
    TRASH_INFO = None


def _guard(abs_path: str):
    if abs_path in FORBIDDEN:
        raise RuntimeError("refusing to clean protected directory %s" % abs_path)
    if not abs_path.startswith(catalog.HOME + os.sep):
        raise RuntimeError("refusing to clean outside the home directory: %s" % abs_path)
    if abs_path.count(os.sep) < 3:
        raise RuntimeError("refusing to clean a top-level directory: %s" % abs_path)
    if os.path.islink(abs_path):
        raise RuntimeError("refusing to clean a symlink: %s" % abs_path)
    if not os.path.isdir(abs_path):
        raise RuntimeError("not a directory: %s" % abs_path)


def _unique_trash_name(name: str) -> str:
    if not os.path.exists(os.path.join(TRASH_FILES, name)):
        return name
    stem, ext = os.path.splitext(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 500):
        suffix = "" if n == 1 else "-%d" % n
        cand = "%s %s%s%s" % (stem, stamp, suffix, ext)
        if not os.path.exists(os.path.join(TRASH_FILES, cand)):
            return cand
    raise RuntimeError("could not find a free name in the Trash for %s" % name)


def _trash(path: str):
    os.makedirs(TRASH_FILES, exist_ok=True)
    name = _unique_trash_name(os.path.basename(path))
    os.rename(path, os.path.join(TRASH_FILES, name))
    if TRASH_INFO is not None:
        # The freedesktop.org Trash spec: a file only counts as trashed to a
        # Linux file manager if this sidecar exists alongside it.
        os.makedirs(TRASH_INFO, exist_ok=True)
        deleted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(os.path.join(TRASH_INFO, name + ".trashinfo"), "w") as fh:
            fh.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (path, deleted_at))


def _remove(path: str, to_trash: bool):
    if to_trash:
        _trash(path)
    elif os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def _delete_counting(path: str, progress, errors: list) -> tuple:
    """Permanently delete the directory `path`, reporting bytes as each file
    goes rather than once at the end — one child of a cache (uv's
    `archive-v0`) can hold most of its 30 GB. Stops between `path`'s own
    children when the job is cancelled, so a cancelled sub-entry is never
    left half-deleted. Returns (bytes freed, finished)."""
    freed = 0
    with os.scandir(path) as it:
        children = list(it)
    for de in children:
        if progress.stopped():
            return freed, False
        progress.begin("%s/%s" % (os.path.basename(path), de.name))
        try:
            if de.is_symlink() or not de.is_dir(follow_symlinks=False):
                size = de.stat(follow_symlinks=False).st_size
                os.unlink(de.path)
                freed += size
                progress.add(size)
                continue
            for root, dirs, files in os.walk(de.path, topdown=False):
                for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
                    fp = os.path.join(root, name)
                    try:
                        size = os.lstat(fp).st_size
                        os.unlink(fp)
                        freed += size
                        progress.add(size)
                    except OSError as err:
                        if len(errors) < 200:
                            errors.append("%s: %s" % (fp, err.strerror or str(err)))
                for name in dirs:
                    dp = os.path.join(root, name)
                    if not os.path.islink(dp):
                        try:
                            os.rmdir(dp)
                        except OSError:
                            pass  # not empty: a file above failed and was reported
            os.rmdir(de.path)
        except OSError as err:
            if len(errors) < 200:
                errors.append("%s: %s" % (de.path, err.strerror or str(err)))
    os.rmdir(path)
    return freed, True


def _clean_dir_contents(abs_path: str, to_trash: bool, deadline: float, progress=None) -> dict:
    freed = 0
    removed = 0
    remaining = 0
    errors = []
    with os.scandir(abs_path) as it:
        children = list(it)

    for i, de in enumerate(children):
        # Deleting a multi-gigabyte cache can outlast the executor's 60 s
        # timeout, so the work is chunked: stop on the budget and report what
        # is left, and the page calls back until `remaining` reaches zero.
        # `progress` (the background job in clean_job.py) can also stop the
        # loop between items when the user cancels.
        if time.monotonic() > deadline or (progress and progress.stopped()):
            remaining = len(children) - i
            break
        if progress:
            progress.begin(de.name)
        try:
            if progress and not to_trash and de.is_dir(follow_symlinks=False) \
                    and not de.is_symlink():
                size, finished = _delete_counting(de.path, progress, errors)
                freed += size
                if not finished:
                    remaining = len(children) - i
                    break
                removed += 1
                progress.removed()
                continue
            if de.is_symlink() or not de.is_dir(follow_symlinks=False):
                size = de.stat(follow_symlinks=False).st_size
            else:
                size = catalog.dir_size(de.path)[0]
            _remove(de.path, to_trash)
            freed += size
            removed += 1
            if progress:
                progress.add(size)
                progress.removed()
        except OSError as err:
            errors.append("%s: %s" % (de.name, err.strerror or str(err)))

    return {"freed": freed, "removed": removed, "remaining": remaining, "errors": errors}


def _clean_entry(entry: dict, to_trash: bool, deadline: float, progress=None) -> dict:
    abs_path = catalog.resolve_entry(entry)
    if abs_path is None:
        raise RuntimeError("%s has no path on this OS" % entry["id"])
    _guard(abs_path)

    if progress:
        progress.entry(entry["name"])
    stats = _clean_dir_contents(abs_path, to_trash, deadline, progress)
    if entry["id"] == "trash" and catalog.OS == "Linux":
        # Emptying the Trash also means dropping its .trashinfo sidecars (and
        # any not-yet-purged "expunged" entries) — only clearing files/ would
        # leave orphaned metadata behind for the file manager to trip over.
        for extra in (TRASH_INFO, os.path.join(XDG_TRASH, "expunged")):
            if extra and os.path.isdir(extra) and time.monotonic() <= deadline:
                more = _clean_dir_contents(extra, to_trash=False, deadline=deadline,
                                           progress=progress)
                stats["removed"] += more["removed"]
                stats["remaining"] += more["remaining"]
                stats["errors"] += more["errors"]

    return {
        "id": entry["id"],
        "name": entry["name"],
        "path": abs_path,
        "freed": stats["freed"],
        "removed": stats["removed"],
        "remaining": stats["remaining"],
        "errors": stats["errors"],
    }


def _report_path(entry: dict) -> str:
    return catalog.entry_path(entry) or entry["id"]


def validate(ids: str, mode: str, confirm: bool) -> list:
    if not confirm:
        raise RuntimeError("clean.py refuses to run without confirm=true")
    if mode not in ("trash", "delete"):
        raise RuntimeError("mode must be 'trash' or 'delete', got %r" % mode)

    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    if not wanted:
        raise RuntimeError("no cache ids given")

    unknown = [i for i in wanted if i not in catalog.CATALOG_BY_ID]
    if unknown:
        raise RuntimeError("unknown cache ids: %s" % ", ".join(unknown))
    unsupported = [i for i in wanted if catalog.entry_path(catalog.CATALOG_BY_ID[i]) is None]
    if unsupported:
        raise RuntimeError("not available on this OS: %s" % ", ".join(unsupported))
    return wanted


def run(wanted: list, mode: str, deadline: float, progress=None) -> dict:
    results = []
    for cid in wanted:
        entry = catalog.CATALOG_BY_ID[cid]
        # The Trash cannot be moved to the Trash — emptying it is always final.
        to_trash = mode == "trash" and cid != "trash"
        try:
            results.append(_clean_entry(entry, to_trash, deadline, progress))
        except (OSError, RuntimeError) as err:
            results.append({
                "id": cid, "name": entry["name"], "path": _report_path(entry),
                "freed": 0, "removed": 0, "remaining": 0,
                "errors": [getattr(err, "strerror", None) or str(err)],
            })

    # Drop the cached sizes so the next scan reflects what just happened.
    try:
        os.remove(os.path.join(catalog.CACHE_DIR, "caches.json"))
    except OSError:
        pass

    return {
        "mode": mode,
        "results": results,
        "freed": sum(r["freed"] for r in results),
        "removed": sum(r["removed"] for r in results),
        # >0 means the time budget ran out: call again with the same ids.
        "remaining": sum(r["remaining"] for r in results),
        "errors": [e for r in results for e in r["errors"]],
    }


def main(ids: str = "", mode: str = "trash", confirm: bool = False, budget: float = 40.0):
    wanted = validate(ids, mode, confirm)
    return run(wanted, mode, time.monotonic() + budget)
