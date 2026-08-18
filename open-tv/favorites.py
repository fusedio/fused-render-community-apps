"""Favorite channels, persisted in favorites.json."""
import json
import os
import sys

import paths
import thumbnails

# The fused-render runner (app >= Jul 2026) exec()s the entry file without
# __file__; its preamble puts the script's directory at sys.path[0].
_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))

CACHE_DIR = paths.CACHE_DIR
PATH = os.path.join(CACHE_DIR, "favorites.json")


def _load() -> dict:
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def main(action: str = "list", channel: str = "") -> dict:
    favs = _load()
    if action == "toggle":
        ch = json.loads(channel)
        if ch["url"] in favs:
            del favs[ch["url"]]
        else:
            favs[ch["url"]] = {k: ch.get(k, "") for k in
                               ("url", "name", "logo", "group", "id")}
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(PATH, "w", encoding="utf-8") as f:
            json.dump(favs, f, indent=1)
    chans = list(favs.values())
    for ch in chans:
        ch["thumb"] = thumbnails.thumb_data_uri(ch["url"])
    import channels as channels_mod
    return {"channels": chans, "urls": list(favs.keys()),
            "categories": channels_mod.CATEGORIES}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
