"""Single source of truth for where OpenTV keeps its data.

Everything OpenTV writes (playlists, thumbnails, favorites, health.parquet)
lives outside the repo, under the shared fused-render cache, so a checkout
stays clean and the data survives moving/re-cloning the app. Override with
OPENTV_CACHE_DIR.
"""
import os

CACHE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("OPENTV_CACHE_DIR")
    or os.path.join("~", ".fused-render", "cache", "OpenTV")))
