"""The audit that re-derives what the catalog claims.

Everything lens shows is derived — a date read out of EXIF, a city from an
offline geocode, a trip inferred from gaps in time, a ranking computed from
vectors on disk — and nothing else checks any of it. An index run that wrote the
wrong date is indistinguishable, from inside the view, from one that got it
right.

So these tests are mostly about the checks *failing correctly*: a number that
only ever says 1.0 is not an audit. Each one breaks exactly one derivation and
pins that the check names it, that the sub-metric beside it stays clean, and
that the arithmetic behind the score adds up.
"""

import random
from pathlib import Path

import numpy as np
import piexif
import pytest
from PIL import Image
from conftest import FakeFaceModel, face_photo
from test_daemon import FakeEmbedder, _camera_photo, _gps_photo

from lens import metadata, validate
from lens.daemon import LensServer
from lens.thumbs import thumb_path

# The two ends of the corpus below: ~575km apart, so the second is a trip.
HOME = (8.5, 115.26)
AWAY = (8.5, 120.5)


def _geocode(monkeypatch):
    """Places by longitude, so the corpus has a home city and an away city
    without reverse_geocoder (and without its data download) in the loop."""
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (("Ubud" if lon < 118 else "Faraway"),
                                          "Bali", "ID"))


def _corpus_root(tmp_path, n_away=3):
    """A library with everything the four checks look at: photographs with GPS
    and a capture date, one trip's worth of them far from home, software-made
    images that are not photographs, and a file nothing can open."""
    root = tmp_path / "photos"
    root.mkdir(parents=True)
    for i in range(4):
        _gps_photo(root / f"h{i}.jpg", f"2025:07:0{i + 1} 10:00:00", *HOME)
    for i in range(n_away):
        _gps_photo(root / f"t{i}.jpg", f"2025:07:1{i} 10:00:00", *AWAY)
    Image.new("RGBA", (32, 32), (255, 0, 0, 0)).save(root / "overlay.png", "PNG")
    Image.new("RGB", (32, 32), "white").save(root / "chart.png", "PNG")
    (root / "torn.jpg").write_bytes(b"not a jpeg")          # an error row
    return root


def _indexed(cache_dir, root, face_model=None):
    srv = LensServer(cache_dir, roots=[str(root)], embedder=FakeEmbedder(),
                     port=0, face_model=face_model or FakeFaceModel())
    srv.index_now()
    return srv


@pytest.fixture
def srv(cache_dir, tmp_path, monkeypatch):
    """A daemon over the corpus above, indexed. Nothing here is served over
    HTTP: the audit is exercised through validate's own functions, and the
    server is what supplies a real `known_places` and a real cache."""
    _geocode(monkeypatch)
    s = _indexed(cache_dir, _corpus_root(tmp_path))
    yield s
    s.shutdown()


@pytest.fixture
def rows_only(cache_dir, tmp_path, monkeypatch):
    """A daemon with no roots, for the checks that never touch the filesystem.

    retrieval_sanity reads nothing but the catalog, so fabricated rows are the
    honest fixture for it — and they let a column be given a value no real
    geocode would produce, which is the whole point of those tests."""
    _geocode(monkeypatch)
    s = LensServer(cache_dir, roots=[], embedder=FakeEmbedder(), port=0)
    yield s
    s.shutdown()


ROW = {"path": "/p/a.jpg", "sha1": "a1", "size": 1, "mtime": 1.0,
       "width": 32, "height": 32, "format": "JPEG",
       "taken_at": "2025-07-15T10:00:00", "lat": -8.4, "lon": 115.1,
       "place_city": "Ubud", "place_region": "Bali", "place_country": "ID",
       "raw_exif": "{}", "error": None, "is_photo": 1}


def _photos(store):
    return store.query_photos("error IS NULL AND is_photo = 1", [])


# ── exif_consistency ───────────────────────────────────────────────────────
def test_exif_consistency_agrees_with_an_untouched_corpus(srv):
    """The baseline: what the indexer just wrote and what the files say are the
    same thing. `compared` and `agreed` are reported so the page can say how
    much the number is worth — 1.0 out of nothing measured is not a pass."""
    out = validate.exif_consistency(srv.store, rng=random.Random(0))
    assert out["score"] == 1.0 and out["pass"] is True
    assert out["sampled_n"] == out["population"] == 7
    assert out["compared"] == out["agreed"] > 0
    assert out["unreadable"] == 0 and out["details"] == []


def test_a_field_neither_side_claims_is_not_a_comparison(cache_dir, tmp_path,
                                                         monkeypatch):
    """A photo with no GPS agrees about nothing there. Counting that as a pass
    would inflate every score by however much of the library carries no
    coordinates — which on a real library is most of it, so the check would
    read 100% while measuring almost nothing.

    Four fields are claimed (EXIF_FIELDS); a corpus with only a capture date
    must therefore report one comparison per photo, not four."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        _camera_photo(root / f"p{i}.jpg")           # date only: no GPS, no camera
    srv = _indexed(cache_dir, root)

    out = validate.exif_consistency(srv.store, rng=random.Random(0))
    assert out["sampled_n"] == 3
    assert out["compared"] == 3, out                # taken_at only
    assert out["compared"] < 3 * len(validate.EXIF_FIELDS)
    assert out["score"] == 1.0 and out["agreed"] == 3

    # ...and the corpus with coordinates does compare them
    _geocode(monkeypatch)
    gps = _indexed(cache_dir, _corpus_root(tmp_path / "with-gps"))
    assert validate.exif_consistency(gps.store,
                                     rng=random.Random(0))["compared"] == 7 * 3
    srv.shutdown()
    gps.shutdown()


def test_exif_disagreement_names_the_field_that_moved(srv):
    """The failure this check exists for: the file on disk says one thing and
    the catalog says another. Rewritten on disk rather than in the catalog, so
    the disagreement is produced by the same extractor the indexer used."""
    victim = _photos(srv.store)[0]
    # edited in place rather than replaced: a bare piexif.insert would also drop
    # the GPS tags, and then three fields would have moved instead of one
    exif = piexif.load(victim["path"])
    exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = b"2019:01:01 00:00:00"
    piexif.insert(piexif.dump(exif), victim["path"])

    out = validate.exif_consistency(srv.store, rng=random.Random(0))
    assert out["pass"] is False
    assert out["agreed"] == out["compared"] - 1     # exactly one field moved
    assert out["score"] == out["agreed"] / out["compared"]
    assert 0 < out["score"] < 1
    named = [d for d in out["details"] if d["path"] == victim["path"]]
    assert len(named) == 1
    assert named[0]["field"] == "taken_at"
    assert named[0]["catalog"] == victim["taken_at"]
    assert named[0]["fresh"] == "2019-01-01T00:00:00"


def test_a_photo_whose_file_is_gone_is_not_sampled(srv):
    """A photo on an unmounted drive stays in the library on purpose (see
    indexer.index_once), and it cannot disagree with a file nothing can read.
    Sampling it would score the audit down for a fact about the mount table."""
    gone = _photos(srv.store)[0]["path"]
    Path(gone).unlink()

    out = validate.exif_consistency(srv.store, rng=random.Random(0))
    assert out["population"] == out["sampled_n"] == 6
    assert out["score"] == 1.0 and out["pass"] is True
    assert all(d["path"] != gone for d in out["details"])


def test_an_unreadable_file_is_one_failed_comparison(srv):
    """The catalog says this row is fine; the file says otherwise. That is a
    finding, not a crash — and it fails the whole row rather than one field, so
    it counts as a single comparison that did not agree."""
    victim = _photos(srv.store)[0]
    with open(victim["path"], "wb") as f:                # present but unopenable
        f.write(b"\xff\xd8truncated")

    out = validate.exif_consistency(srv.store, rng=random.Random(0))
    assert out["unreadable"] == 1
    assert out["pass"] is False
    assert out["agreed"] == out["compared"] - 1
    assert out["compared"] == 6 * 3 + 1              # six good photos, plus this
    named = [d for d in out["details"] if d["path"] == victim["path"]]
    assert [d["field"] for d in named] == ["readable"]
    assert named[0]["catalog"] == "no error" and named[0]["fresh"]


# ── retrieval_sanity ───────────────────────────────────────────────────────
def test_every_photo_is_findable_by_its_own_place_and_month(srv):
    """The one check that runs the query path end to end: the words are built
    from the catalog's own columns, parsed by the real parser and filtered by the
    real WHERE builder. A photo that cannot be found by the place and month lens
    itself claims for it is unreachable in the view."""
    out = validate.retrieval_sanity(srv.store, srv.known_places,
                                    rng=random.Random(0))
    assert out["score"] == 1.0 and out["pass"] is True
    assert out["found"] == out["sampled_n"] == 7
    assert out["residual"] == 0                      # no leftover words
    assert out["details"] == []


def test_a_place_the_parser_reads_as_a_date_is_reported_as_a_miss(rows_only):
    """A city called "March" is swallowed by the date parser before the place
    vocabulary is ever consulted, so the query built from this row's own columns
    filters on the wrong month and cannot return it.

    That is the shape of the bug this check is for — a column value the rest of
    lens cannot round-trip — and it must come back as a miss carrying the query,
    not as a silent 1.0."""
    store = rows_only.store
    store.upsert_photo(dict(ROW))
    store.upsert_photo(dict(ROW, path="/p/b.jpg", place_city="March"))

    out = validate.retrieval_sanity(store, rows_only.known_places,
                                    rng=random.Random(0))
    assert out["sampled_n"] == 2 and out["found"] == 1
    assert out["score"] == 0.5 and out["pass"] is False
    assert len(out["details"]) == 1
    miss = out["details"][0]
    assert miss["path"] == "/p/b.jpg"
    assert miss["query"] == "March Jul 2025"
    assert miss["place"] == "March" and miss["matched"] == 0
    assert miss["taken_at"] == ROW["taken_at"]


def test_a_leftover_word_is_counted_as_a_residual(rows_only):
    """A residual means the daemon would also have ranked semantically and cut
    the weak tail, so the recall measured here is the *filter's* — an upper
    bound on what the user would have seen. Counted and reported rather than
    hidden, and independent of recall: this row is found, and still residual.

    A two-letter city is deliberately outside the daemon's place vocabulary
    ("NO", "ID", "us" hijack ordinary English), so its own name comes back as a
    word the parser could not place."""
    store = rows_only.store
    store.upsert_photo(dict(ROW))
    store.upsert_photo(dict(ROW, path="/p/b.jpg", place_city="Ai"))

    out = validate.retrieval_sanity(store, rows_only.known_places,
                                    rng=random.Random(0))
    assert out["found"] == out["sampled_n"] == 2      # recall is untouched
    assert out["score"] == 1.0
    assert out["residual"] == 1
    assert out["details"] == []


# ── embedding_integrity ────────────────────────────────────────────────────
def test_embedding_integrity_passes_on_a_freshly_indexed_corpus(srv):
    out = validate.embedding_integrity(srv.store, srv.cache, rng=random.Random(0))
    assert out["score"] == 1.0 and out["pass"] is True
    assert out["aligned"] is True
    assert out["ids"] == out["rows"] == out["embedded"] == 9
    assert out["dims"] == 4
    assert out["coverage"] == 1.0 and out["total"] == 9
    assert out["norms_ok"] == out["sampled_n"] == 9
    assert out["thumbs_ok"] == out["thumbs_n"] == 9
    assert out["details"] == []


def test_an_unnormalized_vector_fails_only_the_norm_check(srv):
    """Cosine ranking is a dot product, so an unnormalized row outranks
    everything by being *longer* rather than closer — it would sit at the top of
    every search for every query. A stored vector is normalized and then rounded
    to float16, so its norm is 1 give or take the rounding; 3 is not rounding."""
    ids, mat = srv.store.load_embeddings()
    srv.store.save_embeddings(ids, mat * 3)

    out = validate.embedding_integrity(srv.store, srv.cache, rng=random.Random(0))
    assert out["norms_ok"] == 0 and out["sampled_n"] == 9
    assert [d["field"] for d in out["details"]] == ["norm"] * 9
    assert out["details"][0]["value"] == pytest.approx(3.0, abs=0.01)
    # the other three sub-metrics are untouched: this is one fault, not four
    assert out["aligned"] is True and out["coverage"] == 1.0
    assert out["thumbs_ok"] == out["thumbs_n"] == 9
    assert out["pass"] is False and 0 < out["score"] < 1


def test_a_missing_thumbnail_fails_only_the_thumb_check(srv):
    """A row with a vector but no thumbnail on disk means the vector was
    computed from an image the view can no longer show. Looked for at
    thumbs.thumb_path — the one spelling of the name, version suffix included,
    or the audit would report the whole cache as missing after a version bump."""
    victim = _photos(srv.store)[0]
    thumb_path(srv.cache, victim["sha1"]).unlink()

    out = validate.embedding_integrity(srv.store, srv.cache, rng=random.Random(0))
    assert out["thumbs_n"] == 9 and out["thumbs_ok"] == 8
    named = [d for d in out["details"] if d["field"] == "thumb"]
    assert [d["value"] for d in named] == [victim["sha1"]]
    assert out["norms_ok"] == out["sampled_n"] == 9      # norms are fine
    assert out["aligned"] is True and out["coverage"] == 1.0
    assert out["pass"] is False and 0 < out["score"] < 1


def test_a_torn_generation_is_reported_as_unaligned(srv):
    """The ids and the matrix are positional halves of one fact — row i is the
    vector for ids[i] — so a length mismatch means every vector after the gap
    belongs to the wrong photo.

    Written by hand because that is the only way in through the public API:
    Store guards the legacy *pair* against this (see _load_legacy) but hands an
    npz's two members back as they are, and save_embeddings can never produce a
    mismatched one. This check is the thing that would notice."""
    with open(srv.cache / "embeddings.npz", "wb") as f:
        np.savez(f, ids=np.array([1, 2, 3], dtype=np.int64),
                 mat=np.ones((2, 4), dtype=np.float16))

    out = validate.embedding_integrity(srv.store, srv.cache, rng=random.Random(0))
    assert out["aligned"] is False
    assert out["ids"] == 3 and out["rows"] == 2
    assert out["pass"] is False and out["score"] < 1
    # nothing was sampled: with the halves out of step there is no row whose
    # norm or thumbnail could be attributed to a photo
    assert out["sampled_n"] == 0 and out["thumbs_n"] == 0


def test_a_photo_with_no_vector_drops_coverage(srv):
    """A searchable row with no vector is invisible to every semantic query, and
    it is how a killed index run or a model swap during an offline root shows
    up. Coverage is the fraction of the searchable library that can be ranked."""
    srv.store.upsert_photo(dict(ROW, path="/p/never-embedded.jpg"))

    out = validate.embedding_integrity(srv.store, srv.cache, rng=random.Random(0))
    assert out["embedded"] == 9 and out["total"] == 10
    assert out["coverage"] == pytest.approx(9 / 10)
    assert out["pass"] is False and 0 < out["score"] < 1
    # the vectors that do exist are still sound
    assert out["aligned"] is True
    assert out["norms_ok"] == out["sampled_n"] == 9
    assert out["thumbs_ok"] == out["thumbs_n"] == 9


# ── trips_invariants ───────────────────────────────────────────────────────
def test_trips_invariants_hold_on_a_freshly_computed_set(srv):
    out = validate.trips_invariants(srv.store)
    assert out["score"] == 1.0 and out["pass"] is True
    assert out["trips"] == out["recomputed"] == 1
    assert out["mismatch"] == 0 and out["agreement"] == 1.0
    assert out["undersized"] == [] and out["details"] == []
    assert out["sampled_n"] == 7                     # exhaustive, every photo row


def test_a_two_photo_trip_is_no_longer_made_at_all(cache_dir, tmp_path,
                                                   monkeypatch):
    """This audit is what moved the size rule: it used to report an undersized
    trip on every library, because compute_trips emitted two-photo trips and only
    the view folded them away. The rule now lives where the trips are made
    (trips.MIN_PHOTOS), so a two-photo excursion produces no trip — and the check
    that used to fail here passes."""
    _geocode(monkeypatch)
    srv = _indexed(cache_dir, _corpus_root(tmp_path, n_away=2))

    assert srv.store.get_trips() == []
    out = validate.trips_invariants(srv.store)
    assert out["undersized"] == [] and out["mismatch"] == 0
    assert out["pass"] is True and out["score"] == 1.0
    srv.shutdown()


def test_an_undersized_stored_trip_is_reported(cache_dir, tmp_path, monkeypatch):
    """A stored trip smaller than the rule allows is a catalog whose trips were
    written before the rule and never rebuilt — a stale derivation, which is
    exactly what this check exists to find. Written by hand because that is now
    the only way to produce one: no index run would.

    It is reported twice over, and both are true: the trip is undersized, *and*
    the recomputation disagrees with it, because the current rule would not
    produce that trip at all."""
    _geocode(monkeypatch)
    srv = _indexed(cache_dir, _corpus_root(tmp_path, n_away=2))
    away = srv.store.query_photos("place_city = ?", ["Faraway"])
    assert len(away) == 2

    srv.store.replace_trips(
        [{"id": 1, "name": "Faraway · Jul 2025", "start": away[-1]["taken_at"],
          "end": away[0]["taken_at"], "place": "Faraway"}],
        {r["id"]: 1 for r in away})

    out = validate.trips_invariants(srv.store)
    assert len(out["undersized"]) == 1
    small = out["undersized"][0]
    assert small == {"id": 1, "name": "Faraway · Jul 2025", "photos": 2}
    assert small["photos"] < validate.TRIP_MIN == 3
    assert out["mismatch"] == 2                  # both rows recompute to no trip
    assert out["pass"] is False and 0 < out["score"] < 1
    assert {"undersized": small} in out["details"]
    srv.shutdown()


def test_a_catalog_that_moved_on_since_the_trips_were_stored_mismatches(srv):
    """The interesting half of this check. compute_trips is re-run over the
    catalog as it stands *now*: the same photos through the same
    gap-and-distance rule can only produce one answer, so a disagreement means
    the catalog moved on without the trips being rebuilt — a photo added, a
    folder removed. That is a stale-derivation bug no view could show."""
    trip = srv.store.get_trips()[0]
    # a photo inside the trip's window that the stored assignments predate.
    # place_city is left unset so it cannot shift which city counts as home.
    late = srv.store.upsert_photo(dict(
        ROW, path="/p/late.jpg", taken_at="2025-07-11T12:00:00",
        lat=-AWAY[0], lon=AWAY[1], place_city=None))

    out = validate.trips_invariants(srv.store)
    assert out["mismatch"] == 1
    assert out["agreement"] == pytest.approx(7 / 8)
    assert out["details"][0] == {"path": "/p/late.jpg", "stored": None,
                                 "recomputed": trip["id"]}
    assert out["pass"] is False and out["score"] < 1
    # the size rule is unaffected — the trip grew, it did not shrink
    assert out["undersized"] == []
    assert srv.store.get_photo_by_id(late)["trip_id"] is None


# ── run() ──────────────────────────────────────────────────────────────────
def _run(srv, seed=0):
    return validate.run(srv.store, srv.cache, srv.known_places,
                        rng=random.Random(seed))


def test_run_reports_every_check_and_the_library_it_measured(srv):
    """The composite is what the page puts in its ring, so it has to be the mean
    of the checks actually beside it — and `library` has to describe the same
    catalog they were measured on."""
    thumb_path(srv.cache, _photos(srv.store)[0]["sha1"]).unlink()  # not all 1.0
    out = _run(srv)

    assert set(out["checks"]) == set(validate.CHECKS)
    scores = [c["score"] for c in out["checks"].values() if c["score"] is not None]
    assert len(scores) == 5
    assert out["composite"] == pytest.approx(sum(scores) / len(scores))
    assert out["composite"] < 1.0                    # the broken thumb shows up

    counts = srv.store.scope_counts()
    assert out["library"] == {"images": counts["all"], "photos": counts["photos"],
                              "videos": counts["videos"],
                              "trips": len(srv.store.get_trips()),
                              "faces": srv.store.face_counts()}
    assert isinstance(out["elapsed_ms"], int) and out["elapsed_ms"] >= 0


def test_a_check_that_raises_is_reported_not_propagated(srv, monkeypatch):
    """Half an audit is worth more than a 500, and "this check could not run" is
    itself the kind of thing an audit exists to surface."""
    def boom(store):
        raise RuntimeError("trip maths exploded")

    monkeypatch.setattr(validate, "trips_invariants", boom)
    out = _run(srv)

    broken = out["checks"]["trips_invariants"]
    assert broken["score"] == 0.0 and broken["pass"] is False
    assert broken["error"] == "trip maths exploded"
    assert broken["details"] == [] and broken["sampled_n"] == 0

    others = [n for n in validate.CHECKS if n != "trips_invariants"]
    assert all(out["checks"][n]["score"] == 1.0 for n in others), out["checks"]
    assert out["composite"] == pytest.approx(4 / 5)


def test_run_is_deterministic_for_a_given_rng(srv):
    """These checks are sampled, so the number moves between runs unless the
    caller can pin the draw — and a score that wobbles is a score nobody can
    act on. The corpus is broken on purpose here: with a failure in it, the
    sample order also decides what lands in `details`."""
    thumb_path(srv.cache, _photos(srv.store)[0]["sha1"]).unlink()

    first, second = _run(srv), _run(srv)
    for out in (first, second):
        out.pop("elapsed_ms")
    assert first == second
    assert first["composite"] < 1.0             # something was actually measured


# ── faces_integrity ────────────────────────────────────────────────────────
@pytest.fixture
def people_srv(cache_dir, tmp_path, monkeypatch):
    """A library with two people in it, indexed with the deterministic face
    model (see tests/conftest.py)."""
    _geocode(monkeypatch)
    root = tmp_path / "photos"
    root.mkdir(parents=True)
    for name in ("ana", "ben"):
        for i in range(3):
            face_photo(root / f"{name}{i}.jpg", [name])
    face_photo(root / "beach.jpg", [])
    s = _indexed(cache_dir, root)
    yield s
    s.shutdown()


def test_faces_integrity_on_a_library_it_has_finished_looking_at(people_srv):
    """The clean case, and the shape of the answer: every face has a vector,
    every vector has a face, every person a face claims exists, and every cover
    belongs to the person it is the cover of."""
    out = validate.faces_integrity(people_srv.store, rng=random.Random(0))

    assert out["pass"] is True and out["score"] == 1.0
    assert out["faces"] == 6 and out["vectors"] == 6 and out["aligned"] is True
    assert out["missing_vectors"] == 0 and out["orphan_vectors"] == 0
    assert out["dangling_persons"] == 0 and out["bad_covers"] == 0
    assert out["people"] == 2 and out["clustered"] == 6
    assert out["coverage"] == 1.0 and out["scanned"] == out["eligible"] == 7
    assert out["details"] == []


def test_a_face_with_no_vector_is_a_finding(people_srv):
    """The row and the vector are written one after the other per photograph, so
    a kill lands exactly here — and a face with no vector cannot be clustered,
    which means somebody's person is missing a photograph."""
    ids, mat = people_srv.store.load_faces()
    people_srv.store.save_faces(ids[:-1], mat[:-1])

    out = validate.faces_integrity(people_srv.store, rng=random.Random(0))
    assert out["pass"] is False and out["score"] < 1
    assert out["missing_vectors"] == 1 and out["orphan_vectors"] == 0
    assert {d["field"] for d in out["details"]} == {"vector"}


def test_a_vector_with_no_face_is_a_finding(people_srv):
    """The other direction: a vector nothing can show. It is what a pruned photo
    leaves behind if its face vectors are not dropped with its rows."""
    ids, mat = people_srv.store.load_faces()
    people_srv.store.save_faces(np.append(ids, 99999),
                               np.vstack([mat, mat[:1]]))

    out = validate.faces_integrity(people_srv.store, rng=random.Random(0))
    assert out["pass"] is False
    assert out["orphan_vectors"] == 1 and out["missing_vectors"] == 0
    assert {d["field"] for d in out["details"]} == {"row"}


def test_an_unnormalized_face_vector_is_a_finding(people_srv):
    """Clustering is a dot product, so a long vector is "similar" to everything
    and drags strangers into a person."""
    ids, mat = people_srv.store.load_faces()
    mat = mat.astype(np.float32)
    mat[0] *= 4
    people_srv.store.save_faces(ids, mat)

    out = validate.faces_integrity(people_srv.store, rng=random.Random(0))
    assert out["pass"] is False
    assert out["norms_ok"] < out["sampled_n"]
    assert any(d["field"] == "norm" for d in out["details"])


def test_a_face_pointing_at_a_person_who_does_not_exist_is_a_finding(people_srv):
    """Person ids are in URLs and carry names: a dangling one is a card the
    People view cannot draw and a filter that returns nothing."""
    face = people_srv.store.all_faces()[0]
    people_srv.store.set_face_persons({face["id"]: 4242})

    out = validate.faces_integrity(people_srv.store, rng=random.Random(0))
    assert out["pass"] is False
    assert out["dangling_persons"] == 1
    assert any(d.get("person") == 4242 for d in out["details"])


def test_a_cover_face_belonging_to_somebody_else_is_a_finding(people_srv):
    """The cover is what the card shows, so a cover from another person is the
    most visible mistake this feature can make."""
    people = people_srv.store.get_persons()
    other = [f for f in people_srv.store.all_faces()
             if f["cluster_id"] != people[0]["id"]][0]
    people_srv.store.replace_persons(
        [{"id": people[0]["id"], "name": None, "cover_face_id": other["id"],
          "centroid": people[0]["centroid"]}])

    out = validate.faces_integrity(people_srv.store, rng=random.Random(0))
    assert out["pass"] is False and out["bad_covers"] == 1
    assert any(d["field"] == "cover" for d in out["details"])


def test_a_face_sweep_still_in_flight_is_reported_not_failed(rows_only):
    """Coverage is a fact, not a fault. The face pass runs *after* the
    photographs are searchable, so a library it has not finished is normal —
    while a photograph with no *embedding* is a broken promise, which is why
    embedding_integrity scores its coverage and this one does not."""
    for i in range(3):
        rows_only.store.upsert_photo(dict(ROW, path=f"/p/{i}.jpg", sha1=f"s{i}"))

    out = validate.faces_integrity(rows_only.store, rng=random.Random(0))

    assert out["scanned"] == 0 and out["eligible"] == 3
    assert out["coverage"] == 0.0
    assert out["pass"] is True and out["score"] == 1.0    # nothing claimed, nothing wrong
    assert out["faces"] == 0 and out["people"] == 0
