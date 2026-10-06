"""Google Fonts for the editor: catalogue, install, and what is on disk.

Every family on fonts.google.com is OFL or Apache licensed, so the agent can
fetch one when a document needs it. The catalogue (fonts.google.com/metadata)
is cached in `.fused/data/fonts/catalogue.json`; an installed family is a
folder `.fused/data/fonts/<Family>/` with one static TTF per weight/italic
(`<Family>-<weight>[i].ttf`) and a `font.json` manifest. `installed.json`
beside the folders indexes every face with its absolute path: `render.py`
reads the folders, and the page reads the index to build @font-face rules
from `fused.rawUrl(path)`, so Pillow and Konva draw the very same file.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(APP_DIR, ".fused", "data", "fonts")
CATALOGUE_PATH = os.path.join(FONTS_DIR, "catalogue.json")
INDEX_PATH = os.path.join(FONTS_DIR, "installed.json")
CATALOGUE_URL = "https://fonts.google.com/metadata/fonts"
CSS2_URL = "https://fonts.googleapis.com/css2"
CATALOGUE_MAX_AGE = 30 * 24 * 3600
# A browser that does not advertise woff2 support gets plain TTF URLs.
_UA = "Mozilla/4.0 (compatible; photo-editor fonts)"
CATEGORIES = ["sans-serif", "serif", "display", "handwriting", "monospace"]

# The fonts the page ships with (Google-hosted) or every Mac has; mirrors
# FONTS in index.html and _FAMILY_FILES in render.py.
BUILTIN = ["Source Sans 3", "Source Serif 4", "Helvetica Neue", "Arial", "Georgia", "Times New Roman",
           "Futura", "Avenir Next", "Gill Sans", "Impact", "Courier New", "Menlo"]


class FontError(Exception):
    pass


def _fetch(url: str, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp-{os.getpid()}-{int(time.time() * 1000)}"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _category(raw: str) -> str:
    return (raw or "").strip().lower().replace(" ", "-")


def _folder_name(family: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]", "", family).strip() or "font"


# ---------------------------------------------------------------- catalogue


def catalogue(refresh: bool = False) -> list[dict]:
    """[{family, category, weights, italics, popularity}] for every Google
    family, from the disk cache unless it is missing, stale or `refresh`."""
    cached = None if refresh else _read_json(CATALOGUE_PATH)
    if cached and time.time() - float(cached.get("fetched", 0)) < CATALOGUE_MAX_AGE:
        return cached["families"]
    try:
        raw = _fetch(CATALOGUE_URL, timeout=40).decode("utf-8")
    except Exception as error:
        if cached:
            return cached["families"]  # stale beats nothing
        raise FontError(f"Could not download the Google Fonts catalogue: {error}") from error
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1]
    data = json.loads(raw)
    families = []
    for item in data.get("familyMetadataList") or []:
        keys = sorted((item.get("fonts") or {}).keys())
        weights = sorted({int(k.rstrip("i")) for k in keys if k.rstrip("i").isdigit()}) or [400]
        italics = sorted({int(k[:-1]) for k in keys if k.endswith("i") and k[:-1].isdigit()})
        families.append({"family": item["family"], "category": _category(item.get("category")),
                         "weights": weights, "italics": italics,
                         "popularity": int(item.get("popularity") or 10**6),
                         "subsets": [s for s in item.get("subsets") or [] if s != "menu"]})
    families.sort(key=lambda f: f["popularity"])
    _write_atomic(CATALOGUE_PATH, json.dumps({"fetched": time.time(), "source": CATALOGUE_URL,
                                              "families": families}).encode("utf-8"))
    return families


def lookup(family: str, refresh: bool = False) -> dict | None:
    """The catalogue entry for `family`, matched case- and space-insensitively."""
    key = re.sub(r"\s+", "", (family or "")).lower()
    if not key:
        return None
    for item in catalogue(refresh):
        if re.sub(r"\s+", "", item["family"]).lower() == key:
            return item
    return None


def search(query: str = "", category: str = "", limit: int = 20) -> list[dict]:
    cat = _category(category)
    if cat and cat not in CATEGORIES:
        raise FontError(f"category must be one of {CATEGORIES}")
    words = [w for w in (query or "").lower().split() if w]
    on_disk = installed()
    out = []
    for item in catalogue():
        if cat and item["category"] != cat:
            continue
        name = item["family"].lower()
        if words and not all(w in name for w in words):
            continue
        entry = dict(item)
        entry["installed"] = item["family"] in on_disk
        out.append(entry)
    # Exact and prefix matches first, then Google's popularity order.
    q = (query or "").lower().strip()
    out.sort(key=lambda f: (0 if f["family"].lower() == q else 1 if q and f["family"].lower().startswith(q) else 2,
                            f["popularity"]))
    return out[: max(1, int(limit or 20))]


# ---------------------------------------------------------------- installed


def installed() -> dict[str, dict]:
    """family -> manifest for every family folder with a font.json."""
    out = {}
    if not os.path.isdir(FONTS_DIR):
        return out
    for entry in sorted(os.listdir(FONTS_DIR)):
        manifest = _read_json(os.path.join(FONTS_DIR, entry, "font.json"))
        if not manifest or not manifest.get("family"):
            continue
        folder = os.path.join(FONTS_DIR, entry)
        faces = [f for f in manifest.get("faces") or [] if os.path.isfile(os.path.join(folder, f.get("file", "")))]
        if faces:
            manifest = dict(manifest, faces=faces, folder=folder)
            out[manifest["family"]] = manifest
    return out


def family_dir(family: str) -> str | None:
    """The folder holding `family`'s TTFs, matched loosely, or None."""
    if not family or not os.path.isdir(FONTS_DIR):
        return None
    key = re.sub(r"\s+", "", family).lower()
    for entry in os.listdir(FONTS_DIR):
        folder = os.path.join(FONTS_DIR, entry)
        if not os.path.isdir(folder):
            continue
        manifest = _read_json(os.path.join(folder, "font.json")) or {}
        names = {re.sub(r"\s+", "", entry).lower(), re.sub(r"\s+", "", manifest.get("family") or "").lower()}
        if key in names:
            return folder
    return None


def canonical(family: str) -> str | None:
    """The proper spelling of a built-in or installed family, or None."""
    key = re.sub(r"\s+", "", family or "").lower()
    for name in BUILTIN:
        if re.sub(r"\s+", "", name).lower() == key:
            return name
    folder = family_dir(family)
    if folder:
        manifest = _read_json(os.path.join(folder, "font.json")) or {}
        return manifest.get("family") or os.path.basename(folder)
    return None


def write_index() -> dict:
    """installed.json: what the page reads to build @font-face rules."""
    families = []
    for family, manifest in installed().items():
        families.append({"family": family, "category": manifest.get("category", ""),
                         "faces": [{"weight": f["weight"], "italic": bool(f.get("italic")),
                                    "path": os.path.join(manifest["folder"], f["file"])}
                                   for f in manifest["faces"]]})
    index = {"updated": time.time(), "families": families}
    _write_atomic(INDEX_PATH, json.dumps(index, indent=1).encode("utf-8"))
    return index


def _css2_url(entry: dict) -> str:
    weights, italics = entry.get("weights") or [400], entry.get("italics") or []
    if italics:
        tuples = sorted([(0, w) for w in weights] + [(1, w) for w in italics])
        axis = "ital,wght@" + ";".join(f"{i},{w}" for i, w in tuples)
    elif weights != [400]:
        axis = "wght@" + ";".join(str(w) for w in weights)
    else:
        axis = ""
    spec = entry["family"].replace(" ", "+") + (":" + axis if axis else "")
    return f"{CSS2_URL}?family={spec}&display=swap"


_FACE_RE = re.compile(r"@font-face\s*{([^}]*)}", re.S)


def _parse_css(css: str) -> list[dict]:
    faces = []
    for block in _FACE_RE.findall(css):
        if "unicode-range" in block and "U+0000-00FF" not in block and "U+0000" not in block:
            continue  # a non-latin subset of the same face
        url = re.search(r"url\(([^)]+)\)", block)
        weight = re.search(r"font-weight:\s*(\d+)", block)
        style = re.search(r"font-style:\s*(\w+)", block)
        if not url:
            continue
        faces.append({"url": url.group(1).strip("'\""), "weight": int(weight.group(1)) if weight else 400,
                      "italic": bool(style and style.group(1) == "italic")})
    seen, unique = set(), []
    for face in faces:
        key = (face["weight"], face["italic"])
        if key not in seen:
            seen.add(key)
            unique.append(face)
    return unique


def install(family: str, refresh: bool = False) -> dict:
    """Download every face of a Google family into FONTS_DIR/<Family>/.
    Already installed -> returns the manifest without touching the network."""
    existing = installed()
    known = canonical(family)
    if known in existing:
        return dict(existing[known], already_installed=True)
    entry = lookup(family, refresh)
    if entry is None:
        if known:
            raise FontError(f"{known} is a built-in font; nothing to install")
        raise FontError(f"No Google font called {family!r}. Try search_fonts to find the exact family name.")
    css_url = _css2_url(entry)
    try:
        faces = _parse_css(_fetch(css_url).decode("utf-8"))
    except Exception as error:
        raise FontError(f"Could not fetch {entry['family']} from Google Fonts: {error}") from error
    if not faces:
        raise FontError(f"Google Fonts returned no TTF files for {entry['family']}")
    folder = os.path.join(FONTS_DIR, _folder_name(entry["family"]))
    os.makedirs(folder, exist_ok=True)
    stem = entry["family"].replace(" ", "")
    manifest_faces = []
    for face in faces:
        file_name = f"{stem}-{face['weight']}{'i' if face['italic'] else ''}.ttf"
        target = os.path.join(folder, file_name)
        if not os.path.isfile(target) or os.path.getsize(target) == 0:
            try:
                _write_atomic(target, _fetch(face["url"], timeout=60))
            except Exception as error:
                raise FontError(f"Could not download {file_name}: {error}") from error
        manifest_faces.append({"weight": face["weight"], "italic": face["italic"], "file": file_name, "url": face["url"]})
    manifest = {"family": entry["family"], "category": entry["category"], "faces": manifest_faces,
                "css2": css_url, "installed": time.time(), "license": "OFL/Apache (Google Fonts)"}
    _write_atomic(os.path.join(folder, "font.json"), json.dumps(manifest, indent=1).encode("utf-8"))
    write_index()
    try:  # a render in this process may have cached "no such family"
        import render

        render._font_faces.cache_clear()
        render.load_font.cache_clear()
    except Exception:
        pass
    return dict(manifest, folder=folder, already_installed=False)
