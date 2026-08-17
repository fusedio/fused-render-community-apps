"""The app's own record of what it rendered — see specs/app-gallery.md.

`fused.ai.image` writes its PNG under `<home>/ai/images/`, outside this app, so
keeping a per-app gallery means copying BYTES. `fused.writeFile` is UTF-8 text
only, which is the whole reason this file exists rather than a few lines of JS.

Stdlib only, so the folder needs no `pyproject.toml` and no first-run install.
"""

import glob
import json
import os
import shutil
from datetime import datetime

GALLERY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gallery")


def _stem(src: str) -> str:
    """The source PNG's own basename, minus the extension.

    Reusing it (rather than stamping a fresh name) keeps a saved image traceable
    to the render that produced it, and makes a double-save idempotent instead of
    leaving two copies of one picture.
    """
    return os.path.splitext(os.path.basename(src))[0] or "image"


def _safe_target(name: str, ext: str) -> str | None:
    """Resolve `name` inside ./gallery, or None if it tries to escape.

    `name` arrives from a URL param, so "../../etc/passwd" is a thing it can say.
    Checking the resolved path stays inside the folder is the check that actually
    holds; the separator test just gives a cheaper, clearer no.
    """
    if not name or os.sep in name or "/" in name or ".." in name:
        return None
    target = os.path.abspath(os.path.join(GALLERY, name + ext))
    if os.path.commonpath([target, os.path.abspath(GALLERY)]) != os.path.abspath(GALLERY):
        return None
    return target


def _record(sidecar: str) -> dict | None:
    """One gallery entry, or None if it isn't a usable pair.

    A corrupt sidecar or a sidecar whose PNG has been deleted is skipped rather
    than raised: one bad file must not blank the whole gallery.
    """
    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    name = os.path.splitext(os.path.basename(sidecar))[0]
    png = os.path.join(GALLERY, name + ".png")
    if not os.path.isfile(png):
        return None
    meta["name"] = name
    meta["png"] = png
    meta["size"] = os.path.getsize(png)
    return meta


def main(
    action: str = "list",
    src: str = "",
    name: str = "",
    prompt: str = "",
    model: str = "",
    width: int = 0,
    height: int = 0,
    steps: int = 0,
    guidance: float = 0.0,
    seed: int = 0,
    surface: str = "studio",
):
    if action == "list":
        records, skipped = [], 0
        for sidecar in glob.glob(os.path.join(GALLERY, "*.json")):
            rec = _record(sidecar)
            if rec is None:
                skipped += 1
            else:
                records.append(rec)
        records.sort(key=lambda r: r.get("saved_at", ""), reverse=True)
        return {"items": records, "skipped": skipped, "dir": GALLERY}

    if action == "save":
        # Errors are RETURNED, not raised. Failing to copy must not read like
        # failing to render — the PNG is still there at `source_path`, and the
        # page needs to say so in a banner rather than throw up a traceback.
        if not src or not os.path.isfile(src):
            return {"error": f"no such image: {src or '(no path given)'}"}
        os.makedirs(GALLERY, exist_ok=True)
        stem = _stem(src)
        png, sidecar = os.path.join(GALLERY, stem + ".png"), os.path.join(GALLERY, stem + ".json")
        try:
            shutil.copyfile(src, png)
        except OSError as e:
            return {"error": f"could not copy the image: {e}"}
        # The SETTLED reply, never the request: a sidecar claiming steps=500 for
        # a render the server clamped to 100 is a lie that outlives the session.
        meta = {
            "prompt": prompt,
            "model": model,
            "width": width,
            "height": height,
            "steps": steps,
            "guidance": guidance,
            "seed": seed,
            "source_path": src,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "surface": surface,
        }
        with open(sidecar, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        meta["name"] = stem
        meta["png"] = png
        meta["size"] = os.path.getsize(png)
        return meta

    if action == "delete":
        png, sidecar = _safe_target(name, ".png"), _safe_target(name, ".json")
        if png is None or sidecar is None:
            return {"error": f"refusing to delete outside the gallery: {name!r}"}
        removed = 0
        for path in (png, sidecar):
            try:
                os.remove(path)
                removed += 1
            except FileNotFoundError:
                pass  # already gone is the state we wanted
            except OSError as e:
                return {"error": f"could not delete {os.path.basename(path)}: {e}"}
        return {"deleted": name, "removed": removed}

    return {"error": f"unknown action {action!r} — expected list, save or delete"}
