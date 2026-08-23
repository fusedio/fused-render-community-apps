import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lens import config, indexer, memguard, query, tags, validate
from lens.store import Store
from lens.thumbs import FACE_SIZE_DEFAULT, ensure_face_thumb, ensure_media_thumb

# The largest /runs will hand back regardless of what a query string asks
# for. Matches indexer.RUNS_HISTORY_MAX — there is never more history than
# that on disk to serve, so an unbounded request just wastes a comparison.
MAX_RUNS = indexer.RUNS_HISTORY_MAX

LIGHTBOX_SIZE = 2048
# The two sizes a face crop is served at: the card avatar, and twice that for a
# retina card. Bounded to a pair for the same reason /thumb is — a size in a URL
# is a request for work, and an unbounded one is a request for arbitrary work.
FACE_SIZES = (FACE_SIZE_DEFAULT, FACE_SIZE_DEFAULT * 2)
# What a person with no name is called, in one place. The view prints the same
# thing, but a client that only speaks JSON (curl, a future CLI) should not have
# to invent it.
UNNAMED = "Person"
# Longest name a person may be given. A name is a label on a card and a phrase in
# the query vocabulary, not a document.
MAX_NAME = 80
# The largest page /query will serve. Matches the view's own ceiling, so a
# hand-made URL cannot ask this daemon for more work than the UI ever will.
MAX_LIMIT = 2000
# a JSON body here is one path; anything larger is not a client we wrote
MAX_BODY = 64 * 1024

# A loopback daemon is reachable from every page in the user's browser, and
# this one hands out photo paths, GPS coordinates and image bytes. Only a
# page served from loopback may read them; only a request that addressed us
# as loopback is talking to us on purpose (see Handler._origin, _host_ok).
# Matched with fullmatch(), never `$`: `$` also matches just before a trailing
# newline, so "http://localhost\n<something>" would have slipped through.
_LOOPBACK = r"(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?"
_LOOPBACK_ORIGIN = re.compile(rf"https?://{_LOOPBACK}", re.I)
_LOOPBACK_HOST = re.compile(_LOOPBACK, re.I)


class RootError(Exception):
    """A refused root. `code` is what the view branches on (`confirm_home` asks
    for a second press), `message` is what it shows."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _resolve(path):
    """The normalized form of `path`, or None when the OS will not even
    consider it — a null byte, an over-long name, a symlink loop. Those are
    the caller's 400, not a traceback."""
    try:
        return config.normalize_root(path)
    except (OSError, ValueError):
        return None


class LensServer:
    """Owns store + embedder + index thread; serves the HTTP API.

    Store access itself is safe under concurrent HTTP-handler threads and
    the index thread: `Store` guards each of its own public methods with a
    per-call lock (see lens/store.py), so readers can observe partial state
    mid-index instead of being blocked out for the whole run. `_lock` here
    only guards the `_indexing` flag's test-and-set."""

    def __init__(self, cache: Path, roots: list = None, embedder=None,
                 port: int = 8877, face_model=None):
        self.cache = Path(cache)
        self.embedder = embedder
        # None means "the process-wide one, on first use" (see faces.model).
        # Injected only by tests, which hand in a deterministic stand-in for two
        # models and a few gigabytes of weights.
        self.face_model = face_model
        self.store = Store(self.cache)
        # The concept vocabulary behind /tags, and the answers it has already
        # given. Built lazily (see _warm_text_encoder), so constructing a server
        # never touches the model.
        self._tags = tags.TagIndex(embedder)
        self._tag_cache = {}
        self._indexing = False
        self._last_stats = None
        # (done, total, stage) of the run in flight, straight off the indexer's
        # own progress callback. (0, 0, …) is the scan phase — it walks the whole
        # tree before it knows how many files there are to do, and claiming a
        # fraction we cannot compute would be worse than admitting we can't.
        # `stage` says which of the run's two sweeps this is (see
        # indexer.STAGE_FACES), so a bar that fills twice can say why.
        self._progress = None
        # When the run in flight started, in monotonic seconds — read only to
        # compute `progress.elapsed_s`/`eta_s` (see status()). None between
        # runs, same as `_progress`; kept as its own field rather than folded
        # into the `_progress` tuple because it is set once, at the top of a
        # run, while `_progress` is overwritten on every callback.
        self._run_start = None
        self._lock = threading.Lock()
        self._cfg_lock = threading.Lock()
        self._index_thread = None
        self._serving = threading.Event()
        # Roots are not state on this object: the view edits them through
        # /roots while the daemon runs, so a list captured here would go stale
        # after the first change (see current_roots). An explicit `roots=`
        # seeds the config file instead — the CLI never needs it, tests use it
        # to pin a temp corpus.
        if roots is not None:
            cfg = config.load_config(self.cache)
            cfg["roots"] = [str(r) for r in roots]
            config.save_config(cfg, self.cache)
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = self._httpd.server_address[1]

    # -- roots ------------------------------------------------------------
    def current_roots(self) -> list:
        """The configured roots, re-read from disk on every call, so an index
        run always scans what the config says right now."""
        return config.load_config(self.cache)["roots"]

    def roots_payload(self) -> dict:
        """`exists` separates "you removed this folder" from "this folder is a
        drive that isn't plugged in": the second keeps its photos in the
        library, and the view says so.

        `photos`/`images` are what this folder actually contributed. A settings
        panel that lists paths and nothing else cannot answer the only question
        anyone opens it with — is this folder the one my photos are in? — and
        the counts are the answer. Attributed by path prefix rather than by a
        stored root id, because a file belongs to whichever root contains it
        and that changes as roots are added and removed."""
        roots = self.current_roots()
        counts = {r: [0, 0] for r in roots}
        # deepest first, so a file under both ~/Pictures and ~ is attributed to
        # ~/Pictures and the two counts still add up to the library
        by_depth = sorted(roots, key=len, reverse=True)
        for path, is_photo in self.store.searchable_paths():
            for r in by_depth:
                if indexer._under_root(path, r):
                    counts[r][0] += bool(is_photo)
                    counts[r][1] += 1
                    break
        return {"roots": [{"path": r, "exists": os.path.isdir(r),
                           "photos": counts[r][0], "images": counts[r][1]}
                          for r in roots],
                "apple": self.apple_payload(),
                # Ridden along here rather than given its own GET: the panel
                # already fetches /roots on open and after every edit (see
                # loadRoots(), afterEdit()), and the memory limit is exactly
                # the same kind of setting the Apple Photos box already is —
                # a fact worth showing next to the folders it governs.
                "max_index_memory_gb": config.load_config(self.cache).get(
                    "max_index_memory_gb", memguard.DEFAULT_LIMIT_GB)}

    def apple_payload(self) -> dict:
        """The Apple Photos section of the settings panel, in one object.

        `enabled` is the config; everything under `last` is what the most recent
        sync reported (found / local / offloaded / movies / error / when), read
        from the catalog rather than by re-opening the library — a settings panel
        must not cost seconds of PhotosDB load, and a poll must not cost it
        repeatedly.

        `rows` is the catalog's own count of Apple rows, which is the number the
        user can actually search. It is deliberately separate from
        `last["indexed"]`: the two differing means a sync is in flight or was
        interrupted, and neither one lying about it is worth the tidier shape."""
        cfg = config.load_config(self.cache)
        last = None
        stored = self.store.get_meta(indexer.APPLE_META)
        if stored:
            try:
                last = json.loads(stored)
            except ValueError:
                last = None
        return {"enabled": bool(cfg.get("apple_photos")),
                "rows": self.store.source_counts().get("apple", 0),
                "last": last if isinstance(last, dict) else None}

    def set_apple(self, enabled: bool) -> dict:
        """Turn Apple Photos ingest on or off, and rescan if that changed
        anything. Same contract as an added folder (see _rescanned), because it
        is the same kind of edit: a source of photos going in or out of the
        library, with a scan behind it to make the catalog say so."""
        with self._cfg_lock:
            cfg = config.load_config(self.cache)
            changed = bool(cfg.get("apple_photos")) != bool(enabled)
            if changed:
                cfg["apple_photos"] = bool(enabled)
                config.save_config(cfg, self.cache)
        return self._rescanned(changed)

    def set_memory_limit(self, gb: float) -> dict:
        """Write `max_index_memory_gb`. Unlike `set_apple`/`add_root`, this
        never starts a scan: it changes what a *future* run's memory guard
        enforces (index_once reads it fresh — see indexer.index_once), not
        anything about which photos are in the library right now."""
        with self._cfg_lock:
            cfg = config.load_config(self.cache)
            cfg["max_index_memory_gb"] = gb
            config.save_config(cfg, self.cache)
        return {"max_index_memory_gb": gb}

    def _rescanned(self, changed: bool) -> dict:
        """Roots as they now stand, whether that edit changed anything, and
        whether a scan is picking it up. An edit that changed nothing starts no
        scan — a no-op must not cost a pass over the whole library. And
        `reindexing: false` on a real change means a run was already in flight:
        it started with the old roots, so the change needs the next one (the
        view offers ↻)."""
        out = self.roots_payload()
        out["changed"] = changed
        out["reindexing"] = self.start_reindex() if changed else False
        return out

    def add_root(self, path: str, confirm: bool = False) -> dict:
        """Raises RootError for anything the view should explain instead of
        indexing: an unusable path, the filesystem root, a folder that isn't
        one, and — until the user says so twice — the home directory, whose
        first scan is long enough that it must not happen by a slip."""
        root = _resolve(path)
        if root is None:
            raise RootError("invalid path",
                            "That path isn’t one this system can open.")
        if root == os.sep:
            raise RootError("root too broad",
                            "Indexing the whole filesystem isn’t supported — "
                            "pick a folder inside it.")
        if not os.path.isdir(root):
            raise RootError("not a directory", f"{root} is not a folder.")
        if root == _resolve(Path.home()) and not confirm:
            raise RootError("confirm_home",
                            "Index your entire home folder? The first scan can "
                            "take a long time.")
        with self._cfg_lock:                  # two POSTs must not lose an edit
            cfg = config.load_config(self.cache)
            changed = root not in cfg["roots"]
            if changed:
                cfg["roots"].append(root)
                config.save_config(cfg, self.cache)
        return self._rescanned(changed)

    def remove_root(self, path: str) -> dict:
        """Drops the folder from the config; the reindex that follows prunes
        its photos out of the catalog. Matched against both the literal string
        and the normalized one, so a root stored before normalization existed
        is still removable."""
        norm = _resolve(path)
        if norm is None:
            raise RootError("invalid path",
                            "That path isn’t one this system can open.")
        wanted = {str(path), norm}
        with self._cfg_lock:
            cfg = config.load_config(self.cache)
            # a stored root that no longer normalizes must not block removing
            # the others, so it is compared on its literal form alone
            keep = [r for r in cfg["roots"]
                    if r not in wanted and _resolve(r) not in wanted]
            changed = keep != cfg["roots"]
            if changed:
                cfg["roots"] = keep
                config.save_config(cfg, self.cache)
        return self._rescanned(changed)

    def list_dirs(self, path: str = None):
        """Subdirectories of `path` (default: home) for the view's folder
        browser — names only, no files, no hidden entries. None when `path`
        isn't a directory. Read-only, and behind the same loopback guards as
        everything else."""
        try:
            base = (Path(path).expanduser() if path else Path.home()).resolve()
            if not base.is_dir():
                return None
        except (OSError, ValueError):     # null byte, over-long name, loop
            return None
        dirs = []
        try:
            with os.scandir(base) as entries:
                for e in entries:
                    if e.name.startswith("."):
                        continue
                    try:
                        if not e.is_dir():
                            continue
                    except OSError:            # a broken symlink, mid-scan
                        continue
                    dirs.append({"name": e.name, "path": str(base / e.name)})
        except (OSError, ValueError):
            pass          # an unreadable folder browses as empty, not as an error
        dirs.sort(key=lambda d: d["name"].lower())
        parent = str(base.parent) if base.parent != base else None
        return {"path": str(base), "parent": parent, "dirs": dirs}

    # -- indexing ---------------------------------------------------------
    def _warm_text_encoder(self):
        """Force the text tower's first forward pass while we are still
        flagged as indexing.

        Indexing only exercises the *image* encoder, so without this the
        first semantic query a user types pays the text encoder's one-off
        setup — model load if nothing was indexed this run, plus backend
        kernel compilation (~3s measured on MPS). That latency landed on an
        interactive keystroke; here it lands on startup instead. Best-effort:
        a failure must never mark the index run as failed.

        The tag vocabulary is built in the same breath, for the same reason and
        on the same thread: it is ~70 more forward passes through the tower that
        has just been warmed, and paying for them here means opening the details
        panel never waits on the model."""
        try:
            self.embedder.embed_text("warm up")
            self._tags.build()
        except Exception:
            pass

    def _do_index(self, warm: bool = True):
        self._run_start = time.monotonic()
        self._progress = (0, 0, indexer.STAGE_INDEX)
        try:
            self._last_stats = indexer.index_once(
                self.store, self.current_roots(), self.embedder, self.cache,
                # one tuple assignment per file: readers see one or the other,
                # never a half-updated pair, so no lock is needed for it
                progress=lambda done, total, stage: setattr(
                    self, "_progress", (done, total, stage)),
                face_model=self.face_model)
            if warm:
                self._warm_text_encoder()
        except Exception as exc:
            self._last_stats = {"error": str(exc)}
        finally:
            self._progress = None
            with self._lock:
                self._indexing = False

    def index_now(self, warm: bool = False):
        """Index on the calling thread. Warm-up is off by default: it only
        pays off for a process that stays up to serve queries, and a
        synchronous caller (a one-shot index, a test) would just wait on it."""
        with self._lock:
            if self._indexing:
                return False
            self._indexing = True
        self._do_index(warm=warm)
        return True

    def start_reindex(self):
        with self._lock:
            if self._indexing:
                return False
            self._indexing = True
        t = threading.Thread(target=self._do_index, daemon=True)
        self._index_thread = t
        t.start()
        return True

    # -- queries ----------------------------------------------------------
    def known_places(self, q: str = "") -> list:
        """Place names a query could plausibly use: cities and admin1 regions.

        Bare two-letter country codes are deliberately left out. The
        vocabulary is matched as whole words anywhere in the query, so "NO"
        (Norway) turned "no dogs" into a Norway filter, "ID" turned "id card"
        into Indonesia, and "us", "at", "is", "it" hijacked ordinary English.
        Regions cover how people actually name places ("bali", "tuscany").

        The one escape is a query that is *nothing but* a country code — "us",
        "it" on its own can only be meant as a place — so `q` is consulted for
        that exact case.
        """
        whole = q.strip().lower()
        seen, out = set(), []
        for col in ("place_city", "place_region", "place_country"):
            for v in self.store.distinct(col):
                if not v or v in seen:
                    continue
                if len(v) <= 2 and v.lower() != whole:
                    continue
                seen.add(v)
                out.append(v)
        return out

    # -- people ------------------------------------------------------------
    def people(self) -> list:
        """Everyone lens has found, most-photographed first, with what a card
        needs: `[{id, name, face_count, photo_count, cover_face_id}]`.

        Ordered by how many photographs they are in rather than by id, because
        that order is the answer to the question the People view is opened with:
        who is in my library? The household comes first and the stranger who
        turned up in three street scenes comes last, without anybody having to
        say so.

        A person whose faces have all gone (their photographs were deleted, or a
        re-detect dropped below the cluster minimum) is not listed: the row
        survives so that a name and a merge survive with it (see
        store.replace_persons), but a card for nobody is not a person.
        """
        counts = self.store.person_counts()
        out = []
        for p in self.store.get_persons():
            faces, photos = counts.get(p["id"], (0, 0))
            if not faces:
                continue
            out.append({"id": p["id"], "name": p.get("name") or None,
                        "face_count": faces, "photo_count": photos,
                        "cover_face_id": p.get("cover_face_id")})
        out.sort(key=lambda p: (-p["photo_count"], -p["face_count"], p["id"]))
        return out

    def face_crop(self, face_id: int, size: int = FACE_SIZE_DEFAULT):
        """The cached crop of one face as a Path, or None if there is no such
        face (or nothing to render it from).

        None rather than a raise for a missing thumbnail: a person's cover can be
        a photograph on an unplugged drive, and the view draws a placeholder for
        that. It is not an error, it is a fact about right now."""
        face = self.store.get_face(int(face_id))
        if not face or not face.get("sha1"):
            return None
        try:
            return ensure_face_thumb(face["path"], self.cache, face["sha1"],
                                     face["bbox"], size, face.get("kind"))
        except (OSError, ValueError):
            return None

    def rename_person(self, person_id: int, name) -> dict:
        """Give a person a name, or take it away (an empty name clears it).

        Clearing is a real operation, not an oversight: a name seeded from the
        Photos library can be wrong, and "no name" is a better state than a wrong
        one. Trimmed and length-capped here rather than in the view, because the
        view is not the only client and a name becomes a phrase in the query
        vocabulary."""
        if name is not None and not isinstance(name, str):
            raise RootError("bad name", "A name has to be text.")
        clean = (name or "").strip()[:MAX_NAME]
        if not self.store.set_person_name(int(person_id), clean or None):
            return None
        # The name is now part of the query vocabulary ("photos of Ana"), and
        # nothing else invalidates that — it is read fresh per query — so there
        # is no cache to clear here. Said out loud because its absence looks like
        # an omission.
        return {"id": int(person_id), "name": clean or None}

    def merge_people(self, keep: int, absorb: int) -> dict:
        """Two cards that are one person, made one. Returns the survivor, or
        None when either id is unknown (or they are the same id — merging
        somebody into themselves is a mis-click, not an operation)."""
        if not self.store.merge_persons(int(keep), int(absorb)):
            return None
        counts = self.store.person_counts()
        faces, photos = counts.get(int(keep), (0, 0))
        row = next((p for p in self.store.get_persons()
                    if p["id"] == int(keep)), None)
        return {"id": int(keep), "name": (row or {}).get("name") or None,
                "face_count": faces, "photo_count": photos,
                "cover_face_id": (row or {}).get("cover_face_id")}

    def people_in(self, photo_id: int) -> list:
        """The people detected in one photograph: `[{face_id, person_id, name,
        prob, bbox}]`, for the details panel's chips.

        Faces with no person are included, with `person_id: null`. "Somebody is
        in this photo and lens has not seen them anywhere else" is a true and
        useful thing to show — it is the difference between a photo it has looked
        at and one it has not."""
        rows = self.store.faces_for_photos([photo_id]).get(int(photo_id), [])
        if not rows:
            return []
        names = {p["id"]: p.get("name") for p in self.store.get_persons()}
        out = []
        for r in rows:
            pid = r.get("cluster_id")
            out.append({"face_id": r["id"], "person_id": pid,
                        "name": names.get(pid) if pid is not None else None,
                        "prob": r.get("prob"), "bbox": list(r["bbox"])})
        return out

    def run_query(self, q: str, limit: int = 200,
                  scope: str = "photos", offset: int = 0,
                  trip: int = None, person: int = None) -> dict:
        """`scope` is "photos" (the default — camera captures only), "videos" or
        "all" (see query.build_where).

        Defaulting to photos is what makes search usable on a real library: a
        home folder holds far more software-made images than photographs, and
        they are what a semantic query returns when the query has no strong
        match. Nothing is hidden — "all" is one click away in the view — but
        the default answers the question people are actually asking.

        Videos rank through the same path as everything else, with no special
        case anywhere below this line: a video's row carries one vector like a
        photograph's, pooled from the frames the indexer sampled (see
        indexer._pool), so the WHERE clause is the only thing that knows the
        difference.

        `trip` narrows everything to one trip, `person` to one person's
        photographs (see query.build_where). Both compose with the words: a
        search typed inside a person's grid searches within it.

        A *named* person is also reachable through the words alone — "photos of
        Ana" — because names join the query vocabulary the way album names do.
        The parser hands back the names it matched and they are resolved to ids
        here: the parser reads words, and only the catalog knows who is who."""
        by_name = self.store.person_names()          # [(id, name)]
        pq = query.parse(q, self.known_places(q),
                         self.store.distinct("camera"),
                         known_albums=self.store.apple_phrases(),
                         known_people=[n for _, n in by_name])
        wanted = {n.lower() for n in pq.people}
        people = [pid for pid, n in by_name if n.lower() in wanted]
        if person is not None:
            # The explicit filter is a person the user pressed; a name in the
            # query is a person they typed. Both mean "and this person too", so
            # the ids are combined rather than one replacing the other.
            people = [person] + [p for p in people if p != person]
        where, params = query.build_where(pq, scope, trip, people)
        rows = self.store.query_photos(where, params)
        strong, cutoff, searched = None, None, None
        if pq.residual:
            searched = len(rows)
            ids, mat = self.store.load_embeddings()
            # the *sentence* is what gets embedded, never the bare residual —
            # see query.TEXT_PROMPT for the measurement behind that
            tvec = self.embedder.embed_text(query.text_prompt(pq.residual))
            # rank without a limit: `total` must describe the whole match set
            # (post-cut), so truncation happens once, below.
            rows = query.rank(rows, ids, mat, tvec, limit=None,
                              ratio=query.RELEVANCE_RATIO)
            # measured over the whole ranked set, before the slice: the
            # boundary belongs to the query, not to the page being served
            strong, cutoff = query.confidence_horizon(rows)
        total = len(rows)
        # `offset` pages a ranking, so it slices the same ordered set every
        # time — the view appends the next page rather than re-rendering, and
        # `total`, `strong` and the horizon all keep describing the whole
        # match set rather than the page being served.
        offset = max(0, offset)
        rows = rows[offset:offset + limit]

        trips = {t["id"]: t for t in self.store.get_trips()}
        # `kind` and `duration_s` ride along with every item, video or not: a card
        # has to know whether to draw a play glyph and a running time before it
        # has fetched anything else, and one nullable float is cheaper than a
        # /meta request per tile.
        keys = ("id", "path", "taken_at", "place_city", "camera", "score",
                "trip_id", "kind", "duration_s")
        items = [{k: r.get(k) for k in keys} for r in rows]
        if pq.trip_mode:
            groups, order = {}, []
            for it in items:
                tid = it.get("trip_id")
                if tid not in groups:
                    groups[tid] = []
                    order.append(tid)
                groups[tid].append(it)
            # the trip's own dates travel with it: two stays in one place a
            # week apart produced two headings reading "Banjar Kerobokan ·
            # Jul 2026", and nothing on screen said which was which
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
                "scope": scope,
                # echoed for the same reason `trip` is: the view has to be able
                # to tell "this page is the person I asked for" from "this page
                # is everything" without trusting that its URL and the response
                # in its hand are the same generation
                "person": person,
                # echoed back so a view can tell "this page is the trip I asked
                # for" from "this page is everything", without trusting that its
                # own URL and the response it is holding are the same generation
                "trip": trip,
                "total": total,
                "limit": limit,
                "offset": offset,
                # Where the answers stop and the padding starts. `strong` counts
                # leading rows of the *whole* ranked set (they are sorted, so
                # the split is positional and survives paging); `strong_cutoff`
                # is the score behind it. Both null when the query had no
                # semantic part at all — then every row is a filter match and
                # there is no confidence to grade. `strong: 0` is the honest
                # "nothing here is a real match" answer.
                "strong": strong,
                "strong_cutoff": cutoff,
                # how many rows the semantic ranking actually looked at, before
                # any cut. `strong` is only meaningful against it: three strong
                # out of 86 is a result, 84 out of 86 is a bar that separated
                # nothing, and the view says so rather than presenting the
                # second as a ranking.
                "searched": searched,
                "groups": gs}

    def trips(self) -> list:
        """Every trip, with what a card needs to stand on its own: how many
        photos it holds and which one to show.

        A trip with no showable photo still travels — the count is the honest
        thing about it, and the view draws a placeholder rather than pretending
        the trip does not exist."""
        counts = self.store.trip_counts()
        out = []
        for t in self.store.get_trips():
            n, cover = counts.get(t["id"], (0, None))
            out.append({"id": t["id"], "name": t["name"], "start": t["start"],
                        "end": t["end"], "place": t["place"],
                        "count": n, "cover_id": cover})
        return out

    def tags_for(self, pid: int):
        """Top concept labels for one photo, or None if there is no such photo.

        `[]` and None are different answers: an empty list is "this photo has no
        vector yet, so there is nothing to describe it with", which the panel
        says out loud rather than leaving a gap where chips should be.

        A real answer is cached for the life of the process: it is a pure
        function of a stored vector and a fixed vocabulary, so only a re-index
        could change it, and this cache is cheap enough (six short strings per
        photo) that clearing it on one is not worth the coupling.

        An empty answer is NOT cached, and that distinction is the whole reason
        this is not a one-liner. `[]` is not the labels of a vector — it is "this
        photo has no vector yet", a fact about the index, and the index moves.
        Cached, it meant that opening the details panel on a photo while a
        reindex was still running pinned "nothing to describe it with" onto that
        photo until the daemon was restarted, however long after the reindex
        actually embedded it."""
        if pid in self._tag_cache:
            return self._tag_cache[pid]
        row = self.store.get_photo_by_id(pid)
        if not row:
            return None
        ids, mat = self.store.load_embeddings()
        pos = {int(i): n for n, i in enumerate(ids)}
        n = pos.get(pid)
        out = ([] if n is None or mat.ndim != 2
               else self._tags.top(mat[n].astype("float32")))
        if out:
            self._tag_cache[pid] = out
        return out

    def validate(self) -> dict:
        return validate.run(self.store, self.cache, self.known_places)

    def _progress_payload(self):
        """`status()["progress"]`, or None between runs.

        `elapsed_s` and `eta_s` are what let the status line say "3m so
        far, about 2m left" instead of a bar with no sense of time — `eta_s`
        is bluntly rate-based (elapsed × remaining-fraction), which is exactly
        as good as the assumption that the rest of the run costs what the
        part already measured cost. It is None until at least one file has
        been reported done: a rate divided from zero done files is not a
        rate, and a "0s left" flashed for the first tick of a real run would
        be a worse answer than admitting there isn't one yet."""
        p = self._progress
        if not p:
            return None
        done, total = p[0], p[1]
        elapsed = (round(time.monotonic() - self._run_start, 1)
                   if self._run_start else None)
        eta = (round(elapsed * (total - done) / done, 1)
               if elapsed is not None and done and total else None)
        return {"done": done, "total": total,
                "stage": p[2] if len(p) > 2 else indexer.STAGE_INDEX,
                "elapsed_s": elapsed, "eta_s": eta}

    def runs(self, limit: int = 20) -> list:
        """The last `limit` runs' metrics, newest last — read straight off
        `<cache>/runs.jsonl` rather than kept in memory, so a daemon that was
        just restarted still has history for explain.html's performance
        panel to show, not only the run since it came up.

        A line this daemon itself never wrote correctly (truncated by a kill
        mid-write, or from a lens version whose schema differed) is skipped
        rather than raising — one bad line must not blank the whole panel."""
        limit = max(1, min(int(limit), MAX_RUNS))
        path = self.cache / indexer.RUNS_HISTORY_FILE
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def status(self) -> dict:
        counts = self.store.scope_counts()
        return {# every file catalogued, unreadable ones included: this is the
                # library's size, not a promise about search
                "photos": len(self.store.path_signatures()),
                # what each scope of /query can actually return, so the view can
                # label its toggle (and offer the other scope) without lying
                "photos_scope": counts["photos"],
                "videos_scope": counts["videos"],
                "all_scope": counts["all"],
                "trips": len(self.store.get_trips()),
                # Faces and people, and — the number that keeps the other two
                # honest — how many rows the face pass has actually been over.
                # The first index of a real library finds its people over
                # several minutes after the photographs are already searchable,
                # and a count with no denominator would read as "these are all
                # the people you have".
                "faces": self.store.face_counts(),
                # how much of the library came out of Apple Photos, and what the
                # last sync made of it — including a permission error, which is
                # the one thing about this feature the user has to be told
                "apple": self.apple_payload(),
                "indexing": self._indexing,
                # {done, total} of the run in flight, so the view can show a
                # real fraction instead of an 11px label and a sweep that
                # means nothing. Absent between runs; total 0 while the scan
                # is still counting what there is to do.
                "progress": self._progress_payload(),
                "model": getattr(self.embedder, "key", "?"),
                # The facts a "how it works" page has to be able to show, all of
                # them cheap enough to ride along on a 10s poll: the weights
                # actually loaded, the shape of the matrix on disk (read from its
                # header, not by loading it), and where on this machine the whole
                # library lives — which is the claim "nothing leaves your
                # computer" made checkable rather than asserted.
                "model_id": getattr(self.embedder, "model_id", None),
                "embeddings": (lambda s: {"count": s[0], "dims": s[1]})(
                    self.store.embedding_shape()),
                "cache": str(self.cache),
                # first run downloads and loads several GB of weights; the
                # view says "loading model…" instead of a silent stall
                "model_loaded": bool(getattr(self.embedder, "loaded", True)),
                "last_index": self._last_stats}

    def get_photo_by_id(self, pid: int):
        return self.store.get_photo_by_id(pid)

    # -- lifecycle ---------------------------------------------------------
    def serve_forever(self):
        self._serving.set()
        try:
            self._httpd.serve_forever()
        finally:
            self._serving.clear()

    def shutdown(self):
        # ThreadingHTTPServer.shutdown() blocks on an event that only
        # serve_forever() ever sets, so calling it on a server that was never
        # served deadlocks. Skip it in that case and just release the socket.
        if self._serving.is_set():
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._index_thread is not None:
            self._index_thread.join(timeout=5)
        self.store.close()


def _make_handler(srv: LensServer):
    class Handler(BaseHTTPRequestHandler):
        # -- access control ------------------------------------------------
        def _origin(self):
            """The Origin to reflect, or None to send no CORS header at all.

            `Access-Control-Allow-Origin: *` let any page on the internet read
            this daemon's responses through the visitor's own browser — every
            photo path, every GPS coordinate, every image byte. Only a page
            served from loopback (the fused-render view) gets a reflection;
            everyone else is refused at the browser's same-origin boundary.
            Requests with no Origin at all (curl, the CLI) are unaffected —
            there is no browser to protect there."""
            origin = self.headers.get("Origin")
            return origin if origin and _LOOPBACK_ORIGIN.fullmatch(origin) else None

        def _host_ok(self):
            """Reject a request that reached us under someone else's name.

            DNS rebinding points an attacker-controlled hostname at 127.0.0.1,
            which makes the page same-origin with the daemon and sidesteps CORS
            entirely — but the Host header still carries that hostname, and a
            genuine local client never sends anything but loopback."""
            host = self.headers.get("Host")
            return host is None or bool(_LOOPBACK_HOST.fullmatch(host))

        def _cors(self):
            # the response varies by Origin, so it must not be cached across
            # different ones
            self.send_header("Vary", "Origin")
            origin = self._origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)

        def _send(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode())

        def _forbidden(self):
            self._json({"error": "forbidden"}, 403)

        def _body(self):
            """The request's JSON object, or None if there isn't a usable one."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n <= 0 or n > MAX_BODY:
                return None
            try:
                body = json.loads(self.rfile.read(n))
            except (ValueError, OSError):
                return None
            return body if isinstance(body, dict) else None

        def do_OPTIONS(self):
            if not self._host_ok():
                return self._forbidden()
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # /roots posts application/json, which is not a CORS-"simple"
            # content type: without this the browser fails the preflight and
            # the view can never add a folder.
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            if not self._host_ok():
                return self._forbidden()
            # POST /reindex is a CORS-"simple" request: no preflight, so the
            # browser sends it before it ever looks at our headers. Withholding
            # the response header stops a foreign page from *reading* the
            # reply, but not from firing the rescan blind — so refuse an
            # Origin we would not have allowed. A missing Origin is still fine
            # (that is the CLI, not a browser).
            #
            #
            # The same check is what protects /roots and /roots/remove, which
            # rewrite the library. Our own view sends them as JSON, which does
            # preflight — but nothing forces an attacker's page to: a POST with
            # a CORS-simple content type reaches this handler without any
            # preflight at all, and we never inspect Content-Type. The explicit
            # Origin refusal below is the defense; the preflight is not.
            if self.headers.get("Origin") and not self._origin():
                return self._forbidden()
            p = urlparse(self.path).path
            try:
                if p == "/reindex":
                    return self._json({"started": srv.start_reindex()})
                if p == "/config":
                    # Two settings, both named explicitly. A generic "merge
                    # this JSON into the config" endpoint would let any
                    # loopback page rewrite `roots` (bypassing every check in
                    # add_root), `model`, or `port` — so the body is read for
                    # the keys this daemon actually offers and nothing else.
                    body = self._body() or {}
                    if "apple_photos" in body:
                        want = body.get("apple_photos")
                        if not isinstance(want, bool):
                            return self._json({"error": "apple_photos must be "
                                                        "true or false"}, 400)
                        return self._json(srv.set_apple(want))
                    if "max_index_memory_gb" in body:
                        gb = body.get("max_index_memory_gb")
                        # bool is an int subclass in Python — excluded
                        # explicitly, or {"max_index_memory_gb": true} would
                        # silently set the limit to 1GB.
                        if (isinstance(gb, bool) or not isinstance(gb, (int, float))
                                or gb <= 0):
                            return self._json(
                                {"error": "max_index_memory_gb must be a "
                                          "positive number"}, 400)
                        return self._json(srv.set_memory_limit(float(gb)))
                    return self._json({"error": "nothing to set"}, 400)
                if p == "/people/merge":
                    # Before the /people/<id>/… pattern below, because "merge"
                    # is not a number and would fall through to a 404 anyway —
                    # but stating the order stops a later edit from making the
                    # id pattern greedy enough to swallow it.
                    body = self._body() or {}
                    try:
                        keep = int(body.get("keep"))
                        absorb = int(body.get("absorb"))
                    except (TypeError, ValueError):
                        return self._json({"error": "keep and absorb "
                                                    "must be person ids"}, 400)
                    out = srv.merge_people(keep, absorb)
                    if out is None:
                        return self._json({"error": "no such person"}, 404)
                    return self._json({"person": out})
                m = re.fullmatch(r"/people/(\d+)/rename", p)
                if m:
                    body = self._body() or {}
                    if "name" not in body:
                        return self._json({"error": "name required"}, 400)
                    try:
                        out = srv.rename_person(int(m.group(1)), body["name"])
                    except RootError as exc:
                        return self._json({"error": exc.code,
                                           "message": exc.message}, 400)
                    if out is None:
                        return self._json({"error": "no such person"}, 404)
                    return self._json({"person": out})
                if p in ("/roots", "/roots/remove"):
                    # read once: the body is a stream, and a second read of it
                    # would come back empty
                    body = self._body() or {}
                    want = body.get("path")
                    want = want.strip() if isinstance(want, str) else ""
                    if not want:
                        return self._json({"error": "path required"}, 400)
                    try:
                        out = (srv.add_root(want, body.get("confirm") is True)
                               if p == "/roots" else srv.remove_root(want))
                    except RootError as exc:
                        return self._json({"error": exc.code,
                                           "message": exc.message}, 400)
                    return self._json(out)
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def do_GET(self):
            if not self._host_ok():
                return self._forbidden()
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            try:
                if u.path == "/status":
                    return self._json(srv.status())
                if u.path == "/roots":
                    return self._json(srv.roots_payload())
                if u.path == "/fs/dirs":
                    out = srv.list_dirs(qs.get("path", [None])[0])
                    if out is None:
                        return self._json({"error": "not a directory"}, 400)
                    return self._json(out)
                if u.path == "/query":
                    q = qs.get("q", [""])[0]
                    try:
                        limit = int(qs.get("limit", ["200"])[0])
                    except ValueError:
                        return self._json({"error": "bad parameter"}, 400)
                    # A page size is a promise about how much work one request
                    # can ask for. Zero or negative slices nonsense out of the
                    # ranking, and an unbounded one hands back the whole
                    # library — neither is a page, and the view never asks for
                    # either (see currentLimit).
                    if not 1 <= limit <= MAX_LIMIT:
                        return self._json({"error": "bad parameter"}, 400)
                    # a scope this daemon does not offer — absent, misspelled, a
                    # link from a newer view — gets the safe default rather than
                    # a 400: the worst outcome of guessing here is showing the
                    # photographs, which is where the page starts anyway
                    want = qs.get("scope", [""])[0]
                    scope = want if want in query.SCOPES else "photos"
                    try:
                        offset = int(qs.get("offset", ["0"])[0])
                        # absent is "every trip"; present-but-not-a-number is a
                        # link the view never wrote, and guessing which trip was
                        # meant would be worse than saying so
                        raw_trip = qs.get("trip", [""])[0]
                        trip = int(raw_trip) if raw_trip != "" else None
                        raw_person = qs.get("person", [""])[0]     # same rule
                        person = int(raw_person) if raw_person != "" else None
                    except ValueError:
                        return self._json({"error": "bad parameter"}, 400)
                    return self._json(
                        srv.run_query(q, limit, scope, offset, trip, person))
                if u.path == "/trips":
                    return self._json({"trips": srv.trips()})
                if u.path == "/people":
                    return self._json({"people": srv.people()})
                m = re.fullmatch(r"/people/(\d+)/face\.webp", u.path)
                if m:
                    # The id in the path is a *face*, not a person: a person's
                    # cover moves as the cluster changes, and the crop is cached
                    # by what it shows (see thumbs.face_thumb_path). The route
                    # sits under /people because that is the only thing that asks
                    # for it.
                    try:
                        size = int(qs.get("s", [str(FACE_SIZE_DEFAULT)])[0])
                    except ValueError:
                        return self._json({"error": "bad parameter"}, 400)
                    size = min(FACE_SIZES, key=lambda s: abs(s - size))
                    p = srv.face_crop(int(m.group(1)), size)
                    if p is None:
                        return self._json({"error": "no such face"}, 404)
                    return self._send(200, p.read_bytes(), "image/webp")
                if u.path == "/validate":
                    return self._json(srv.validate())
                if u.path == "/runs":
                    # explain.html's performance panel: the real numbers off
                    # the last N runs, not a page asserting anything about
                    # itself. Loopback-guarded like every other GET here
                    # (_host_ok, above) — the same reason /status is.
                    try:
                        limit = int(qs.get("limit", ["20"])[0])
                    except ValueError:
                        return self._json({"error": "bad parameter"}, 400)
                    return self._json({"runs": srv.runs(limit)})
                m = re.fullmatch(r"/tags/(\d+)", u.path)
                if m:
                    out = srv.tags_for(int(m.group(1)))
                    if out is None:
                        return self._json({"error": "no such photo"}, 404)
                    return self._json({"tags": out})
                m = re.fullmatch(r"/thumb/(\d+)", u.path)
                if m:
                    row = srv.get_photo_by_id(int(m.group(1)))
                    if not row or not row.get("sha1"):
                        return self._json({"error": "no such photo"}, 404)
                    try:
                        size = int(qs.get("s", ["512"])[0])
                    except ValueError:
                        return self._json({"error": "bad parameter"}, 400)
                    size = LIGHTBOX_SIZE if size > 512 else 512
                    # `kind` from the row rather than guessed: a video is
                    # rendered by decoding one frame, and at this size that frame
                    # was never rendered at index time (see
                    # thumbs.ensure_video_thumb).
                    p = ensure_media_thumb(row["path"], srv.cache, row["sha1"],
                                           size, row.get("kind"))
                    return self._send(200, p.read_bytes(), "image/webp")
                m = re.fullmatch(r"/meta/(\d+)", u.path)
                if m:
                    pid = int(m.group(1))
                    row = srv.get_photo_by_id(pid)
                    if not row:
                        return self._json({"error": "no such photo"}, 404)
                    row["raw_exif"] = json.loads(row.get("raw_exif") or "{}")
                    # Who is in it, on the same request as everything else about
                    # it: the details panel draws these as chips beside the
                    # camera and the exposure, and a second round trip for four
                    # rows out of a table we have just read would be one.
                    row["people"] = srv.people_in(pid)
                    return self._json(row)
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def log_message(self, *a):
            pass

    return Handler
