import sqlite3
import zipfile

import numpy as np
import pytest

from lens.store import (_ADDED_IN_5, _ADDED_SINCE_3, SCHEMA_VERSION, Store,
                        _npy_shape)

REC = {
    "path": "/p/a.jpg", "sha1": "abc", "size": 10, "mtime": 1.0,
    "width": 100, "height": 50, "format": "JPEG",
    "taken_at": "2025-07-01T10:00:00", "lat": -8.4, "lon": 115.1,
    "place_city": "Ubud", "place_country": "Indonesia",
    "camera": "Apple iPhone 15 Pro", "lens": "Main", "iso": 100,
    "f_number": 1.8, "exposure": "1/120", "focal_length": 6.8,
    "raw_exif": "{\"Make\": \"Apple\"}", "error": None,
}


def test_upsert_and_get(cache_dir):
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    row = s.get_photo("/p/a.jpg")
    assert row["id"] == pid and row["place_city"] == "Ubud"
    pid2 = s.upsert_photo(dict(REC, iso=200))
    assert pid2 == pid                      # same path → same row
    assert s.get_photo("/p/a.jpg")["iso"] == 200


def test_signatures_remove_distinct(cache_dir):
    s = Store(cache_dir)
    s.upsert_photo(dict(REC))
    s.upsert_photo(dict(REC, path="/p/b.jpg", place_city="Bali"))
    assert s.path_signatures() == {"/p/a.jpg": (1.0, 10), "/p/b.jpg": (1.0, 10)}
    assert sorted(s.distinct("place_city")) == ["Bali", "Ubud"]
    s.remove_paths(["/p/a.jpg"])
    assert list(s.path_signatures()) == ["/p/b.jpg"]


def test_query_meta_trips(cache_dir):
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    rows = s.query_photos("place_city = ?", ["Ubud"])
    assert [r["id"] for r in rows] == [pid]
    s.set_meta("status", "idle")
    assert s.get_meta("status") == "idle"
    s.replace_trips(
        [{"id": 1, "name": "Ubud · Jul 2025", "start": "2025-07-01",
          "end": "2025-07-05", "place": "Ubud"}], {pid: 1})
    assert s.get_trips()[0]["name"] == "Ubud · Jul 2025"
    assert s.get_photo("/p/a.jpg")["trip_id"] == 1


def test_scope_counts_split_photos_from_graphics(cache_dir):
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, is_photo=1))
    s.upsert_photo(dict(REC, path="/p/logo.png", is_photo=0))
    s.upsert_photo(dict(REC, path="/p/unclassified.png"))     # is_photo NULL
    assert s.scope_counts() == {"all": 3, "photos": 1, "videos": 0}
    # NULL is not 1, so a row the indexer never classified stays out of the
    # photos scope rather than defaulting into it
    assert [r["path"] for r in s.query_photos("is_photo = 1", [])] == ["/p/a.jpg"]


def test_scope_counts_exclude_unreadable_files(cache_dir):
    """These counts describe what a query can return, and every query's WHERE
    clause starts with `error IS NULL`. Counting a file Pillow cannot open would
    promise results no query can produce."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, is_photo=1))
    s.upsert_photo(dict(REC, path="/p/torn.jpg", is_photo=1, error="boom"))
    s.upsert_photo(dict(REC, path="/p/torn.png", is_photo=0, error="boom"))
    assert s.scope_counts() == {"all": 1, "photos": 1, "videos": 0}


def test_scope_counts_on_an_empty_catalog(cache_dir):
    """SUM() over no rows is NULL in sqlite, which would serialise as a null
    count in /status."""
    assert Store(cache_dir).scope_counts() == {"all": 0, "photos": 0, "videos": 0}


def test_distinct_rejects_unknown_column(cache_dir):
    """The column name is interpolated into SQL, so it is validated even when
    assertions are stripped (-O)."""
    s = Store(cache_dir)
    with pytest.raises(ValueError):
        s.distinct("path FROM photos; DROP TABLE photos; --")


def _make_v1_catalog(cache_dir):
    """A catalog in the shape that shipped before place_region existed."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC))
    s.save_embeddings(np.array([1], dtype=np.int64),
                      np.ones((1, 4), dtype=np.float16))
    s.close()

    db = sqlite3.connect(cache_dir / "catalog.sqlite")
    cols = [c for c in (r[1] for r in db.execute("PRAGMA table_info(photos)"))
            if c != "place_region"]
    db.execute("CREATE TABLE old AS SELECT %s FROM photos" % ", ".join(cols))
    db.execute("DROP TABLE photos")
    db.execute("ALTER TABLE old RENAME TO photos")
    db.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    db.commit()
    db.close()


def test_outdated_catalog_is_rebuilt_not_fatal(cache_dir, capsys):
    """A catalog written by an older schema used to fail every upsert with
    OperationalError (no such column: place_region) and offer no way out.
    It is a cache, so the fix is to drop it and re-index."""
    _make_v1_catalog(cache_dir)
    assert (cache_dir / "embeddings.npz").exists()

    s = Store(cache_dir)                       # must not raise

    assert "full re-index required" in capsys.readouterr().out
    assert s.get_meta("schema_version") == SCHEMA_VERSION
    assert s.path_signatures() == {}            # rebuilt empty, ready to fill
    ids, mat = s.load_embeddings()               # stale vectors dropped too
    assert ids.shape == (0,)
    assert not (cache_dir / "embeddings.npz").exists()

    pid = s.upsert_photo(dict(REC, place_region="Bali"))
    assert s.get_photo("/p/a.jpg")["place_region"] == "Bali"
    assert s.query_photos("place_region = ?", ["Bali"])[0]["id"] == pid


def _make_v3_catalog(cache_dir):
    """A catalog in the shape that shipped before Apple Photos: everything this
    schema has, minus the three columns v4 added."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, is_photo=1))
    s.upsert_photo(dict(REC, path="/p/b.jpg", is_photo=1))
    s.save_embeddings(np.array([1, 2], dtype=np.int64),
                      np.ones((2, 4), dtype=np.float16))
    s.close()

    # The v3 table, written out: rebuilding it with CREATE TABLE AS SELECT would
    # quietly drop the UNIQUE(path) constraint every upsert conflicts on, and
    # ALTER TABLE DROP COLUMN re-parses the original CREATE statement, which
    # sqlite cannot do with the comments in ours.
    gone = {c for c, _ in _ADDED_SINCE_3}
    db = sqlite3.connect(cache_dir / "catalog.sqlite")
    cols = [c for c in (r[1] for r in db.execute("PRAGMA table_info(photos)"))
            if c not in gone and c != "id"]
    for index in ("idx_source", "idx_kind"):          # v3 had no such columns
        db.execute(f"DROP INDEX IF EXISTS {index}")
    db.execute("CREATE TABLE old (id INTEGER PRIMARY KEY, "
               "path TEXT UNIQUE NOT NULL, %s)"
               % ", ".join(f"{c} BLOB" for c in cols if c != "path"))
    db.execute("INSERT INTO old (id, %s) SELECT id, %s FROM photos"
               % (", ".join(cols), ", ".join(cols)))
    db.execute("DROP TABLE photos")
    db.execute("ALTER TABLE old RENAME TO photos")
    db.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    db.commit()
    db.close()


def test_the_apple_columns_are_added_without_a_re_index(cache_dir, capsys):
    """Schema 4 only *adds* columns, and re-indexing a real library to learn
    nothing about the rows already in it is tens of minutes of GPU work. So a v3
    catalog is extended in place and keeps every row, every thumbnail and every
    vector — the reset path stays for versions far enough back that their
    existing columns disagree."""
    _make_v3_catalog(cache_dir)

    s = Store(cache_dir)

    assert "extended in place" in capsys.readouterr().out
    assert s.get_meta("schema_version") == SCHEMA_VERSION
    assert sorted(s.path_signatures()) == ["/p/a.jpg", "/p/b.jpg"]
    ids, mat = s.load_embeddings()
    assert list(ids) == [1, 2] and mat.shape == (2, 4)      # vectors survive
    # a row the walker created answers 'folder' for the column it never wrote
    row = s.get_photo("/p/a.jpg")
    assert row["source"] == "folder"
    assert row["apple_uuid"] is None and row["apple_text"] is None
    assert s.source_counts() == {"folder": 2}
    # and the extended table takes the new columns
    s.upsert_photo(dict(REC, path="/p/c.jpg", source="apple", apple_uuid="u1",
                        apple_text="Bali"))
    assert s.apple_paths() == {"/p/c.jpg": "u1"}


def _make_v4_catalog(cache_dir):
    """A catalog in the shape that shipped before videos: everything this schema
    has, minus the two columns v5 added."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, is_photo=1))
    s.upsert_photo(dict(REC, path="/p/b.png", is_photo=0))
    s.save_embeddings(np.array([1, 2], dtype=np.int64),
                      np.ones((2, 4), dtype=np.float16))
    s.close()

    gone = {c for c, _ in _ADDED_IN_5}
    db = sqlite3.connect(cache_dir / "catalog.sqlite")
    cols = [c for c in (r[1] for r in db.execute("PRAGMA table_info(photos)"))
            if c not in gone and c != "id"]
    db.execute("DROP INDEX IF EXISTS idx_kind")       # v4 had no such column
    db.execute("CREATE TABLE old (id INTEGER PRIMARY KEY, "
               "path TEXT UNIQUE NOT NULL, %s)"
               % ", ".join(f"{c} BLOB" for c in cols if c != "path"))
    db.execute("INSERT INTO old (id, %s) SELECT id, %s FROM photos"
               % (", ".join(cols), ", ".join(cols)))
    db.execute("DROP TABLE photos")
    db.execute("ALTER TABLE old RENAME TO photos")
    db.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    db.commit()
    db.close()


def test_the_video_columns_are_added_without_a_re_index(cache_dir, capsys):
    """Schema 5 only adds `kind` and `duration_s`, so a v4 catalog is extended in
    place — the alternative is re-embedding a whole library of photographs to
    learn that every one of them is still a photograph.

    The declared default is what makes that safe: every row already there
    answers 'image' for a column it never wrote, which is exactly what it is, so
    the photos scope keeps returning them the moment the daemon comes up."""
    _make_v4_catalog(cache_dir)

    s = Store(cache_dir)

    assert "extended in place" in capsys.readouterr().out
    assert s.get_meta("schema_version") == SCHEMA_VERSION
    assert sorted(s.path_signatures()) == ["/p/a.jpg", "/p/b.png"]
    ids, mat = s.load_embeddings()
    assert list(ids) == [1, 2] and mat.shape == (2, 4)      # vectors survive
    row = s.get_photo("/p/a.jpg")
    assert row["kind"] == "image" and row["duration_s"] is None
    assert s.scope_counts() == {"all": 2, "photos": 1, "videos": 0}
    # ...and the extended table takes a video
    s.upsert_photo(dict(REC, path="/p/c.mov", kind="video", duration_s=6.5,
                        is_photo=0))
    assert s.get_photo("/p/c.mov")["duration_s"] == 6.5
    assert s.scope_counts()["videos"] == 1


def test_kind_and_duration_round_trip(cache_dir):
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, path="/p/clip.mov", kind="video", duration_s=42.25,
                        is_photo=0, format="MOV"))
    row = s.get_photo("/p/clip.mov")
    assert row["kind"] == "video" and row["duration_s"] == 42.25


def test_kind_defaults_to_image_however_the_row_arrived(cache_dir):
    """Every scope filter tests this column, so a NULL in it is a row in no scope
    at all — counted in the library and unreachable by any search. The caller (an
    error row built from four fields, say) should not have to remember."""
    s = Store(cache_dir)
    s.upsert_photo({"path": "/p/torn.jpg", "error": "boom"})
    assert s.get_photo("/p/torn.jpg")["kind"] == "image"


def test_scope_counts_separate_videos_from_photographs(cache_dir):
    """The three scopes do not partition the library — "all" also holds the
    graphics, which are in neither of the others — and the toggle in the view
    labels each of its buttons off these numbers."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, is_photo=1))
    s.upsert_photo(dict(REC, path="/p/logo.png", is_photo=0))
    s.upsert_photo(dict(REC, path="/p/clip.mov", kind="video", is_photo=0))
    s.upsert_photo(dict(REC, path="/p/rec.webm", kind="video", is_photo=0))
    assert s.scope_counts() == {"all": 4, "photos": 1, "videos": 2}


def test_a_video_is_never_counted_as_a_photograph(cache_dir):
    """Belt and braces: `is_photo` is derived at index time, and a video row that
    somehow carried a 1 must still not be a photograph — the two columns are
    tested together (see store.scope_counts)."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, path="/p/clip.mov", kind="video", is_photo=1))
    assert s.scope_counts() == {"all": 1, "photos": 0, "videos": 1}


def test_an_unextendable_catalog_still_falls_back_to_the_reset(cache_dir, capsys,
                                                              monkeypatch):
    """ALTER TABLE is the cheap path, not the guaranteed one. When it cannot be
    taken the catalog is a cache like any other and is rebuilt."""
    _make_v3_catalog(cache_dir)
    monkeypatch.setattr(Store, "_extend", lambda self, cols: False)

    s = Store(cache_dir)

    assert "full re-index required" in capsys.readouterr().out
    assert s.path_signatures() == {}
    assert s.get_meta("schema_version") == SCHEMA_VERSION


def test_source_defaults_to_folder_however_the_row_arrived(cache_dir):
    """Every read that separates the two ingest paths asks this column, so a NULL
    in it is a row belonging to neither — and the caller (an error row built from
    four fields, say) should not have to remember."""
    s = Store(cache_dir)
    s.upsert_photo({"path": "/p/torn.jpg", "error": "boom"})
    assert s.get_photo("/p/torn.jpg")["source"] == "folder"
    s.upsert_photo(dict(REC, source="apple", apple_uuid="u1"))
    assert s.source_counts() == {"folder": 1, "apple": 1}


def test_apple_paths_and_phrases_describe_only_the_apple_rows(cache_dir):
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, camera="Nikon"))                    # a folder row
    s.upsert_photo(dict(REC, path="/p/x.jpg", source="apple", apple_uuid="u1",
                        apple_text="Sunset\nBali 2025"))
    s.upsert_photo(dict(REC, path="/p/y.jpg", source="apple", apple_uuid="u2",
                        apple_text="Bali 2025\nMe"))

    assert s.apple_paths() == {"/p/x.jpg": "u1", "/p/y.jpg": "u2"}
    # one phrase per line, de-duplicated across rows, and "Me" is too short to be
    # offered as a search phrase — a two-letter vocabulary entry hijacks ordinary
    # English the way bare country codes did
    assert sorted(s.apple_phrases()) == ["Bali 2025", "Sunset"]


def test_apple_phrases_on_a_library_with_no_apple_rows(cache_dir):
    s = Store(cache_dir)
    s.upsert_photo(dict(REC))
    assert s.apple_phrases() == []
    assert s.apple_paths() == {}


def test_fresh_catalog_is_not_announced_as_an_upgrade(cache_dir, capsys):
    Store(cache_dir)
    assert capsys.readouterr().out == ""


def test_reopening_a_current_catalog_keeps_its_rows(cache_dir, capsys):
    s = Store(cache_dir)
    s.upsert_photo(dict(REC))
    s.close()
    s2 = Store(cache_dir)
    assert list(s2.path_signatures()) == ["/p/a.jpg"]
    assert capsys.readouterr().out == ""


def test_embeddings_roundtrip(cache_dir):
    s = Store(cache_dir)
    ids0, mat0 = s.load_embeddings()
    assert ids0.shape == (0,) and mat0.shape == (0, 0)
    ids = np.array([1, 2], dtype=np.int64)
    mat = np.ones((2, 4), dtype=np.float16)
    s.save_embeddings(ids, mat)
    ids2, mat2 = s.load_embeddings()
    assert ids2.tolist() == [1, 2] and mat2.shape == (2, 4)


def test_a_generation_is_swapped_in_with_one_rename(cache_dir, monkeypatch):
    """Ids and matrix are positional halves of one fact, so they move together.

    Two files could not: a kill between the two renames left N+1 ids against N
    rows, and that is not a stale index but a poisoned one — every semantic
    query raised, and so did the next index run, which has to read the pair
    before it can rebuild it. One file and one rename leave no instant at which
    a torn generation exists to be observed."""
    import os

    import numpy as np

    from lens.store import Store

    s = Store(cache_dir)
    s.save_embeddings(np.array([1, 2], dtype=np.int64),
                      np.ones((2, 4), dtype=np.float16))

    swaps = []
    real_replace = os.replace

    def watched(src, dst):
        # what is on disk at the instant before the swap — i.e. what a reader
        # that got in just now would see
        with np.load(cache_dir / "embeddings.npz") as z:
            swaps.append((os.path.basename(dst), z["ids"].tolist(),
                          z["mat"].shape))
        return real_replace(src, dst)

    monkeypatch.setattr("lens.store.os.replace", watched)
    s.save_embeddings(np.array([1, 2, 3], dtype=np.int64),
                      np.zeros((3, 4), dtype=np.float16))

    # exactly one rename, and the whole previous generation up to it
    assert [name for name, _, _ in swaps] == ["embeddings.npz"]
    assert [(i, m) for _, i, m in swaps] == [([1, 2], (2, 4))]
    assert not list(cache_dir.glob("*.tmp"))

    ids, mat = s.load_embeddings()
    assert ids.tolist() == [1, 2, 3] and mat.shape == (3, 4)
    s.close()


def test_a_failed_write_leaves_the_previous_generation_alone(cache_dir,
                                                             monkeypatch):
    import numpy as np

    from lens.store import Store

    s = Store(cache_dir)
    s.save_embeddings(np.array([1, 2], dtype=np.int64),
                      np.ones((2, 4), dtype=np.float16))

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("lens.store.os.replace", boom)
    try:
        s.save_embeddings(np.array([9], dtype=np.int64),
                          np.zeros((1, 4), dtype=np.float16))
    except OSError:
        pass
    ids, mat = s.load_embeddings()
    assert ids.tolist() == [1, 2] and mat.shape == (2, 4)
    assert not list(cache_dir.glob("*.tmp"))
    s.close()


def test_a_torn_legacy_pair_reads_as_no_embeddings_at_all(cache_dir):
    """The shape a lens that wrote two files could be killed into. Handing the
    mismatched pair back would attach vectors to the wrong photos; raising
    would break search *and* the index run that exists to repair it. "Nothing
    indexed" is the one answer that is both true and recoverable — the indexer
    already rebuilds every row it has no vector for."""
    import numpy as np

    from lens.store import Store

    Store(cache_dir).close()          # a catalog already at the live schema,
    # so opening it again is not a migration that would drop these outright
    np.save(cache_dir / "emb_ids.npy", np.array([1, 2, 3], dtype=np.int64))
    np.save(cache_dir / "embeddings.npy", np.ones((2, 4), dtype=np.float16))

    s = Store(cache_dir)
    ids, mat = s.load_embeddings()
    assert ids.shape == (0,) and mat.shape[0] == 0

    # ...and the same for a pair missing its other half entirely
    (cache_dir / "embeddings.npy").unlink()
    assert s.load_embeddings()[0].shape == (0,)
    s.close()


def test_a_consistent_legacy_pair_still_loads_then_is_retired(cache_dir):
    """An existing cache must not be thrown away by the upgrade — but the next
    generation written replaces it, and the superseded files go with it."""
    import numpy as np

    from lens.store import Store

    Store(cache_dir).close()                     # see the torn-pair test
    np.save(cache_dir / "emb_ids.npy", np.array([7, 8], dtype=np.int64))
    np.save(cache_dir / "embeddings.npy",
            np.full((2, 4), 0.5, dtype=np.float16))

    s = Store(cache_dir)
    ids, mat = s.load_embeddings()
    assert ids.tolist() == [7, 8] and mat.shape == (2, 4)

    s.save_embeddings(np.array([7, 8, 9], dtype=np.int64),
                      np.zeros((3, 4), dtype=np.float16))
    assert (cache_dir / "embeddings.npz").exists()
    assert not (cache_dir / "emb_ids.npy").exists()
    assert not (cache_dir / "embeddings.npy").exists()
    assert s.load_embeddings()[0].tolist() == [7, 8, 9]
    s.close()


def test_a_corrupt_generation_is_a_rebuild_not_a_traceback(cache_dir):
    import numpy as np

    from lens.store import Store

    s = Store(cache_dir)
    s.save_embeddings(np.array([1], dtype=np.int64),
                      np.ones((1, 4), dtype=np.float16))
    (cache_dir / "embeddings.npz").write_bytes(b"not an archive")
    ids, mat = s.load_embeddings()
    assert ids.shape == (0,) and mat.shape[0] == 0
    s.close()


# ── what a trip card needs ─────────────────────────────────────────────────
def _trip(tid, name="Ubud · Jul 2025"):
    return {"id": tid, "name": name, "start": "2025-07-01",
            "end": "2025-07-05", "place": "Ubud"}


def _in_trip(s, tid, n, **overrides):
    """`n` photos assigned to trip `tid`, one per day so their order is known."""
    ids = []
    for i in range(n):
        ids.append(s.upsert_photo(dict(
            REC, path=f"/t{tid}/{i}.jpg",
            taken_at=f"2025-07-0{i + 1}T09:00:00",
            **{k: v[i] for k, v in overrides.items()})))
    return ids


def test_trip_counts_gives_each_trip_its_size_and_a_cover(cache_dir):
    """The trips view draws a card per trip, and a card cannot be drawn from a
    name alone: it needs the size and one photo to show. Both come out of a
    single statement rather than a query per trip — five trips is five round
    trips today and fifty tomorrow."""
    s = Store(cache_dir)
    first, second, third = _in_trip(s, 1, 3)
    other = s.upsert_photo(dict(REC, path="/loose.jpg"))       # in no trip
    s.replace_trips([_trip(1)], {first: 1, second: 1, third: 1})

    assert s.trip_counts() == {1: (3, first)}     # the earliest photo is the cover
    assert other not in (first, second, third)

    # a trip nothing ended up in produces no row at all, which is why the
    # daemon reads this mapping with a (0, None) default
    s.replace_trips([_trip(1), _trip(2, "Elsewhere · Aug 2025")],
                    {first: 1, second: 1, third: 1})
    assert set(s.trip_counts()) == {1}


def test_a_cover_is_the_earliest_photo_that_can_be_shown(cache_dir):
    """The cover is about to be asked for as a thumbnail, and a row that never
    got as far as a hash has none to serve. The MIN() is taken over a CASE that
    is NULL for those rows, so the cover is the earliest photo that *can* be
    shown — while COUNT(*) still counts the whole trip, because the trip really
    does hold them."""
    s = Store(cache_dir)
    ids = _in_trip(s, 1, 3, sha1=[None, "s2", "s3"])
    s.replace_trips([_trip(1)], {pid: 1 for pid in ids})

    n, cover = s.trip_counts()[1]
    assert n == 3                                # the hashless photo still counts
    assert cover == ids[1]                       # ...but is not the cover

    # a file nothing could open is out of the count as well, for the same reason
    # every other count excludes it (see scope_counts)
    broken = s.upsert_photo(dict(REC, path="/t1/torn.jpg", error="boom"))
    s.replace_trips([_trip(1)], {**{pid: 1 for pid in ids}, broken: 1})
    assert s.trip_counts()[1] == (3, ids[1])


def test_a_trip_with_nothing_showable_still_reports_its_size(cache_dir):
    """`cover_id: None` is the honest answer, and the view draws a placeholder
    rather than pretending the trip does not exist. Without the guard on the
    MIN() being NULL, sqlite's bare-column rule has no minimum row to point at
    and the cover would be an arbitrary photo — one with no thumbnail to serve,
    so the card's image would 404."""
    s = Store(cache_dir)
    ids = _in_trip(s, 1, 2, sha1=[None, None])
    s.replace_trips([_trip(1)], {pid: 1 for pid in ids})
    assert s.trip_counts() == {1: (2, None)}


# ── the shape of the matrix, without reading it ────────────────────────────
def test_embedding_shape_reads_the_header_not_the_matrix(cache_dir, monkeypatch):
    """/status polls this every ten seconds for as long as anyone has the view
    open, so it has to cost a few hundred bytes off disk instead of re-reading
    the whole 4MB matrix. np.load is broken on purpose here: an implementation
    that loaded the array to measure it could not answer at all."""
    s = Store(cache_dir)
    assert s.embedding_shape() == (0, 0)             # nothing indexed yet
    s.save_embeddings(np.array([1, 2, 3], dtype=np.int64),
                      np.ones((3, 8), dtype=np.float16))
    assert s.embedding_shape() == (3, 8)

    def no_loading(*a, **kw):
        raise AssertionError("embedding_shape loaded the matrix to measure it")

    monkeypatch.setattr(np, "load", no_loading)
    assert s.embedding_shape() == (3, 8)
    s.close()


def test_embedding_shape_of_a_file_it_cannot_read_is_zero(cache_dir):
    """A fact to display, never a reason to fail a poll: an unreadable
    generation means the vectors have to be rebuilt, and /status is not where
    the user finds that out by getting a traceback."""
    s = Store(cache_dir)
    s.save_embeddings(np.array([1], dtype=np.int64),
                      np.ones((1, 4), dtype=np.float16))
    npz = cache_dir / "embeddings.npz"

    npz.write_bytes(b"not an archive")
    assert s.embedding_shape() == (0, 0)

    # a real archive whose matrix member is not an .npy at all
    with zipfile.ZipFile(npz, "w") as z:
        z.writestr("mat.npy", "not a numpy array")
    assert s.embedding_shape() == (0, 0)

    # ...and one with no matrix member to read
    with zipfile.ZipFile(npz, "w") as z:
        z.writestr("ids.npy", "x")
    assert s.embedding_shape() == (0, 0)
    s.close()


def test_embedding_shape_still_answers_for_a_legacy_pair(cache_dir):
    """A cache written before the npz still loads (see load_embeddings), so
    /status has to be able to describe the generation it is really reading —
    otherwise the view says "0 vectors" about a library that searches fine."""
    Store(cache_dir).close()          # a catalog already at the live schema, so
    # reopening it is not a migration that would drop these outright
    np.save(cache_dir / "embeddings.npy", np.ones((5, 4), dtype=np.float16))

    s = Store(cache_dir)
    assert s.embedding_shape() == (5, 4)
    s.close()


def test_a_matrix_with_no_second_axis_reports_no_columns(cache_dir):
    """A 1-D array holds no vectors, which is the same thing an empty cache
    means — so it reports 0 columns rather than raising on a missing axis. The
    two .npy header versions encode their length prefix differently and numpy
    exposes a reader per version, so this has to dispatch between them."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / "probe.npy"

    np.save(p, np.arange(6, dtype=np.float16))
    with open(p, "rb") as f:
        assert _npy_shape(f) == (6, 0)

    np.save(p, np.ones((2, 3), dtype=np.float16))
    with open(p, "rb") as f:
        assert _npy_shape(f) == (2, 3)


def test_embeddings_survive_a_round_trip_unchanged(cache_dir):
    import numpy as np

    from lens.store import Store

    s = Store(cache_dir)
    ids = np.array([3, 1, 4, 1, 5], dtype=np.int64)
    mat = (np.arange(20, dtype=np.float32).reshape(5, 4) / 32).astype(np.float16)
    s.save_embeddings(ids, mat)
    got_ids, got_mat = s.load_embeddings()
    assert got_ids.tolist() == ids.tolist()
    assert got_ids.dtype == np.int64 and got_mat.dtype == np.float16
    assert np.array_equal(got_mat, mat)
    s.close()


# ── faces and people ───────────────────────────────────────────────────────
def _face(x0=0.1, prob=0.99):
    return {"bbox": (x0, 0.2, x0 + 0.2, 0.6), "prob": prob}


def test_faces_are_replaced_per_photo_and_stamped_with_the_generation(cache_dir):
    """`replace_photo_faces` is the only writer, and it writes all three facts at
    once: the old rows go, the new ones arrive, and the photo records which model
    looked at it. A stamp without rows would be a photo permanently claiming it
    has nobody in it."""
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    assert s.faces_pending("gen-1") == [
        {"id": pid, "path": "/p/a.jpg", "sha1": "abc", "kind": "image"}]

    ids = s.replace_photo_faces(pid, [_face(0.1), _face(0.5)], version="gen-1")
    assert len(ids) == 2
    assert s.faces_pending("gen-1") == []          # this generation is done
    assert len(s.faces_pending("gen-2")) == 1      # a new model re-detects
    rows = s.all_faces()
    assert [r["photo_id"] for r in rows] == [pid, pid]
    assert rows[0]["bbox"] == (0.1, 0.2, 0.3, 0.6)

    # re-detecting the same photo replaces the rows rather than adding to them
    again = s.replace_photo_faces(pid, [_face(0.3)], version="gen-2")
    assert len(s.all_faces()) == 1 and s.all_faces()[0]["id"] == again[0]
    # ...and finding nobody is a real answer, not a no-op
    s.replace_photo_faces(pid, [], version="gen-2")
    assert s.all_faces() == [] and s.faces_pending("gen-2") == []


def test_faces_pending_ignores_rows_it_could_never_render(cache_dir):
    """The face pass reads the thumbnail the index rendered, so a row with no
    hash has nothing to look at, and a row that failed to index is not a
    photograph yet."""
    s = Store(cache_dir)
    s.upsert_photo(dict(REC, path="/p/ok.jpg"))
    s.upsert_photo(dict(REC, path="/p/nohash.jpg", sha1=None))
    s.upsert_photo(dict(REC, path="/p/broken.jpg", error="cannot open"))
    assert [r["path"] for r in s.faces_pending("gen-1")] == ["/p/ok.jpg"]


def test_deleting_a_photo_deletes_its_faces(cache_dir):
    """A face row outliving its photograph is not a stale cache: photo_id is how
    the People view gets a picture to crop, so a person made of orphans is a card
    that can never draw."""
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    fids = s.replace_photo_faces(pid, [_face()], version="gen-1")
    assert s.face_ids_for_paths(["/p/a.jpg"]) == fids
    s.remove_paths(["/p/a.jpg"])
    assert s.all_faces() == []


def test_persons_keep_their_names_across_a_rewrite(cache_dir):
    """`replace_persons` is called after every index run and must never carry a
    name over a rename that landed in between — it writes the geometry, the user
    owns the name."""
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    f1, f2, f3 = s.replace_photo_faces(pid, [_face(0.1), _face(0.4), _face(0.7)],
                                       version="gen-1")
    cen = np.ones(4, dtype=np.float32) / 2
    s.replace_persons([{"id": 1, "name": None, "cover_face_id": f1,
                        "centroid": cen}])
    s.set_face_persons({f1: 1, f2: 1, f3: None})

    assert s.set_person_name(1, "Ana") is True
    s.replace_persons([{"id": 1, "name": None, "cover_face_id": f2,
                        "centroid": cen}])
    people = s.get_persons()
    assert people[0]["name"] == "Ana"              # survived the rewrite
    assert people[0]["cover_face_id"] == f2        # the geometry did move
    assert np.allclose(people[0]["centroid"], cen)
    assert s.person_counts() == {1: (2, 1)}        # two faces, one photograph
    assert s.person_names() == [(1, "Ana")]

    # clearing is a real operation: a seeded name can be wrong
    assert s.set_person_name(1, None) is True
    assert s.get_persons()[0]["name"] is None
    assert s.person_names() == []
    assert s.set_person_name(99, "Nobody") is False


def test_a_short_name_stays_out_of_the_query_vocabulary(cache_dir):
    """Names are matched as whole words anywhere in a query, so somebody called
    "Jo" would turn every "jo" into a filter (same rule as album names)."""
    s = Store(cache_dir)
    s.replace_persons([{"id": 1, "name": None, "cover_face_id": None,
                        "centroid": None}])
    s.set_person_name(1, "Jo")
    assert s.person_names() == []
    s.set_person_name(1, "Ana")
    assert s.person_names() == [(1, "Ana")]


def test_merging_moves_the_faces_and_keeps_the_absorbed_row(cache_dir):
    """The absorbed row is what makes the merge survive the next re-clustering,
    so it stays — pointing at the survivor, and out of every list."""
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    a, b = s.replace_photo_faces(pid, [_face(0.1), _face(0.5)], version="gen-1")
    cen = np.ones(4, dtype=np.float32) / 2
    s.replace_persons([{"id": 1, "name": None, "cover_face_id": a,
                        "centroid": cen},
                       {"id": 2, "name": "Ana", "cover_face_id": b,
                        "centroid": cen}])
    s.set_face_persons({a: 1, b: 2})

    assert s.merge_persons(1, 2) is True
    assert s.person_counts() == {1: (2, 1)}
    kept = s.get_persons()
    assert [p["id"] for p in kept] == [1]
    # the name came across: "merge the unnamed one into Ana" and the reverse are
    # the same intention
    assert kept[0]["name"] == "Ana"
    with_merged = s.get_persons(include_merged=True)
    assert [(p["id"], p["merged_into"]) for p in with_merged] == [(1, None), (2, 1)]
    assert with_merged[1]["centroid"] is not None   # still matchable next run

    # a merge nobody can perform is refused rather than half-applied
    assert s.merge_persons(1, 1) is False
    assert s.merge_persons(1, 2) is False           # 2 is already merged away
    assert s.merge_persons(1, 99) is False


def test_face_counts_report_the_denominator_too(cache_dir):
    """A count of people means nothing without how much of the library has been
    looked at: the face pass runs after the photographs are searchable."""
    s = Store(cache_dir)
    a = s.upsert_photo(dict(REC, path="/p/a.jpg"))
    s.upsert_photo(dict(REC, path="/p/b.jpg"))
    s.upsert_photo(dict(REC, path="/p/broken.jpg", error="nope"))
    f1, f2 = s.replace_photo_faces(a, [_face(0.1), _face(0.5)], version="gen-1")
    s.replace_persons([{"id": 1, "name": "Ana", "cover_face_id": f1,
                        "centroid": None}])
    s.set_face_persons({f1: 1, f2: None})
    assert s.face_counts() == {"faces": 2, "clustered": 1, "people": 1,
                               "named": 1, "scanned": 1, "eligible": 2}


def test_face_vectors_round_trip_and_are_their_own_file(cache_dir):
    """Face vectors are written by a different pipeline stage at a different
    cadence from the image vectors, so a checkpoint of one must never truncate
    the other."""
    s = Store(cache_dir)
    s.save_embeddings(np.array([1], dtype=np.int64),
                      np.ones((1, 4), dtype=np.float16))
    ids = np.array([7, 9], dtype=np.int64)
    mat = (np.arange(8, dtype=np.float32).reshape(2, 4) / 8).astype(np.float16)
    s.save_faces(ids, mat)

    got_ids, got_mat = s.load_faces()
    assert got_ids.tolist() == [7, 9] and np.array_equal(got_mat, mat)
    assert s.faces_vector_shape() == (2, 4)
    assert (cache_dir / "faces.npz").exists()
    emb_ids, emb_mat = s.load_embeddings()          # untouched
    assert emb_ids.tolist() == [1] and emb_mat.shape == (1, 4)


def test_a_corrupt_face_generation_reads_as_an_empty_one(cache_dir):
    """Same recovery as the image vectors: a file that cannot be read means the
    faces have to be found again, which the indexer already treats as work to
    do — never a traceback in the middle of a poll."""
    s = Store(cache_dir)
    (cache_dir / "faces.npz").write_bytes(b"not a zip at all")
    ids, mat = s.load_faces()
    assert len(ids) == 0 and mat.size == 0
    assert s.faces_vector_shape() == (0, 0)


def test_a_damaged_face_box_reads_as_the_whole_frame(cache_dir):
    """A crop rectangle nobody can parse is worth showing as the picture it came
    from; raising here would take out the whole People view over one bad string.
    """
    s = Store(cache_dir)
    pid = s.upsert_photo(dict(REC))
    s.replace_photo_faces(pid, [_face()], version="gen-1")
    db = sqlite3.connect(cache_dir / "catalog.sqlite")
    db.execute("UPDATE faces SET bbox_json = 'nonsense'")
    db.commit()
    db.close()
    s2 = Store(cache_dir)
    assert s2.all_faces()[0]["bbox"] == (0.0, 0.0, 1.0, 1.0)


def test_apple_persons_reads_the_names_out_of_the_raw_dump(cache_dir):
    """The catalog has no column for a name list, and this is the only reader —
    so the names live where apple_photos.merge left them."""
    s = Store(cache_dir)
    a = s.upsert_photo(dict(
        REC, path="/p/apple.jpg", source="apple",
        raw_exif='{"_apple": {"persons": ["Ana", " "], "albums": []}}'))
    b = s.upsert_photo(dict(REC, path="/p/folder.jpg",
                            raw_exif='{"_apple": {"persons": ["Ben"]}}'))
    c = s.upsert_photo(dict(REC, path="/p/broken.jpg", source="apple",
                            raw_exif="{not json"))
    # only Apple rows, only the ids asked for, blanks dropped
    assert s.apple_persons([a, b, c]) == {a: ["Ana"]}
    assert s.apple_persons([]) == {}


def test_the_faces_column_is_added_without_a_re_index(cache_dir, capsys):
    """Schema 6 adds two empty tables and one nullable column, so a v5 catalog
    keeps every row, every thumbnail and every vector — and NULL is exactly what
    is true of those rows: nobody has looked for faces in them yet."""
    _make_v4_catalog(cache_dir)                     # v4 → v5 → v6 in one step
    s = Store(cache_dir)
    assert "extended in place" in capsys.readouterr().out
    assert s.get_meta("schema_version") == SCHEMA_VERSION
    ids, mat = s.load_embeddings()
    assert list(ids) == [1, 2] and mat.shape == (2, 4)
    assert len(s.faces_pending("gen-1")) == 2       # both rows, never scanned
    assert s.all_faces() == [] and s.get_persons() == []
