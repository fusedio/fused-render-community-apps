"""The lens API, as a fused-render data file instead of an HTTP daemon.

lens used to answer its views over loopback: a long-lived process that held the
store, the SigLIP weights and an index thread, and served `/status`, `/query`,
`/thumb/<id>` and the rest to a page that fetched them. That process is gone.
Every one of those endpoints is now an `op` on this file's `main()`, which the
page reaches through `fused.runPython("./views/lens_api.py", {op: …})`.

What that trade actually costs, said plainly, because it shapes everything
below:

  * **No process to hold anything.** Each call is a fresh subprocess with a 60s
    cap, so nothing may be cached in a global and nothing may load a model. The
    catalog is opened, read and closed per call — which is cheap, because it is
    sqlite — and the embedding matrix is read off disk as an mmap-backed npz.
  * **No model.** The daemon owned the text tower and embedded a query itself.
    Here the *page* embeds it, through fused-render's own `fused.ai.embed`, and
    hands the vector down as JSON (`op=query`, `vec=…`). The maths — a dot
    product against the stored fp16 matrix — stays on this side, where the
    matrix already is. See `op_query` for the two-phase handshake that makes
    this one round trip look like one call to the page.
  * **No image server.** The daemon rendered thumbnails on demand and served
    the bytes. fused-render already serves any absolute path off local disk
    (`fused.rawUrl`), so this file hands back *paths* — resolved here so the
    page never has to guess the cache layout — and the browser fetches them.
  * **No indexer in *this* process.** Indexing is minutes of work over every
    file under every root, which a 60s subprocess cannot be. So `op=reindex`
    does not run it — it launches `scripts/index_worker.py` detached, and
    `op=status` reports on it by reading the record that worker keeps on disk
    (`_live_run`). The worker embeds through fused-render's own resident model
    (`lens.embed.ApiEmbedder`), so a run still costs no second copy of a
    4.55GB tower and no torch anywhere.

Response shapes are the daemon's, key for key, deliberately: the 4,000 lines of
lens.html that render them are the specification, and this port is a change of
transport, not of contract. Where a shape had to grow it only ever *gained*
keys (`thumb`, `full`, `face_url` — the paths that used to be daemon URLs).

Dependency rule: this file runs on fused-render's own interpreter, which ships
numpy and Pillow and nothing of lens's own. So it imports `lens.query`,
`lens.store` and `lens.validate` — which are numpy-and-stdlib — and
deliberately does NOT import `lens.config`, `lens.indexer` or `lens.thumbs`,
each of which reaches psutil or torch through its own imports. The handful of
lines those three would have provided (a config read/write, two cache path
spellings, a root prefix test) are restated here, each marked with where it
came from so a change on that side is findable from this one.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# The lens package lives one directory up from views/. Relative paths in a
# fused-render data file resolve next to the file itself, so this is stable
# wherever the repo is checked out.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import numpy as np                                            # noqa: E402

from lens import query as lens_query                          # noqa: E402
from lens.store import Store                                  # noqa: E402

# ── the constants that used to be imported ────────────────────────────────
# Each of these has a home in the lens package that this file cannot import
# (see the module docstring). Restated with its source named, so the two can be
# kept in step by anyone who finds either one.

# lens.thumbs.THUMB_VERSION / THUMB_SIZE_DEFAULT — part of the cached file name,
# so a bump on that side must be a bump here or every thumbnail reads as missing.
THUMB_VERSION = 2
THUMB_SIZE_DEFAULT = 512
# lens.thumbs.FACE_SIZE_DEFAULT / FACE_COVER_MARGIN, and daemon.FACE_SIZES.
FACE_SIZE_DEFAULT = 200
FACE_COVER_MARGIN = 0.35
FACE_SIZES = (FACE_SIZE_DEFAULT, FACE_SIZE_DEFAULT * 2)
# lens.indexer.APPLE_META / RUNS_HISTORY_FILE / RUNS_HISTORY_MAX / STAGE_INDEX.
APPLE_META = "apple_last"
RUNS_HISTORY_FILE = "runs.jsonl"
MAX_RUNS = 200
STAGE_INDEX = "index"
# scripts/index_worker.py's RUN_FILE / LOG_FILE / JOB_ID / STALE_AFTER_S — the
# whole interface between this file and the run it starts. Restated rather than
# imported for the reason in the module docstring (that module reaches psutil
# and av through lens.config and lens.indexer); a change to either side has to
# be a change to both, and `_live_run` below is where it would show.
INDEX_RUN_FILE = "index_run.json"
INDEX_LOG_FILE = "index.log"
INDEX_JOB_ID = "lens:index"
INDEX_STALE_AFTER_S = 90
# lens.memguard.DEFAULT_LIMIT_GB — the fallback the guard itself applies when
# the key is simply absent from the config.
DEFAULT_LIMIT_GB = 8
# daemon.MAX_LIMIT / MAX_NAME.
MAX_LIMIT = 2000
MAX_NAME = 80

# Which model's vectors are in `embeddings.npz`, spelled as the Hugging Face id
# the page must ask `fused.ai.embed` for. The stored matrix is 1152-dimensional
# and only this model produces 1152 dimensions; anything else would rank noise.
# Returned in `op=status` and in the `need_embed` handshake so the page never
# has the id written down twice.
MODEL_ID = "google/siglip2-so400m-patch14-384"
EMBED_DIMS = 1152

# Formats a browser will decode itself, for the lightbox's full-size render.
# The daemon answered /thumb?s=2048 by rendering one; nothing here can render a
# HEIC (that needs pillow-heif) or seek a video frame (that needs ffmpeg), so
# the honest answer for those is the 512px thumbnail the index already made,
# and the original file only for the formats that need no help.
BROWSER_STILLS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def _cache_dir() -> Path:
    """lens.config.cache_dir, without the psutil chain behind it."""
    d = Path(os.environ.get("LENS_CACHE", Path.home() / ".fused-render/cache/lens"))
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── config (lens.config, restated) ────────────────────────────────────────
_CFG_DEFAULTS = {"roots": [], "port": 8877, "model": "siglip2",
                 "apple_photos": False,
                 "max_index_memory_gb": DEFAULT_LIMIT_GB}


def load_config(cache: Path) -> dict:
    """Never raises on a damaged file — same rule as lens.config.load_config:
    this is the only source of roots, and an unparseable file must degrade to
    the defaults rather than take the view down."""
    cfg = dict(_CFG_DEFAULTS)
    p = Path(cache) / "config.json"
    if p.exists():
        try:
            stored = json.loads(p.read_text())
        except (ValueError, OSError):
            stored = None
        if isinstance(stored, dict):
            cfg.update(stored)
    cfg["roots"] = list(cfg["roots"]) if isinstance(cfg.get("roots"), list) else []
    return cfg


def save_config(cfg: dict, cache: Path) -> None:
    """Written to a sibling and moved into place, so a crash mid-write cannot
    leave half a config behind (lens.config.save_config)."""
    p = Path(cache) / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def normalize_root(path: str) -> str:
    """lens.config.normalize_root — the one spelling a root is stored under."""
    return str(Path(path).expanduser().resolve())


def _resolve(path):
    """The normalized form, or None when the OS will not even consider it (a
    null byte, an over-long name, a symlink loop). daemon._resolve."""
    try:
        return normalize_root(path)
    except (OSError, ValueError):
        return None


def _under_root(path, root) -> bool:
    """lens.indexer._under_root, restated."""
    path = os.path.normpath(path)
    root = os.path.normpath(root).rstrip(os.sep)
    if not root:
        return path.startswith(os.sep)
    return path == root or path.startswith(root + os.sep)


# ── cache paths (lens.thumbs, restated) ───────────────────────────────────
def thumb_path(cache, sha1: str, size: int = THUMB_SIZE_DEFAULT) -> Path:
    return Path(cache) / "thumbs" / f"{sha1}-{size}-v{THUMB_VERSION}.webp"


def face_thumb_path(cache, sha1: str, bbox, size: int = FACE_SIZE_DEFAULT) -> Path:
    key = hashlib.sha1(
        (",".join(f"{float(v):.5f}" for v in bbox)).encode()).hexdigest()[:10]
    return Path(cache) / "faces" / f"{sha1}-{key}-{size}-v{THUMB_VERSION}.webp"


def _thumb_url(cache, row) -> str | None:
    """The absolute path of this row's 512px thumbnail, or None.

    None where the daemon would have rendered one on the spot: a row with no
    sha1 never got that far, and a thumb missing from the cache cannot be
    remade here (a HEIC needs pillow-heif, a video needs ffmpeg — neither is in
    this interpreter). The page draws the same empty tile it drew for a photo
    on an unplugged drive, which is what this is."""
    if not row.get("sha1"):
        return None
    p = thumb_path(cache, row["sha1"])
    return str(p) if p.exists() else None


def _full_url(row) -> str | None:
    """The original file, when the browser can decode it itself — the lightbox's
    2048px render, replaced by the real thing. None for a HEIC or a video, where
    the 512px thumbnail stays the best this build can show."""
    if row.get("kind") == "video" or not row.get("path"):
        return None
    if os.path.splitext(row["path"])[1].lower() not in BROWSER_STILLS:
        return None
    return row["path"] if os.path.exists(row["path"]) else None


def _ensure_face_crop(cache, face, size: int) -> str | None:
    """One face's square crop as a path, cropped out of the 512px thumbnail the
    detector itself looked at (lens.thumbs.ensure_face_thumb, minus the ability
    to render a missing base thumbnail).

    Pillow only — the crop is a resize of a webp that is already on disk, so
    this is the one piece of image work that survives the move off the daemon
    intact. None when there is nothing to crop from, which the page draws as a
    placeholder rather than as an error: a person's cover can be a photograph on
    a drive that is not plugged in."""
    if not face or not face.get("sha1"):
        return None
    out = face_thumb_path(cache, face["sha1"], face["bbox"], size)
    if out.exists():
        return str(out)
    base = thumb_path(cache, face["sha1"])
    if not base.exists():
        return None
    try:
        from PIL import Image
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(base) as img:
            crop = _crop_face(img, face["bbox"], size, FACE_COVER_MARGIN)
        # Same atomic swap as lens.thumbs: two of these calls can be in flight
        # in two subprocesses, and a reader must see nothing or the whole file.
        tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
        try:
            crop.save(tmp, "WEBP", quality=88)
            os.replace(tmp, out)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return str(out)
    except (OSError, ValueError):
        return None


def _crop_face(img, bbox, size: int, margin: float):
    """lens.faces.crop_face, restated — the box is normalized 0–1, the crop is
    square (the longer side, centred), and it may run off the edge, which PIL
    fills with black rather than cutting the chin off an edge face."""
    w, h = img.size
    x0, y0, x1, y1 = (float(v) for v in bbox)
    px0, py0, px1, py1 = x0 * w, y0 * h, x1 * w, y1 * h
    cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
    side = max(px1 - px0, py1 - py0) * (1 + 2 * margin)
    side = max(side, 8.0)
    half = side / 2
    box = (int(round(cx - half)), int(round(cy - half)),
           int(round(cx + half)), int(round(cy + half)))
    return img.convert("RGB").crop(box).resize((size, size))


# ── the error contract ────────────────────────────────────────────────────
class ApiError(Exception):
    """A refusal the page should render rather than a traceback.

    `code` and `message` are exactly the daemon's `{"error": …, "message": …}`
    body: lens.html's `api()` throws an Error carrying `.code` from it, and
    `confirm_home` in particular is branched on (it asks for a second press).
    Serialized as `{"error": code, "message": …}` by `main`, so the page's own
    error handling is unchanged."""

    def __init__(self, code: str, message: str = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ── status ────────────────────────────────────────────────────────────────
def op_status(store, cache) -> dict:
    """What `pollStatus` reads, and only true things.

    Two keys are answered differently from the daemon and each difference is
    a fact about this build rather than a placeholder:

      * `indexing`/`progress` come from the *worker's* run record on disk
        rather than from an index thread in this process — there is none, and
        the run is a detached process (see `op_reindex` and `_live_run`). The
        header's spinner, its progress line and its disabled buttons read these
        two keys and cannot tell the difference, which is the point.
      * `model_loaded` is true. The weights are fused-render's now, not this
        process's, and a page that has not asked for an embedding yet cannot
        say whether they are resident — but it *will* be told, by the
        `model_loading` rejection on its own first `fused.ai.embed` call, which
        is a better signal than a poll could be.
      * `last_index` is the last line of runs.jsonl rather than the run this
        process did (it did none). The banner it feeds — "3 files could not be
        read in the last index run" — is still describing the run that actually
        produced this catalog.
    """
    counts = store.scope_counts()
    shape = store.embedding_shape()
    cfg = load_config(cache)
    live = _live_run(cache)
    indexed = len(store.path_signatures())
    roots = [r for r in cfg["roots"] if isinstance(r, str) and r]
    # ── the first-run state, as a fact rather than a failure ───────────────
    # `library` is the one key the view needs to tell "lens has nothing to show
    # you yet" from "lens has nothing that matches". It is derived from CONTENT,
    # never from whether catalog.sqlite exists on disk: `main` now creates an
    # empty catalog on the first call, so a file test would say "first run" once
    # and "ready" on the very next poll two seconds later — the state would
    # flicker away while the user was still reading it.
    #
    #   "empty"  nothing catalogued, nothing running, and no run has ever
    #            finished here. A machine where lens has just been installed.
    #   "ready"  everything else, including a library whose files have all been
    #            deleted since — an index run HAS happened, so an empty grid is
    #            news about the folders and not about the install.
    #
    # `root_paths` rides along because the invitation has two different next
    # actions and only this says which: with no folder configured the one thing
    # to do is add one, and with folders already added it is to start the scan.
    # Named `root_paths` and not `roots` deliberately — `/roots` already answers
    # a `roots` key and it is a list of OBJECTS. One name for two shapes in one
    # API is how a `r.path` ends up undefined three call sites away.
    ever_indexed = (Path(cache) / RUNS_HISTORY_FILE).exists()
    library = ("empty" if not indexed and live is None and not ever_indexed
               else "ready")
    return {"library": library,
            "root_paths": roots,
            "photos": indexed,
            "photos_scope": counts["photos"],
            "videos_scope": counts["videos"],
            "all_scope": counts["all"],
            "trips": len(store.get_trips()),
            "faces": store.face_counts(),
            "apple": _apple_payload(store, cache),
            "indexing": live is not None,
            "progress": _progress_payload(live),
            "model": cfg.get("model", "siglip2"),
            "model_id": MODEL_ID,
            "embeddings": {"count": shape[0], "dims": shape[1]},
            "cache": str(cache),
            "model_loaded": True,
            "last_index": _last_run(cache)}


def _last_run(cache):
    """The newest line of runs.jsonl, or None. A line this file cannot parse is
    skipped rather than raised on — one truncated write must not blank the
    status banner (daemon.runs applies the same rule)."""
    path = Path(cache) / RUNS_HISTORY_FILE
    if not path.exists():
        return None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            return rec
    return None


def _apple_payload(store, cache) -> dict:
    """daemon.LensServer.apple_payload, unchanged: the config flag, the
    catalog's own count of Apple rows, and whatever the last sync reported —
    read out of the catalog rather than by re-opening the Photos library, which
    a settings panel must never cost."""
    cfg = load_config(cache)
    last = None
    stored = store.get_meta(APPLE_META)
    if stored:
        try:
            last = json.loads(stored)
        except ValueError:
            last = None
    return {"enabled": bool(cfg.get("apple_photos")),
            "rows": store.source_counts().get("apple", 0),
            "last": last if isinstance(last, dict) else None}


# ── query ─────────────────────────────────────────────────────────────────
def _known_places(store, q: str) -> list:
    """daemon.LensServer.known_places, unchanged — including the reason bare
    two-letter country codes are left out unless the query is nothing but one
    ("NO" turned "no dogs" into a Norway filter)."""
    whole = (q or "").strip().lower()
    seen, out = set(), []
    for col in ("place_city", "place_region", "place_country"):
        for v in store.distinct(col):
            if not v or v in seen:
                continue
            if len(v) <= 2 and v.lower() != whole:
                continue
            seen.add(v)
            out.append(v)
    return out


def _load_matrix(store):
    """`(ids, mat)` off embeddings.npz, as float32 and unit-normalized.

    The stored matrix is float16 and already normalized (lens.embed._normalize,
    then a cast that costs about 1e-4 of the norm), and the vectors
    `fused.ai.embed` hands back are unit too — so the dot product below is a
    cosine either way. Renormalizing regardless is a few milliseconds against
    the possibility of a matrix written by something that did not, and it is
    the one thing that would turn a ranking into noise silently."""
    ids, mat = store.load_embeddings()
    if mat.ndim != 2 or mat.shape[0] == 0:
        return ids, np.zeros((0, 0), dtype=np.float32)
    m = mat.astype(np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, m / norms


def op_query(store, cache, q: str = "", limit: int = 200, scope: str = "photos",
             offset: int = 0, trip=None, person=None, vec: str = "") -> dict:
    """daemon `GET /query`, in two halves that look like one call to the page.

    The daemon embedded the query itself. Here the model lives in fused-render,
    so the *parse* has to happen before the embedding and the embedding before
    the ranking — three steps across two processes. Rather than make the page
    own that sequence, this op answers a query it cannot rank with

        {"need_embed": "a photo of a beach", "model": …, "dims": 1152}

    and lens.html's `api()` embeds that exact sentence and calls straight back
    with `vec`. The sentence is `query.text_prompt(residual)` — the caption
    form, never the bare residual, for the measured reason in query.TEXT_PROMPT
    — and it is built here so the page never has to know the prompt at all.

    A filter-only query ("July 2025", "bali") has no residual, needs no vector
    and is answered on the first call, exactly as it always was.
    """
    limit = int(limit)
    if not 1 <= limit <= MAX_LIMIT:
        raise ApiError("bad parameter", "That page size isn’t one this view asks for.")
    if scope not in lens_query.SCOPES:
        # A scope this build does not offer gets the safe default rather than a
        # refusal, same as the daemon: the worst outcome of guessing is showing
        # the photographs, which is where the page starts anyway.
        scope = "photos"
    offset = max(0, int(offset))
    trip = None if trip in (None, "", "null") else int(trip)
    person = None if person in (None, "", "null") else int(person)

    by_name = store.person_names()                       # [(id, name)]
    pq = lens_query.parse(q or "", _known_places(store, q),
                          store.distinct("camera"),
                          known_albums=store.apple_phrases(),
                          known_people=[n for _, n in by_name])
    wanted = {n.lower() for n in pq.people}
    people = [pid for pid, n in by_name if n.lower() in wanted]
    if person is not None:
        # The explicit filter is a person the user pressed; a name in the query
        # is one they typed. Both mean "and this person too".
        people = [person] + [p for p in people if p != person]

    text_vec = None
    if pq.residual:
        if not vec:
            return {"need_embed": lens_query.text_prompt(pq.residual),
                    "model": MODEL_ID, "dims": EMBED_DIMS}
        try:
            text_vec = np.asarray(json.loads(vec), dtype=np.float32).ravel()
        except (ValueError, TypeError):
            raise ApiError("bad vector", "The query vector could not be read.")

    where, params = lens_query.build_where(pq, scope, trip, people)
    rows = store.query_photos(where, params)
    strong, cutoff, searched = None, None, None
    if text_vec is not None:
        searched = len(rows)
        ids, mat = _load_matrix(store)
        if mat.shape[0] and text_vec.shape[0] != mat.shape[1]:
            # A dim mismatch means the page embedded with the wrong model. Say
            # which and stop — the alternative is a dot product that raises, or
            # worse, one that silently succeeds against a truncated vector and
            # returns a ranking of nothing.
            raise ApiError(
                "model mismatch",
                f"This library was indexed with {MODEL_ID} "
                f"({mat.shape[1]} dimensions) but the query was embedded to "
                f"{text_vec.shape[0]}. Search would be meaningless.")
        n = np.linalg.norm(text_vec)
        if n:
            text_vec = text_vec / n
        rows = lens_query.rank(rows, ids, mat, text_vec, limit=None,
                               ratio=lens_query.RELEVANCE_RATIO)
        strong, cutoff = lens_query.confidence_horizon(rows)
    total = len(rows)
    rows = rows[offset:offset + limit]

    trips = {t["id"]: t for t in store.get_trips()}
    # The daemon's item keys, plus the three that used to be a daemon URL:
    # `thumb` and `full` are what fused.rawUrl() is pointed at, and `sha1` is
    # what names them, kept so the page can say why a tile is empty.
    keys = ("id", "path", "taken_at", "place_city", "camera", "score",
            "trip_id", "kind", "duration_s", "sha1")
    items = []
    for r in rows:
        it = {k: r.get(k) for k in keys}
        it["thumb"] = _thumb_url(cache, r)
        it["full"] = _full_url(r)
        items.append(it)
    if pq.trip_mode:
        groups, order = {}, []
        for it in items:
            tid = it.get("trip_id")
            if tid not in groups:
                groups[tid] = []
                order.append(tid)
            groups[tid].append(it)
        gs = [{"trip": trips.get(tid, {}).get("name") if tid is not None else None,
               "start": trips.get(tid, {}).get("start") if tid is not None else None,
               "end": trips.get(tid, {}).get("end") if tid is not None else None,
               "items": groups[tid]} for tid in order]
    else:
        gs = [{"trip": None, "items": items}]
    return {"parsed": {"date_from": pq.date_from, "date_to": pq.date_to,
                       "places": pq.places, "cameras": pq.cameras,
                       "albums": pq.albums, "people": pq.people,
                       "trip_mode": pq.trip_mode, "residual": pq.residual},
            "scope": scope, "person": person, "trip": trip,
            "total": total, "limit": limit, "offset": offset,
            "strong": strong, "strong_cutoff": cutoff, "searched": searched,
            "groups": gs}


# ── one photo ─────────────────────────────────────────────────────────────
def op_meta(store, cache, id: int) -> dict:
    """daemon `GET /meta/<id>` — the whole row, `raw_exif` parsed, and who is in
    it. `thumb`/`full` ride along for the same reason they do on a query item,
    and each person chip carries the path of its own crop (`face_url`), which is
    what the daemon's /people/<face>/face.webp route was for."""
    pid = int(id)
    row = store.get_photo_by_id(pid)
    if not row:
        raise ApiError("no such photo", "That photo isn’t in the library.")
    try:
        row["raw_exif"] = json.loads(row.get("raw_exif") or "{}")
    except ValueError:
        row["raw_exif"] = {}
    row["thumb"] = _thumb_url(cache, row)
    row["full"] = _full_url(row)
    row["people"] = _people_in(store, cache, pid)
    return row


def _people_in(store, cache, photo_id: int) -> list:
    """daemon.LensServer.people_in, plus the crop path.

    Faces with no person are included with `person_id: null`: "somebody is in
    this photo and lens has not seen them anywhere else" is a true and useful
    thing to show."""
    rows = store.faces_for_photos([photo_id]).get(int(photo_id), [])
    if not rows:
        return []
    names = {p["id"]: p.get("name") for p in store.get_persons()}
    photo = store.get_photo_by_id(int(photo_id)) or {}
    out = []
    for r in rows:
        pid = r.get("cluster_id")
        face = {"sha1": photo.get("sha1"), "bbox": r["bbox"]}
        out.append({"face_id": r["id"], "person_id": pid,
                    "name": names.get(pid) if pid is not None else None,
                    "prob": r.get("prob"), "bbox": list(r["bbox"]),
                    "face_url": _ensure_face_crop(cache, face,
                                                  FACE_SIZE_DEFAULT)})
    return out


# ── trips ─────────────────────────────────────────────────────────────────
def op_trips(store, cache) -> dict:
    """daemon `GET /trips`. A trip with no showable photo still travels — the
    count is the honest thing about it, and `cover` is null so the view draws a
    placeholder rather than pretending the trip does not exist."""
    counts = store.trip_counts()
    out = []
    for t in store.get_trips():
        n, cover = counts.get(t["id"], (0, None))
        row = store.get_photo_by_id(cover) if cover is not None else None
        out.append({"id": t["id"], "name": t["name"], "start": t["start"],
                    "end": t["end"], "place": t["place"],
                    "count": n, "cover_id": cover,
                    "cover": _thumb_url(cache, row) if row else None})
    return {"trips": out}


# ── people ────────────────────────────────────────────────────────────────
def op_people(store, cache, size: int = FACE_SIZE_DEFAULT * 2) -> dict:
    """daemon `GET /people` — most-photographed first, which is the answer to
    the question the People view is opened with.

    A person whose faces have all gone is not listed: the row survives so a name
    and a merge survive with it, but a card for nobody is not a person.

    `cover` is the crop path the daemon served from /people/<face>/face.webp.
    Rendered at 400 (the retina card) because that is the size the cards ask
    for, and the smaller one is a resize of the same crop anyway."""
    size = min(FACE_SIZES, key=lambda s: abs(s - int(size)))
    counts = store.person_counts()
    out = []
    for p in store.get_persons():
        faces, photos = counts.get(p["id"], (0, 0))
        if not faces:
            continue
        out.append({"id": p["id"], "name": p.get("name") or None,
                    "face_count": faces, "photo_count": photos,
                    "cover_face_id": p.get("cover_face_id"),
                    "cover": _cover_url(store, cache, p.get("cover_face_id"),
                                        size)})
    out.sort(key=lambda p: (-p["photo_count"], -p["face_count"], p["id"]))
    return {"people": out}


def _cover_url(store, cache, face_id, size):
    if face_id is None:
        return None
    return _ensure_face_crop(cache, store.get_face(int(face_id)), size)


def op_face(store, cache, id: int, size: int = FACE_SIZE_DEFAULT) -> dict:
    """daemon `GET /people/<face_id>/face.webp`, as a path instead of bytes.

    Bounded to FACE_SIZES for the same reason the daemon bounded it: a size in a
    request is a request for work, and an unbounded one is a request for
    arbitrary work."""
    size = min(FACE_SIZES, key=lambda s: abs(s - int(size)))
    url = _ensure_face_crop(cache, store.get_face(int(id)), size)
    if url is None:
        raise ApiError("no such face", "There is no crop for that face.")
    return {"face_id": int(id), "size": size, "url": url}


def op_people_merge(store, cache, keep, absorb) -> dict:
    """daemon `POST /people/merge`. Two cards that are one person, made one."""
    try:
        keep, absorb = int(keep), int(absorb)
    except (TypeError, ValueError):
        raise ApiError("bad request", "keep and absorb must be person ids")
    if not store.merge_persons(keep, absorb):
        raise ApiError("no such person", "One of those people is already gone.")
    counts = store.person_counts()
    faces, photos = counts.get(keep, (0, 0))
    row = next((p for p in store.get_persons() if p["id"] == keep), None)
    return {"person": {"id": keep, "name": (row or {}).get("name") or None,
                       "face_count": faces, "photo_count": photos,
                       "cover_face_id": (row or {}).get("cover_face_id"),
                       "cover": _cover_url(store, cache,
                                           (row or {}).get("cover_face_id"),
                                           FACE_SIZES[-1])}}


def op_person_rename(store, cache, id, name=None) -> dict:
    """daemon `POST /people/<id>/rename`. An empty name clears it, which is a
    real operation: a name seeded from the Photos library can be wrong, and "no
    name" is a better state than a wrong one."""
    if name is not None and not isinstance(name, str):
        raise ApiError("bad name", "A name has to be text.")
    clean = (name or "").strip()[:MAX_NAME]
    if not store.set_person_name(int(id), clean or None):
        raise ApiError("no such person", "That person isn’t in the library.")
    return {"person": {"id": int(id), "name": clean or None}}


# ── tags ──────────────────────────────────────────────────────────────────
# The concept vocabulary behind the details panel's chips. The daemon embedded
# these ~70 labels once at start-up, on the thread that warmed the text encoder,
# so opening the panel never waited on the model. There is no start-up here and
# no model here — so the labels are embedded by the *page*, once, and the matrix
# is cached to disk under the model that produced it. `op=tags` answers
# `{"need_vocab": [...prompts]}` until that file exists, which is the same
# handshake `op=query` uses for a query vector.
#
# Copied from lens.tags rather than imported for the usual reason (lens.tags →
# lens.query is fine, but the vocabulary is data and this file must be able to
# state its own cache key). A change on that side needs a change here, and the
# cache file name carries a digest of the list so a stale matrix can never be
# scored against a newer vocabulary.
TAG_VOCAB = [
    "people", "portrait", "selfie", "baby", "wedding", "crowd",
    "dog", "cat", "bird", "horse", "fish",
    "food", "coffee", "cocktail", "restaurant", "fruit", "cake",
    "beach", "ocean", "waves", "mountains", "forest", "waterfall", "lake",
    "river", "desert", "snow", "sunset", "sunrise", "night sky", "clouds",
    "rain", "flowers", "palm trees", "garden",
    "city street", "skyline", "building", "temple", "church", "market",
    "bridge", "road", "airport", "train", "boat", "car", "motorbike",
    "bicycle",
    "indoor room", "kitchen", "bedroom", "office desk", "swimming pool",
    "hotel room",
    "concert", "sports", "surfing", "hiking", "dancing", "yoga",
    "screenshot", "document", "chart", "logo", "artwork", "poster", "map",
    "computer screen",
    "statue", "fireworks",
]
TAG_TOP_K = 6
TAG_RATIO = 0.66


def _tag_cache_path(cache) -> Path:
    key = hashlib.sha1(("\n".join(TAG_VOCAB)).encode()).hexdigest()[:10]
    slug = MODEL_ID.replace("/", "--")
    return Path(cache) / f"tag_vocab-{slug}-{key}.npz"


def op_tag_vocab_build(store, cache, vecs: str = "") -> dict:
    """Store the vocabulary matrix the page just embedded.

    Validated hard, because a wrong matrix here would mislabel every photo in
    the library for as long as the file survives: the row count must be the
    vocabulary's and the width must be the library's."""
    try:
        mat = np.asarray(json.loads(vecs), dtype=np.float32)
    except (ValueError, TypeError):
        raise ApiError("bad vector", "The label vectors could not be read.")
    if mat.ndim != 2 or mat.shape[0] != len(TAG_VOCAB):
        raise ApiError("bad vector",
                       f"Expected {len(TAG_VOCAB)} label vectors, "
                       f"got {mat.shape[0] if mat.ndim == 2 else '?'}.")
    if mat.shape[1] != EMBED_DIMS:
        raise ApiError("model mismatch",
                       f"Labels were embedded to {mat.shape[1]} dimensions, "
                       f"but this library uses {EMBED_DIMS}.")
    out = _tag_cache_path(cache)
    # Handed an open file rather than a path: np.savez APPENDS ".npz" to a path
    # that does not already end in it, so a `.tmp` name would be written as
    # `.tmp.npz` and the os.replace below would fail on a file that isn't there.
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            np.savez(f, mat=mat)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return {"built": True, "labels": len(TAG_VOCAB), "dims": int(mat.shape[1])}


def op_tags(store, cache, id: int) -> dict:
    """daemon `GET /tags/<id>` — top concept labels for one photo.

    `[]` and "no such photo" are different answers, and so is `need_vocab`: an
    empty list is "this photo has no vector yet, so there is nothing to describe
    it with", which the panel says out loud rather than leaving a gap."""
    pid = int(id)
    row = store.get_photo_by_id(pid)
    if not row:
        raise ApiError("no such photo", "That photo isn’t in the library.")
    vocab = _tag_cache_path(cache)
    if not vocab.exists():
        return {"need_vocab": [lens_query.text_prompt(lab) for lab in TAG_VOCAB],
                "model": MODEL_ID, "dims": EMBED_DIMS}
    ids, mat = _load_matrix(store)
    pos = {int(i): n for n, i in enumerate(ids)}
    n = pos.get(pid)
    if n is None or mat.shape[0] == 0:
        return {"tags": []}
    with np.load(vocab) as z:
        labels = z["mat"].astype(np.float32)
    v = mat[n]
    if labels.shape[1] != v.shape[0]:
        raise ApiError("model mismatch",
                       "The cached label vectors don’t match this library.")
    scores = labels @ v
    order = np.argsort(-scores)[:max(1, TAG_TOP_K)]
    top = float(scores[order[0]])
    cut = top * TAG_RATIO if top > 0 else float("-inf")
    out = [{"label": TAG_VOCAB[i], "score": float(scores[i])}
           for i in order if float(scores[i]) >= cut]
    # A non-positive best score means the vocabulary matched nothing at all;
    # one label, honestly the closest, is the answer there.
    return {"tags": out if top > 0 else out[:1]}


# ── roots and folders ─────────────────────────────────────────────────────
def _roots_payload(store, cache) -> dict:
    """daemon.LensServer.roots_payload, unchanged.

    `exists` separates "you removed this folder" from "this folder is a drive
    that isn't plugged in". The per-folder counts are attributed by path prefix,
    deepest root first, so a file under both ~/Pictures and ~ is counted once
    and the numbers still add up to the library."""
    cfg = load_config(cache)
    roots = cfg["roots"]
    counts = {r: [0, 0] for r in roots}
    by_depth = sorted(roots, key=len, reverse=True)
    for path, is_photo in store.searchable_paths():
        for r in by_depth:
            if _under_root(path, r):
                counts[r][0] += bool(is_photo)
                counts[r][1] += 1
                break
    return {"roots": [{"path": r, "exists": os.path.isdir(r),
                       "photos": counts[r][0], "images": counts[r][1]}
                      for r in roots],
            "apple": _apple_payload(store, cache),
            "max_index_memory_gb": cfg.get("max_index_memory_gb",
                                           DEFAULT_LIMIT_GB)}


def op_roots(store, cache) -> dict:
    return _roots_payload(store, cache)


def _edited(store, cache, changed: bool) -> dict:
    """daemon.LensServer._rescanned, with one deliberate difference:
    `reindexing` is always false, because adding or removing a folder does not
    start a scan by itself any more.

    It could — `op_reindex` really launches one now — and it is left not to. The
    daemon's automatic rescan was free to it: it already held the model and the
    run was a thread. Here a scan is a detached process over every root, and
    starting one as a side effect of *browsing folders* (add, look, remove,
    add another) would mean minutes of work nobody asked for. The view already
    handles `reindexing: false` — it is what the daemon answered when a run was
    already in flight — and it offers ↻ right there, which is the ask made
    explicit rather than assumed."""
    out = _roots_payload(store, cache)
    out["changed"] = changed
    out["reindexing"] = False
    return out


def op_roots_add(store, cache, path: str = "", confirm=False) -> dict:
    """daemon `POST /roots`. Every refusal is a code the view explains rather
    than a traceback — including `confirm_home`, which asks for a second press
    because a first scan of a whole home folder must not happen by a slip."""
    want = (path or "").strip()
    if not want:
        raise ApiError("path required", "A folder path is required.")
    root = _resolve(want)
    if root is None:
        raise ApiError("invalid path", "That path isn’t one this system can open.")
    if root == os.sep:
        raise ApiError("root too broad",
                       "Indexing the whole filesystem isn’t supported — "
                       "pick a folder inside it.")
    if not os.path.isdir(root):
        raise ApiError("not a directory", f"{root} is not a folder.")
    if root == _resolve(Path.home()) and not _truthy(confirm):
        raise ApiError("confirm_home",
                       "Index your entire home folder? The first scan can "
                       "take a long time.")
    cfg = load_config(cache)
    changed = root not in cfg["roots"]
    if changed:
        cfg["roots"].append(root)
        save_config(cfg, cache)
    return _edited(store, cache, changed)


def op_roots_remove(store, cache, path: str = "") -> dict:
    """daemon `POST /roots/remove`. Matched against both the literal string and
    the normalized one, so a root stored before normalization existed is still
    removable — and a stored root that no longer normalizes must not block
    removing the others."""
    want = (path or "").strip()
    if not want:
        raise ApiError("path required", "A folder path is required.")
    norm = _resolve(want)
    if norm is None:
        raise ApiError("invalid path", "That path isn’t one this system can open.")
    wanted = {want, norm}
    cfg = load_config(cache)
    keep = [r for r in cfg["roots"]
            if r not in wanted and _resolve(r) not in wanted]
    changed = keep != cfg["roots"]
    if changed:
        cfg["roots"] = keep
        save_config(cfg, cache)
    return _edited(store, cache, changed)


def op_fs_dirs(store, cache, path: str = "") -> dict:
    """daemon `GET /fs/dirs` — subdirectories for the folder browser. Names
    only, no files, no hidden entries; an unreadable folder browses as empty
    rather than as an error."""
    try:
        base = (Path(path).expanduser() if path else Path.home()).resolve()
        if not base.is_dir():
            raise ApiError("not a directory", "That isn’t a folder.")
    except (OSError, ValueError):
        raise ApiError("not a directory", "That isn’t a folder.")
    dirs = []
    try:
        with os.scandir(base) as entries:
            for e in entries:
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir():
                        continue
                except OSError:                # a broken symlink, mid-scan
                    continue
                dirs.append({"name": e.name, "path": str(base / e.name)})
    except (OSError, ValueError):
        pass
    dirs.sort(key=lambda d: d["name"].lower())
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "dirs": dirs}


# ── config ────────────────────────────────────────────────────────────────
def op_config(store, cache, apple_photos=None, max_index_memory_gb=None) -> dict:
    """daemon `POST /config`. Two settings, both named explicitly — a generic
    "merge this JSON into the config" op would let a page rewrite `roots`
    (bypassing every check in `op_roots_add`), `model` or `port`.

    Turning Apple Photos on or off used to start a rescan, because it is a
    source of photos going in or out of the library. It cannot here, so the
    answer says `reindexing: false` and the view offers ↻."""
    if apple_photos is not None:
        want = _truthy(apple_photos)
        cfg = load_config(cache)
        changed = bool(cfg.get("apple_photos")) != want
        if changed:
            cfg["apple_photos"] = want
            save_config(cfg, cache)
        return _edited(store, cache, changed)
    if max_index_memory_gb is not None:
        try:
            gb = float(max_index_memory_gb)
        except (TypeError, ValueError):
            gb = 0.0
        if not gb > 0:
            raise ApiError("max_index_memory_gb must be a positive number",
                           "That memory limit isn’t a positive number.")
        cfg = load_config(cache)
        cfg["max_index_memory_gb"] = gb
        save_config(cfg, cache)
        # Unlike the Apple toggle this never implied a scan even on the daemon:
        # it changes what a *future* run's memory guard enforces, not which
        # photos are in the library right now.
        return {"max_index_memory_gb": gb}
    raise ApiError("nothing to set", "There was nothing to change.")


# ── indexing ──────────────────────────────────────────────────────────────
def op_reindex(store, cache) -> dict:
    """daemon `POST /reindex` — started, not performed.

    Indexing is a walk of every root, a thumbnail and a vector per new file and
    then the face sweep: twenty-one minutes for the catalog on disk right now.
    This call is a fresh subprocess with a 60s cap, so it cannot *be* the run —
    but it can start one that outlives it, which is what `scripts/index_worker.py`
    is (read its docstring for the whole arrangement). `start_new_session=True`
    is the part that matters here: without its own session the worker is in this
    subprocess's process group and dies with it, seconds into a run.

    What comes back is `{"started": true, "job_id": …}` — the shape the ↻ button
    already reads, and the *only* thing it treats as permission to start its
    spinner. Every refusal below is therefore a refusal in the view too, which
    is why each one is a sentence rather than a code: nothing may answer
    `started` for a run that did not begin.
    """
    cfg = load_config(cache)
    roots = [r for r in cfg["roots"] if isinstance(r, str) and r]
    if not roots:
        raise ApiError(
            "no folders",
            "There are no photo folders to scan yet — add one from the ⚙ menu "
            "and the scan will have somewhere to look.")

    live = _live_run(cache)
    if live is not None:
        # Not an error the user caused, and not a start either: the run they are
        # asking for is already going. Said with its progress, because "already
        # indexing" with no number reads as a stuck button.
        done, total = live.get("done") or 0, live.get("total") or 0
        where = f" ({done} of {total})" if total else ""
        raise ApiError("already indexing",
                       f"A scan is already running{where} — it will finish on "
                       "its own, and you can stop it from the activity list.")

    reason = _embeddings_unavailable()
    if reason:
        raise ApiError("indexing unavailable", reason)

    worker = _REPO / "scripts" / "index_worker.py"
    if not worker.exists():
        raise ApiError("indexing unavailable",
                       f"The index worker is missing from this checkout "
                       f"({worker}).")

    log = Path(cache) / INDEX_LOG_FILE
    try:
        # Appended rather than truncated, and it is the only record of a run
        # that failed before it could report anything — a traceback out of the
        # worker's own imports, for one, which no job row would ever carry.
        handle = open(log, "a")
    except OSError as exc:
        raise ApiError("indexing unavailable",
                       f"Could not open the index log at {log}: {exc}")
    try:
        proc = subprocess.Popen(
            [_worker_python(), str(worker)],
            stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Detached from this subprocess's process group, so the run survives
            # this call returning — and survives the page navigating away, which
            # is the whole point of a worker.
            start_new_session=True,
            cwd=str(_REPO))
    except OSError as exc:
        raise ApiError("indexing unavailable",
                       f"Could not start the index worker: {exc}")
    finally:
        # This process's handle only; the child holds its own.
        handle.close()
    return {"started": True, "job_id": INDEX_JOB_ID, "pid": proc.pid,
            "log": str(log)}


def _worker_python() -> str:
    """The interpreter to run the worker under, and it is NOT this one.

    This file runs on fused-render's interpreter, which ships numpy and Pillow
    and nothing of lens's own (see the module docstring) — no pillow-heif, no
    av, no psutil. The worker needs all three: HEIC is most of a real photo
    library, video keyframes are av, and the memory guard is psutil. lens's own
    virtualenv has them, so it is what a run is launched with, and
    `sys.executable` is the fallback for a checkout that has none — where the
    worker will say what it is missing far more clearly than a guess here
    could."""
    candidate = _REPO / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _embeddings_unavailable():
    """None if fused-render can embed here, else the sentence saying why.

    Asked before the worker is spawned rather than left for it to discover,
    because the two failures read completely differently to a user: a worker
    that starts and then dies is a scan that "did nothing", while this is the
    app telling them, in the moment they pressed the button, that this machine
    has no embedding engine. The probe is `/api/ai/runtime`, which is a
    describe — it does not load anything, so it costs nothing and cannot itself
    be the thing that takes thirty seconds."""
    origin = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
    if not origin:
        return ("This build cannot start a scan: it does not know where "
                "fused-render is listening (FUSED_RENDER_ORIGIN is unset), so "
                "it has no way to reach the image model.")
    try:
        with urllib.request.urlopen(f"{origin}/api/ai/runtime", timeout=10) as r:
            described = json.load(r)
    except Exception as exc:
        return (f"This build cannot start a scan: {origin} did not answer when "
                f"asked which models it can run ({exc}).")
    runners = [x for x in (described.get("runners") or [])
               if x.get("capability") == "embeddings"]
    if any(x.get("available") for x in runners):
        return None
    # The runner's own reason, when there is one: it is the only text that says
    # what to do about it ("needs an NVIDIA GPU", "needs Linux").
    reasons = [x.get("reason") for x in runners if x.get("reason")]
    detail = f" — {reasons[0]}" if reasons else ""
    return ("Indexing needs an image-embedding model and this machine has no "
            f"engine that can run one{detail}. Your library is searchable "
            "exactly as the last index run left it.")


# ── the worker's run record (scripts/index_worker.py, restated) ────────────
def _read_run(cache):
    """The worker's run record, or None — parse failures included.

    The file is written by a process that can be killed mid-write, so an
    unreadable one means "no information", never an exception: `op=status` is
    polled every second and must not start failing because a scan was killed."""
    path = Path(cache) / INDEX_RUN_FILE
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    return record if isinstance(record, dict) else None


def _live_run(cache):
    """The record of a run that is genuinely still going, or None.

    index_worker.live_run, restated (that module imports psutil through
    lens.config, which this file may not). The three ways a `running` record is
    not a live run — dead pid, dead heartbeat, terminal state — are exactly why
    this is not just "does the file exist": a killed worker must not leave a
    spinner turning forever, and it must not block the next scan either."""
    record = _read_run(cache)
    if not record or record.get("state") != "running":
        return None
    pid = int(record.get("pid") or 0)
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)                       # exists, and we may signal it
    except ProcessLookupError:
        return None
    except OSError:
        pass                                  # alive, under another user
    if time.time() - float(record.get("updated_at") or 0) > INDEX_STALE_AFTER_S:
        return None
    return record


def _progress_payload(record):
    """`status()["progress"]` out of the worker's record.

    Key for key what daemon._progress_payload produced, because lens.html's
    `indexingLabel`/`setProgress` are the specification: `done`/`total`/`stage`
    for the bar and its two sweeps, `elapsed_s`/`eta_s` for the "3m so far,
    about 2m left" half of the line — and `eta_s` absent rather than guessed
    until there is a rate to project from (the worker applies that rule)."""
    if not record:
        return None
    return {"done": record.get("done") or 0,
            "total": record.get("total") or 0,
            "stage": record.get("stage") or STAGE_INDEX,
            "elapsed_s": record.get("elapsed_s"),
            "eta_s": record.get("eta_s")}


# ── audit and history ─────────────────────────────────────────────────────
def op_validate(store, cache) -> dict:
    """daemon `GET /validate` — the audit, run through lens.validate itself so
    the numbers are the same numbers. Slow by nature (it re-opens sampled files
    and re-runs the reverse geocode), which is why nothing polls it."""
    from lens import validate
    return validate.run(store, cache, lambda q="": _known_places(store, q))


def op_runs(store, cache, limit: int = 20) -> dict:
    """daemon `GET /runs` — the last N index runs' metrics, read straight off
    runs.jsonl. A line that cannot be parsed is skipped rather than raised on:
    one bad line must not blank the whole panel."""
    limit = max(1, min(int(limit), MAX_RUNS))
    path = Path(cache) / RUNS_HISTORY_FILE
    if not path.exists():
        return {"runs": []}
    out = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return {"runs": out}


# ── dispatch ──────────────────────────────────────────────────────────────
def _truthy(v) -> bool:
    """Params arrive as strings from a URL, so `"false"` must not be true."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


OPS = {
    "status": op_status,
    "query": op_query,
    "meta": op_meta,
    "trips": op_trips,
    "people": op_people,
    "face": op_face,
    "people_merge": op_people_merge,
    "person_rename": op_person_rename,
    "tags": op_tags,
    "tag_vocab_build": op_tag_vocab_build,
    "roots": op_roots,
    "roots_add": op_roots_add,
    "roots_remove": op_roots_remove,
    "fs_dirs": op_fs_dirs,
    "config": op_config,
    "reindex": op_reindex,
    "validate": op_validate,
    "runs": op_runs,
}


def main(op: str = "status", **kw):
    """One entry point, dispatching on `op`.

    One function rather than one file per endpoint because the page has exactly
    one seam — lens.html's `api()` — and a file per op would mean nineteen
    copies of the store-open/close and the error contract. The store is opened
    per call and closed in a finally: a subprocess that dies with a connection
    open leaves a stale -wal beside a catalog other pages read.

    A refusal comes back as `{"error": code, "message": …}` with an HTTP 200,
    which is exactly what the daemon's error bodies looked like to `api()` —
    the page reads `.error`/`.message`, never the status code. Raising instead
    would paint fused-render's red traceback overlay over a view that has a
    perfectly good way to explain itself.
    """
    handler = OPS.get(str(op))
    if handler is None:
        return {"error": "not found", "message": f"No such operation: {op!r}"}
    try:
        cache = _cache_dir()
    except OSError as exc:
        # The cache directory could not even be created — a read-only home, a
        # full disk. Third of the three states below, and the only one where
        # there is nothing for this file to open.
        return {"error": "cache unavailable",
                "message": f"lens could not create its cache directory: {exc}"}
    store = None
    try:
        # **There used to be a refusal here**, and it was the single worst thing
        # about installing lens: a `{"error": "no library"}` for every op the
        # moment `catalog.sqlite` was absent. The page turned that into "Your
        # library isn't available", so a first-run user met a broken app — and
        # worse, the refusal covered `roots_add`, `fs_dirs` and `reindex` too,
        # which are precisely the three ops that would have got them OUT of it.
        # There was no way forward from the empty state at all.
        #
        # Opening the store unconditionally is the fix. `Store.__init__` is
        # `CREATE TABLE IF NOT EXISTS` throughout, so on a machine that has
        # never indexed anything it writes an empty, schema-current catalog and
        # every op then answers truthfully about a library of zero photos. That
        # is not a side effect worth hiding from: `_cache_dir()` already creates
        # the directory on import, and an empty catalog beside an empty config
        # is what "lens is installed and has nothing yet" actually looks like.
        #
        # Which leaves three distinguishable states rather than one, each with
        # its own answer to the page:
        #
        #   1. **No store yet** — a normal first run. Not an error at all: the
        #      handler runs, and `op_status` reports `library: "empty"` (see
        #      there) so the view can render an invitation instead of a fault.
        #   2. **The store is damaged** — a catalog that is not a database, a
        #      migration that cannot be applied, an unreadable cache file. That
        #      raises out of `Store(...)` or out of the handler, and comes back
        #      as `store damaged` with the sentence sqlite gave us.
        #   3. **The environment failed to build** — no dependencies, so this
        #      module never imported and `main` was never called. Nothing here
        #      can answer that one; it surfaces to the page as a runPython
        #      rejection with no `.code`, which is exactly how the view tells
        #      "lens refused" from "nothing answered".
        store = Store(cache)
        return handler(store, cache, **kw)
    except ApiError as exc:
        return {"error": exc.code, "message": exc.message}
    except (sqlite3.Error, OSError) as exc:
        # State 2 above. A damaged catalog or an unreadable cache is a fact to
        # show, not a traceback: the view has an error state and this is what it
        # is for. The code says *damaged* rather than the old "store error" so
        # the sentence the view writes can be about repair — this is no longer
        # the code a fresh install arrives on, and it must not read like one.
        return {"error": "store damaged",
                "message": f"{exc} — the lens catalog in {cache} could not be "
                           f"read. Deleting that folder rebuilds it from your "
                           f"photo folders on the next index run."}
    except TypeError as exc:
        # An op called with a parameter it does not take — a page and a build
        # out of step. Named, because the alternative is a traceback overlay
        # over a view that would have explained it.
        return {"error": "bad request", "message": str(exc)}
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
