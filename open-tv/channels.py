"""Fetches and parses the iptv-org playlist; returns the channel list."""
import os
import sys
import re
import time
import urllib.request

import paths

# The fused-render runner (app >= Jul 2026) exec()s the entry file without
# __file__; its preamble puts the script's directory at sys.path[0].
_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))

CACHE_DIR = paths.CACHE_DIR
CACHE_TTL = 24 * 3600  # refetch after 24 hours

CATEGORIES = ["all", "auto", "animation", "business", "classic", "comedy",
              "cooking", "culture", "documentary", "education",
              "entertainment", "family", "general", "interactive", "kids",
              "legislative", "lifestyle", "movies", "music", "news",
              "outdoor", "public", "relax", "religious", "series", "science",
              "shop", "sports", "travel", "weather", "xxx"]

ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')


def _playlist_path(category: str, refresh: bool) -> str:
    """Cached playlist file. Network is touched only when the cache is
    missing, or when refresh=True and the cache is older than CACHE_TTL."""
    if category == "all":
        url = "https://iptv-org.github.io/iptv/index.m3u"
    else:
        url = f"https://iptv-org.github.io/iptv/categories/{category}.m3u"
    path = os.path.join(CACHE_DIR, "playlists", f"{category}.m3u")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    stale = exists and time.time() - os.path.getmtime(path) >= CACHE_TTL
    if not exists or (refresh and stale):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            with open(path, "wb") as f:
                f.write(data)
        except Exception:
            if not exists:
                raise  # no cache to fall back on
    return path


def _category_counts() -> dict:
    """Channel count per category, derived from the full index playlist
    (group-title tokens). Cached alongside the playlist."""
    import json
    all_path = _playlist_path("all", False)
    counts_path = os.path.join(CACHE_DIR, "playlists", "counts.json")
    if (os.path.exists(counts_path)
            and os.path.getmtime(counts_path) >= os.path.getmtime(all_path)):
        with open(counts_path, encoding="utf-8") as f:
            return json.load(f)
    counts = {c: 0 for c in CATEGORIES if c != "all"}
    total = 0
    with open(all_path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("#EXTINF"):
                continue
            total += 1
            attrs = dict(ATTR_RE.findall(line))
            for tok in attrs.get("group-title", "").split(";"):
                tok = tok.strip().lower()
                if tok in counts:
                    counts[tok] += 1
    counts["all"] = total
    with open(counts_path, "w", encoding="utf-8") as f:
        json.dump(counts, f)
    return counts


def main(category: str = "sports", refresh: int = 0) -> dict:
    if category not in CATEGORIES:
        return {"error": f"unknown category {category!r}", "categories": CATEGORIES}
    channels = []
    current = None
    with open(_playlist_path(category, bool(int(refresh))), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                attrs = dict(ATTR_RE.findall(line))
                name = line.rsplit(",", 1)[-1].strip()
                current = {
                    "name": name,
                    "logo": attrs.get("tvg-logo", ""),
                    "group": attrs.get("group-title", ""),
                    "id": attrs.get("tvg-id", ""),
                }
            elif line.startswith("#"):
                continue
            elif current is not None:
                current["url"] = line
                channels.append(current)
                current = None
    import thumbnails
    for ch in channels:
        ch["thumb"] = thumbnails.thumb_data_uri(ch["url"])
    print(f"parsed {len(channels)} channels ({category})")
    return {"channels": channels, "categories": CATEGORIES,
            "category": category, "counts": _category_counts()}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
