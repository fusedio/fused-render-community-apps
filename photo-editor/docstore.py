"""Documents on disk: autosave, step history and the "current document" pointer.

The page and the agent tools (`agent/tools.py`, published over MCP) both edit a
document by committing a new revision here, so either side can pick up what the
other did:

    .fused/data/documents/
        current.json            which document is open, and its latest rev
        <doc id>/
            doc.json            {id, name, rev, created_at, updated_at, snap}
            history/000042.json one file per revision: {rev, label, source, time, snap}
            thumbs/000042.png   small render of that revision
            thumb.png           latest thumbnail (the start screen's Recent list)

`snap` is exactly what the page's `snapshot()` produces: page size, background,
guides and a list of serialised layers. Image layers point at PNGs under
`.fused/data/sessions/` by absolute `workingPath`; the page rebuilds each
layer's URL from that path, so `src` is never stored.

The page polls `current.json` (a cheap `fused.readFile`), so a revision the
agent commits shows up in the open editor within about a second.

stdlib only. Every write is atomic, and a per-document `flock` serialises the
page and the agent when both commit at once.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, ".fused", "data")
DOCS_DIR = os.path.join(DATA_DIR, "documents")
CURRENT_PATH = os.path.join(DOCS_DIR, "current.json")

#: Where Save and Export write finished files (not ~/Downloads).
SAVE_DIR = os.path.expanduser(os.environ.get("PHOTO_EDITOR_SAVE_DIR", "~/Pictures/Photo Editor"))

#: Revisions kept per document; older history files are pruned.
HISTORY_LIMIT = 200

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DocumentError(ValueError):
    """A document id that does not exist, or a request that cannot apply."""


# ---------------------------------------------------------------- low level


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _write_json(path: str, value) -> None:
    _write_atomic(path, json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _read_json(path: str):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def doc_dir(doc_id: str) -> str:
    if not _SAFE_ID.match(doc_id or ""):
        raise DocumentError(f"Invalid document id: {doc_id!r}")
    return os.path.join(DOCS_DIR, doc_id)


@contextlib.contextmanager
def _locked(doc_id: str):
    folder = doc_dir(doc_id)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, ".lock"), "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield folder
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _clean_snap(snap: dict) -> dict:
    """Drop what is only meaningful inside one page load (image URLs)."""
    snap = dict(snap or {})
    layers = []
    for layer in snap.get("layers") or []:
        layer = dict(layer)
        layer.pop("src", None)
        layers.append(layer)
    snap["layers"] = layers
    return snap


def _decode_png(data_url: str) -> bytes:
    return base64.b64decode(data_url.split(",", 1)[-1])


# ---------------------------------------------------------------- current pointer


def get_current() -> dict:
    try:
        value = _read_json(CURRENT_PATH)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def set_current(doc_id: str, rev: int, source: str) -> dict:
    """Point the editor at `doc_id`.

    `seq` changes on every write so the page can tell a fresh pointer from the
    one it saw last. `source` is "user" (the page) or "agent": the page only
    *switches* documents for an agent pointer, so two open tabs never pull each
    other around.
    """
    value = {"id": doc_id, "rev": int(rev), "source": source,
             "seq": f"{time.time_ns()}-{secrets.token_hex(2)}", "time": time.time()}
    _write_json(CURRENT_PATH, value)
    return value


def _follow(doc_id: str, rev: int, source: str, claim_from: str | None = None) -> None:
    """Move the pointer after a write. The agent always takes it. A page save
    only keeps it fresh when it already names this document (or `claim_from`,
    the document the page had open before starting this one): a save that
    lands after the editor or Claude moved on must not pull the pointer back."""
    if source != "user":
        set_current(doc_id, rev, source)
        return
    held = get_current().get("id") or ""
    if held in ("", doc_id) or (claim_from is not None and held == claim_from):
        set_current(doc_id, rev, source)


# ---------------------------------------------------------------- documents


def new_id() -> str:
    return "d" + time.strftime("%Y%m%d") + "-" + secrets.token_hex(4)


def create(name: str, snap: dict, source: str = "user", label: str = "new document",
           thumb_png: bytes | None = None, make_current: bool = True, doc_id: str = "",
           claim_from: str | None = None) -> dict:
    """New document. A caller-chosen `doc_id` makes this idempotent: the page
    picks the id up front, so a create retried after a reload (or queued
    behind the first one) lands on the document the first attempt made
    instead of a duplicate, committing its snapshot as the next step when it
    differs from what is on disk."""
    doc_id = doc_id or new_id()
    now = time.time()
    snap = _clean_snap(snap)
    with _locked(doc_id) as folder:
        if os.path.exists(os.path.join(folder, "doc.json")):
            record = load(doc_id)
            if snap != record.get("snap"):
                record["parent"] = int(record["rev"])
                record["rev"] = int(record["rev"]) + 1
                record.update(updated_at=now, source=source, label=label or "edit", snap=snap)
                _write_history(folder, record, thumb_png)
                _write_json(os.path.join(folder, "doc.json"), record)
        else:
            record = {"id": doc_id, "name": name or "Untitled", "rev": 1, "created_at": now,
                      "updated_at": now, "source": source, "label": label, "snap": snap}
            _write_history(folder, record, thumb_png)
            _write_json(os.path.join(folder, "doc.json"), record)
    if make_current:
        _follow(doc_id, record["rev"], source, claim_from)
    return record


def load(doc_id: str) -> dict:
    path = os.path.join(doc_dir(doc_id), "doc.json")
    try:
        return _read_json(path)
    except FileNotFoundError:
        raise DocumentError(f"No document {doc_id!r}") from None


def commit(doc_id: str, snap: dict, label: str, source: str, base_rev: int | None = None,
           name: str | None = None, thumb_png: bytes | None = None,
           make_current: bool = True, parent: int | None = None) -> dict:
    """Write a new revision. With `base_rev`, refuse if someone else got there
    first -- the page then reloads the newer revision instead of overwriting it.

    `parent` is the revision this state follows from (default: the previous
    one); a restore passes the restored step's own parent, so undoing twice
    walks back through history instead of flipping between two states."""
    with _locked(doc_id) as folder:
        record = load(doc_id)
        if base_rev is not None and int(base_rev) != int(record["rev"]):
            return {"ok": False, "conflict": True, "rev": record["rev"], "doc": record}
        record["parent"] = int(record["rev"]) if parent is None else int(parent)
        record["rev"] = int(record["rev"]) + 1
        record["updated_at"] = time.time()
        record["source"] = source
        record["label"] = label or "edit"
        record["snap"] = _clean_snap(snap)
        if name:
            record["name"] = name
        _write_history(folder, record, thumb_png)
        _write_json(os.path.join(folder, "doc.json"), record)
    if make_current:
        _follow(doc_id, record["rev"], source)
    return {"ok": True, "rev": record["rev"], "doc": record}


def rename(doc_id: str, name: str) -> dict:
    with _locked(doc_id) as folder:
        record = load(doc_id)
        record["name"] = name or "Untitled"
        _write_json(os.path.join(folder, "doc.json"), record)
    return record


def _write_history(folder: str, record: dict, thumb_png: bytes | None) -> None:
    rev = int(record["rev"])
    entry = {"rev": rev, "parent": record.get("parent", rev - 1), "label": record.get("label", ""),
             "source": record.get("source", ""), "time": record.get("updated_at", time.time()), "snap": record["snap"]}
    _write_json(os.path.join(folder, "history", f"{rev:06d}.json"), entry)
    if thumb_png:
        _write_atomic(os.path.join(folder, "thumbs", f"{rev:06d}.png"), thumb_png)
        _write_atomic(os.path.join(folder, "thumb.png"), thumb_png)
    _prune(folder, rev)


def _prune(folder: str, rev: int) -> None:
    floor = rev - HISTORY_LIMIT
    if floor <= 0:
        return
    for sub, ext in (("history", ".json"), ("thumbs", ".png")):
        directory = os.path.join(folder, sub)
        for entry in os.listdir(directory) if os.path.isdir(directory) else []:
            stem = entry[: -len(ext)]
            if entry.endswith(ext) and stem.isdigit() and int(stem) <= floor:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(directory, entry))


def history(doc_id: str) -> list[dict]:
    """Every kept revision, oldest first, without the (large) snapshots."""
    folder = os.path.join(doc_dir(doc_id), "history")
    out = []
    for entry in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not entry.endswith(".json"):
            continue
        try:
            item = _read_json(os.path.join(folder, entry))
        except (OSError, ValueError):
            continue
        rev = item.get("rev")
        thumb = os.path.join(doc_dir(doc_id), "thumbs", f"{int(rev):06d}.png")
        out.append({"rev": rev, "parent": item.get("parent", int(rev) - 1), "label": item.get("label", ""), "source": item.get("source", ""),
                    "time": item.get("time"), "layers": len((item.get("snap") or {}).get("layers") or []),
                    "thumb": thumb if os.path.exists(thumb) else ""})
    return out


def history_entry(doc_id: str, rev: int) -> dict:
    path = os.path.join(doc_dir(doc_id), "history", f"{int(rev):06d}.json")
    try:
        return _read_json(path)
    except FileNotFoundError:
        raise DocumentError(f"Document {doc_id!r} has no revision {rev} (it may have been pruned)") from None


def snapshot_at(doc_id: str, rev: int) -> dict:
    return history_entry(doc_id, rev)["snap"]


def restore(doc_id: str, rev: int, source: str) -> dict:
    """Go back to `rev` by committing it again as the newest revision, so the
    steps after it stay in history and can be returned to."""
    entry = history_entry(doc_id, rev)
    thumb = os.path.join(doc_dir(doc_id), "thumbs", f"{int(rev):06d}.png")
    png = open(thumb, "rb").read() if os.path.exists(thumb) else None
    return commit(doc_id, entry["snap"], f"back to step {rev}", source, thumb_png=png,
                  parent=entry.get("parent", int(rev) - 1))


def undo(doc_id: str, source: str) -> dict | None:
    """Back to the state before the current one; None at the first step."""
    record = load(doc_id)
    parent = record.get("parent", int(record["rev"]) - 1)
    if not parent or parent < 1:
        return None
    return restore(doc_id, parent, source)


def list_documents(limit: int = 50) -> list[dict]:
    out = []
    for entry in os.listdir(DOCS_DIR) if os.path.isdir(DOCS_DIR) else []:
        path = os.path.join(DOCS_DIR, entry, "doc.json")
        if not _SAFE_ID.match(entry) or not os.path.exists(path):
            continue
        try:
            record = _read_json(path)
        except (OSError, ValueError):
            continue
        snap = record.get("snap") or {}
        thumb = os.path.join(DOCS_DIR, entry, "thumb.png")
        out.append({"id": record["id"], "name": record.get("name", "Untitled"), "rev": record.get("rev"),
                    "updated_at": record.get("updated_at"), "created_at": record.get("created_at"),
                    "width": snap.get("width"), "height": snap.get("height"),
                    "layers": len(snap.get("layers") or []),
                    "thumb": thumb if os.path.exists(thumb) else ""})
    out.sort(key=lambda d: d.get("updated_at") or 0, reverse=True)
    return out[: max(1, int(limit))]


def delete(doc_id: str) -> None:
    folder = doc_dir(doc_id)
    if not os.path.isdir(folder):
        raise DocumentError(f"No document {doc_id!r}")
    shutil.rmtree(folder)
    if get_current().get("id") == doc_id:
        with contextlib.suppress(OSError):
            os.remove(CURRENT_PATH)


def resolve(document: str = "") -> str:
    """A document id from an id, an exact name (newest wins) or "" (current)."""
    document = (document or "").strip()
    if not document:
        current = get_current().get("id")
        if current and os.path.exists(os.path.join(doc_dir(current), "doc.json")):
            return current
        raise DocumentError("No document is open. Create one with new_document or open_image first.")
    if _SAFE_ID.match(document) and os.path.exists(os.path.join(DOCS_DIR, document, "doc.json")):
        return document
    for item in list_documents(limit=10_000):
        if item["name"].lower() == document.lower():
            return item["id"]
    raise DocumentError(f"No document with id or name {document!r}")


# ---------------------------------------------------------------- saving files


def save_path(name: str, ext: str, folder: str = "") -> str:
    """A fresh path in the save folder; never overwrites an earlier save."""
    folder = os.path.expanduser(folder) if folder else SAVE_DIR
    os.makedirs(folder, exist_ok=True)
    stem = re.sub(r'[\\/:*?"<>|]+', "-", (name or "").strip()) or "design"
    path = os.path.join(folder, f"{stem}.{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} {n}.{ext}")
        n += 1
    return path


def decode_png(data_url: str) -> bytes:
    return _decode_png(data_url)


# ---------------------------------------------------------------- view settings


VIEW_PATH = os.path.join(DOCS_DIR, "view.json")
VIEW_KEYS = ("rulers", "grid", "gridSize", "snap", "guides", "printGuides", "unit")


def get_view() -> dict:
    """The editor's View settings (grid, snap, guides...), as the page last
    published them, plus `grid_step`: the grid spacing in page pixels at the
    page's current zoom (the "Auto" grid follows the zoom)."""
    try:
        value = _read_json(VIEW_PATH)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def set_view(changes: dict, source: str, base_seq: str | None = None) -> dict:
    """Merge `changes` in. With `base_seq` (the page), refuse to overwrite an
    agent change the page has not applied yet: the caller gets the current
    view back with `conflict` set, applies it, and publishes again."""
    view = get_view()
    if base_seq is not None and view.get("seq") and view.get("seq") != base_seq and view.get("source") == "agent":
        return dict(view, conflict=True)
    for key, value in (changes or {}).items():
        if key in VIEW_KEYS or key in ("grid_step", "grid_doc"):
            view[key] = value
    view.update(source=source, seq=f"{time.time_ns()}-{secrets.token_hex(2)}", time=time.time())
    _write_json(VIEW_PATH, view)
    return view


# ---------------------------------------------------------------- editor screenshots
#
# The agent asks, the open page answers: `request.json` names a document and
# a mode, the page renders it with Konva (real fonts, exactly what is on
# screen) and posts the PNG back, which lands in screens/<request id>.png.

REQUEST_PATH = os.path.join(DOCS_DIR, "request.json")
SCREENS_DIR = os.path.join(DOCS_DIR, "screens")
_SCREEN_KEEP = 30


def request_screenshot(doc_id: str, rev: int, mode: str, max_size: int) -> str:
    request_id = f"s{time.time_ns()}-{secrets.token_hex(2)}"
    _write_json(REQUEST_PATH, {"id": request_id, "kind": "screenshot", "doc": doc_id, "rev": int(rev),
                               "mode": mode, "max_size": int(max_size), "time": time.time()})
    return request_id


def save_screenshot(request_id: str, png: bytes, meta: dict) -> dict:
    if not _SAFE_ID.match(request_id or ""):
        raise DocumentError(f"Invalid request id: {request_id!r}")
    path = os.path.join(SCREENS_DIR, f"{request_id}.png")
    if png:
        _write_atomic(path, png)
    else:
        # The page could not draw it faithfully (images still decoding): an
        # empty `path` tells the agent to fall back to the Python render.
        path = ""
    info = dict(meta or {}, path=path, time=time.time())
    _write_json(os.path.join(SCREENS_DIR, f"{request_id}.json"), info)
    entries = sorted(e for e in os.listdir(SCREENS_DIR) if e.endswith(".json"))
    for old in entries[:-_SCREEN_KEEP]:
        for ext in (".json", ".png"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(SCREENS_DIR, old[:-5] + ext))
    return info


def screenshot_result(request_id: str) -> dict | None:
    try:
        return _read_json(os.path.join(SCREENS_DIR, f"{request_id}.json"))
    except (OSError, ValueError):
        return None
