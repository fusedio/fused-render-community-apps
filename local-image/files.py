"""Bring an outside picture into the app, and hold prompt history — see
specs/app-edit.md and specs/app-v2.md §3.5.

`fused.ai.image({image})` edits a file on disk, so a photo the user drops in or
snaps with the webcam has to BECOME a file before it can be edited. The page has
the bytes as a data URL and `fused.writeFile` is UTF-8 text only, so the decode
happens here, exactly like gallery.py copies bytes for the same reason.

Prompt history is a second, unrelated bit of accumulated state that landed in
this file rather than a third `.py`, per the app's own convention of one data
file per concern only when the concern is big enough to earn it — a ~30-entry
JSON list is not.

Both live in `.fused/data/`, not beside index.html: they are state the app
accumulated, not authored content, and they must not turn up in the app's git
history.

Stdlib only, so the folder still needs no `pyproject.toml`.
"""

import base64
import binascii
import json
import os
import re
import time

APP = os.path.dirname(os.path.abspath(__file__))
IMPORTS = os.path.join(APP, ".fused", "data", "imports")
HISTORY_PATH = os.path.join(APP, ".fused", "data", "prompt_history.json")
MAX_HISTORY = 30

# What a data URL from <canvas>.toDataURL or a FileReader looks like, and the
# only three encodings we are willing to write out.
DATA_URL = re.compile(r"^data:image/(png|jpeg|webp);base64,", re.I)
EXT = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
MAX_BYTES = 24 * 1024 * 1024


def _slug(name: str) -> str:
    """A short, filesystem-safe stem from a user-supplied filename."""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    return (stem[:40] or "import")


def _load_history() -> list:
    """The stored list, oldest-safe: a missing or corrupt file just means "no
    history yet" rather than an error the page has to show."""
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, str) and p.strip()]


def _save_history(items: list) -> None:
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    # Same atomic-write shape as the image import below: a reader mid-write
    # must see the old list or the new one, never a half-written file.
    tmp = f"{HISTORY_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2)
    os.replace(tmp, HISTORY_PATH)


def main(action: str = "import", data: str = "", name: str = "", source: str = "upload", prompt: str = ""):
    if action == "history_list":
        return {"items": _load_history()}

    if action == "history_add":
        p = prompt.strip()
        if not p:
            return {"items": _load_history()}
        items = _load_history()
        # Dedup by exact text, most-recent-first: a re-rendered prompt moves to
        # the front rather than appearing twice.
        items = [p] + [x for x in items if x != p]
        items = items[:MAX_HISTORY]
        _save_history(items)
        return {"items": items}

    if action == "import":
        # Errors are RETURNED, not raised, for the same reason gallery.py
        # returns them: a failed import is a banner on a working page, not a
        # traceback overlay that hides the picture the user was editing.
        m = DATA_URL.match(data or "")
        if not m:
            return {"error": "expected a data: URL for a PNG, JPEG or WebP image"}
        try:
            raw = base64.b64decode(data[m.end():], validate=True)
        except (binascii.Error, ValueError) as e:
            return {"error": f"could not decode the image data: {e}"}
        if not raw:
            return {"error": "the image data was empty"}
        if len(raw) > MAX_BYTES:
            return {"error": f"image is {len(raw) // (1024 * 1024)} MB — the limit is {MAX_BYTES // (1024 * 1024)} MB"}

        os.makedirs(IMPORTS, exist_ok=True)
        ext = EXT[m.group(1).lower()]
        # Time-ordered like the server's own image folder, so the directory
        # sorts chronologically when someone goes looking through it.
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(name)}"
        path = os.path.join(IMPORTS, stem + ext)
        n = 1
        while os.path.exists(path):
            path = os.path.join(IMPORTS, f"{stem}-{n}{ext}")
            n += 1
        # Written through a temp file and moved into place: an edit that reads
        # the path while the write is still going would otherwise see a
        # truncated image rather than no image.
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"error": f"could not write the image: {e}"}
        return {"path": path, "bytes": len(raw), "source": source, "name": os.path.basename(path)}

    return {"error": f"unknown action {action!r} — expected import, history_list or history_add"}
