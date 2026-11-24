"""Record what the page's browser engine can decode (sent once per page load)
to <cache dir>/player_caps.json, so playback problems can be diagnosed from
the shell: codec support decides whether streams can go through hls.js
as-is or must be transcoded by the ffmpeg bridge."""
import json
import os

import paths

PATH = os.path.join(paths.CACHE_DIR, "player_caps.json")


def main(caps: str = "{}") -> dict:
    try:
        data = json.loads(caps)
    except json.JSONDecodeError:
        return {"error": "caps is not JSON"}
    os.makedirs(paths.CACHE_DIR, exist_ok=True)
    tmp = PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PATH)
    print("player caps: " + json.dumps(data))
    return {"ok": True}


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
