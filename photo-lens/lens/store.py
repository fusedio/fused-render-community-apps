import json
import os
import sqlite3
import threading
import zipfile
from pathlib import Path

import numpy as np

SCHEMA_VERSION = "6"

_TABLES = ("photos", "trips", "meta", "faces", "persons")

# Versions this one can be reached from without discarding anything: schemas 4,
# 5 and 6 only *add* things — columns, and (in 6) two whole tables that start
# empty — so a v3, v4 or v5 catalog is upgraded in place with ALTER TABLE and
# keeps every row, every thumbnail and every vector.
#
# The reset path below stays for everything older. Re-indexing a real library is
# tens of minutes of GPU work, and it is not worth paying that to learn three
# columns' worth of nothing about the rows that already exist — but a catalog far
# enough behind that its *existing* columns disagree cannot be patched, and
# guessing which is which is how a migration corrupts a cache.
_ADDITIVE_FROM = {"3", "4", "5"}
_ADDED_IN_4 = (("source", "TEXT DEFAULT 'folder'"),
               ("apple_uuid", "TEXT"),
               ("apple_text", "TEXT"))
_ADDED_IN_5 = (("kind", "TEXT DEFAULT 'image'"),
               ("duration_s", "REAL"))
# NULL means "faces have never been looked for in this photo", which is exactly
# what is true of every row in a catalog written before this column existed —
# so the whole library is face-scanned incrementally, in the background, without
# a single vector or thumbnail being thrown away.
_ADDED_IN_6 = (("faces_v", "TEXT"),)
# Every additive column since the oldest version that can be patched, in one
# tuple: `_extend` skips the ones a catalog already has, so a v3 and a v4 cache
# are the same call and neither needs a per-version list of its own.
_ADDED_SINCE_3 = _ADDED_IN_4 + _ADDED_IN_5 + _ADDED_IN_6

# Shortest album name or title offered to the query parser as a searchable
# phrase. The vocabulary is matched as whole words anywhere in a query, so a
# two-letter album ("Me", "NY") would hijack ordinary English exactly the way
# bare country codes did (see daemon.known_places).
MIN_PHRASE = 3

# One generation of vectors, in one file. The ids and the matrix are positional
# halves of a single fact — row i of the matrix is the vector for ids[i] — and
# two files could not be swapped together: a kill between the two os.replace
# calls left N+1 ids against N rows, which is not a stale index but a poisoned
# one. Every semantic query then raised, and so did the next index run, which
# reads the pair before it can rebuild it. There was no way out but deleting
# the cache by hand.
_EMB_NPZ = "embeddings.npz"
# ...and the pair that used to hold them, still read once so an existing cache
# survives the upgrade, and deleted the first time a new generation is written.
_EMB_LEGACY = ("emb_ids.npy", "embeddings.npy")

# Face vectors, in their own file, by the same single-file atomic rule (see
# save_embeddings). Separate from the image vectors rather than a second matrix
# in one archive: they are written by a different pipeline stage at a different
# cadence, they have a different dimensionality, and a checkpoint of one must
# never rewrite — or truncate — the other.
_FACE_NPZ = "faces.npz"
_EMB_FILES = _EMB_LEGACY + (_EMB_NPZ, _FACE_NPZ)   # dropped on a schema rebuild

# np.load raises any of these on a file that is not the archive it claims to be
_BAD_ARCHIVE = (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile)

_COLUMNS = [
    "path", "sha1", "size", "mtime", "width", "height", "format",
    "taken_at", "lat", "lon", "place_city", "place_region", "place_country",
    "camera", "lens", "iso", "f_number", "exposure", "focal_length",
    "raw_exif", "error", "is_photo",
    "source", "apple_uuid", "apple_text",
    "kind", "duration_s",
]

# Columns an upsert must not be allowed to leave NULL, whatever the caller
# passed. `source` is one: every read that separates the two ingest paths — the
# folder pruner, the Apple pruner, the status counts — asks the column, and a
# NULL there is a row belonging to neither. `kind` is the same story for stills
# and videos: every scope filter tests it, so a NULL there is a row in no scope
# at all — unreachable by search while still being counted. The caller shouldn't
# have to remember, so the defaults live with the schema instead.
_COLUMN_DEFAULTS = {"source": "folder", "kind": "image"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    sha1 TEXT, size INTEGER, mtime REAL,
    width INTEGER, height INTEGER, format TEXT,
    taken_at TEXT, lat REAL, lon REAL,
    place_city TEXT, place_region TEXT, place_country TEXT,
    camera TEXT, lens TEXT, iso INTEGER, f_number REAL,
    exposure TEXT, focal_length REAL,
    raw_exif TEXT, error TEXT,
    trip_id INTEGER,
    -- derived at index time (metadata.is_photo): camera capture vs. software
    -- artwork. The default scope of every search filters on it.
    is_photo INTEGER,
    -- which ingest put this row here: 'folder' (the walker) or 'apple' (the
    -- Photos library, read through lens/apple_photos.py). The two are pruned by
    -- different rules, so every row has to say which one it answers to.
    source TEXT DEFAULT 'folder',
    apple_uuid TEXT,
    -- title + album names, one per line: the phrase vocabulary behind
    -- "photos in album Bali" (see apple_photos.phrases, store.apple_phrases)
    apple_text TEXT,
    -- 'image' or 'video' (metadata.kind_for). Separate from is_photo, which
    -- stays a claim about *stills*: "a camera took this, it is not a graphic".
    -- A video is never is_photo = 1, and the two columns together are what the
    -- three search scopes are built from (see query.build_where).
    kind TEXT DEFAULT 'image',
    -- a video's length in seconds; NULL for a still
    duration_s REAL,
    -- which face-model generation last looked for faces in this row
    -- (faces.FaceModel.key), or NULL for "never looked". Not a boolean,
    -- because changing the detector's threshold or the recognition weights
    -- changes both which faces exist and what "the same person" means — so the
    -- generation has to be comparable, not merely present (see
    -- indexer._index_faces).
    faces_v TEXT
);
CREATE INDEX IF NOT EXISTS idx_taken ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_source ON photos(source);
CREATE INDEX IF NOT EXISTS idx_kind ON photos(kind);
CREATE INDEX IF NOT EXISTS idx_city ON photos(place_city);
CREATE INDEX IF NOT EXISTS idx_region ON photos(place_region);
CREATE INDEX IF NOT EXISTS idx_is_photo ON photos(is_photo);
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY, name TEXT, start TEXT, end TEXT, place TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- One detected face. The *vector* is not here: it lives in faces.npz by row id,
-- the same split (and for the same reasons) as photos ↔ embeddings.npz.
CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    photo_id INTEGER NOT NULL,
    -- (x0, y0, x1, y1) as fractions of the picture, never pixels: the box is
    -- read back against a 512px thumbnail today and a 2048px render tomorrow
    -- (see faces.crop_face).
    bbox_json TEXT NOT NULL,
    prob REAL,
    -- which person this face belongs to (persons.id), or NULL for a face in no
    -- cluster — one or two sightings of a stranger, kept as a face and as a
    -- vector so that a third sighting promotes all of them at once.
    cluster_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
-- A person is a cluster of faces that has been given an identity stable enough
-- to name and to link to. `centroid` is what makes it stable: re-clustering
-- recomputes the groups from scratch after every index run, and a fresh group
-- whose centroid is close to this one *is* this person (see
-- persons.assign_persons).
CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    -- what the user called them, or NULL for "Person N". Never invented from
    -- the vectors; only typed, or seeded from names the Photos library already
    -- holds (persons.seed_names).
    name TEXT,
    cover_face_id INTEGER,
    -- float32 unit vector, raw bytes
    centroid BLOB,
    -- set when this person was merged into another: the row stays, because its
    -- centroid is what makes the merge survive the next re-clustering, and its
    -- id is what an old link still points at.
    merged_into INTEGER
);
"""


def _npy_shape(f):
    """`(rows, cols)` from an open .npy stream, reading only its header.

    The two header versions differ in how the length prefix is encoded, and
    numpy exposes a reader per version but nothing that dispatches between them
    — so this does. A 1-D array reports 0 columns rather than raising: a matrix
    with no second axis holds no vectors, which is the same thing an empty cache
    means."""
    version = np.lib.format.read_magic(f)
    reader = (np.lib.format.read_array_header_2_0 if version >= (2, 0)
              else np.lib.format.read_array_header_1_0)
    shape = reader(f)[0]
    return (int(shape[0]) if shape else 0,
            int(shape[1]) if len(shape) > 1 else 0)


def _bbox(text):
    """A stored face box back as a 4-tuple of floats, or `(0, 0, 1, 1)` for one
    that cannot be read.

    The fallback is the whole frame rather than an exception: this value is a
    crop rectangle, and a face row with a damaged box is worth showing as the
    picture it came from — a raise here would take out the whole People view
    over one bad string."""
    try:
        v = json.loads(text or "")
    except ValueError:
        return (0.0, 0.0, 1.0, 1.0)
    if not isinstance(v, list) or len(v) != 4:
        return (0.0, 0.0, 1.0, 1.0)
    try:
        return tuple(float(x) for x in v)
    except (TypeError, ValueError):
        return (0.0, 0.0, 1.0, 1.0)


def _vec(blob):
    """A centroid BLOB back as a float32 array; None for absent or unreadable.

    Stored as raw bytes rather than as JSON: it is 512 floats per person, read
    on every re-cluster, and a text round trip would both cost more and lose
    precision. None (rather than an empty array) because "this person has no
    centroid" is a real state — a row written before the vectors existed — and
    the matcher has to skip it rather than compare against zeros."""
    if not blob:
        return None
    try:
        v = np.frombuffer(blob, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    return v if v.size else None


def _blob(vec):
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


class Store:
    """A single sqlite3 connection (check_same_thread=False) is shared across
    threads — the daemon's HTTP handler threads read while the index thread
    writes. sqlite3 does not serialize concurrent statement execution on one
    connection, so every public method below is a self-contained
    execute→fetch/commit→return unit guarded by `_lock`. Locking per call
    (rather than around a whole caller-side operation) lets readers observe
    partial state while a long index run is in progress, instead of being
    blocked out for its entire duration."""

    def __init__(self, cache: Path):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.cache / "catalog.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._migrate()
        self.db.executescript(_SCHEMA)
        self.set_meta("schema_version", SCHEMA_VERSION)

    # -- schema ------------------------------------------------------------
    def _stored_version(self):
        """None when there is no meta table at all — a brand-new file, or one
        old enough to predate it."""
        try:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            return None
        return row["value"] if row else None

    def _columns(self):
        return {r["name"] for r in self.db.execute("PRAGMA table_info(photos)")}

    def _extend(self, columns) -> bool:
        """Add missing columns to `photos` in place. True if that worked.

        Every column added this way is nullable or defaulted, so existing rows
        answer for it immediately — sqlite fills them with the declared default
        (`source` → 'folder'), which is exactly what a row the walker created
        should say. False when there is no table to extend, or sqlite refuses:
        the caller then falls back to the reset, which always works."""
        try:
            have = self._columns()
            if not have:
                return False
            for name, decl in columns:
                if name not in have:
                    self.db.execute(
                        f"ALTER TABLE photos ADD COLUMN {name} {decl}")
            self.db.commit()
            return True
        except sqlite3.DatabaseError:
            return False

    def _migrate(self):
        """Bring an older catalog up to this schema — in place where the change
        only added columns, by dropping it where it did not.

        `CREATE TABLE IF NOT EXISTS` silently keeps an outdated table, so a
        catalog from before a column was added blew up on the next upsert
        with an OperationalError and no way forward. Nothing here is a system
        of record — every row is re-derivable from the photos on disk — so the
        cheapest *correct* migration is to drop it and re-index, and that is
        still the fallback. It is not the cheapest *acceptable* one: a full
        re-index of a real library is tens of minutes of GPU work, so a version
        step that merely adds columns (see _ADDITIVE_FROM) is applied with
        ALTER TABLE and keeps the rows, the thumbnails and the vectors.

        On the reset path the embedding matrices go too: their row ids referred
        to the discarded photo rows."""
        stored = self._stored_version()
        if stored == SCHEMA_VERSION:
            return
        if stored in _ADDITIVE_FROM and self._extend(_ADDED_SINCE_3):
            print("lens: catalog schema extended in place; no re-index needed")
            return
        existing = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        stale = {r["name"] for r in existing} & set(_TABLES)
        for table in _TABLES:
            self.db.execute(f"DROP TABLE IF EXISTS {table}")
        self.db.commit()
        for name in _EMB_FILES:
            (self.cache / name).unlink(missing_ok=True)
        if stale:                        # a fresh cache is not an "upgrade"
            print("lens: catalog schema upgraded, full re-index required")

    def upsert_photo(self, rec: dict) -> int:
        cols = ", ".join(_COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        sets = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c != "path")
        with self._lock:
            self.db.execute(
                f"INSERT INTO photos ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(path) DO UPDATE SET {sets}",
                [rec.get(c) if rec.get(c) is not None
                 else _COLUMN_DEFAULTS.get(c) for c in _COLUMNS],
            )
            self.db.commit()
            return self.db.execute(
                "SELECT id FROM photos WHERE path = ?", [rec["path"]]
            ).fetchone()["id"]

    def get_photo(self, path: str):
        with self._lock:
            row = self.db.execute("SELECT * FROM photos WHERE path = ?", [path]).fetchone()
            return dict(row) if row else None

    def get_photo_by_id(self, pid: int):
        with self._lock:
            row = self.db.execute("SELECT * FROM photos WHERE id = ?", [pid]).fetchone()
            return dict(row) if row else None

    def path_signatures(self) -> dict:
        with self._lock:
            rows = self.db.execute("SELECT path, mtime, size FROM photos").fetchall()
            return {r["path"]: (r["mtime"], r["size"]) for r in rows}

    def path_ids(self) -> dict:
        """{path: row id} — lets the indexer tell which catalogued files are
        missing an embedding without loading a row per file."""
        with self._lock:
            rows = self.db.execute("SELECT path, id FROM photos").fetchall()
            return {r["path"]: r["id"] for r in rows}

    def scope_counts(self) -> dict:
        """How many rows each /query scope can return: {"all", "photos",
        "videos"}.

        The three do not partition the library — "all" holds the graphics too,
        which are in neither of the others — and that is the point of showing all
        three: the toggle in the view can say what each of its buttons will
        actually return.

        `error IS NULL` because that is where every query's WHERE clause starts
        — a file Pillow could not open is catalogued but unsearchable, and
        counting it here would promise results that no query can produce (the
        view offers "search all N images" off this number). SUM() over no rows
        is NULL in sqlite, hence the COALESCE."""
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(CASE WHEN is_photo = 1 AND kind = 'image' "
                "                  THEN 1 ELSE 0 END), 0) AS p, "
                "COALESCE(SUM(CASE WHEN kind = 'video' THEN 1 ELSE 0 END), 0) AS v "
                "FROM photos WHERE error IS NULL").fetchone()
            return {"all": row["n"], "photos": row["p"], "videos": row["v"]}

    def searchable_paths(self):
        """[(path, is_photo)] for every row a query could return.

        Same `error IS NULL` gate as scope_counts, so the per-folder numbers
        the settings panel shows add up to the totals in the header rather than
        promising results from files nothing can open."""
        with self._lock:
            rows = self.db.execute(
                "SELECT path, is_photo FROM photos WHERE error IS NULL").fetchall()
            return [(r["path"], r["is_photo"]) for r in rows]

    def apple_paths(self) -> dict:
        """{path: apple_uuid} for every row that came from the Photos library.

        The Apple half of the library is pruned by uuid rather than by absence
        from a folder scan — a photo whose original iCloud offloaded between two
        runs is still in the library and must keep its row, exactly as a photo
        on an unmounted drive does. So the pruner needs to know which rows are
        Apple's, and what to diff them against."""
        with self._lock:
            rows = self.db.execute(
                "SELECT path, apple_uuid FROM photos WHERE source = 'apple'"
            ).fetchall()
            return {r["path"]: r["apple_uuid"] for r in rows}

    def source_counts(self) -> dict:
        """{source: rows} over the whole catalog — what the status line means by
        "N from Apple Photos". Counted over every row, unreadable ones included,
        to match `status.photos` (the library's size, not a search promise)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT COALESCE(source, 'folder') AS s, COUNT(*) AS n "
                "FROM photos GROUP BY s").fetchall()
            return {r["s"]: r["n"] for r in rows}

    def apple_phrases(self) -> list:
        """Album names and titles, as phrases a query can be matched against.

        Stored one per line in a single column rather than in a table of their
        own: an album is not an entity lens does anything with — it is words a
        photo can be found by, in the same way a camera name is (see
        query._consume_phrases). Splitting the column here is what turns the two
        into the same mechanism."""
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT apple_text FROM photos "
                "WHERE apple_text IS NOT NULL AND apple_text != ''").fetchall()
        seen, out = set(), []
        for r in rows:
            for phrase in str(r["apple_text"]).split("\n"):
                phrase = phrase.strip()
                key = phrase.lower()
                if len(phrase) >= MIN_PHRASE and key not in seen:
                    seen.add(key)
                    out.append(phrase)
        return out

    def remove_paths(self, paths):
        """Drop these photo rows — and the faces found in them.

        The faces go in the same transaction, because a face row outliving its
        photograph is not a stale cache: `faces.photo_id` is how the People view
        gets a picture to crop, and a person made of orphaned faces is a card
        that can never render. The *vectors* are the caller's to prune (see
        face_ids_for_paths), the same division of labour as photo embeddings."""
        paths = list(paths)
        if not paths:
            return
        with self._lock:
            self.db.executemany(
                "DELETE FROM faces WHERE photo_id IN "
                "(SELECT id FROM photos WHERE path = ?)", [[p] for p in paths])
            self.db.executemany("DELETE FROM photos WHERE path = ?",
                                [[p] for p in paths])
            self.db.commit()

    def apple_persons(self, photo_ids) -> dict:
        """`{photo id: [names]}` — who the Photos library says is in these
        photographs.

        Read out of `raw_exif._apple.persons`, where apple_photos.merge left it:
        the catalog has no column for a name list, and this is the only reader,
        so parsing the JSON here is cheaper than a column that would have to be
        kept in step. Only the ids asked for, because the caller only ever wants
        the photographs that actually have a detected face in them."""
        ids = [int(i) for i in photo_ids]
        if not ids:
            return {}
        marks = ", ".join("?" * len(ids))
        with self._lock:
            rows = self.db.execute(
                f"SELECT id, raw_exif FROM photos WHERE id IN ({marks}) "
                f"AND source = 'apple' AND raw_exif IS NOT NULL", ids).fetchall()
        out = {}
        for r in rows:
            try:
                raw = json.loads(r["raw_exif"] or "{}")
            except ValueError:
                continue
            ap = raw.get("_apple") if isinstance(raw, dict) else None
            names = (ap or {}).get("persons") if isinstance(ap, dict) else None
            names = [str(n).strip() for n in (names or []) if str(n).strip()]
            if names:
                out[r["id"]] = names
        return out

    def distinct(self, col: str):
        # interpolated straight into SQL, so it must be a known column name
        if col not in _COLUMNS:
            raise ValueError(f"unknown column {col!r}")
        with self._lock:
            rows = self.db.execute(
                f"SELECT DISTINCT {col} AS v FROM photos WHERE {col} IS NOT NULL"
            ).fetchall()
            return [r["v"] for r in rows]

    def query_photos(self, where: str, params: list):
        with self._lock:
            rows = self.db.execute(
                f"SELECT * FROM photos WHERE {where} ORDER BY taken_at DESC", params
            ).fetchall()
            return [dict(r) for r in rows]

    def set_meta(self, k: str, v: str):
        with self._lock:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", [k, v])
            self.db.commit()

    def get_meta(self, k: str):
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", [k]).fetchone()
            return row["value"] if row else None

    def replace_trips(self, trips: list, assignments: dict):
        with self._lock:
            self.db.execute("DELETE FROM trips")
            self.db.execute("UPDATE photos SET trip_id = NULL")
            self.db.executemany(
                "INSERT INTO trips (id, name, start, end, place) VALUES (?, ?, ?, ?, ?)",
                [[t["id"], t["name"], t["start"], t["end"], t["place"]] for t in trips])
            self.db.executemany(
                "UPDATE photos SET trip_id = ? WHERE id = ?",
                [[tid, pid] for pid, tid in assignments.items()])
            self.db.commit()

    def get_trips(self):
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM trips ORDER BY start")]

    def trip_counts(self) -> dict:
        """{trip id: (photo count, cover photo id)} for the trips view.

        The cover is the trip's earliest photo, and it is picked in the same
        statement as the count rather than by a query per trip: five trips is
        five round trips today and fifty tomorrow. SQLite's documented rule for
        a bare column beside a single MIN() is that it comes from the row that
        produced the minimum, which is exactly the photo wanted.

        The MIN() is taken over a CASE that is NULL for a row carrying no sha1,
        because the cover is about to be asked for as a thumbnail and a row that
        never got as far as a hash has none to serve. MIN ignores NULLs, so the
        cover is the earliest photo that *can* be shown — while COUNT(*) still
        counts the whole trip."""
        with self._lock:
            rows = self.db.execute(
                "SELECT trip_id, COUNT(*) AS n, "
                "MIN(CASE WHEN sha1 IS NOT NULL THEN taken_at END) AS first, "
                "id AS cover FROM photos "
                "WHERE trip_id IS NOT NULL AND error IS NULL "
                "GROUP BY trip_id").fetchall()
            return {r["trip_id"]: (r["n"], r["cover"] if r["first"] else None)
                    for r in rows}

    # -- faces -------------------------------------------------------------
    def faces_pending(self, version: str, limit: int = None):
        """Rows whose faces have not been looked for by *this* face-model
        generation: `[{id, path, sha1, kind}]`, oldest photograph first.

        `error IS NULL AND sha1 IS NOT NULL` because the face pass reads the
        thumbnail the index already rendered, and a row that never got as far as
        a hash has none. Ordered by `taken_at DESC` so that on a first,
        interrupted pass over a large library the faces that exist are the ones
        from the photographs the user is most likely looking at.

        `IS NOT ?` rather than `!= ?`: `faces_v` is NULL for every row in a
        catalog that predates the column, and `NULL != 'x'` is NULL in SQL — so
        the plain comparison silently matched nothing at all and no face was ever
        detected in an upgraded library."""
        sql = ("SELECT id, path, sha1, kind FROM photos "
               "WHERE error IS NULL AND sha1 IS NOT NULL "
               "AND faces_v IS NOT ? ORDER BY taken_at DESC")
        params = [version]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, params)]

    def photo_face_ids(self, photo_id: int) -> list:
        with self._lock:
            return [r["id"] for r in self.db.execute(
                "SELECT id FROM faces WHERE photo_id = ? ORDER BY id",
                [photo_id])]

    def replace_photo_faces(self, photo_id: int, faces: list,
                            version: str = None) -> list:
        """This photo's faces, as the only faces it has: the old rows go, the
        new ones are inserted, and the row is stamped with the generation that
        scanned it. Returns the new face ids, in the order given.

        One transaction for all three, because a partial application is a
        catalog that lies about itself in a way nothing recovers from: rows
        without a stamp are re-scanned (harmless), but a stamp without rows is a
        photo permanently claiming it has no faces in it.

        The delete is unconditional even when `faces` is empty — that is how a
        photograph someone edited a face out of loses it."""
        with self._lock:
            self.db.execute("DELETE FROM faces WHERE photo_id = ?", [photo_id])
            ids = []
            for f in faces:
                cur = self.db.execute(
                    "INSERT INTO faces (photo_id, bbox_json, prob, cluster_id) "
                    "VALUES (?, ?, ?, NULL)",
                    [photo_id, json.dumps([round(float(v), 6)
                                           for v in f["bbox"]]),
                     None if f.get("prob") is None else float(f["prob"])])
                ids.append(int(cur.lastrowid))
            if version is not None:
                self.db.execute("UPDATE photos SET faces_v = ? WHERE id = ?",
                                [version, photo_id])
            self.db.commit()
            return ids

    def all_faces(self) -> list:
        """Every face row, decoded: `[{id, photo_id, bbox, prob, cluster_id}]`.

        Exhaustive on purpose — this is what the clustering runs over, and a
        sample of the faces would produce a sample of the people."""
        with self._lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT id, photo_id, bbox_json, prob, cluster_id FROM faces "
                "ORDER BY id")]
        for r in rows:
            r["bbox"] = _bbox(r.pop("bbox_json"))
        return rows

    def get_face(self, face_id: int):
        """One face and the photo it is in, in a single row — the face's box
        plus the `sha1`/`path`/`kind` needed to render a crop of it."""
        with self._lock:
            row = self.db.execute(
                "SELECT f.id AS id, f.photo_id AS photo_id, f.bbox_json AS bbox_json, "
                "f.prob AS prob, f.cluster_id AS cluster_id, "
                "p.path AS path, p.sha1 AS sha1, p.kind AS kind "
                "FROM faces f JOIN photos p ON p.id = f.photo_id "
                "WHERE f.id = ?", [face_id]).fetchone()
        if not row:
            return None
        out = dict(row)
        out["bbox"] = _bbox(out.pop("bbox_json"))
        return out

    def faces_for_photos(self, photo_ids) -> dict:
        """`{photo id: [{id, bbox, prob, cluster_id}]}` for the ids given.

        One statement for the whole set: the details panel asks for one photo,
        but the audit asks for a sample of twenty-five, and that must not be
        twenty-five round trips."""
        ids = [int(i) for i in photo_ids]
        if not ids:
            return {}
        marks = ", ".join("?" * len(ids))
        with self._lock:
            rows = [dict(r) for r in self.db.execute(
                f"SELECT id, photo_id, bbox_json, prob, cluster_id FROM faces "
                f"WHERE photo_id IN ({marks}) ORDER BY id", ids)]
        out = {}
        for r in rows:
            r["bbox"] = _bbox(r.pop("bbox_json"))
            out.setdefault(r["photo_id"], []).append(r)
        return out

    def face_ids_for_paths(self, paths) -> list:
        """Face ids belonging to these photo paths — read *before* the rows are
        deleted, because that is the only moment the link still exists and the
        caller has a matrix to prune."""
        paths = list(paths)
        if not paths:
            return []
        marks = ", ".join("?" * len(paths))
        with self._lock:
            return [r["id"] for r in self.db.execute(
                f"SELECT f.id AS id FROM faces f JOIN photos p ON p.id = f.photo_id "
                f"WHERE p.path IN ({marks})", paths)]

    def set_face_persons(self, mapping: dict):
        """`{face id: person id or None}` written in one transaction.

        One statement per face rather than one per person: the mapping is the
        clustering's whole answer, and applying it in pieces would leave the
        catalog with faces pointing at last run's people while a query is
        reading them."""
        if not mapping:
            return
        with self._lock:
            self.db.executemany(
                "UPDATE faces SET cluster_id = ? WHERE id = ?",
                [[None if pid is None else int(pid), int(fid)]
                 for fid, pid in mapping.items()])
            self.db.commit()

    # -- people ------------------------------------------------------------
    def get_persons(self, include_merged: bool = False) -> list:
        """`[{id, name, cover_face_id, centroid, merged_into}]`, centroid decoded
        to a float32 array (or None).

        `include_merged` is for the clustering, which has to see the absorbed
        rows: their centroids are what makes a merge survive a re-cluster. Every
        other caller wants the people who currently exist."""
        sql = "SELECT * FROM persons"
        if not include_merged:
            sql += " WHERE merged_into IS NULL"
        sql += " ORDER BY id"
        with self._lock:
            rows = [dict(r) for r in self.db.execute(sql)]
        for r in rows:
            r["centroid"] = _vec(r.get("centroid"))
        return rows

    def replace_persons(self, persons: list):
        """The people this run computed, written over the ones it recomputed.

        An upsert per row, not a DELETE + INSERT: `persons.id` is what a URL, a
        rename and a merge all point at, and dropping the table would break
        every one of them for the duration of the write. Rows that no longer
        correspond to a cluster keep their names and centroids too — a person
        whose only three photographs are on an unplugged drive comes back as
        themselves when it is reconnected, rather than as a new stranger.

        `name` is *not* written here: names belong to the user (or to a seeded
        agreement) and this call carries whatever the clustering inherited, which
        would otherwise overwrite a rename that landed mid-run."""
        if not persons:
            return
        with self._lock:
            self.db.executemany(
                "INSERT INTO persons (id, name, cover_face_id, centroid) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET cover_face_id=excluded.cover_face_id, "
                "centroid=excluded.centroid",
                [[int(p["id"]), p.get("name"),
                  None if p.get("cover_face_id") is None
                  else int(p["cover_face_id"]),
                  _blob(p.get("centroid"))] for p in persons])
            self.db.commit()

    def person_counts(self) -> dict:
        """`{person id: (faces, photos)}` — the two numbers a card shows.

        Both from one grouped statement: a person is a set of faces, but the
        photographs are what the user will page through, and a group shot with
        three of the same person in it is one photo and three faces."""
        with self._lock:
            rows = self.db.execute(
                "SELECT cluster_id, COUNT(*) AS faces, "
                "COUNT(DISTINCT photo_id) AS photos FROM faces "
                "WHERE cluster_id IS NOT NULL GROUP BY cluster_id").fetchall()
            return {r["cluster_id"]: (r["faces"], r["photos"]) for r in rows}

    def set_person_name(self, person_id: int, name: str = None) -> bool:
        """Name (or un-name) one person. False if there is no such person — a
        rename against a stale id is a 404, not a silent success."""
        with self._lock:
            row = self.db.execute(
                "SELECT id FROM persons WHERE id = ? AND merged_into IS NULL",
                [person_id]).fetchone()
            if not row:
                return False
            self.db.execute("UPDATE persons SET name = ? WHERE id = ?",
                            [name or None, person_id])
            self.db.commit()
            return True

    def merge_persons(self, keep: int, absorb: int) -> bool:
        """Fold `absorb` into `keep`: its faces change hands, its row stays.

        The row stays *on purpose*, carrying `merged_into`. It is not
        bookkeeping: re-clustering recomputes the groups from scratch after every
        index run, and the absorbed centroid is what that run matches its second
        group against — following the pointer is what makes a merge survive
        (see persons.assign_persons). Deleting the row would let the two people
        split apart again on the next index.

        The kept person's centroid is recomputed by the next re-cluster from the
        combined faces. The name is filled in from the absorbed row when the kept
        one has none, because "merge the unnamed one into Ana" and "merge Ana
        into the unnamed one" are the same intention."""
        keep, absorb = int(keep), int(absorb)
        if keep == absorb:
            return False
        with self._lock:
            rows = {r["id"]: dict(r) for r in self.db.execute(
                "SELECT id, name, cover_face_id FROM persons "
                "WHERE id IN (?, ?) AND merged_into IS NULL", [keep, absorb])}
            if keep not in rows or absorb not in rows:
                return False
            self.db.execute(
                "UPDATE faces SET cluster_id = ? WHERE cluster_id = ?",
                [keep, absorb])
            if not rows[keep].get("name") and rows[absorb].get("name"):
                self.db.execute("UPDATE persons SET name = ? WHERE id = ?",
                                [rows[absorb]["name"], keep])
            if rows[keep].get("cover_face_id") is None:
                self.db.execute(
                    "UPDATE persons SET cover_face_id = ? WHERE id = ?",
                    [rows[absorb].get("cover_face_id"), keep])
            self.db.execute(
                "UPDATE persons SET merged_into = ?, name = NULL WHERE id = ?",
                [keep, absorb])
            self.db.commit()
            return True

    def person_names(self) -> list:
        """Named people, as phrases a query can be matched against:
        `[(id, name)]`.

        Short names are left out by the same rule album names are (MIN_PHRASE):
        the vocabulary is matched as whole words anywhere in a query, so a person
        called "Jo" would turn every "jo" into a filter."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, name FROM persons "
                "WHERE name IS NOT NULL AND name != '' AND merged_into IS NULL "
                "ORDER BY id").fetchall()
        return [(r["id"], r["name"]) for r in rows
                if len(str(r["name"]).strip()) >= MIN_PHRASE]

    def face_counts(self) -> dict:
        """`{"faces", "clustered", "people", "scanned"}` — the library's face
        totals, for a status poll and the audit. `scanned` is how many rows the
        face pass has been over, which is the only honest denominator for the
        rest: 40 faces in a library where 90 of 1,800 rows have been scanned is
        a different fact from 40 in a library that is finished."""
        with self._lock:
            faces = self.db.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(CASE WHEN cluster_id IS NOT NULL THEN 1 ELSE 0 END), 0) "
                "AS c FROM faces").fetchone()
            people = self.db.execute(
                "SELECT COUNT(*) AS n FROM persons "
                "WHERE merged_into IS NULL AND id IN "
                "(SELECT DISTINCT cluster_id FROM faces "
                " WHERE cluster_id IS NOT NULL)").fetchone()
            scanned = self.db.execute(
                "SELECT COUNT(*) AS n FROM photos "
                "WHERE faces_v IS NOT NULL").fetchone()
            # The denominator `scanned` is a fraction of: every row the face pass
            # will ever look at. Same gate as faces_pending, so the two agree by
            # construction rather than by both being right.
            eligible = self.db.execute(
                "SELECT COUNT(*) AS n FROM photos "
                "WHERE error IS NULL AND sha1 IS NOT NULL").fetchone()
            named = self.db.execute(
                "SELECT COUNT(*) AS n FROM persons "
                "WHERE merged_into IS NULL AND name IS NOT NULL "
                "AND name != ''").fetchone()
        return {"faces": faces["n"], "clustered": faces["c"],
                "people": people["n"], "named": named["n"],
                "scanned": scanned["n"], "eligible": eligible["n"]}

    @staticmethod
    def _no_embeddings():
        """What a caller gets when there is no usable generation on disk.

        Deliberately the same answer for "nothing indexed yet" and "what is
        there cannot be trusted": both mean the vectors have to be rebuilt, and
        the indexer already treats a row with no vector as work to do (see the
        `pid not in emb` clause in index_once). Recovery is a re-index, not a
        traceback the user cannot act on."""
        return np.zeros((0,), dtype=np.int64), np.zeros((0, 0), dtype=np.float16)

    def _load_legacy(self):
        """The pre-npz two-file generation, if it is internally consistent.

        Consistency has to be checked rather than assumed: these two files were
        swapped one after the other, so a cache written by an older lens — or
        one killed between the two swaps — can hold an id list and a matrix of
        different lengths. Reading it would mean handing out vectors belonging
        to the wrong photos, so a mismatched pair is treated as absent."""
        ids_p, mat_p = (self.cache / n for n in _EMB_LEGACY)
        if not (ids_p.exists() and mat_p.exists()):
            return self._no_embeddings()
        try:
            ids, mat = np.load(ids_p), np.load(mat_p)
        except _BAD_ARCHIVE:
            return self._no_embeddings()
        if len(ids) != mat.shape[0]:
            return self._no_embeddings()
        return ids, mat

    def load_embeddings(self):
        with self._lock:
            npz = self.cache / _EMB_NPZ
            if not npz.exists():
                return self._load_legacy()
            try:
                with np.load(npz) as z:
                    return z["ids"], z["mat"]
            except _BAD_ARCHIVE:
                return self._no_embeddings()

    def embedding_shape(self):
        """`(count, dims)` of the stored generation, without reading it.

        An .npz is an uncompressed zip of .npy members, and a .npy member's
        header states its shape — so the answer costs a few hundred bytes off
        disk instead of the whole matrix. That is the difference between /status
        being free and /status re-reading 4MB every ten seconds for as long as
        anyone has the view open.

        `(0, 0)` for a cache with nothing in it, and for one whose file cannot
        be read: this is a fact to display, never a reason to fail a poll."""
        with self._lock:
            npz = self.cache / _EMB_NPZ
            if not npz.exists():
                legacy = self.cache / _EMB_LEGACY[1]
                if not legacy.exists():
                    return 0, 0
                try:
                    with open(legacy, "rb") as f:
                        return _npy_shape(f)
                except _BAD_ARCHIVE:
                    return 0, 0
            try:
                with zipfile.ZipFile(npz) as z, z.open("mat.npy") as f:
                    return _npy_shape(f)
            except _BAD_ARCHIVE:
                return 0, 0

    def _write_npz(self, name, ids, mat):
        """One generation of vectors, written to a temp file and swapped in with
        a single os.replace.

        The index checkpoints these every CHECKPOINT_EVERY images through a run
        that lasts tens of minutes, so the write must be safe against being
        interrupted at any instant: writing in place left a window where a
        query read a truncated file, and writing two files left a window where
        it read two halves of different generations. One file and one atomic
        rename close both — there is no state between "the old generation" and
        "the new one" for anything to observe.

        Shared by the image vectors and the face vectors because it is the same
        guarantee, not because the two are the same data: they are separate
        files, written at separate cadences by separate pipeline stages."""
        path = self.cache / name
        # np.savez appends ".npz" to a *name* that lacks it, which would
        # make the temp file permanent; an open handle is written verbatim
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "wb") as f:
                np.savez(f, ids=np.asarray(ids).astype(np.int64),
                         mat=np.asarray(mat).astype(np.float16))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)      # never leave a partial beside it
            raise

    def save_embeddings(self, ids, mat):
        with self._lock:
            self._write_npz(_EMB_NPZ, ids, mat)
            # Only now, with the generation safely in one file: the pair is
            # dead weight, and leaving it would let a later downgrade read
            # vectors this run has already superseded.
            for name in _EMB_LEGACY:
                (self.cache / name).unlink(missing_ok=True)

    # -- face vectors ------------------------------------------------------
    def load_faces(self):
        """`(ids, matrix)` for the face vectors — row i belongs to face ids[i].

        Same contract, same failure mode and same recovery as load_embeddings:
        an unreadable file reads as an empty generation, and the indexer treats a
        face row with no vector as work to do."""
        with self._lock:
            npz = self.cache / _FACE_NPZ
            if not npz.exists():
                return self._no_embeddings()
            try:
                with np.load(npz) as z:
                    return z["ids"], z["mat"]
            except _BAD_ARCHIVE:
                return self._no_embeddings()

    def save_faces(self, ids, mat):
        with self._lock:
            self._write_npz(_FACE_NPZ, ids, mat)

    def faces_vector_shape(self):
        """`(count, dims)` of the face matrix, read from its header — the same
        trick embedding_shape uses, for the same reason (a poll must not read
        the whole matrix)."""
        with self._lock:
            npz = self.cache / _FACE_NPZ
            if not npz.exists():
                return 0, 0
            try:
                with zipfile.ZipFile(npz) as z, z.open("mat.npy") as f:
                    return _npy_shape(f)
            except _BAD_ARCHIVE:
                return 0, 0

    def close(self):
        with self._lock:
            self.db.close()
