"""Instant top-level directory listing for the drill-down browser.

Returns the volume gauge numbers and `path`'s children right away — files are
already sized (one stat() call, cheap). For directories, this also checks
each one's cached recursive size (same cache size_dir.py writes) and inlines
it directly when fresh, so a directory that hasn't changed comes back fully
sized in this one call — no per-directory round trip needed. Only a genuine
cache miss (or `refresh`) comes back size=None/pending=True, for the page to
resolve with its own concurrent call to size_dir.py.

This split matters because each size_dir.py call costs ~100ms of subprocess
overhead regardless of whether the actual lookup is a cache hit — with ~50
top-level directories, firing one per directory on every page load would
cost seconds even when nothing on disk had changed.
"""

from __future__ import annotations

import os
import shutil

import catalog


def _volume(abs_path: str) -> dict:
    usage = shutil.disk_usage(abs_path)
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0.0,
    }


def main(path: str = "~", max_age: float = 3600.0):
    abs_path = catalog.resolve(path or "~")
    if not os.path.isdir(abs_path):
        raise RuntimeError("not a directory: %s" % abs_path)

    try:
        root_dev = os.stat(abs_path).st_dev
        with os.scandir(abs_path) as it:
            raw = list(it)
    except OSError as err:
        raise RuntimeError("cannot read %s: %s" % (abs_path, err.strerror))

    children = []
    for de in raw:
        try:
            if de.is_symlink():
                children.append({
                    "name": de.name, "path": de.path, "is_dir": False,
                    "size": 0, "files": 0, "pending": False,
                    "symlink": True, "other_volume": False,
                })
                continue
            st = de.stat(follow_symlinks=False)
            if de.is_dir(follow_symlinks=False):
                if st.st_dev != root_dev:
                    # A mount point: sizing it would measure another volume
                    # (or a remote bucket, over the network). Listed, not walked.
                    children.append({
                        "name": de.name, "path": de.path, "is_dir": True,
                        "size": 0, "files": 0, "pending": False,
                        "symlink": False, "other_volume": True,
                    })
                else:
                    cached = catalog.read_size_cache(de.path, max_age)
                    if cached:
                        children.append({
                            "name": de.name, "path": de.path, "is_dir": True,
                            "size": cached["size"], "files": cached["files"], "pending": False,
                            "truncated": cached.get("truncated", False),
                            "symlink": False, "other_volume": False,
                        })
                    else:
                        children.append({
                            "name": de.name, "path": de.path, "is_dir": True,
                            "size": None, "files": None, "pending": True,
                            "symlink": False, "other_volume": False,
                        })
            else:
                children.append({
                    "name": de.name, "path": de.path, "is_dir": False,
                    "size": st.st_size, "files": 1, "pending": False,
                    "symlink": False, "other_volume": False,
                })
        except OSError:
            continue

    parent = os.path.dirname(abs_path)
    return {
        "path": abs_path,
        "parent": parent if parent != abs_path else None,
        "home": catalog.HOME,
        "volume": _volume(abs_path),
        "children": children,
    }
