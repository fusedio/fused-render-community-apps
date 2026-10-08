"""Size every cache location in the catalog.

Sizing ~25 directories means a lot of stat() calls, so results are cached in
.cache/caches.json and reused until `refresh` is passed. Directories are walked
on a thread pool — the work is pure I/O wait, so a wider pool than CPU count
still pays off.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import catalog

# The executor runs this file with the working directory set beside it (and
# without defining __file__), so the cache dir is addressed relatively.
CACHE_FILE = os.path.join(catalog.CACHE_DIR, "caches.json")


def _load_cached(max_age: float):
    try:
        with open(CACHE_FILE) as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    age = time.time() - payload.get("scanned_at", 0)
    if age > max_age:
        return None
    payload["age"] = age
    payload["cached"] = True
    return payload


def _save_cached(payload: dict):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    # Overview and Cleanup can both trigger a caches.py run around the same
    # time — a per-process tmp name keeps two concurrent writers from racing
    # on the same file (one truncating/replacing what the other just wrote).
    tmp = "%s.%d.tmp" % (CACHE_FILE, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, CACHE_FILE)


def main(refresh: bool = False, max_age: float = 900.0, budget: float = 45.0):
    if not refresh:
        cached = _load_cached(max_age)
        if cached:
            return cached

    deadline = time.monotonic() + budget
    started = time.monotonic()
    applicable = catalog.catalog_for_os()
    with ThreadPoolExecutor(max_workers=16) as pool:
        entries = list(pool.map(lambda e: catalog.entry_size(e, deadline), applicable))

    catalog.mark_nested(entries)
    entries.sort(key=lambda e: -e["size"])
    # A nested entry's bytes are already inside its ancestor's size, so the
    # aggregate totals only sum entries that aren't nested under another one
    # present here — otherwise e.g. Chrome's cache gets counted once on its
    # own and again inside "Application caches".
    countable = [e for e in entries if e["nested_under"] is None]
    payload = {
        "entries": entries,
        "os": catalog.OS,
        "total": sum(e["size"] for e in countable),
        "reclaimable": sum(e["size"] for e in countable if e["risk"] != "review"),
        "scanned_at": time.time(),
        "elapsed": round(time.monotonic() - started, 2),
        "truncated": any(e["truncated"] for e in entries),
        "age": 0.0,
        "cached": False,
    }
    _save_cached(payload)
    return payload
