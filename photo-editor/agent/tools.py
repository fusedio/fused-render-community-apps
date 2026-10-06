"""MCP tools that let a Claude session drive the Photo Editor.

Published by `fused app serve <this folder>` from `mcp.toml` beside this file,
one tool per function below. Every edit is a new revision of a document in
`../.fused/data/documents/` (see `docstore.py`): it is autosaved, kept in the
document's step history, and picked up by an open editor within a second, so
you can watch Claude draw and step back to any point.

Coordinates are page pixels with (0, 0) at the top-left of the page (bleed
included). Where a tool takes `x`/`y` they are the top-left corner of the
layer's box; leave them out and use `position` ("center", "top", "bottom",
"left", "right", "top-left", "top-right", "bottom-left", "bottom-right") to
place it inside the page's safe margin instead.

A document can have several pages, each with its own size and layers. Tools
act on the page the editor shows; go_to_page switches it, and add_page,
duplicate_page, delete_page, move_page and rename_page manage them.

This folder has its own `pyproject.toml` on purpose: the editor's own
dependencies include a vendored wheel that `fused app serve` cannot install,
and none of these tools needs a model.
"""

from __future__ import annotations

import math
import os
import secrets
import subprocess
import sys
import tempfile
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import docstore  # noqa: E402
import fonts  # noqa: E402
import imaging  # noqa: E402
import render  # noqa: E402

MM_PER_IN = 25.4

# Mirrors PRESETS / PRESET_CATS in index.html: id -> (w, h, unit, category).
PRESETS = {
    "a4": (210, 297, "mm", "paper"), "a5": (148, 210, "mm", "paper"), "a3": (297, 420, "mm", "paper"),
    "a6": (105, 148, "mm", "paper"), "b5": (176, 250, "mm", "paper"), "b4": (250, 353, "mm", "paper"),
    "letter": (8.5, 11, "in", "paper"), "legal": (8.5, 14, "in", "paper"), "tabloid": (11, 17, "in", "paper"),
    "halfletter": (5.5, 8.5, "in", "paper"),
    "p4x6": (4, 6, "in", "photo"), "p5x7": (5, 7, "in", "photo"), "p8x10": (8, 10, "in", "photo"),
    "p8x12": (8, 12, "in", "photo"), "p11x14": (11, 14, "in", "photo"), "p10x15": (100, 150, "mm", "photo"),
    "p13x18": (130, 180, "mm", "photo"), "p20x30": (200, 300, "mm", "photo"), "p30x40": (300, 400, "mm", "photo"),
    "sq20": (200, 200, "mm", "photo"), "passport": (35, 45, "mm", "photo"),
    "bcard": (85, 55, "mm", "cards"), "bcard-us": (3.5, 2, "in", "cards"), "postcard": (148, 105, "mm", "cards"),
    "postcard-us": (6, 4, "in", "cards"), "dl": (99, 210, "mm", "cards"), "a5flyer": (148, 210, "mm", "cards"),
    "sqcard": (148, 148, "mm", "cards"), "cd": (120, 120, "mm", "cards"),
    "a2": (420, 594, "mm", "posters"), "a1": (594, 841, "mm", "posters"), "a0": (841, 1189, "mm", "posters"),
    "p50x70": (500, 700, "mm", "posters"), "p18x24": (18, 24, "in", "posters"), "p24x36": (24, 36, "in", "posters"),
    "ig-post": (1080, 1080, "px", "screen"), "ig-portrait": (1080, 1350, "px", "screen"),
    "story": (1080, 1920, "px", "screen"), "fhd": (1920, 1080, "px", "screen"), "qhd": (2560, 1440, "px", "screen"),
    "uhd": (3840, 2160, "px", "screen"), "yt-thumb": (1280, 720, "px", "screen"), "fb-cover": (1640, 624, "px", "screen"),
    "x-header": (1500, 500, "px", "screen"), "li-banner": (1584, 396, "px", "screen"),
}
# category -> (dpi, bleed mm, safe mm)
CATEGORY_DEFAULTS = {"paper": (300, 3, 5), "photo": (300, 0, 3), "cards": (300, 3, 4),
                     "posters": (300, 3, 10), "screen": (72, 0, 0)}
FILTERS = ["vivid", "mono", "noir", "warm", "cool", "faded"]
ADJUSTMENTS = ["brightness", "contrast", "saturation", "temperature", "tint", "highlights",
               "shadows", "sharpness", "vignette", "blur"]
POSITIONS = ["center", "top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right"]


# ---------------------------------------------------------------- helpers


def _blank_adjust() -> dict:
    return {k: 0 for k in ADJUSTMENTS}


def _layer_id() -> str:
    return "A" + secrets.token_hex(3) + "-" + format(int(time.time() * 1000), "x")


def _bleed_px(snap: dict) -> int:
    return int(round((snap.get("bleed") or 0) * (snap.get("dpi") or 300) / MM_PER_IN))


def _trim(snap: dict) -> tuple[float, float, float, float]:
    b = _bleed_px(snap)
    return b, b, max(1, snap["width"] - 2 * b), max(1, snap["height"] - 2 * b)


def _margin(snap: dict) -> float:
    safe = (snap.get("safe") or 0) * (snap.get("dpi") or 300) / MM_PER_IN
    _, _, w, h = _trim(snap)
    return max(safe, min(w, h) * 0.05)


def _place(snap: dict, w: float, h: float, position: str, x, y) -> tuple[float, float]:
    """Top-left corner for a w x h box: explicit x/y win, else `position`."""
    tx, ty, tw, th = _trim(snap)
    m = _margin(snap)
    pos = (position or "center").lower().replace("_", "-").replace(" ", "-")
    if pos not in POSITIONS:
        raise ValueError(f"position must be one of {POSITIONS}, got {position!r}")
    horiz = "left" if "left" in pos else "right" if "right" in pos else "center"
    vert = "top" if "top" in pos else "bottom" if "bottom" in pos else "center"
    px = tx + m if horiz == "left" else tx + tw - m - w if horiz == "right" else tx + (tw - w) / 2
    py = ty + m if vert == "top" else ty + th - m - h if vert == "bottom" else ty + (th - h) / 2
    return _snap_box(snap, (float(x) if x is not None else px), (float(y) if y is not None else py), w, h)


# ---------------------------------------------------------------- snapping
#
# The same rules the editor applies to a dragged layer (snapLayer in
# index.html): with Snap on, a box's left/centre/right (top/middle/bottom)
# lands on the page and trim edges, the trim centre, the safe margin, the
# guides and -- when the grid is showing -- the grid lines, if one is close.


def _grid_step(snap: dict, view: dict, doc_id: str = "") -> float:
    if not view.get("grid"):
        return 0.0
    if view.get("grid_step") and (not doc_id or view.get("grid_doc") == doc_id):
        return float(view["grid_step"])
    dpi = snap.get("dpi") or 300
    size = str(view.get("gridSize") or "auto")
    import re

    match = re.fullmatch(r"([\d.]+)(mm|in|px)", size)
    if match:
        value, unit = float(match.group(1)), match.group(2)
        return value if unit == "px" else value * (MM_PER_IN if unit == "in" else 1) * dpi / MM_PER_IN
    return 100.0 if snap.get("sizing") == "pixels" else 10 * dpi / MM_PER_IN


def _snap_targets(snap: dict, axis: str, view: dict) -> list[float]:
    tx, ty, tw, th = _trim(snap)
    start, length = (tx, tw) if axis == "x" else (ty, th)
    full = snap["width"] if axis == "x" else snap["height"]
    out = [0, full, start, start + length, start + length / 2]
    safe = (snap.get("safe") or 0) * (snap.get("dpi") or 300) / MM_PER_IN
    if safe > 0:
        out += [start + safe, start + length - safe]
    if view.get("guides", True):
        out += [g["pos"] for g in snap.get("guides") or [] if g.get("axis") == axis]
    return out


def _snap_value(snap: dict, axis: str, values: list[float], view: dict) -> float:
    """The shift for a box whose [start, centre, end] on `axis` are `values`.

    Page/trim edges, the trim centre, the safe margin and guides win when one
    is close (the editor's rule, as a share of the page). Otherwise, with the
    grid showing, the start edge goes to the nearest grid line -- for the
    agent "snap to grid" means on the grid, not merely near it."""
    tol = max(snap["width"], snap["height"]) * 0.006
    best = None
    for value in values:
        for target in _snap_targets(snap, axis, view):
            d = target - value
            if abs(d) <= tol and (best is None or abs(d) < abs(best)):
                best = d
    if best is not None:
        return best
    step = _grid_step(snap, view, snap.get("_doc_id", ""))
    if step:
        origin = _trim(snap)[0 if axis == "x" else 1]
        return origin + round((values[0] - origin) / step) * step - values[0]
    return 0.0


def _snap_box(snap: dict, left: float, top: float, w: float, h: float) -> tuple[float, float]:
    view = docstore.get_view()
    if not view.get("snap", True):
        return left, top
    left += _snap_value(snap, "x", [left, left + w / 2, left + w], view)
    top += _snap_value(snap, "y", [top, top + h / 2, top + h], view)
    return left, top


def _snap_point(snap: dict, x: float, y: float) -> tuple[float, float]:
    view = docstore.get_view()
    if not view.get("snap", True):
        return x, y
    return x + _snap_value(snap, "x", [x], view), y + _snap_value(snap, "y", [y], view)


# ---------------------------------------------------------------- pages
#
# A document holds one or more pages (docstore.py, "pages"). _load hands a
# tool one page shaped like a whole single-page snapshot -- size, setup,
# guides, layers -- so every tool below edits it the way it always did, and
# _commit folds it back into the document as the page in view. Which page:
# `page` when the tool takes one, else the page the editor shows (it
# publishes that to view.json, as does go_to_page), else the one that was in
# view at the last save.


def _page_id() -> str:
    return "P" + secrets.token_hex(3) + "-" + format(int(time.time() * 1000), "x")


def _page_ref(pages: list[dict], ref) -> int:
    """Index of a page from its number (1 = first), id or name."""
    key = str(ref).strip()
    if key.isdigit():
        number = int(key)
        if 1 <= number <= len(pages):
            return number - 1
        raise ValueError(f"There is no page {number}; the document has {len(pages)} page(s)")
    for i, page in enumerate(pages):
        if page.get("id") == key:
            return i
    for i, page in enumerate(pages):
        if (page.get("name") or "").lower() == key.lower():
            return i
    raise ValueError(f"No page {ref!r}. Pages: " +
                     ", ".join(f"{i + 1}. {p.get('name')} ({p.get('id')})" for i, p in enumerate(pages)))


def _page_index(full: dict, doc_id: str, page=None) -> int:
    pages = full["pages"]
    if page not in (None, ""):
        return _page_ref(pages, page)
    view = docstore.get_view()
    if view.get("page_doc") == doc_id:
        for i, item in enumerate(pages):
            if item.get("id") == view.get("page_id"):
                return i
    return docstore.page_index(full)


def _show_page(doc_id: str, page_id: str) -> None:
    """Make `page_id` the page in view: the editor switches to it, and later
    tools that take no page act on it."""
    docstore.set_view({"page_doc": doc_id, "page_id": page_id}, "agent")


def _page_summary(page: dict, index: int) -> dict:
    return {"number": index + 1, "id": page.get("id"), "name": page.get("name"),
            "width": page.get("width"), "height": page.get("height"), "dpi": page.get("dpi"),
            "layers": len(page.get("layers") or [])}


def _load(document: str, page=None) -> tuple[str, dict]:
    doc_id = docstore.resolve(document)
    record = docstore.load(doc_id)
    full = docstore.as_pages(record["snap"])
    index = _page_index(full, doc_id, page)
    snap = dict(full["pages"][index])
    snap.update({k: v for k, v in full.items() if k not in ("pages", "page")})
    snap["_doc_id"] = doc_id  # for the grid-step lookup; stripped on commit
    snap["_doc"], snap["_page"] = full, index
    record["snap"] = snap
    return doc_id, record


def _fold(snap: dict) -> dict:
    """A page from _load back into its document, as the page in view. A
    whole document passes through unchanged."""
    full, index = snap.pop("_doc", None), snap.pop("_page", None)
    snap.pop("_doc_id", None)
    if full is None:
        return snap
    doc_keys = [k for k in full if k not in ("pages", "page")]
    pages = list(full["pages"])
    pages[index] = {k: v for k, v in snap.items() if k not in doc_keys}
    out = {k: snap.get(k, full[k]) for k in doc_keys}
    out.update(pages=pages, page=index)
    return out


def _commit(doc_id: str, snap: dict, label: str, extra: dict | None = None) -> dict:
    snap = _fold(snap)
    try:
        thumb = render.thumbnail_png(snap)
    except Exception as error:  # a thumbnail must never cost the edit
        print(f"[photo-editor] thumbnail failed: {error}")
        thumb = None
    result = docstore.commit(doc_id, snap, label, "agent", thumb_png=thumb)
    record = result["doc"]
    out = {"ok": True, "document": doc_id, "name": record["name"], "rev": record["rev"], "step": label}
    out.update(extra or {})
    return out


def _find_layer(snap: dict, layer: str) -> dict:
    layers = snap.get("layers") or []
    key = (layer or "").strip()
    if not key:
        if snap.get("activeId"):
            key = snap["activeId"]
        elif layers:
            return layers[-1]
        else:
            raise ValueError("The document has no layers")
    for item in layers:
        if item.get("id") == key:
            return item
    matches = [item for item in layers if (item.get("name") or "").lower() == key.lower()]
    if matches:
        return matches[-1]
    raise ValueError(f"No layer with id or name {layer!r}. Layers: " +
                     ", ".join(f"{l.get('name')} ({l.get('id')})" for l in layers))


def _default_name(snap: dict, kind: str) -> str:
    label = {"image": "Image", "rect": "Rectangle", "ellipse": "Ellipse", "line": "Line", "text": "Text"}[kind]
    return f"{label} {sum(1 for l in snap.get('layers') or [] if l.get('type') == kind) + 1}"


def _layer_summary(layer: dict) -> dict:
    a = layer.get("attrs") or {}
    out = {"id": layer.get("id"), "name": layer.get("name"), "type": layer.get("type"),
           "visible": layer.get("visible", True), "locked": layer.get("locked", False),
           "opacity": layer.get("opacity", 1), "blend": layer.get("blend", "normal"),
           "bounds": render.layer_bounds(layer), "rotation": a.get("rotation", 0)}
    kind = layer.get("type")
    if kind == "text":
        out.update(text=a.get("text"), font=a.get("fontFamily"), weight=a.get("fontStyle"),
                   font_size=a.get("fontSize"), color=a.get("fill"), align=a.get("align"))
    elif kind in ("rect", "ellipse"):
        out.update(fill=a.get("fill"), stroke=a.get("stroke"), stroke_width=a.get("strokeWidth"))
    elif kind == "line":
        out.update(color=a.get("stroke"), stroke_width=a.get("strokeWidth"), points=a.get("points"),
                   arrow_end=a.get("pointerAtEnding"), arrow_start=a.get("pointerAtBeginning"))
    elif kind == "image":
        out.update(pixels=[layer.get("naturalWidth"), layer.get("naturalHeight")], file=layer.get("workingPath"))
        content = _content_summary(layer)
        if content:
            out["content"] = content
    return out


def _ensure_png_readable(path: str) -> str:
    """HEIC and other formats Pillow cannot open go through macOS `sips`."""
    from PIL import Image

    try:
        with Image.open(path) as handle:
            handle.verify()
        return path
    except Exception:
        out = os.path.join(tempfile.mkdtemp(prefix="photo-editor-"), "import.png")
        subprocess.run(["sips", "-s", "format", "png", path, "--out", out],
                       check=True, capture_output=True, timeout=50)
        return out


def _import_image(path: str, name: str = "") -> dict:
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No image at {path}")
    readable = _ensure_png_readable(path)
    session = imaging.new_session_id()
    return imaging.save_source_path(session, readable, name=name or os.path.splitext(os.path.basename(path))[0])


def _image_layer(snap: dict, info: dict, name: str, scale: float, cx: float, cy: float) -> dict:
    return {"id": _layer_id(), "type": "image", "name": name or _default_name(snap, "image"),
            "visible": True, "locked": False, "opacity": 1, "blend": "normal",
            "attrs": {"x": cx, "y": cy, "rotation": 0, "scaleX": scale, "scaleY": scale, "opacity": 1},
            "sessionId": info["session_id"], "workingPath": info["path"],
            "naturalWidth": info["width"], "naturalHeight": info["height"],
            "adjust": _blank_adjust(), "filter": "none", "filterStrength": 100}


def _blank_snap(width: int, height: int, dpi: int, bleed: float, safe: float, sizing: str,
                preset_id: str, background: str, transparent: bool) -> dict:
    return {"width": int(width), "height": int(height), "background": background or "#ffffff",
            "transparent": bool(transparent), "activeId": None, "layers": [], "dpi": int(dpi),
            "bleed": bleed, "safe": safe, "sizing": sizing, "presetId": preset_id, "guides": [],
            "localSession": "doc" + secrets.token_hex(8)}


def _add_layer(doc_id: str, record: dict, layer: dict, label: str) -> dict:
    snap = record["snap"]
    snap.setdefault("layers", []).append(layer)
    snap["activeId"] = layer["id"]
    return _commit(doc_id, snap, label, {"layer": _layer_summary(layer)})


# ---------------------------------------------------------------- documents


def list_documents(limit: int = 20) -> dict:
    current = docstore.get_current().get("id")
    docs = docstore.list_documents(limit=limit)
    for item in docs:
        item["open"] = item["id"] == current
        item["updated"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("updated_at") or 0))
    return {"documents": docs, "current": current, "save_folder": docstore.SAVE_DIR}


def _sized_snap(preset: str, width: int, height: int, landscape: bool, dpi: int,
                background: str, transparent: bool) -> dict:
    """A blank page from a preset (with its category's DPI, bleed and safe
    margin) or a pixel size."""
    if preset:
        key = preset.lower().strip()
        if key not in PRESETS:
            raise ValueError(f"Unknown preset {preset!r}. Presets: {', '.join(PRESETS)}")
        w, h, unit, cat = PRESETS[key]
        cdpi, bleed, safe = CATEGORY_DEFAULTS[cat]
        dpi = int(dpi or cdpi)
        if unit == "px":
            tw, th, sizing = int(w), int(h), "pixels"
        else:
            mm = MM_PER_IN if unit == "in" else 1
            tw, th, sizing = round(w * mm * dpi / MM_PER_IN), round(h * mm * dpi / MM_PER_IN), "print"
        if w != h and (tw > th) != bool(landscape):
            tw, th = th, tw
    else:
        if not (width and height):
            raise ValueError("Give a preset, or width and height in pixels")
        tw, th, sizing, bleed, safe, key = int(width), int(height), "pixels", 0, 0, ""
        dpi = int(dpi or 300)
    b = round(bleed * dpi / MM_PER_IN)
    return _blank_snap(tw + 2 * b, th + 2 * b, dpi, bleed, safe, sizing, key if preset else "", background, transparent)


def new_document(name: str = "Untitled", preset: str = "", width: int = 0, height: int = 0,
                 landscape: bool = False, dpi: int = 0, background: str = "#ffffff",
                 transparent: bool = False) -> dict:
    snap = _sized_snap(preset, width, height, landscape, dpi, background, transparent)
    record = docstore.create(name or "Untitled", snap, source="agent", label="new document",
                             thumb_png=render.thumbnail_png(snap))
    return {"ok": True, "document": record["id"], "name": record["name"], "rev": 1,
            "width": snap["width"], "height": snap["height"], "dpi": snap["dpi"], "bleed_mm": snap["bleed"]}


def open_image(path: str, name: str = "") -> dict:
    """New document sized to the photo, with the photo as its first layer."""
    info = _import_image(path, name)
    snap = _blank_snap(info["width"], info["height"], 300, 0, 0, "pixels", "", "#ffffff", False)
    title = name or os.path.splitext(os.path.basename(path))[0]
    layer = _image_layer(snap, info, title, 1.0, info["width"] / 2, info["height"] / 2)
    snap["layers"].append(layer)
    snap["activeId"] = layer["id"]
    record = docstore.create(title, snap, source="agent", label="open " + os.path.basename(path),
                             thumb_png=render.thumbnail_png(snap))
    return {"ok": True, "document": record["id"], "name": title, "rev": 1,
            "width": info["width"], "height": info["height"], "layer": _layer_summary(layer)}


def open_document(document: str) -> dict:
    """Bring a document up in the editor (it switches to it)."""
    doc_id, record = _load(document)
    docstore.set_current(doc_id, record["rev"], "agent")
    return {"ok": True, "document": doc_id, "name": record["name"], "rev": record["rev"]}


def get_document(document: str = "", page=None) -> dict:
    doc_id, record = _load(document, page)
    snap = record["snap"]
    pages, index = snap["_doc"]["pages"], snap["_page"]
    tx, ty, tw, th = _trim(snap)
    return {"document": doc_id, "name": record["name"], "rev": record["rev"],
            "page": {"number": index + 1, "of": len(pages), "id": snap.get("id"), "name": snap.get("name"),
                     "width": snap["width"], "height": snap["height"], "dpi": snap.get("dpi"),
                     "bleed_mm": snap.get("bleed", 0), "safe_mm": snap.get("safe", 0),
                     "trim": {"x": tx, "y": ty, "width": tw, "height": th},
                     "background": None if snap.get("transparent") else snap.get("background")},
            "pages": [_page_summary(p, i) for i, p in enumerate(pages)],
            "selected": snap.get("activeId"),
            "layers": [_layer_summary(l) for l in snap.get("layers") or []],
            "note": "Layers (of this page) are listed bottom to top. Bounds are page pixels. "
                    "Tools act on the page the editor shows; go_to_page switches it."}


# ---------------------------------------------------------------- pages


def go_to_page(page, document: str = "") -> dict:
    """Show a page (number from 1, id or name) in the editor. Later tools that
    take no page act on it."""
    doc_id, record = _load(document, page)
    snap = record["snap"]
    if docstore.get_current().get("id") != doc_id:
        docstore.set_current(doc_id, record["rev"], "agent")
    _show_page(doc_id, snap["id"])
    return {"ok": True, "document": doc_id, "page": _page_summary(snap, snap["_page"])}


def _commit_pages(doc_id: str, full: dict, pages: list[dict], show: str, label: str,
                  extra: dict | None = None) -> dict:
    """Commit a new page list with page `show` (an id) in view."""
    index = next(i for i, p in enumerate(pages) if p.get("id") == show)
    out = _commit(doc_id, dict(full, pages=pages, page=index), label, extra)
    _show_page(doc_id, show)
    out["pages"] = [_page_summary(p, i) for i, p in enumerate(pages)]
    return out


def add_page(document: str = "", name: str = "", preset: str = "", width: int = 0, height: int = 0,
             landscape: bool = False, background: str = "", after=None) -> dict:
    """A blank page after the page in view (or after `after`), shown in the
    editor. It has that page's size and print setup unless a preset or
    width/height in pixels is given."""
    doc_id, record = _load(document, after)
    base = record["snap"]
    full, index = base["_doc"], base["_page"]
    if background and render.parse_color(background) is None:
        raise ValueError(f"Not a colour: {background!r}")
    if preset or (width and height):
        page = _sized_snap(preset, width, height, landscape, 0, background or "#ffffff", False)
        page.pop("localSession", None)
    else:
        page = {k: base.get(k) for k in ("width", "height", "dpi", "bleed", "safe", "sizing", "presetId")}
        page.update(background=background or base.get("background") or "#ffffff", transparent=False,
                    activeId=None, layers=[], guides=[])
    page.update(id=_page_id(), name=name or f"Page {len(full['pages']) + 1}")
    pages = list(full["pages"])
    pages.insert(index + 1, page)
    return _commit_pages(doc_id, full, pages, page["id"], f"add page {index + 2}",
                         {"page": _page_summary(page, index + 1)})


def duplicate_page(page=None, name: str = "", document: str = "") -> dict:
    """Copy a page (default: the one in view) with all its layers, right after
    it, and show the copy. Layers in the copy get new ids."""
    import copy

    doc_id, record = _load(document, page)
    source = record["snap"]
    full, index = source["_doc"], source["_page"]
    dup = copy.deepcopy(full["pages"][index])
    ids = {}
    for layer in dup.get("layers") or []:
        ids[layer.get("id")] = layer["id"] = _layer_id()
    dup["activeId"] = ids.get(dup.get("activeId"))
    dup.update(id=_page_id(), name=name or f"{dup.get('name') or 'Page'} copy")
    pages = list(full["pages"])
    pages.insert(index + 1, dup)
    return _commit_pages(doc_id, full, pages, dup["id"], f"duplicate page {index + 1}",
                         {"page": _page_summary(dup, index + 1)})


def delete_page(page, document: str = "") -> dict:
    """Delete a page and its layers (restore_version or undo brings it back).
    A document keeps at least one page."""
    doc_id, record = _load(document, page)
    snap = record["snap"]
    full, index = snap["_doc"], snap["_page"]
    if len(full["pages"]) < 2:
        raise ValueError("A document keeps at least one page")
    shown = full["pages"][_page_index(full, doc_id)]["id"]
    pages = [p for i, p in enumerate(full["pages"]) if i != index]
    if shown == snap["id"]:
        shown = pages[max(0, index - 1)]["id"]
    return _commit_pages(doc_id, full, pages, shown, f"delete page {index + 1}")


def move_page(page, to: int, document: str = "") -> dict:
    """Move a page to position `to` (1 = first)."""
    doc_id, record = _load(document, page)
    snap = record["snap"]
    full, index = snap["_doc"], snap["_page"]
    shown = full["pages"][_page_index(full, doc_id)]["id"]
    pages = list(full["pages"])
    moved = pages.pop(index)
    target = min(max(int(to), 1), len(pages) + 1) - 1
    pages.insert(target, moved)
    return _commit_pages(doc_id, full, pages, shown, f"move page {index + 1} to {target + 1}",
                         {"page": _page_summary(moved, target)})


def copy_layers(to_page, layers: list = None, from_page=None, document: str = "", move: bool = False) -> dict:
    """Copy layers onto another page exactly as they are -- position, size,
    rotation, style, stacking order -- on top of that page's layers, with new
    ids. `layers`: ids or names (a list, or one comma-separated string), "all",
    or empty for the selected layer; a text label's box comes along with it.
    `from_page` defaults to the page in view, `to_page` is a number (from 1),
    id or name. move=True takes them off the source page. Shows the target."""
    import copy

    doc_id, record = _load(document, from_page)
    source = record["snap"]
    full, si = source["_doc"], source["_page"]
    pages = list(full["pages"])
    ti = _page_ref(pages, to_page)
    if move and ti == si:
        raise ValueError("Those layers are already on that page")
    picked = _pick_layers(source, layers)
    for layer in list(picked):
        for box in _label_boxes(source, layer):
            if all(box is not p for p in picked):
                picked.append(box)
    order = source.get("layers") or []
    picked.sort(key=lambda l: next(i for i, x in enumerate(order) if x is l))
    copies = []
    for layer in picked:
        dup = copy.deepcopy(layer)
        dup["id"] = _layer_id()
        copies.append(dup)
    if move:
        gone = {id(l) for l in picked}
        kept = [l for l in order if id(l) not in gone]
        active = pages[si].get("activeId")
        if active in {l.get("id") for l in picked}:
            active = kept[-1]["id"] if kept else None
        pages[si] = dict(pages[si], layers=kept, activeId=active)
    target = pages[ti]
    pages[ti] = dict(target, layers=list(target.get("layers") or []) + copies, activeId=copies[-1]["id"])
    verb = "move" if move else "copy"
    label = f"{verb} {len(copies)} layer{'s' if len(copies) != 1 else ''} to page {ti + 1}"
    return _commit_pages(doc_id, full, pages, pages[ti]["id"], label,
                         {"to_page": _page_summary(pages[ti], ti), "layers": [_layer_summary(l) for l in copies]})


def rename_page(name: str, page=None, document: str = "") -> dict:
    """Rename a page (default: the one in view)."""
    doc_id, record = _load(document, page)
    snap = record["snap"]
    full, index = snap["_doc"], snap["_page"]
    shown = full["pages"][_page_index(full, doc_id)]["id"]
    pages = list(full["pages"])
    pages[index] = dict(pages[index], name=(name or "").strip() or f"Page {index + 1}")
    return _commit_pages(doc_id, full, pages, shown, "rename page",
                         {"page": _page_summary(pages[index], index)})


def rename_document(name: str, document: str = "") -> dict:
    doc_id = docstore.resolve(document)
    record = docstore.rename(doc_id, name)
    return {"ok": True, "document": doc_id, "name": record["name"]}


def set_page(document: str = "", background: str = "", transparent=None) -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    if background:
        if render.parse_color(background) is None:
            raise ValueError(f"Not a colour: {background!r}")
        snap["background"] = background
        snap["transparent"] = False
    if transparent is not None:
        snap["transparent"] = bool(transparent)
    return _commit(doc_id, snap, "page background")


# ---------------------------------------------------------------- adding layers


def add_image(path: str, document: str = "", position: str = "center", x=None, y=None,
              width: float = 0, height: float = 0, fit: str = "", name: str = "") -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    info = _import_image(path, name)
    tx, ty, tw, th = _trim(snap)
    nw, nh = info["width"], info["height"]
    if fit == "fill":
        k = max(snap["width"] / nw, snap["height"] / nh)
    elif fit == "fit":
        k = min(tw / nw, th / nh)
    elif width:
        k = float(width) / nw
    elif height:
        k = float(height) / nh
    else:
        k = min(1.0, tw * 0.8 / nw, th * 0.8 / nh)
    w, h = nw * k, nh * k
    if fit:
        left, top = (snap["width"] - w) / 2, (snap["height"] - h) / 2
    else:
        left, top = _place(snap, w, h, position, x, y)
    layer = _image_layer(snap, info, name or info.get("name") or os.path.splitext(os.path.basename(path))[0],
                         k, left + w / 2, top + h / 2)
    return _add_layer(doc_id, record, layer, "add image")


def add_text(text: str, document: str = "", position: str = "center", x=None, y=None,
             font_size: float = 64, color: str = "#ffffff", font: str = "Source Sans 3", weight: str = "600",
             italic: bool = False, align: str = "", width: float = 0, outline_color: str = "",
             outline_width: float = 0, line_height: float = 1.15, letter_spacing: float = 0,
             background_color: str = "", background_padding: float = 0, corner_radius: float = 12,
             rotation: float = 0, name: str = "") -> dict:
    font = _ensure_font(font)  # any built-in, installed or Google family (installed on first use)
    doc_id, record = _load(document)
    snap = record["snap"]
    tx, ty, tw, th = _trim(snap)
    m = _margin(snap)
    pos = (position or "center").lower()
    style = (str(weight or "400") + (" italic" if italic else "")).strip()
    attrs = {"text": text, "fontSize": float(font_size), "fontFamily": font or "Source Sans 3",
             "fontStyle": style, "fill": color or "#ffffff", "lineHeight": float(line_height),
             "letterSpacing": float(letter_spacing), "stroke": outline_color or None,
             "strokeWidth": float(outline_width or 0), "textDecoration": "", "padding": 4,
             "rotation": float(rotation), "scaleX": 1, "scaleY": 1}
    pad = float(background_padding or (font_size * 0.35 if background_color else 0))
    if width:
        attrs["width"] = float(width)
        attrs["align"] = align or "left"
    elif background_color or x is not None:
        # A label box hugs its text, so measure it.
        attrs["width"] = render.text_metrics(attrs)["width"]
        attrs["align"] = align or "center"
    else:
        # Span the page between the margins and let `align` do the placing:
        # the browser's font metrics then cannot knock the text off-centre.
        horiz = "left" if "left" in pos else "right" if "right" in pos else "center"
        attrs["width"] = tw - 2 * m
        attrs["align"] = align or horiz
    box = render.text_metrics(attrs)
    left, top = _place(snap, attrs["width"] + 2 * pad, box["height"] + 2 * pad, pos, x, y)
    attrs["x"], attrs["y"] = left + pad, top + pad
    added = []
    if background_color:
        label_name = name or text.split("\n")[0][:32] or _default_name(snap, "text")
        rect = {"id": _layer_id(), "type": "rect", "name": label_name + " box", "visible": True,
                "locked": False, "opacity": 1, "blend": "normal",
                "attrs": {"x": left, "y": top, "width": attrs["width"] + 2 * pad, "height": box["height"] + 2 * pad,
                          "fill": background_color, "stroke": "#ffffff", "strokeWidth": 0,
                          "cornerRadius": float(corner_radius), "rotation": float(rotation),
                          "scaleX": 1, "scaleY": 1, "opacity": 1}}
        snap.setdefault("layers", []).append(rect)
        added.append(_layer_summary(rect))
    layer = {"id": _layer_id(), "type": "text", "name": name or text.split("\n")[0][:32] or _default_name(snap, "text"),
             "visible": True, "locked": False, "opacity": 1, "blend": "normal", "attrs": attrs}
    snap.setdefault("layers", []).append(layer)
    snap["activeId"] = layer["id"]
    added.append(_layer_summary(layer))
    return _commit(doc_id, snap, "add text", {"layers_added": added})


def add_shape(shape: str = "rect", document: str = "", position: str = "center", x=None,
              y=None, width: float = 240, height: float = 160, fill: str = "#378ef0",
              stroke: str = "", stroke_width: float = 0, corner_radius: float = 0, rotation: float = 0,
              opacity: float = 1, name: str = "") -> dict:
    kind = (shape or "rect").lower()
    if kind in ("rectangle", "square", "box"):
        kind = "rect"
    if kind in ("circle", "oval"):
        kind = "ellipse"
    if kind not in ("rect", "ellipse"):
        raise ValueError("shape must be rect, square, ellipse or circle (use add_line for lines and arrows)")
    if shape.lower() in ("circle", "square"):
        height = width
    doc_id, record = _load(document)
    snap = record["snap"]
    w, h = float(width), float(height)
    left, top = _place(snap, w, h, position, x, y)
    attrs = {"fill": fill or None, "stroke": stroke or "#ffffff", "strokeWidth": float(stroke_width or 0),
             "rotation": float(rotation), "scaleX": 1, "scaleY": 1, "opacity": 1}
    if kind == "rect":
        attrs.update(x=left, y=top, width=w, height=h, cornerRadius=float(corner_radius))
    else:
        attrs.update(x=left + w / 2, y=top + h / 2, radiusX=w / 2, radiusY=h / 2)
    layer = {"id": _layer_id(), "type": kind, "name": name or _default_name(snap, kind), "visible": True,
             "locked": False, "opacity": float(opacity), "blend": "normal", "attrs": attrs}
    return _add_layer(doc_id, record, layer, f"add {shape}")


def add_line(x1: float, y1: float, x2: float, y2: float, document: str = "", color: str = "#ffffff",
             width: float = 4, arrow_end: bool = False, arrow_start: bool = False, dashed: bool = False,
             name: str = "") -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    x1, y1 = _snap_point(snap, float(x1), float(y1))
    x2, y2 = _snap_point(snap, float(x2), float(y2))
    head = max(12.0, float(width) * 4)
    attrs = {"x": float(x1), "y": float(y1), "points": [0, 0, float(x2) - float(x1), float(y2) - float(y1)],
             "stroke": color, "fill": color, "strokeWidth": float(width), "pointerAtEnding": bool(arrow_end),
             "pointerAtBeginning": bool(arrow_start), "dash": [12, 8] if dashed else [],
             "pointerLength": head, "pointerWidth": head * 0.9, "rotation": 0, "scaleX": 1, "scaleY": 1, "opacity": 1}
    layer = {"id": _layer_id(), "type": "line", "name": name or ("Arrow" if arrow_end or arrow_start else _default_name(snap, "line")),
             "visible": True, "locked": False, "opacity": 1, "blend": "normal", "attrs": attrs}
    return _add_layer(doc_id, record, layer, "add arrow" if arrow_end or arrow_start else "add line")


# ---------------------------------------------------------------- changing layers


def _label_boxes(snap: dict, target: dict) -> list[dict]:
    """The box add_text(background_color=...) put behind a text layer."""
    if target.get("type") != "text":
        return []
    name = (target.get("name") or "") + " box"
    return [l for l in snap.get("layers") or [] if l.get("type") == "rect" and l.get("name") == name]


def update_layer(layer: str = "", document: str = "", changes: dict = None) -> dict:
    """Change any of: name, visible, locked, opacity, blend, x, y (box top-left),
    position, width, height, rotation, text, color, fill, stroke, stroke_width,
    font, weight, font_size, align, corner_radius, line_height, letter_spacing."""
    changes = dict(changes or {})
    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    a = target.setdefault("attrs", {})
    kind = target["type"]
    for key in ("name", "visible", "locked", "opacity", "blend"):
        if key in changes:
            target[key] = changes.pop(key)
    if "font" in changes:
        changes["font"] = _ensure_font(str(changes["font"]))
    simple = {"rotation": "rotation", "text": "text", "font": "fontFamily", "font_size": "fontSize",
              "align": "align", "corner_radius": "cornerRadius", "line_height": "lineHeight",
              "letter_spacing": "letterSpacing", "stroke_width": "strokeWidth", "stroke": "stroke"}
    for key, attr in simple.items():
        if key in changes:
            a[attr] = changes.pop(key)
    if "weight" in changes:
        a["fontStyle"] = str(changes.pop("weight"))
    for key in ("color", "fill"):
        if key in changes:
            value = changes.pop(key)
            if kind == "line":
                a["stroke"] = a["fill"] = value
            else:
                a["fill"] = value
    if "width" in changes or "height" in changes:
        b = render.layer_bounds(target)
        w = float(changes.pop("width", b["width"]))
        h = float(changes.pop("height", b["height"]))
        if kind == "rect":
            a["width"], a["height"], a["scaleX"], a["scaleY"] = w, h, 1, 1
        elif kind == "ellipse":
            a["radiusX"], a["radiusY"], a["scaleX"], a["scaleY"] = w / 2, h / 2, 1, 1
        elif kind == "text":
            a["width"] = w
        elif kind == "image":
            k = w / (target.get("naturalWidth") or 1)
            a["scaleX"] = a["scaleY"] = k
    if "x" in changes or "y" in changes or "position" in changes:
        b = render.layer_bounds(target)
        if "position" in changes:
            left, top = _place(snap, b["width"], b["height"], changes.pop("position"),
                               changes.pop("x", None), changes.pop("y", None))
        else:
            left, top = float(changes.pop("x", b["x"])), float(changes.pop("y", b["y"]))
        dx, dy = left - b["x"], top - b["y"]
        for item in [target] + _label_boxes(snap, target):
            item["attrs"]["x"] = (item["attrs"].get("x") or 0) + dx
            item["attrs"]["y"] = (item["attrs"].get("y") or 0) + dy
    if changes:
        raise ValueError(f"Unknown change(s): {', '.join(changes)}")
    snap["activeId"] = target["id"]
    return _commit(doc_id, snap, "edit " + (target.get("name") or kind), {"layer": _layer_summary(target)})


def delete_layer(layer: str, document: str = "") -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    gone = [target] + _label_boxes(snap, target)
    snap["layers"] = [l for l in snap["layers"] if all(l is not g for g in gone)]
    if snap.get("activeId") == target["id"]:
        snap["activeId"] = snap["layers"][-1]["id"] if snap["layers"] else None
    return _commit(doc_id, snap, "delete " + (target.get("name") or target["type"]))


def arrange_layer(layer: str, to: str = "front", document: str = "") -> dict:
    """to: front, back, forward, backward."""
    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    layers = snap["layers"]
    index = layers.index(target)
    layers.pop(index)
    step = {"front": len(layers), "back": 0, "forward": min(len(layers), index + 1), "backward": max(0, index - 1)}
    if to not in step:
        raise ValueError("to must be front, back, forward or backward")
    layers.insert(step[to], target)
    return _commit(doc_id, snap, f"move {target.get('name')} {to}")


def _swap_pixels(doc_id: str, snap: dict, target: dict, result_path: str, label: str) -> dict:
    from PIL import Image

    with Image.open(result_path) as handle:
        w, h = handle.size
    # Keep the on-page size when the pixel count changes (crop keeps scale).
    target["workingPath"] = result_path
    target["naturalWidth"], target["naturalHeight"] = w, h
    snap["activeId"] = target["id"]
    return _commit(doc_id, snap, label, {"layer": _layer_summary(target)})


def adjust_image(layer: str = "", document: str = "", brightness: float = 0, contrast: float = 0,
                 saturation: float = 0, temperature: float = 0, tint: float = 0, highlights: float = 0,
                 shadows: float = 0, sharpness: float = 0, vignette: float = 0, blur: float = 0) -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    if target["type"] != "image":
        raise ValueError(f"{target.get('name')} is a {target['type']} layer; adjustments need an image layer")
    values = {"brightness": brightness, "contrast": contrast, "saturation": saturation,
              "temperature": temperature, "tint": tint, "highlights": highlights, "shadows": shadows,
              "sharpness": sharpness, "vignette": vignette, "blur": blur}
    folder = imaging.session_dir(target["sessionId"], create=True)
    output = os.path.join(folder, f"adjusted-{int(time.time() * 1000)}.png")
    imaging.apply_adjustments(imaging.resolve_source(target["sessionId"], target["workingPath"]), output, values)
    changed = ", ".join(f"{k} {v:+g}" for k, v in values.items() if v)
    return _swap_pixels(doc_id, snap, target, output, "adjust " + (changed or "(no change)"))


def apply_filter(filter: str, layer: str = "", document: str = "", strength: float = 100) -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    if target["type"] != "image":
        raise ValueError("Filters need an image layer")
    if filter not in FILTERS:
        raise ValueError(f"filter must be one of {FILTERS}")
    folder = imaging.session_dir(target["sessionId"], create=True)
    output = os.path.join(folder, f"filtered-{int(time.time() * 1000)}.png")
    imaging.apply_filter_preset(imaging.resolve_source(target["sessionId"], target["workingPath"]),
                                output, filter, float(strength))
    return _swap_pixels(doc_id, snap, target, output, f"filter {filter} {strength:g}%")


def remove_background(layer: str = "", document: str = "") -> dict:
    import bgremove

    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    if target["type"] != "image":
        raise ValueError("Background removal needs an image layer")
    folder = imaging.session_dir(target["sessionId"], create=True)
    output = os.path.join(folder, f"cutout-{int(time.time() * 1000)}.png")
    try:
        bgremove.remove_background(imaging.resolve_source(target["sessionId"], target["workingPath"]), output)
    except bgremove.BackgroundRemovalError as error:
        return {"ok": False, "message": str(error)}
    return _swap_pixels(doc_id, snap, target, output, "remove background")


def crop_image(left: float, top: float, width: float, height: float, layer: str = "", document: str = "") -> dict:
    """Crop an image layer to a box given in that image's own pixels."""
    from PIL import Image

    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    if target["type"] != "image":
        raise ValueError("Crop needs an image layer")
    source = imaging.resolve_source(target["sessionId"], target["workingPath"])
    with Image.open(source) as handle:
        box = (max(0, int(left)), max(0, int(top)), min(handle.width, int(left + width)), min(handle.height, int(top + height)))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("The crop box is outside the image")
        cropped = handle.crop(box)
        nw, nh = handle.size
    output = os.path.join(imaging.session_dir(target["sessionId"], create=True), f"frame-{int(time.time() * 1000)}.png")
    cropped.save(output)
    # Keep the kept pixels where they were on the page.
    a = target["attrs"]
    import math

    sx, sy, r = a.get("scaleX", 1), a.get("scaleY", 1), math.radians(a.get("rotation", 0))
    dx = ((box[0] + box[2]) / 2 - nw / 2) * sx
    dy = ((box[1] + box[3]) / 2 - nh / 2) * sy
    a["x"] = a.get("x", 0) + dx * math.cos(r) - dy * math.sin(r)
    a["y"] = a.get("y", 0) + dx * math.sin(r) + dy * math.cos(r)
    return _swap_pixels(doc_id, snap, target, output, "crop")


# ---------------------------------------------------------------- panning content
#
# pan_image_content moves the picture inside an image layer's box. The box
# (attrs, naturalWidth/Height) is untouched; a new working file the same size
# is baked from the layer's untouched source with the picture shifted and
# zoomed, so what leaves the box is clipped and the rest is transparent. The
# layer keeps a `content` record -- source file, offset in image pixels, zoom,
# and the file it baked -- so pans accumulate and are always re-baked from the
# source: panning back restores the clipped pixels. Any other pixel edit
# (adjust, filter, crop, cutout, the editor's own tools) changes workingPath,
# which makes that record stale and the new pixels the source from then on.
# Baking (rather than a Konva crop window) keeps every other tool -- masks,
# smart select, the crop overlay, upscale, render.py -- working in plain
# source pixels, and the editor and the Pillow render stay identical for free.


def _content_state(layer: dict) -> dict:
    content = layer.get("content") or {}
    if content.get("baked") and content["baked"] == layer.get("workingPath") and os.path.isfile(content.get("source") or ""):
        return dict(content)
    return {"source": layer.get("workingPath"), "x": 0.0, "y": 0.0, "zoom": 1.0}


def _content_summary(layer: dict) -> dict | None:
    """The live pan/zoom of an image layer's content, in image and page pixels."""
    content = layer.get("content") or {}
    if not content.get("baked") or content["baked"] != layer.get("workingPath"):
        return None
    import math

    a = layer.get("attrs") or {}
    sx, sy, r = a.get("scaleX", 1) or 1, a.get("scaleY", 1) or 1, math.radians(a.get("rotation", 0) or 0)
    ix, iy = float(content.get("x", 0)), float(content.get("y", 0))
    px, py = ix * sx, iy * sy
    return {"offset_px": [round(ix, 1), round(iy, 1)], "zoom": round(float(content.get("zoom", 1)), 4),
            "offset_page": [round(px * math.cos(r) - py * math.sin(r), 1), round(px * math.sin(r) + py * math.cos(r), 1)],
            "source": content.get("source")}


def pan_image_content(dx: float = 0, dy: float = 0, zoom: float = 1, anchor: str = "center",
                      layer: str = "", document: str = "", reset: bool = False) -> dict:
    """Move (and zoom) the picture inside an image layer without moving the
    layer's box. dx/dy are page pixels (positive = the picture moves right /
    down on the page, whatever the layer's rotation or scale); zoom scales the
    picture about `anchor`, a point of the box (center, top, bottom, left,
    right, top-left, ...). What leaves the box is clipped and the uncovered
    part is transparent. Pans add up and are baked from the untouched source
    each time, so panning back brings clipped pixels back; reset restores it."""
    import math

    from PIL import Image

    doc_id, record = _load(document)
    snap = record["snap"]
    target = _find_layer(snap, layer)
    if target["type"] != "image":
        raise ValueError(f"{target.get('name')} is a {target['type']} layer; panning content needs an image layer")
    state = _content_state(target)
    source = imaging.resolve_source(target["sessionId"], state["source"])
    a = target["attrs"]
    sx, sy, r = a.get("scaleX", 1) or 1, a.get("scaleY", 1) or 1, math.radians(a.get("rotation", 0) or 0)
    with Image.open(source) as handle:
        picture = handle.convert("RGBA")
    nw, nh = picture.size
    if reset:
        x = y = 0.0
        z = 1.0
    else:
        factor = float(zoom or 1)
        if factor <= 0:
            raise ValueError("zoom must be greater than 0")
        pos = (anchor or "center").lower().replace("_", "-").replace(" ", "-")
        if pos not in POSITIONS:
            raise ValueError(f"anchor must be one of {POSITIONS}, got {anchor!r}")
        ax = 0 if "left" in pos else nw if "right" in pos else nw / 2
        ay = 0 if "top" in pos else nh if "bottom" in pos else nh / 2
        # Page pixels -> image pixels: undo the layer's rotation, then its scale.
        ix = (float(dx) * math.cos(r) + float(dy) * math.sin(r)) / sx
        iy = (-float(dx) * math.sin(r) + float(dy) * math.cos(r)) / sy
        z = float(state["zoom"]) * factor
        x = ax + (float(state["x"]) - ax) * factor + ix
        y = ay + (float(state["y"]) - ay) * factor + iy
    if abs(x) < 1e-6 and abs(y) < 1e-6 and abs(z - 1) < 1e-9:
        # Back where it started: the untouched source is the picture again.
        target.pop("content", None)
        return _swap_pixels(doc_id, snap, target, state["source"], "reset content")
    zw, zh = max(1, int(round(nw * z))), max(1, int(round(nh * z)))
    zoomed = picture if (zw, zh) == (nw, nh) else picture.resize((zw, zh), Image.LANCZOS)
    if abs(x - round(x)) < 1e-6 and abs(y - round(y)) < 1e-6:
        baked = Image.new("RGBA", (nw, nh), (0, 0, 0, 0))
        baked.paste(zoomed, (int(round(x)), int(round(y))))
    else:
        baked = zoomed.transform((nw, nh), Image.AFFINE, (1, 0, -x, 0, 1, -y), resample=Image.BICUBIC)
    output = os.path.join(imaging.session_dir(target["sessionId"], create=True), f"panned-{int(time.time() * 1000)}.png")
    baked.save(output)
    target["content"] = {"source": state["source"], "x": x, "y": y, "zoom": z, "baked": output}
    parts = []
    if dx or dy:
        parts.append(f"pan content {float(dx):+g}, {float(dy):+g}")
    if not reset and abs(float(zoom or 1) - 1) > 1e-9:
        parts.append(f"zoom content x{float(zoom):g}")
    return _swap_pixels(doc_id, snap, target, output, " / ".join(parts) or "pan content")


# ---------------------------------------------------------------- looking and saving


def render_document(document: str = "", max_size: int = 1600, rev: int = 0, page=None) -> dict:
    """Render a page (default: the one in view) to a PNG and return its path,
    so it can be looked at."""
    doc_id, record = _load(document, page)
    snap, number = record["snap"], record["snap"]["_page"] + 1
    if rev:
        old = docstore.snapshot_at(doc_id, rev)
        index = docstore.page_index(old) if page in (None, "") else _page_ref(docstore.pages_of(old), page)
        snap, number = docstore.page_at(old, index), index + 1
    longest = max(snap["width"], snap["height"])
    image = render.render(snap, scale=min(1.0, float(max_size) / longest))
    folder = os.path.join(tempfile.gettempdir(), "photo-editor-renders")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{doc_id}-r{rev or record['rev']}-p{number}.png")
    image.save(path)
    return {"path": path, "width": image.width, "height": image.height, "rev": rev or record["rev"],
            "page": number,
            "note": "Text uses the system copy of each font, so it can differ slightly from the editor."}


def save_document(document: str = "", format: str = "png", file_name: str = "", folder: str = "",
                  scale: float = 1, quality: int = 92, pages: str = "") -> dict:
    """Write the finished file into the save folder (~/Pictures/Photo Editor).
    `pages`: "all", "current" (the page in view) or one page (number, id or
    name). By default a PDF holds every page and other formats the page in
    view; "all" in another format writes one file per page."""
    doc_id, record = _load(document)
    snap = record["snap"]
    full = snap["_doc"]
    fmt = (format or "png").lower()
    ext = {"jpeg": "jpg", "tiff": "tif"}.get(fmt, fmt)
    if ext not in ("png", "jpg", "webp", "pdf", "tif"):
        raise ValueError("format must be png, jpeg, webp, pdf or tiff")
    which = (str(pages).strip().lower() if pages not in (None, "") else ("all" if ext == "pdf" else "current"))
    if which == "all":
        indexes = list(range(len(full["pages"])))
    elif which == "current":
        indexes = [snap["_page"]]
    else:
        indexes = [_page_ref(full["pages"], pages)]
    name = file_name or record["name"]
    out = []
    with tempfile.TemporaryDirectory() as scratch:
        flats = []
        for index in indexes:
            page = full["pages"][index]
            image = render.render(page, scale=float(scale))
            flat = os.path.join(scratch, f"page-{index + 1}.png")
            image.save(flat)
            flats.append((index, page, flat, image.size))
        first = flats[0][1]
        if ext == "pdf" and len(flats) > 1:
            path = docstore.save_path(name, ext, folder)
            imaging.export_pdf_pages([f for _, _, f, _ in flats], path, background="#ffffff",
                                     dpi=float(first.get("dpi") or 0) / float(scale or 1))
            out.append({"path": path, "pages": [i + 1 for i, _, _, _ in flats]})
        else:
            for index, page, flat, size in flats:
                stem = name if len(flats) == 1 else f"{name} – page {index + 1}"
                path = docstore.save_path(stem, ext, folder)
                imaging.export_image(flat, path, fmt=fmt, quality=int(quality),
                                     background=page.get("background") or "#ffffff",
                                     dpi=float(page.get("dpi") or 0) / float(scale or 1))
                out.append({"path": path, "page": index + 1, "width": size[0], "height": size[1]})
    if len(out) == 1:
        return {"ok": True, **out[0], "format": ext}
    return {"ok": True, "files": out, "format": ext}


def list_history(document: str = "") -> dict:
    doc_id, record = _load(document)
    steps = docstore.history(doc_id)
    for step in steps:
        step["when"] = time.strftime("%H:%M:%S", time.localtime(step.get("time") or 0))
    return {"document": doc_id, "rev": record["rev"], "steps": steps}


def restore_version(rev: int, document: str = "") -> dict:
    """Go back to step `rev`. Later steps stay in history."""
    doc_id = docstore.resolve(document)
    result = docstore.restore(doc_id, int(rev), "agent")
    return {"ok": True, "document": doc_id, "rev": result["rev"], "restored": int(rev)}


def undo(document: str = "") -> dict:
    """Step back to the state before the current one. It is recorded as a new
    step, so restore_version can return to anything that was undone."""
    doc_id = docstore.resolve(document)
    result = docstore.undo(doc_id, "agent")
    if result is None:
        return {"ok": False, "message": "Nothing to undo"}
    return {"ok": True, "document": doc_id, "rev": result["rev"], "step": result["doc"]["label"]}


# ---------------------------------------------------------------- view, guides, alignment


def get_view() -> dict:
    """The editor's View settings and the grid spacing in page pixels."""
    return docstore.get_view()


def set_view(grid=None, snap=None, rulers=None, guides=None, print_guides=None,
             grid_size: str = "", unit: str = "") -> dict:
    """Turn the editor's grid / snap / rulers / guides / bleed-trim overlay on
    or off, and set the grid spacing ("auto", "5mm", "10mm", "25mm", "0.25in",
    "0.5in", "1in", "100px") or ruler unit (mm, cm, in, px). An open editor
    applies it within a second; tools that place layers then snap to it."""
    changes = {}
    for key, value in (("grid", grid), ("snap", snap), ("rulers", rulers), ("guides", guides),
                       ("printGuides", print_guides)):
        if value is not None:
            changes[key] = bool(value)
    if grid_size:
        if grid_size not in ("auto", "5mm", "10mm", "25mm", "0.25in", "0.5in", "1in", "100px"):
            raise ValueError("grid_size must be auto, 5mm, 10mm, 25mm, 0.25in, 0.5in, 1in or 100px")
        changes["gridSize"] = grid_size
        changes["grid"] = True if grid is None else changes["grid"]
    if unit:
        if unit not in ("mm", "cm", "in", "px"):
            raise ValueError("unit must be mm, cm, in or px")
        changes["unit"] = unit
    if "gridSize" in changes:
        changes["grid_step"] = 0  # the page republishes the real step
    view = docstore.set_view(changes, "agent")
    return {"ok": True, "view": {k: view.get(k) for k in docstore.VIEW_KEYS}}


def add_guide(axis: str, position: float, document: str = "") -> dict:
    """A ruler guide. axis 'x' / 'vertical' is a vertical line at x = position;
    'y' / 'horizontal' a horizontal line at y = position (page pixels)."""
    axis = {"vertical": "x", "horizontal": "y"}.get(axis, axis)
    if axis not in ("x", "y"):
        raise ValueError("axis must be x (vertical) or y (horizontal)")
    doc_id, record = _load(document)
    snap = record["snap"]
    snap.setdefault("guides", []).append({"axis": axis, "pos": float(position)})
    return _commit(doc_id, snap, f"add {'vertical' if axis == 'x' else 'horizontal'} guide")


def clear_guides(document: str = "") -> dict:
    doc_id, record = _load(document)
    snap = record["snap"]
    snap["guides"] = []
    return _commit(doc_id, snap, "clear guides")


def _unit_bounds(snap: dict, layer: dict) -> dict:
    """A layer's box, including the label box behind a text label."""
    boxes = [render.layer_bounds(l) for l in [layer] + _label_boxes(snap, layer)]
    x0 = min(b["x"] for b in boxes)
    y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["width"] for b in boxes)
    y1 = max(b["y"] + b["height"] for b in boxes)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _move(snap: dict, layer: dict, dx: float, dy: float) -> None:
    for item in [layer] + _label_boxes(snap, layer):
        item["attrs"]["x"] = (item["attrs"].get("x") or 0) + dx
        item["attrs"]["y"] = (item["attrs"].get("y") or 0) + dy


def _pick_layers(snap: dict, layers) -> list[dict]:
    if isinstance(layers, str):
        layers = [p.strip() for p in layers.split(",") if p.strip()]
    if not layers:
        return [_find_layer(snap, "")]
    if len(layers) == 1 and str(layers[0]).lower() == "all":
        boxes = {id(b) for l in snap.get("layers") or [] for b in _label_boxes(snap, l)}
        return [l for l in snap.get("layers") or [] if id(l) not in boxes and not l.get("locked")]
    picked = []
    for key in layers:
        layer = _find_layer(snap, str(key))
        if all(layer is not p for p in picked):
            picked.append(layer)
    return picked


def align_layers(layers: list = None, align: str = "center", relative_to: str = "auto",
                 document: str = "") -> dict:
    """Line layers up. layers: ids or names, ["all"], or omit for the selected
    one. align: left, center, right, top, middle, bottom. relative_to: page
    (the trim box), selection (the box around the layers), a layer id/name
    to align to, or auto (page for one layer, selection for several)."""
    doc_id, record = _load(document)
    snap = record["snap"]
    picked = _pick_layers(snap, layers)
    align = {"centre": "center", "hcenter": "center", "vcenter": "middle", "centre-vertical": "middle"}.get(align, align)
    if align not in ("left", "center", "right", "top", "middle", "bottom"):
        raise ValueError("align must be left, center, right, top, middle or bottom")
    ref = (relative_to or "auto").lower()
    if ref == "auto":
        ref = "page" if len(picked) == 1 else "selection"
    if ref == "page":
        tx, ty, tw, th = _trim(snap)
        box = {"x": tx, "y": ty, "width": tw, "height": th}
    elif ref == "selection":
        boxes = [_unit_bounds(snap, l) for l in picked]
        x0, y0 = min(b["x"] for b in boxes), min(b["y"] for b in boxes)
        box = {"x": x0, "y": y0, "width": max(b["x"] + b["width"] for b in boxes) - x0,
               "height": max(b["y"] + b["height"] for b in boxes) - y0}
    else:
        anchor = _find_layer(snap, relative_to)
        box = _unit_bounds(snap, anchor)
        picked = [l for l in picked if l is not anchor]
    for layer in picked:
        b = _unit_bounds(snap, layer)
        dx = dy = 0.0
        if align == "left":
            dx = box["x"] - b["x"]
        elif align == "center":
            dx = box["x"] + box["width"] / 2 - (b["x"] + b["width"] / 2)
        elif align == "right":
            dx = box["x"] + box["width"] - (b["x"] + b["width"])
        elif align == "top":
            dy = box["y"] - b["y"]
        elif align == "middle":
            dy = box["y"] + box["height"] / 2 - (b["y"] + b["height"] / 2)
        else:
            dy = box["y"] + box["height"] - (b["y"] + b["height"])
        _move(snap, layer, dx, dy)
    return _commit(doc_id, snap, f"align {align} ({ref})",
                   {"layers": [_layer_summary(l) for l in picked]})


def distribute_layers(layers: list = None, direction: str = "horizontal", gap=None,
                      document: str = "") -> dict:
    """Space layers evenly. direction: horizontal (left to right) or vertical.
    Without gap the outermost layers stay put and the space between the rest
    is equalised; with gap (page pixels) they are packed that far apart,
    starting from the first. layers: ids/names or ["all"]; needs at least 2."""
    doc_id, record = _load(document)
    snap = record["snap"]
    picked = _pick_layers(snap, layers or ["all"])
    if len(picked) < 2:
        raise ValueError("Distribute needs at least two layers")
    horizontal = direction.lower().startswith("h")
    pos, size = ("x", "width") if horizontal else ("y", "height")
    items = sorted(((l, _unit_bounds(snap, l)) for l in picked), key=lambda p: p[1][pos])
    if gap is None:
        span = items[-1][1][pos] + items[-1][1][size] - items[0][1][pos]
        free = span - sum(b[size] for _, b in items)
        spacing = free / (len(items) - 1)
    else:
        spacing = float(gap)
    cursor = items[0][1][pos]
    for layer, b in items:
        shift = cursor - b[pos]
        _move(snap, layer, shift if horizontal else 0, 0 if horizontal else shift)
        cursor += b[size] + spacing
    return _commit(doc_id, snap, f"distribute {'horizontally' if horizontal else 'vertically'}",
                   {"layers": [_layer_summary(l) for l, _ in items], "spacing": round(spacing, 1)})


def screenshot_editor(document: str = "", mode: str = "page", max_size: int = 1600,
                      wait_seconds: float = 12, page=None) -> dict:
    """Capture what the editor shows, to check placement. mode 'page': the
    page alone, drawn by the editor itself (real fonts, blend modes, live
    adjustments). mode 'editor': the canvas area as it looks on screen --
    grid, guides, rulers, the selection box and handles included. Opens the
    document (and `page`, default the one in view) in the editor first if
    another one is showing. With no editor open it falls back to
    render_document and says so."""
    if mode not in ("page", "editor"):
        raise ValueError("mode must be page or editor")
    doc_id, record = _load(document, page)
    snap = record["snap"]
    if docstore.get_current().get("id") != doc_id:
        docstore.set_current(doc_id, record["rev"], "agent")
    if page not in (None, ""):
        _show_page(doc_id, snap["id"])
    request_id = docstore.request_screenshot(doc_id, record["rev"], mode, max_size, page_id=snap["id"])
    deadline = time.time() + max(1.0, float(wait_seconds))
    result = None
    while time.time() < deadline:
        result = docstore.screenshot_result(request_id)
        if result and not result.get("path"):
            break  # the page answered but could not draw it (images not loaded)
        if result:
            return {"ok": True, "source": "editor", "path": result["path"], "mode": mode,
                    "width": result.get("width"), "height": result.get("height"),
                    "rev": result.get("rev"), "zoom": result.get("zoom"), "page": snap["_page"] + 1,
                    "note": "Open the PNG at `path` to look at it." +
                            (" Page pixels = screenshot pixels / scale." if mode == "page" else
                             " This is the visible canvas area at the editor's current zoom.")
                            + f" scale={result.get('scale')}"}
        time.sleep(0.25)
    fallback = render_document(doc_id, max_size=max_size, page=snap["_page"] + 1)
    why = (f"The editor could not draw it ({result.get('reason') or 'images not loaded'})" if result else
           "No open editor answered within " + f"{wait_seconds:g}s")
    fallback.update(ok=True, source="python-render",
                    note=why + ", so this is the Python render (system fonts, no grid or selection). "
                         "Open the Photo Editor page to get real editor screenshots.")
    return fallback


# ---------------------------------------------------------------- fonts
#
# Text may use any Google Fonts family (all OFL/Apache), not only the twelve
# the page ships with. search_fonts reads the catalogue, install_font puts a
# family's TTFs in .fused/data/fonts/<Family>/ (fonts.py), render.py reads
# them from there, and the page builds @font-face rules from installed.json
# via fused.rawUrl, so Pillow and Konva draw from the very same file.


def _ensure_font(family: str) -> str:
    """The canonical name of a font a text layer may use. Built-in and
    installed families pass as they are; a Google family is installed first;
    any other name is accepted if this Mac has it, else refused with a hint."""
    name = (family or "").strip()
    if not name:
        return "Source Sans 3"
    known = fonts.canonical(name)
    if known:
        return known
    entry = None
    try:
        entry = fonts.lookup(name)
    except fonts.FontError as error:
        print(f"[photo-editor] font catalogue unavailable: {error}")
    if entry:
        try:
            fonts.install(entry["family"])
        except fonts.FontError as error:
            print(f"[photo-editor] could not install {entry['family']}: {error}")
        return entry["family"]
    if render._font_faces(name):
        return name  # a font this Mac has
    raise ValueError(f"Unknown font {family!r}. search_fonts finds Google fonts (install_font fetches one); "
                     "list_fonts shows what is already available.")


def search_fonts(query: str = "", category: str = "", limit: int = 20) -> dict:
    """Search the Google Fonts catalogue by name words and/or category
    (sans-serif, serif, display, handwriting, monospace), most popular first."""
    try:
        results = fonts.search(query, category, limit)
    except fonts.FontError as error:
        raise ValueError(str(error))
    return {"fonts": [{"family": f["family"], "category": f["category"], "weights": f["weights"],
                       "italics": f["italics"], "installed": f["installed"]} for f in results],
            "count": len(results), "categories": fonts.CATEGORIES,
            "note": "add_text(font=<family>) installs a Google family on first use; install_font does it explicitly."}


def install_font(family: str) -> dict:
    """Download a Google family's TTFs into the app's font folder (idempotent)."""
    try:
        manifest = fonts.install(family)
    except fonts.FontError as error:
        raise ValueError(str(error))
    return {"ok": True, "family": manifest["family"], "category": manifest.get("category"),
            "folder": manifest["folder"], "already_installed": manifest["already_installed"],
            "faces": [{"weight": f["weight"], "italic": f["italic"], "file": f["file"]} for f in manifest["faces"]],
            "note": "Use it with add_text(font=...) or update_layer(changes={'font': ...}); "
                    "an open editor loads it within a second."}


def list_fonts() -> dict:
    """Built-in fonts, installed Google fonts, and the signature styles."""
    installed = [{"family": name, "category": m.get("category"),
                  "weights": sorted({f["weight"] for f in m["faces"]}),
                  "italics": sorted({f["weight"] for f in m["faces"] if f.get("italic")}), "folder": m["folder"]}
                 for name, m in fonts.installed().items()]
    return {"builtin": list(fonts.BUILTIN), "installed": installed, "folder": fonts.FONTS_DIR,
            "signature_styles": {k: v[0] for k, v in SIGNATURE_STYLES.items()}}


# ---------------------------------------------------------------- signatures
#
# A signature is an ordinary text layer in a handwriting font, so it can be
# moved, recoloured and restyled like any text. The styles below are Google
# families that read as real signatures; the font is installed on first use.

SIGNATURE_STYLES = {
    # style: (Google family, weight, what it looks like)
    "elegant": ("Great Vibes", "400", "classic calligraphic script with long swashes"),
    "flowing": ("Allura", "400", "smooth, rounded, connected script"),
    "airy": ("Sacramento", "400", "thin monoline script, light and open"),
    "bold-scrawl": ("Mr Dafoe", "400", "fast brush-pen scrawl with heavy strokes"),
    "handwritten": ("Homemade Apple", "400", "a real pen on paper, slightly uneven"),
    "neat": ("Dancing Script", "600", "tidy, legible cursive"),
    "formal": ("Herr Von Muellerhoff", "400", "slanted copperplate-style signature"),
    "ornate": ("Monsieur La Doulaise", "400", "ornate script with large flourished capitals"),
}
SIGNATURE_INK = "#1d2a44"


def _signature_style(style: str) -> tuple[str, str, str]:
    key = (style or "elegant").strip().lower().replace("_", "-").replace(" ", "-")
    if key not in SIGNATURE_STYLES:
        raise ValueError(f"style must be one of {', '.join(SIGNATURE_STYLES)}; got {style!r}")
    return (key,) + SIGNATURE_STYLES[key][:2]


def _signature_attrs(name: str, family: str, weight: str, font_size: float, color: str, rotation: float) -> dict:
    """A text layer's attrs for a one-line signature, the box hugging the
    text (with a little slack so the browser's metrics cannot wrap it)."""
    attrs = {"text": name, "fontSize": float(font_size), "fontFamily": family, "fontStyle": weight,
             "fill": color or SIGNATURE_INK, "lineHeight": 1.15, "letterSpacing": 0, "stroke": None,
             "strokeWidth": 0, "textDecoration": "", "padding": 4, "rotation": float(rotation),
             "scaleX": 1, "scaleY": 1, "align": "center"}
    attrs["width"] = render.text_metrics(attrs)["width"] + font_size * 0.08 + 8
    return attrs


def _signature_size_for_width(name: str, family: str, weight: str, target_width: float) -> float:
    """The font size at which `name` in this face is `target_width` wide."""
    probe = render.text_metrics({"text": name, "fontFamily": family, "fontStyle": weight, "fontSize": 100,
                                 "padding": 0, "letterSpacing": 0})
    ink = max(1.0, probe["widths"][0] if probe["widths"] else 1.0)
    return max(8.0, 100.0 * float(target_width) / ink)


def _signature_baseline(attrs: dict) -> float:
    """Where the signature's baseline sits inside its box (local y). Konva
    draws each line at padding + lineHeight/2 with textBaseline 'middle',
    which is (ascent - descent) / 2 above the baseline; render.py does the
    same with Pillow's 'lm' anchor, so this matches both."""
    m = render.text_metrics(attrs)
    ascent, descent = m["font"].getmetrics()
    scale = m["size"] / max(1, m["font"].size)
    return m["padding"] + m["line_height"] / 2 + (ascent - descent) / 2 * scale


def _line_geometry(layer: dict) -> dict:
    """A line layer's two ends on the page, its length, angle and centre."""
    a = layer.get("attrs") or {}
    flat = a.get("points") or [0, 0, 0, 0]
    matrix = render._node_matrix(a)
    (x0, y0), (x1, y1) = render._apply(matrix, flat[0], flat[1]), render._apply(matrix, flat[-2], flat[-1])
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "length": math.hypot(x1 - x0, y1 - y0),
            "angle": math.degrees(math.atan2(y1 - y0, x1 - x0)), "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
            "stroke_width": float(a.get("strokeWidth", 4) or 0)}


def preview_signatures(name: str, styles: str = "", color: str = SIGNATURE_INK) -> dict:
    """Draw `name` in every signature style (or the comma-separated `styles`)
    on one labelled contact sheet, each sitting on a signature line the way
    add_signature would place it, and return the PNG path to look at."""
    from PIL import Image, ImageDraw

    if not (name or "").strip():
        raise ValueError("name is required")
    keys = [k.strip() for k in (styles or "").split(",") if k.strip()] or list(SIGNATURE_STYLES)
    picked = [_signature_style(k) for k in keys]
    for _, family, _ in picked:
        _ensure_font(family)
    sheet_w, row_h, pad = 1400, 250, 40
    line_w = 760
    sheet = Image.new("RGB", (sheet_w, pad + row_h * len(picked) + pad // 2), (250, 248, 243))
    draw = ImageDraw.Draw(sheet)
    label_font = render.load_font("Helvetica Neue", "500", 26)
    small_font = render.load_font("Helvetica Neue", "400", 20)
    ink = render.parse_color(color, render.parse_color(SIGNATURE_INK))
    rows = []
    for i, (key, family, weight) in enumerate(picked):
        top = pad + i * row_h
        size = _signature_size_for_width(name, family, weight, line_w * 0.55)
        size = min(size, row_h * 0.62)
        attrs = _signature_attrs(name, family, weight, size, color, 0)
        text_img, origin, k = render._draw_text(attrs)
        text_img = text_img.resize((max(1, int(text_img.width / k)), max(1, int(text_img.height / k))), Image.LANCZOS)
        line_y = top + row_h - 74
        line_x0 = sheet_w - pad - line_w
        baseline = _signature_baseline(attrs)
        tx = int(round(line_x0 + (line_w - attrs["width"]) / 2 + origin[0]))
        ty = int(round(line_y + size * 0.03 - baseline + origin[1]))
        sheet.paste(text_img, (tx, ty), text_img)
        draw.line([(line_x0, line_y), (line_x0 + line_w, line_y)], fill=(90, 90, 90), width=3)
        draw.text((pad, top + 30), key, font=label_font, fill=(30, 30, 30))
        draw.text((pad, top + 66), family + (f" {weight}" if weight != "400" else ""), font=small_font, fill=(110, 110, 110))
        draw.text((pad, top + 96), SIGNATURE_STYLES[key][2], font=small_font, fill=(140, 140, 140))
        if i:
            draw.line([(pad, top), (sheet_w - pad, top)], fill=(225, 222, 215), width=1)
        rows.append({"style": key, "font": family, "weight": weight, "look": SIGNATURE_STYLES[key][2]})
    folder = os.path.join(tempfile.gettempdir(), "photo-editor-renders")
    os.makedirs(folder, exist_ok=True)
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:40] or "signature"
    path = os.path.join(folder, f"signatures-{slug}-{int(time.time())}.png")
    sheet.save(path)
    return {"path": path, "styles": rows, "width": sheet.width, "height": sheet.height,
            "note": "Open the PNG to compare, then add_signature(name, style=...)."}


def add_signature(name: str, style: str = "elegant", document: str = "", above_line: str = "",
                  position: str = "center", x=None, y=None, width: float = 0, font_size: float = 0,
                  color: str = SIGNATURE_INK, rotation: float = -3, fit: float = 0.52,
                  layer_name: str = "") -> dict:
    """Sign `name` on the page as an editable text layer in a handwriting font.
    above_line = a line layer's id or name: the signature is sized to `fit`
    (0.3-0.9) of the line's length, centred on it, with its baseline just
    through the stroke the way a pen would land. Otherwise width or font_size
    sets the size and position / x / y place it like add_text."""
    doc_id, record = _load(document)
    snap = record["snap"]
    line = None
    if above_line:
        line = _find_layer(snap, above_line)
        if line.get("type") != "line":
            raise ValueError(f"{line.get('name')} is a {line.get('type')} layer; above_line needs a line layer")
    layer, info = signature_layer(snap, name, style, line, position=position, x=x, y=y, width=width,
                                  font_size=font_size, color=color, rotation=rotation, fit=fit,
                                  layer_name=layer_name)
    out = _add_layer(doc_id, record, layer, f"add signature ({info['style']})")
    out["signature"] = info
    return out


def signature_layer(snap: dict, name: str, style: str = "elegant", line: dict | None = None,
                    position: str = "center", x=None, y=None, width: float = 0, font_size: float = 0,
                    color: str = SIGNATURE_INK, rotation: float = -3, fit: float = 0.52,
                    layer_name: str = "") -> tuple[dict, dict]:
    """The text layer add_signature adds, and its description -- the one
    place the sizing and placement live. The editor's Sign panel calls it
    too (photo_editor.py `signature_layer`), so a click there and the MCP
    tool put the same layer in the same spot. `line` is a line layer dict."""
    if not (name or "").strip():
        raise ValueError("name is required")
    key, family, weight = _signature_style(style)
    family = _ensure_font(family)
    tx, ty, tw, th = _trim(snap)
    if line is not None:
        geo = _line_geometry(line)
        if geo["length"] < 1:
            raise ValueError("That line has no length")
        share = min(0.9, max(0.3, float(fit or 0.52)))
        target = float(width) if width else geo["length"] * share
        size = _signature_size_for_width(name, family, weight, target)
    elif width:
        size = _signature_size_for_width(name, family, weight, float(width))
    elif font_size:
        size = float(font_size)
    else:
        size = _signature_size_for_width(name, family, weight, tw * 0.28)
    size = round(size, 1)
    attrs = _signature_attrs(name, family, weight, size, color, rotation)
    box = render.text_metrics(attrs)
    w, h = box["width"], box["height"]
    if line is not None:
        # Put the baseline's midpoint on the line's centre (a hair below it),
        # then find the box origin that lands it there once rotated.
        total = float(rotation) + geo["angle"]
        attrs["rotation"] = total
        r = math.radians(total)
        local = (w / 2, _signature_baseline(attrs))
        anchor = (geo["cx"], geo["cy"] + geo["stroke_width"] / 2 + size * 0.03)
        attrs["x"] = anchor[0] - (local[0] * math.cos(r) - local[1] * math.sin(r))
        attrs["y"] = anchor[1] - (local[0] * math.sin(r) + local[1] * math.cos(r))
    else:
        left, top = _place(snap, w, h, position, x, y)
        attrs["x"], attrs["y"] = left, top
    layer = {"id": _layer_id(), "type": "text", "name": layer_name or f"Signature – {name.strip()}",
             "visible": True, "locked": False, "opacity": 1, "blend": "normal", "attrs": attrs}
    info = {"style": key, "font": family, "weight": weight, "font_size": round(size, 1),
            "line": line.get("id") if line is not None else None}
    return layer, info
