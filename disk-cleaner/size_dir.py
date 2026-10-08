"""Recursive size of ONE directory, cached per path.

Split out of scan.py so the page can size many sibling directories
concurrently — one call per directory — instead of blocking on a single big
walk. scan.py itself already inlines a cache hit into the listing response,
so this file is only ever called for directories that missed that cache.
"""

from __future__ import annotations

import os
import time

import catalog


def main(path: str = "", refresh: bool = False, max_age: float = 3600.0, budget: float = 45.0):
    abs_path = catalog.resolve(path)
    if not os.path.isdir(abs_path):
        raise RuntimeError("not a directory: %s" % abs_path)

    if not refresh:
        cached = catalog.read_size_cache(abs_path, max_age)
        if cached:
            return cached

    started = time.monotonic()
    size, files, truncated = catalog.dir_size(abs_path, time.monotonic() + budget)
    payload = catalog.write_size_cache(abs_path, size, files, truncated)
    payload["elapsed"] = round(time.monotonic() - started, 2)
    return payload
