"""Single source of truth for where OpenTV keeps its data.

Everything OpenTV writes (playlists, thumbnails, favorites, health.parquet,
background run logs) lives outside the repo, under the shared fused-render
cache, so a checkout stays clean and the data survives moving or re-cloning
the app. Override the location with OPEN_TV_CACHE_DIR.
"""
import os

CACHE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("OPEN_TV_CACHE_DIR")
    or os.path.join("~", ".fused-render", "cache", "open-tv")))

# Background health/thumbnail run logs (one directory per run).
RUNS_DIR = os.path.join(CACHE_DIR, "runs")
