import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from lens import apple_photos, config, faces, memguard, metadata, persons, trips, video
from lens.thumbs import (THUMB_SIZE_DEFAULT, THUMB_VERSION, ensure_thumb,
                         ensure_thumb_from_image, thumb_path)

# The size every photo is rendered at, and the one thing that may be assumed to
# exist on disk afterwards. Re-exported from thumbs rather than repeated: an
# audit looks for the file this run wrote (see validate.embedding_integrity),
# and two spellings of 512 is one too many.
THUMB_SIZE = THUMB_SIZE_DEFAULT

# Frames sampled per video. Taken from lens/video.py rather than restated: the
# thumbnail is defined as the middle of *this* grid, and the two halves of that
# definition must not be able to drift apart.
VIDEO_FRAMES = video.KEYFRAMES_DEFAULT

# Images handed to the encoder in one call. A batch is a queue of *rows*, and a
# video row is up to VIDEO_FRAMES images, so the trigger counts images rather
# than rows: without that, sixteen videos in a row would have arrived as ninety-
# six frames in one call and the peak was several times what a still batch costs.
EMBED_BATCH_IMAGES = 16

# Vectors written to disk this often (in images embedded) instead of only at the
# end of the run. A first index of a real library is tens of minutes of GPU
# work, and anything that ends the process before the final save — an OOM kill,
# a laptop lid, ^C — used to discard every vector it had computed, so the next
# run started from zero and met the same wall again. The write is one 4MB numpy
# file; checkpointing costs nothing next to re-embedding.
CHECKPOINT_EVERY = 128

# The same protection for the face pass, counted in photos scanned. A smaller
# number because a face row is not just a vector: it is a table row too, and the
# two have to agree — a checkpoint is the only moment where they are known to
# (see _index_faces).
FACE_CHECKPOINT_EVERY = 32

# Which pass a progress report is about. An index run is two sweeps over the
# library now — read-and-embed, then find-faces — and a bar that fills, resets
# and fills again with no explanation reads as a bug. The stage travels with the
# fraction so the view can say which sweep it is watching.
STAGE_INDEX = "index"
STAGE_FACES = "faces"

# Directory names never worth walking. A whole home directory is a perfectly
# reasonable root now that folders are picked in the UI, and without this the
# scan disappears into caches, app bundles and dependency trees — none of which
# hold anyone's photos. Dot-directories (`.git`, `.venv`, `.cache`, Photos
# libraries' internals) are excluded by the hidden rule instead of by name.
SKIP_DIRS = {"node_modules", "__pycache__", "venv",
             "Library", "Applications", "System"}

# Bundles that look like directories but are one application's private storage.
# An Apple Photos library holds every original *and* every derivative render
# under Masters/resources, so walking one indexes each photo several times over
# under paths the user never chose. lens reads those libraries through their own
# database instead (lens/apple_photos.py) — which is why this stays, and must:
# the bundle is never walked, whether or not Apple ingest is switched on.
SKIP_SUFFIXES = (".photoslibrary",)

# Where the last Apple Photos sync's report is kept, so a status line can show
# "N found, M offloaded" — or the permission error that stopped it — without the
# daemon having to re-open the library to answer a poll.
APPLE_META = "apple_last"

# History of run stats, one JSON line per run, trimmed here rather than left to
# grow across months of daily reindexes. Named for what explain.html's
# performance panel reads it as: a small time series, not a log.
RUNS_HISTORY_FILE = "runs.jsonl"
RUNS_HISTORY_MAX = 200


class MemoryLimitHit(Exception):
    """Raised from inside `flush()` when the memory guard reports a hard
    breach — still over the limit on the flush right after the one that
    checkpointed and released caches. Caught in `index_once`'s own per-file
    loop, the only place that knows how to end a run early without leaving
    anything half-written."""

    def __init__(self, gb: float):
        self.gb = gb
        super().__init__(f"memory limit hit at {gb:.1f}GB")


def _prune(dirnames):
    """Drop the noise, in place: assigning to the slice is what stops os.walk
    from descending into those directories at all (rebinding the name would
    not)."""
    dirnames[:] = [d for d in dirnames
                   if not d.startswith(".") and d not in SKIP_DIRS
                   and not d.lower().endswith(SKIP_SUFFIXES)]


def scan_roots(roots):
    """Returns (found, scanned_roots): `found` is {path: (mtime, size)} for
    every media file under the roots that actually exist; `scanned_roots` is
    the subset of `roots` that were valid directories at scan time — the
    single source of truth for "was this root actually scanned", so callers
    never re-check isdir separately and race against it."""
    found = {}
    scanned_roots = []
    for root in roots:
        if not os.path.isdir(root):
            print(f"lens: skipping missing root: {root}")
            continue
        scanned_roots.append(root)
        for dirpath, dirnames, filenames in os.walk(root):
            _prune(dirnames)
            for name in filenames:
                if Path(name).suffix.lower() in metadata.MEDIA_EXTS:
                    p = os.path.join(dirpath, name)
                    try:
                        st = os.stat(p)
                    except FileNotFoundError:
                        continue
                    found[p] = (st.st_mtime, st.st_size)
    return found, scanned_roots


def _sha1_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _pool(vecs):
    """Several frame vectors → the one vector a video is stored as.

    Mean, then re-normalized. Both halves matter: the mean of unit vectors is the
    direction the clip is *about* (a frame that only appears once pulls it a
    sixth of the way, which is the weight it deserves), and it is shorter than
    unit — cosine ranking is a bare dot product against the text vector (see
    query.rank), so leaving it short would make every video score lower than
    every photograph for the same content, and re-normalizing it is what puts
    the two on the same scale.

    The tradeoff this accepts: one vector cannot say "there is a dog in second
    nine". A search for a thing that appears in one frame of a long clip is
    diluted by the other five, and the fix — keeping every frame vector and
    ranking on the best one (max-sim) — needs a second matrix, a row-to-frames
    index and a different `rank`. Deferred deliberately; the schema is
    unchanged by this design, which is what keeps that door open.

    A degenerate mean (six frames whose vectors cancel out) keeps its zero
    length rather than being divided by ~0: a zero vector scores 0 against every
    query, which is the honest answer for a clip nothing can be said about."""
    if len(vecs) == 1:
        return vecs[0]
    v = np.asarray(vecs, dtype=np.float32).mean(axis=0)
    norm = float(np.linalg.norm(v))
    if norm > 1e-8:
        v = v / norm
    return v.astype(np.float16)


def _under_root(path, root):
    path = os.path.normpath(path)
    # normpath leaves "/" as "/", and "/" + os.sep would be "//" — a prefix of
    # nothing. The filesystem root contains every absolute path, so say that
    # instead of quietly matching none of them.
    root = os.path.normpath(root).rstrip(os.sep)
    if not root:
        return path.startswith(os.sep)
    return path == root or path.startswith(root + os.sep)


def _apple_enabled(cache, apple):
    """Is Apple Photos ingest on for this run? `apple=None` asks the config,
    which is where the settings panel writes the toggle; an explicit True/False
    is for a caller that already knows (the tests, a one-off)."""
    if apple is not None:
        return bool(apple)
    return bool(config.load_config(cache).get("apple_photos"))


def _face_source(cache, row):
    """The image a photo's faces are looked for in, or None.

    Always the 512px thumbnail — which for a video is the middle keyframe the
    index already rendered from it (see thumbs.ensure_thumb_from_image). One
    source for both kinds means the face pass has no idea whether it is looking
    at a photograph or a frame, and neither re-decodes a 48-megapixel HEIC nor
    seeks into a 4K clip to find a face it can see perfectly well at 512px.

    None when the thumbnail is not there: that is a row the main pass has not
    got to yet (or one whose file has gone), and it is left for the next run
    rather than being marked as having no faces."""
    p = thumb_path(cache, row["sha1"], THUMB_SIZE)
    if not p.exists():
        return None
    try:
        with Image.open(p) as img:
            return img.convert("RGB").copy()
    except Exception:
        return None


def _index_faces(store, cache, face_model, stats, dead_faces=(), progress=None):
    """The second sweep: find the faces in every row this face model has not
    seen, and keep their vectors beside the image vectors.

    Separate from the main loop rather than folded into it, deliberately. The
    main loop's job is to make the library *searchable* — hash, metadata,
    thumbnail, vector — and that is what someone waiting on a first index is
    waiting for. Faces are a second, slower question asked of the same
    thumbnails, and asking it afterwards means a run that is killed halfway
    leaves a fully searchable library with some of its faces found, rather than a
    partly searchable one.

    The checkpoint discipline is the embed pass's, with one extra rule: the table
    row and the vector are written in that order, per photo, so the only
    inconsistency a kill can produce is a face row whose vector has not been
    saved yet — which the next run fixes by re-detecting that photo (its
    `faces_v` was never stamped). The reverse order could leave a vector keyed to
    a row id that does not exist.
    """
    pending = store.faces_pending(face_model.key)
    ids, mat = store.load_faces()
    # A generation with a different width is not a generation of *these*
    # vectors: comparing a 512-dim face against a 128-dim one is meaningless, so
    # a model change starts the matrix again rather than stacking two shapes.
    if mat.size and mat.shape[1] != face_model.dim:
        vecs = {}
    else:
        vecs = {int(i): mat[n] for n, i in enumerate(ids)}
    for fid in dead_faces:                       # photos pruned this run
        vecs.pop(int(fid), None)

    def save():
        if vecs:
            out = np.array(sorted(vecs), dtype=np.int64)
            store.save_faces(out, np.stack([vecs[i] for i in out]))
        else:
            store.save_faces(np.zeros((0,), dtype=np.int64),
                             np.zeros((0, face_model.dim), dtype=np.float16))

    # The pruning above reaches the disk before anything else happens, and
    # before the weights are asked for: a vector belonging to a deleted photo's
    # face must not survive because the face *model* turned out to be missing.
    if dead_faces or not pending:
        save()
    if not pending:
        return
    # Up front, once, rather than on the first photo — a machine without the
    # optional dependency would otherwise fail 1,800 times over and count 1,800
    # unreadable photographs, when the real answer is one sentence about pip.
    face_model.load()

    since = 0
    for n, row in enumerate(pending):
        try:
            img = _face_source(cache, row)
            if img is None:
                continue
            found = face_model.detect(img)
            crops = [faces.crop_face(img, f["bbox"]) for f in found]
            mats = face_model.embed(crops)
            old = store.photo_face_ids(row["id"])
            new_ids = store.replace_photo_faces(row["id"], found,
                                                version=face_model.key)
            for fid in old:
                vecs.pop(int(fid), None)
            for fid, vec in zip(new_ids, mats):
                vecs[int(fid)] = vec
            stats["faces"] += len(new_ids)
            stats["face_photos"] += 1
            since += 1
        except Exception as exc:
            # A photo whose faces cannot be read is not a photo that failed to
            # index: it is searchable, it has a thumbnail, and it keeps both.
            # `faces_v` is left unstamped so the next run tries again — the same
            # rule an embed failure follows — and the count is reported rather
            # than printed per file.
            stats["face_errors"] += 1
            stats.setdefault("face_error", str(exc)[:200])
            since += 1
        # The checkpoint happens *before* the report, not after: the fraction the
        # view is showing should never be ahead of what is actually on disk, and
        # a reader that acts on "4 of 9 done" (the audit, a test, the next run
        # after a kill) has to find four.
        if since >= FACE_CHECKPOINT_EVERY:
            save()
            since = 0
        if progress:
            progress(n + 1, len(pending), STAGE_FACES)
    save()


def recluster(store) -> dict:
    """Group every face vector into people, and keep the people the user knows.

    Run after each index pass, over the whole matrix rather than over what
    changed: a cluster is a fact about all the faces at once, and six new
    photographs can turn two clusters of two into one person of four. It is cheap
    — a few thousand faces against a few dozen centroids, twice — next to the
    detection that produced them.

    Everything that makes a person *a* person rather than a fresh group of
    vectors is in lens/persons.py: the centroid matching that carries an id, a
    name and a merge across the recompute. This function is the wiring, plus the
    one thing that needs the catalog: the names Apple Photos already holds.
    """
    ids, mat = store.load_faces()
    rows = store.all_faces()
    have = {int(i) for i in ids}
    labels = persons.cluster(ids, mat)
    # A face row whose vector never made it to disk (a kill between the two
    # writes) belongs to nobody until it is re-detected. Said explicitly rather
    # than left out: left out, it would keep whichever person it was assigned to
    # last run, and that assignment is no longer supported by anything.
    for r in rows:
        if r["id"] not in have:
            labels[r["id"]] = None
    prev = store.get_persons(include_merged=True)
    people, face_person = persons.assign_persons(labels, ids, mat, prev)

    # A name Photos has already written on a face, taken only where it can mean
    # exactly one thing: a photograph with a single detected face and a single
    # name on it. A group shot's five names against five faces is a permutation
    # problem, and guessing it would put the wrong name on somebody.
    single = {}
    per_photo = {}
    for r in rows:
        per_photo.setdefault(r["photo_id"], []).append(r["id"])
    lone = {pid: fids[0] for pid, fids in per_photo.items() if len(fids) == 1}
    if lone:
        for pid, names in store.apple_persons(lone).items():
            if len(names) == 1:
                single[lone[pid]] = names[0]
    seeded = persons.seed_names(people, face_person, single)

    store.replace_persons(people)
    for pid, name in seeded.items():
        store.set_person_name(pid, name)
    store.set_face_persons(face_person)
    return {"people": len(people),
            "clustered": sum(1 for v in face_person.values() if v is not None),
            "named": len(seeded)}


def _append_run_history(cache, stats) -> None:
    """One line of this run's metrics, appended to `<cache>/runs.jsonl` and
    trimmed to the last RUNS_HISTORY_MAX — explain.html's performance panel
    reads this file for real numbers rather than trusting a page that could
    say anything. Written even for an aborted run: a run that hit the memory
    limit is exactly the kind of history that panel exists to show.

    Same atomic-write shape as config.save_config (tmp file, then
    os.replace): a reader must never see a half-written file, and two runs
    finishing close together must not interleave their writes into one
    corrupt line."""
    path = Path(cache) / RUNS_HISTORY_FILE
    line = json.dumps({
        "at": datetime.now().isoformat(timespec="seconds"),
        "duration_s": stats.get("duration_s"),
        "stages": stats.get("stages"),
        "rate": stats.get("rate"),
        "mem_peak_gb": stats.get("mem_peak_gb"),
        "embedded": stats.get("embedded"),
        "errors": stats.get("errors"),
        "error": stats.get("error"),
    })
    lines = []
    if path.exists():
        try:
            lines = path.read_text().splitlines()
        except OSError:
            lines = []
    lines.append(line)
    lines = lines[-RUNS_HISTORY_MAX:]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def index_once(store, roots, embedder, cache, progress=None, apple=None,
               face_model=None, mem_guard=None):
    t_run = time.monotonic()
    # Time spent in each part of the run, reported on `stats["stages"]` so a
    # settings panel (or a slow run someone is trying to understand) can see
    # *where* the minutes went rather than only that there were some. Kept as
    # one dict threaded through the function rather than seven local floats,
    # because every call site below only ever adds to one of these and reads
    # none of them back until the end.
    stages = dict(walk_s=0.0, metadata_s=0.0, thumbs_s=0.0, embed_s=0.0,
                  faces_s=0.0, trips_s=0.0, apple_s=0.0)
    stats = dict(added=0, changed=0, removed=0, moved=0,
                 skipped=0, embedded=0, errors=0,
                 # the face pass's own counts, reported separately because they
                 # describe a different sweep over the same library: how many
                 # rows it scanned, how many faces that found, and how many rows
                 # it could not read (which is not the same as a file lens could
                 # not open — see _index_faces)
                 faces=0, face_photos=0, face_errors=0,
                 mem_peak_gb=0.0)

    # `mem_guard=None` (every real caller) reads the limit from config on
    # every run, the same way `_apple_enabled` reads its toggle — so a
    # setting changed through the panel takes effect on the *next* run
    # without the daemon having to be told separately. Tests hand in a
    # MemGuard built on a fake footprint function, which is the only way to
    # exercise a breach without actually allocating gigabytes.
    if mem_guard is None:
        limit_gb = config.load_config(cache).get(
            "max_index_memory_gb", memguard.DEFAULT_LIMIT_GB)
        mem_guard = memguard.MemGuard(limit_gb)

    t0 = time.monotonic()
    on_disk, valid_roots = scan_roots(roots)
    stages["walk_s"] = round(time.monotonic() - t0, 3)

    # Apple Photos, when it is switched on: the library's own database is asked
    # which files are the originals, and those files join `on_disk` as if the
    # walker had found them. Everything downstream — the (path, mtime, size)
    # skip, the sha1 move detection, thumbnails, embedding, checkpointing — then
    # applies to them unchanged, because they *are* ordinary files; the only
    # thing special about them is how they were discovered and that Photos knows
    # more about them than their EXIF does (see apple_photos.merge).
    #
    # PhotosDB takes seconds to load on a real library, so it is opened exactly
    # once per run, and only when the feature is on.
    apple_on = _apple_enabled(cache, apple)
    apple_items, apple_report = [], None
    if apple_on:
        t0 = time.monotonic()
        apple_items, apple_report = apple_photos.enumerate_library()
        apple_report["seconds"] = round(time.monotonic() - t0, 1)
        stages["apple_s"] = apple_report["seconds"]
        if apple_report["error"]:
            print(f"lens: Apple Photos: {apple_report['error']}")
    # An item with no path is an offloaded original: it is in the library (so its
    # uuid counts as live, below) but there is no file to read this run.
    apple_by_path = {}
    for it in apple_items:
        if it.path:
            apple_by_path[it.path] = it
            on_disk[it.path] = it.sig

    known = store.path_signatures()
    errored_paths = {r["path"] for r in store.query_photos("error IS NOT NULL", [])}

    # Swapping the embedding model invalidates every stored vector: they have
    # the old model's dimensionality (np.stack would raise on the mix) and its
    # coordinate space (cosine against the new text tower is meaningless).
    # Re-embed the whole library rather than leave a half-converted matrix.
    model_key = getattr(embedder, "key", None)
    prev_model = store.get_meta("embed_model")
    model_changed = prev_model is not None and prev_model != model_key
    if model_changed:
        print(f"lens: embedding model changed ({prev_model} → {model_key}); "
              "re-embedding the whole library")

    # Vectors are computed from the *thumbnail*, never from the original file,
    # so a change to how thumbs are rendered changes what the image encoder
    # saw — the stored vector describes an image lens no longer produces.
    # Transparency compositing onto black (thumb v1) is exactly that case: it
    # embedded a black rectangle for every transparent PNG. The bump is the
    # signal to redo them.
    thumb_key = str(THUMB_VERSION)
    prev_thumb = store.get_meta("thumb_version")
    # `known` empty means a fresh catalog — nothing stale to redo, and no
    # "changed" message to print about a library that does not exist yet. A
    # catalog with rows but no recorded version predates versioning, so it is
    # stale by definition.
    thumbs_changed = bool(known) and prev_thumb != thumb_key
    if thumbs_changed:
        print(f"lens: thumbnail rendering changed ({prev_thumb} → {thumb_key}); "
              "re-embedding the whole library")
    rebuild = model_changed or thumbs_changed

    ids, mat = store.load_embeddings()
    if rebuild or mat.size == 0:
        ids = np.zeros((0,), dtype=np.int64)
        mat = np.zeros((0, embedder.dim), dtype=np.float16)
    emb = {int(i): mat[n] for n, i in enumerate(ids)}
    # `rebuild` is committed to disk before any work, not after it, so that the
    # invariant holds at every instant: *the stored matrix only ever contains
    # vectors valid for the recorded model and thumb version.* Recording the
    # versions at the end instead meant a run killed halfway left them unset,
    # the next run called the whole library stale again, and no amount of
    # checkpointing could ever make progress — each attempt started over and met
    # the same kill. Discarding vectors we have already declared invalid costs
    # nothing; leaving them behind a recorded version would silently keep them
    # forever.
    if rebuild:
        wipe_ids = np.zeros((0,), dtype=np.int64)
        store.save_embeddings(wipe_ids, np.zeros((0, embedder.dim), dtype=np.float16))
    if model_key is not None:
        store.set_meta("embed_model", str(model_key))
    store.set_meta("thumb_version", thumb_key)

    # A row with an error flag must be retried every run (transient embed
    # failures shouldn't drop photos forever just because their on-disk
    # signature hasn't changed since the failed attempt).
    #
    # So must a row that has no vector: the catalog and the embedding matrix
    # are separate files that can fall out of step — a model swap performed
    # while a root was offline drops the vectors for photos that were never
    # rescanned, and a run killed mid-flight leaves the same gap. Without this
    # those rows match on signature forever and are never backfilled, so they
    # stay permanently unreachable by semantic search.
    path_id = store.path_ids()
    todo = [p for p, sig in on_disk.items()
            if rebuild or known.get(p) != sig or p in errored_paths
            or path_id.get(p) not in emb]
    stats["skipped"] = len(on_disk) - len(todo)
    # A catalogued file that isn't on disk this run is pruned for one of two
    # reasons, and the case in between them must be left alone:
    #   * its root was scanned and the file is genuinely gone → prune;
    #   * no configured root covers it any more, because the user removed that
    #     folder from the library → prune, that is what removing a folder means;
    #   * its root is configured but missing right now (an unmounted drive) →
    #     keep. Nothing was scanned there, so "absent" says nothing about the
    #     files, and they must survive to be found again when it comes back.
    #
    # Apple rows are exempt from all of that and pruned by the rule below
    # instead: they live inside a bundle no root covers, so the "no configured
    # root any more" clause would delete every one of them on every run.
    apple_known = store.apple_paths()             # {path: apple_uuid}
    removed_paths = [p for p in known
                      if p not in on_disk and p not in apple_known
                      and (any(_under_root(p, r) for r in valid_roots)
                           or not any(_under_root(p, r) for r in roots))]

    # An Apple row is pruned when its photo has left the library — the uuid the
    # row was built from is no longer in PhotosDB — or when the feature has been
    # switched off, which means the same thing as removing a folder does.
    #
    # Absence from `on_disk` is deliberately *not* the test here: iCloud can
    # offload an original between two runs, and that photo is still in the
    # user's library. Same rule as an unmounted drive — nothing was read, so
    # nothing is known, so the row stays (and its thumbnail still shows it).
    #
    # An enumeration that failed prunes nothing at all: "macOS would not let us
    # read the library" is not evidence that the library is empty, and treating
    # it as such would delete a whole library's rows on a permissions slip.
    if apple_on and apple_report and not apple_report["error"]:
        live = {it.uuid for it in apple_items}
        removed_paths += [p for p, uuid in apple_known.items()
                          if p not in on_disk and uuid not in live]
    elif not apple_on:
        removed_paths += [p for p in apple_known if p not in on_disk]

    removed_by_sha = {}
    for p in removed_paths:
        row = store.get_photo(p)
        if row and row.get("sha1"):
            removed_by_sha[row["sha1"]] = row

    # The queue waiting to be embedded: one entry per catalog row, holding the
    # image(s) that describe it — one for a still, up to VIDEO_FRAMES for a
    # video. Frames travel together because they have to be pooled together
    # (see _pool); splitting them across two batches would lose which row they
    # belonged to.
    pending, pending_imgs = [], 0
    since_save = 0

    def save_vectors():
        """Write the vectors we hold. Keys are photo row ids, so a partial
        matrix is still a consistent one: the rows it lacks are exactly the rows
        the next run re-embeds (the `pid not in emb` clause in `todo`), so a
        checkpoint always leaves the catalog resumable rather than half-valid."""
        if emb:
            ids_out = np.array(sorted(emb), dtype=np.int64)
            store.save_embeddings(ids_out, np.stack([emb[i] for i in ids_out]))
        else:
            store.save_embeddings(np.zeros((0,), dtype=np.int64),
                                  np.zeros((0, embedder.dim), dtype=np.float16))

    def flush():
        nonlocal since_save, pending_imgs
        if not pending:
            return
        # One call for the whole batch, with a note of which slice of the answer
        # belongs to which row: a video's frames are contiguous in `imgs`, so
        # pooling is a slice rather than a second bookkeeping structure.
        imgs, spans = [], []
        for pid, frames in pending:
            spans.append((pid, len(imgs), len(frames)))
            imgs.extend(frames)
        t0 = time.monotonic()
        try:
            vecs = embedder.embed_images(imgs)
        except Exception as exc:                      # embed failure: flag
            for pid, _ in pending:                     # only this batch,
                stats["errors"] += 1                   # never the unrelated
                emb.pop(pid, None)                      # file being scanned
                row = store.get_photo_by_id(pid)
                if row:
                    row["error"] = str(exc)[:500]
                    store.upsert_photo(row)
        else:
            for pid, start, n in spans:
                emb[pid] = _pool(vecs[start:start + n])
            stats["embedded"] += len(pending)
        stages["embed_s"] += time.monotonic() - t0
        # Counted in *images*, not rows, which is what CHECKPOINT_EVERY has always
        # meant and now matters: a video row is six frames and a decode, so it is
        # tens of times the work of a photograph. Counting rows would have stretched
        # the interval between checkpoints from a minute to half an hour on a
        # library of clips — and this daemon has already been killed mid-run once,
        # losing everything since the last save.
        since_save += len(imgs)
        pending.clear()
        pending_imgs = 0
        if since_save >= CHECKPOINT_EVERY:
            save_vectors()
            since_save = 0
        # The memory guard's own checkpoint discipline, layered on top of the
        # ordinary one above rather than replacing it: CHECKPOINT_EVERY exists
        # to bound how much work an unrelated kill can lose, and a breach is a
        # *foreseen* stop, so it earns an unconditional save regardless of
        # where `since_save` happens to be — the point of noticing early is to
        # act on it immediately, not to wait for the next scheduled save.
        status, gb = mem_guard.check()
        stats["mem_peak_gb"] = max(stats["mem_peak_gb"], gb)
        if status == "soft":
            save_vectors()
            since_save = 0
            memguard.release()
            print(f"lens: memory guard: {gb:.1f}GB over the "
                  f"{mem_guard.limit_gb:.1f}GB limit — checkpointed and "
                  "released caches")
        elif status == "hard":
            save_vectors()
            raise MemoryLimitHit(gb)

    def queue(pid, frames):
        nonlocal pending_imgs
        pending.append((pid, frames))
        pending_imgs += len(frames)
        if pending_imgs >= EMBED_BATCH_IMAGES:
            flush()

    # Set the moment a hard breach is reported (see the `except MemoryLimitHit`
    # clause below), and never otherwise. A `break` rather than letting the
    # exception propagate: the checkpoint it triggered has already run inside
    # `flush()`, by the time this loop sees it there is nothing further to
    # unwind, and every line after the loop already knows how to skip itself
    # on this one flag rather than needing a second try/except wrapped around
    # a block that would otherwise have to be re-indented wholesale.
    aborted_gb = None
    for n, path in enumerate(sorted(todo)):
        try:
            t0 = time.monotonic()
            sha1 = _sha1_file(path)
            rec = metadata.extract(path)
            rec["sha1"] = sha1
            # metadata.extract runs for an Apple original too — the file is
            # where the dimensions, the format and the whole raw tag dump come
            # from — and then Photos' own answers are folded over the two fields
            # it knows better (see apple_photos.merge).
            if path in apple_by_path:
                apple_photos.merge(rec, apple_by_path[path])
            stages["metadata_s"] += time.monotonic() - t0
            old = removed_by_sha.pop(sha1, None)
            was_known = path in known
            pid = store.upsert_photo(rec)
            if old:                                   # move: reuse embedding
                if old["id"] in emb:
                    emb[pid] = emb.pop(old["id"])
                removed_paths.remove(old["path"])
                store.remove_paths([old["path"]])
                stats["moved"] += 1
            elif was_known:
                stats["changed"] += 1
            else:
                stats["added"] += 1
            # A file whose bytes have not moved and whose vector is already on
            # disk needs neither of the two expensive things below.
            fresh = pid not in emb or (was_known and known[path] != on_disk[path])
            t0 = time.monotonic()
            if rec.get("kind") == "video":
                # Decoding is the cost here, not embedding — a 4K clip is a
                # second or two — so the frames are pulled once and used twice:
                # the middle one becomes the thumbnail, all of them become the
                # pooled vector. Skipped entirely when the vector is current and
                # the thumbnail is already on disk.
                if fresh or not thumb_path(cache, sha1, THUMB_SIZE).exists():
                    frames = video.keyframes(path, VIDEO_FRAMES, size=THUMB_SIZE)
                    ensure_thumb_from_image(frames[len(frames) // 2], cache,
                                            sha1, THUMB_SIZE)
                    stages["thumbs_s"] += time.monotonic() - t0
                    if fresh:
                        queue(pid, frames)     # may flush, which may abort
                else:
                    stages["thumbs_s"] += time.monotonic() - t0
            else:
                thumb = ensure_thumb(path, cache, sha1, THUMB_SIZE)
                stages["thumbs_s"] += time.monotonic() - t0
                if fresh:
                    with Image.open(thumb) as t:
                        queue(pid, [t.convert("RGB").copy()])  # may flush
        except MemoryLimitHit as exc:
            # Raised from inside `queue()` → `flush()`, several frames below
            # this `try` — caught here rather than at `flush()`'s own call site
            # so it cannot be relabelled as "this file is corrupt" by the
            # generic clause right below (that one is for a file lens failed
            # to read; this is the run stopping on purpose). `flush()` has
            # already checkpointed everything it holds by the time this fires
            # — see the "hard" branch there — so there is nothing left to do
            # for *this* photo; the file itself is neither added, changed nor
            # errored this run, and the next run picks it up exactly as if
            # this one had not reached it yet.
            aborted_gb = exc.gb
            break
        except Exception as exc:                      # corrupt file, flag, continue
            stats["errors"] += 1
            stats["added"] += 0 if path in known else 1
            # `kind` even on the error path: a video nothing can decode is still
            # a video, and a row that claimed to be an image would sit in the
            # photographs' scope counts (see store.scope_counts) describing a
            # file that is not one.
            row = {"path": path, "size": on_disk[path][1],
                   "mtime": on_disk[path][0], "raw_exif": "{}",
                   "kind": metadata.kind_for(path),
                   "error": str(exc)[:500]}
            # An Apple original that cannot be opened is still an Apple row: it
            # has to keep saying so, or the pruner above would stop recognising
            # it as one and the folder rule would delete it on the next run.
            it = apple_by_path.get(path)
            if it is not None:
                row.update(source="apple", apple_uuid=it.uuid,
                           apple_text=apple_photos.phrases(it) or None)
            pid = store.upsert_photo(row)
            emb.pop(pid, None)                         # drop any stale embedding
        if progress:
            progress(n + 1, len(todo), STAGE_INDEX)
    if aborted_gb is None:
        # The tail end of `todo` — fewer than EMBED_BATCH_IMAGES images,
        # otherwise `queue()` would already have flushed it — goes through
        # the same guard as every other flush: a breach found only here (the
        # loop above never got another chance to check) must abort exactly
        # like one found mid-loop, not escape as an uncaught exception.
        try:
            flush()
        except MemoryLimitHit as exc:
            aborted_gb = exc.gb

    def _finish(stats):
        """The four run-metrics fields, and the history line they end up in —
        shared between the normal return and the aborted one below, so
        neither path can add one without the other."""
        stats["duration_s"] = round(time.monotonic() - t_run, 3)
        stats["stages"] = {k: round(v, 3) for k, v in stages.items()}
        # "files/s" in the embed stage specifically: it is the stage the daemon
        # actually blocks on for search-readiness, and walk/metadata/thumbs
        # times swing wildly with how much of the run was cache hits.
        stats["rate"] = (round(stats["embedded"] / stages["embed_s"], 2)
                          if stages["embed_s"] > 0 else 0.0)
        # The guard's own high-water mark and the OS's since-process-start one,
        # whichever is larger: a run that never breached still climbed toward
        # *something*, and ru_maxrss caught it even on the flushes this run's
        # own MemGuard never got to see (the tail past the last embed batch,
        # for one).
        stats["mem_peak_gb"] = round(
            max(stats["mem_peak_gb"], memguard.peak_rss_gb()), 2)
        _append_run_history(cache, stats)
        return stats

    if aborted_gb is not None:
        # Everything below this point — pruning, trips, faces, the Apple
        # report — is additional work over a catalog that is *already*
        # consistent (see `flush()`'s "hard" branch): stopping here is not
        # leaving anything half-done, it is choosing not to start more work
        # while memory is still the reason this run stopped. The next run (or
        # one started after `max_index_memory_gb` is raised) resumes exactly
        # where this one left off — the unfinished rows in `todo` are still
        # unfinished, which is what makes them `todo` again.
        stats["error"] = (
            f"memory limit hit at {aborted_gb:.1f}GB — run aborted safely, "
            "progress saved; raise max_index_memory_gb in config or reindex "
            "to continue")
        print(f"lens: {stats['error']}")
        return _finish(stats)

    # Face rows are deleted with their photographs (store.remove_paths), but
    # their *vectors* are a file the face pass owns — so the ids are read while
    # the rows still exist and handed to it to drop.
    dead_faces = store.face_ids_for_paths(removed_paths) if removed_paths else []
    if removed_paths:
        for p in removed_paths:
            row = store.get_photo(p)
            if row:
                emb.pop(row["id"], None)
        store.remove_paths(removed_paths)
        stats["removed"] = len(removed_paths)

    save_vectors()               # the authoritative one: prunes as well as adds

    # Photographs and videos — the things that were actually shot somewhere.
    # The clause lives in lens/trips.py beside the rule it feeds, because the
    # audit re-runs the same computation over the same rows to check this answer
    # is still current (see trips.TRIP_ROWS_WHERE).
    t0 = time.monotonic()
    trip_rows = store.query_photos(trips.TRIP_ROWS_WHERE, [])
    trip_list, assign = trips.compute_trips(trip_rows)
    store.replace_trips(trip_list, assign)
    stages["trips_s"] = round(time.monotonic() - t0, 3)

    # ...and then the people, which is the slow sweep and so goes last: by the
    # time it starts, everything a search needs is already on disk.
    #
    # The whole stage is best-effort. The models are an optional dependency
    # (see faces.INSTALL_HINT), the weights are a download, and neither "this
    # machine has no facenet" nor "the first face crop raised" is a reason to
    # fail an index run that has just made 1,800 photographs searchable. It is
    # reported on the run's own stats instead, where the status line can say so.
    t0 = time.monotonic()
    try:
        model = face_model if face_model is not None else faces.model()
        _index_faces(store, cache, model, stats, dead_faces, progress)
    except Exception as exc:
        stats["faces_error"] = str(exc)[:300]
        print(f"lens: faces: {exc}")
    stages["faces_s"] = round(time.monotonic() - t0, 3)
    # Clustered whatever happened above: the faces already on disk are still
    # faces, a pruned photo's people still have to lose them, and a library whose
    # face model is missing must not also lose the people it found last week.
    try:
        stats["people"] = recluster(store)
    except Exception as exc:
        stats.setdefault("faces_error", str(exc)[:300])
        print(f"lens: people: {exc}")

    # What the Photos sync did, kept where a poll can read it. Written after the
    # rows exist, so `indexed` is a count of the catalog rather than a promise
    # about one: an interrupted run leaves the previous report standing, which is
    # the honest thing — it describes the last sync that finished.
    if apple_report is not None:
        apple_report["indexed"] = store.source_counts().get("apple", 0)
        apple_report["at"] = datetime.now().isoformat(timespec="seconds")
        store.set_meta(APPLE_META, json.dumps(apple_report))
        stats["apple"] = apple_report
    elif store.get_meta(APPLE_META):
        # Switched off: the rows are gone (pruned above) and last time's counts
        # would keep describing a library lens is no longer reading.
        store.set_meta(APPLE_META, "")
    return _finish(stats)
