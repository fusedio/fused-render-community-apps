"""Apple Photos ingest, against a fake library.

`osxphotos` is not in the loop here, and deliberately: it needs macOS, a real
Photos database and a privacy permission a test run cannot grant, so a suite that
depended on it would be a suite that only ever ran on one machine. What lens
actually depends on is a narrow shape — a `PhotosDB` whose `.photos()` hands back
objects with a handful of attributes — so that shape is what these tests supply,
by putting a fake module in `sys.modules` before the lazy import inside
`apple_photos._photos_db` runs.

The fake is checked against the real API in one place: test_the_fake_matches_the
_real_osxphotos_api, which is skipped when osxphotos is not installed. Without
it these tests could drift into testing a library nobody ships.
"""

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from PIL import Image

from lens import apple_photos, config, indexer, metadata
from lens.store import Store


class FakeEmbedder:
    dim = 4
    key = "fake"

    def embed_images(self, imgs):
        return np.full((len(imgs), 4), 0.5, dtype=np.float16)


class FakeExif:
    def __init__(self, make=None, model=None):
        self.camera_make = make
        self.camera_model = model


class FakePhoto:
    """One PhotoInfo, as much of it as lens reads."""

    def __init__(self, uuid, path=None, date=None, latitude=None, longitude=None,
                 title=None, description=None, albums=(), persons=(),
                 favorite=False, hidden=False, intrash=False, ismovie=False,
                 exif_info=None):
        self.uuid = uuid
        self.path = path
        self.date = date
        self.latitude = latitude
        self.longitude = longitude
        self.title = title
        self.description = description
        self.albums = list(albums)
        self.persons = list(persons)
        self.favorite = favorite
        self.hidden = hidden
        self.intrash = intrash
        self.ismovie = ismovie
        self.exif_info = exif_info


def install_fake(monkeypatch, photos, raises=None, on_list=None):
    """Put a fake `osxphotos` in sys.modules. `raises` is what PhotosDB() throws
    instead of opening — how a TCC refusal arrives."""
    mod = types.ModuleType("osxphotos")
    seen = {"loads": 0}

    class PhotosDB:
        def __init__(self, *a, **kw):
            seen["loads"] += 1
            if raises is not None:
                raise raises

        def photos(self, images=True, movies=True, intrash=False):
            if on_list is not None:
                raise on_list
            assert images and not movies and not intrash
            return list(photos)

    mod.PhotosDB = PhotosDB
    monkeypatch.setitem(sys.modules, "osxphotos", mod)
    return seen


def _shoot(path, when=None):
    """A plain JPEG with no EXIF at all — the case Photos' own metadata has to
    carry on its own."""
    Image.new("RGB", (32, 32), "green").save(path, "JPEG")
    return str(path)


TZ = timezone(timedelta(hours=8))            # what Photos hands back: aware


# ── enumeration ────────────────────────────────────────────────────────────
def test_enumerate_reports_what_it_can_and_cannot_index(tmp_path, monkeypatch):
    local = _shoot(tmp_path / "a.jpg")
    install_fake(monkeypatch, [
        FakePhoto("u-local", path=local, date=datetime(2025, 7, 1, 10, tzinfo=TZ),
                  latitude=-8.5, longitude=115.2, title="Sunset",
                  albums=["Bali 2025", "Best of"], persons=["Ana", "_UNKNOWN_"],
                  favorite=True, exif_info=FakeExif("Apple", "iPhone 15")),
        FakePhoto("u-cloud"),                                  # offloaded
        FakePhoto("u-gone", path=str(tmp_path / "missing.jpg")),  # gone
        FakePhoto("u-hidden", path=local, hidden=True),
        FakePhoto("u-trash", path=local, intrash=True),
        FakePhoto("u-movie", path=local, ismovie=True),
    ])

    items, report = apple_photos.enumerate_library()

    # every photograph in the library, hidden / trashed / movie excluded — the
    # two with no readable original among them, carrying a None path, because
    # they are still in the library and still count against the pruner
    assert [it.uuid for it in items] == ["u-local", "u-cloud", "u-gone"]
    assert [it.path for it in items[1:]] == [None, None]
    assert [it.sig for it in items[1:]] == [None, None]
    it = items[0]
    assert it.path == local
    assert it.taken_at == "2025-07-01T10:00:00"          # naive local wall clock
    assert (it.lat, it.lon) == (-8.5, 115.2)
    assert it.camera == "Apple iPhone 15"
    assert it.albums == ["Bali 2025", "Best of"]
    assert it.persons == ["Ana"]                         # unnamed face dropped
    assert it.favorite is True
    # found counts the photographs lens would index; a movie is not one, and
    # hidden and trashed are not in the library the user is searching
    assert report["found"] == 3
    assert report["local"] == 1
    assert report["offloaded"] == 2       # the iCloud one and the vanished one
    assert report["movies"] == 1
    assert report["error"] is None


def test_a_permission_error_is_a_message_not_a_crash(monkeypatch):
    """macOS refuses the library until the user grants Full Disk Access, and the
    refusal arrives on the very first call. An index run has folders to scan
    either way, so this can only ever be a status line."""
    install_fake(monkeypatch, [], raises=PermissionError(1, "Operation not permitted"))

    items, report = apple_photos.enumerate_library()

    assert items == []
    assert report["found"] == 0 and report["local"] == 0
    assert "Full Disk Access" in report["error"]
    assert "Operation not permitted" in report["error"]


@pytest.mark.parametrize("exc, expect", [
    (OSError("no such database"), "Full Disk Access"),
    (RuntimeError("unsupported library version"), "Could not read"),
])
def test_any_failure_to_open_the_library_is_reported(monkeypatch, exc, expect):
    install_fake(monkeypatch, [], raises=exc)
    items, report = apple_photos.enumerate_library()
    assert items == [] and expect in report["error"]


def test_a_failure_to_list_is_reported_too(tmp_path, monkeypatch):
    install_fake(monkeypatch, [], on_list=RuntimeError("db locked"))
    items, report = apple_photos.enumerate_library()
    assert items == [] and "Could not list" in report["error"]


def test_a_missing_osxphotos_is_reported_not_raised(monkeypatch):
    """The dependency is macOS-only and lazily imported, so lens has to work on a
    machine that does not have it — including saying so."""
    monkeypatch.setitem(sys.modules, "osxphotos", None)   # import → ImportError
    items, report = apple_photos.enumerate_library()
    assert items == [] and "osxphotos is not installed" in report["error"]


def test_one_unreadable_photo_does_not_lose_the_rest(tmp_path, monkeypatch):
    class Exploding(FakePhoto):
        @property
        def albums(self):
            raise RuntimeError("row is corrupt")

        @albums.setter
        def albums(self, v):
            pass

    good = _shoot(tmp_path / "a.jpg")
    install_fake(monkeypatch, [Exploding("u-bad", path=good),
                              FakePhoto("u-good", path=good)])
    items, report = apple_photos.enumerate_library()
    assert [it.uuid for it in items] == ["u-good"]
    assert report["error"] is None


def test_a_photo_with_no_uuid_is_not_indexed(tmp_path, monkeypatch):
    """The uuid is how a row is recognised when the photo leaves the library. A
    row that could never be pruned is worse than a row that never existed."""
    p = _shoot(tmp_path / "a.jpg")
    install_fake(monkeypatch, [FakePhoto("", path=p), FakePhoto("u", path=p)])
    items, _ = apple_photos.enumerate_library()
    assert [it.uuid for it in items] == ["u"]


# ── merge ──────────────────────────────────────────────────────────────────
def _item(**kw):
    kw.setdefault("uuid", "u1")
    kw.setdefault("path", "/p/a.jpg")
    kw.setdefault("sig", (1.0, 10))
    return apple_photos.ApplePhoto(**kw)


def test_photos_wins_on_when_and_where_and_nothing_else(monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Ubud", "Bali", "ID"))
    rec = {"taken_at": "2001-01-01T00:00:00", "lat": 1.0, "lon": 2.0,
           "place_city": "Nowhere", "place_region": None, "place_country": "XX",
           "camera": "Nikon D40", "format": "JPEG", "width": 4, "height": 4,
           "raw_exif": json.dumps({"Make": "Nikon"})}

    apple_photos.merge(rec, _item(taken_at="2025-07-01T10:00:00", lat=-8.5,
                                  lon=115.2, camera="Apple iPhone 15"))

    assert rec["taken_at"] == "2025-07-01T10:00:00"
    assert (rec["lat"], rec["lon"]) == (-8.5, 115.2)
    # the place names describe the coordinates that won, not the ones that lost
    assert (rec["place_city"], rec["place_region"]) == ("Ubud", "Bali")
    assert rec["place_country"] == "ID"
    # the file's own camera is the truth about the file; Photos only fills gaps
    assert rec["camera"] == "Nikon D40"
    assert rec["width"] == 4 and rec["format"] == "JPEG"
    assert json.loads(rec["raw_exif"])["Make"] == "Nikon"
    assert rec["source"] == "apple" and rec["apple_uuid"] == "u1"


def test_photos_fills_a_camera_the_file_does_not_have(monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    rec = {"camera": None, "raw_exif": "{}", "format": "JPEG"}
    apple_photos.merge(rec, _item(camera="Apple iPhone 15"))
    assert rec["camera"] == "Apple iPhone 15"


def test_a_failed_geocode_keeps_the_coordinates(monkeypatch):
    monkeypatch.setattr(metadata, "geocode",
                        lambda a, b: (_ for _ in ()).throw(RuntimeError("no data")))
    rec = {"raw_exif": "{}", "place_city": None}
    apple_photos.merge(rec, _item(lat=-8.5, lon=115.2))
    assert (rec["lat"], rec["lon"]) == (-8.5, 115.2)
    assert rec["place_city"] is None


def test_what_photos_knows_and_the_catalog_has_no_column_for_rides_in_raw_exif():
    rec = {"raw_exif": json.dumps({"Make": "Apple"}), "format": "HEIF"}
    apple_photos.merge(rec, _item(title="Sunset", description="on the last night",
                                  albums=["Bali 2025"], persons=["Ana", "Bo"],
                                  favorite=True))
    raw = json.loads(rec["raw_exif"])
    assert raw["Make"] == "Apple"                      # nothing displaced
    assert raw["_apple"] == {"uuid": "u1", "albums": ["Bali 2025"],
                             "favorite": True, "persons": ["Ana", "Bo"],
                             "title": "Sunset",
                             "description": "on the last night"}


def test_a_photos_date_makes_a_stripped_heic_a_photograph_again():
    """A HEIC out of a sharing pipeline has no Make, no Model and no capture tag,
    so metadata.is_photo cannot tell it from a graphic — and the default scope of
    every search would hide it. Photos holds the date the file lost, and a
    capture format with a capture date is the rule that already exists."""
    rec = {"format": "HEIF", "camera": None, "lat": None, "raw_exif": "{}"}
    assert metadata.is_photo(rec, {}) is False
    apple_photos.merge(rec, _item(taken_at="2025-07-01T10:00:00"))
    assert rec["is_photo"] == 1

    # ...and a screenshot filed in Photos is still not a photograph
    shot = {"format": "PNG", "camera": None, "lat": None, "raw_exif": "{}"}
    apple_photos.merge(shot, _item(taken_at="2025-07-01T10:00:00"))
    assert shot["is_photo"] == 0


def test_albums_and_title_become_the_searchable_phrases():
    it = _item(title="Sunset", albums=["Bali 2025", "sunset", "Best of"])
    # de-duplicated case-insensitively: the title and an album of the same name
    # are one phrase, not two
    assert apple_photos.phrases(it) == "Sunset\nBali 2025\nBest of"
    assert apple_photos.phrases(_item()) == ""


def test_a_description_is_kept_but_not_made_searchable():
    """A sentence of prose is not a phrase anyone types, and every phrase becomes
    vocabulary matched against every query."""
    it = _item(description="the last night of the trip")
    assert apple_photos.phrases(it) == ""
    rec = {"raw_exif": "{}", "format": "JPEG"}
    apple_photos.merge(rec, it)
    assert json.loads(rec["raw_exif"])["_apple"]["description"] == \
        "the last night of the trip"


# ── through the indexer ────────────────────────────────────────────────────
def _apple_library(tmp_path, monkeypatch, n=2, **kw):
    """A .photoslibrary bundle with `n` originals inside it, and a fake osxphotos
    that reports them. The bundle is real: the walker must be seen to skip it."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Ubud", "Bali", "ID"))
    bundle = tmp_path / "Photos Library.photoslibrary" / "originals"
    bundle.mkdir(parents=True)
    photos = []
    for i in range(n):
        p = _shoot(bundle / f"IMG_{i}.jpg")
        photos.append(FakePhoto(
            f"u{i}", path=p, date=datetime(2025, 7, 1 + i, 10, tzinfo=TZ),
            latitude=-8.5, longitude=115.2, albums=["Bali 2025"],
            title=f"Shot {i}", favorite=(i == 0), persons=["Ana"], **kw))
    return bundle.parent, photos


def _cfg(cache_dir, **kw):
    config.save_config({**config.DEFAULTS, **kw}, cache_dir)


def test_the_bundle_is_ingested_without_ever_being_walked(cache_dir, tmp_path,
                                                          monkeypatch):
    """The walker still refuses the bundle (it holds a derivative render for
    every original, so walking one catalogues each photo several times). The rows
    arrive through the library's database instead, pointing at the originals
    where they lie."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    # the bundle's own parent is a configured root, so the walker sees it and
    # must still decline to descend
    stats = indexer.index_once(store, [str(tmp_path)], FakeEmbedder(), cache_dir)

    assert stats["added"] == 2 and stats["embedded"] == 2
    rows = store.query_photos("1 = 1", [])
    assert {r["source"] for r in rows} == {"apple"}
    assert sorted(r["apple_uuid"] for r in rows) == ["u0", "u1"]
    assert all(str(bundle) in r["path"] for r in rows)
    assert stats["apple"]["found"] == 2 and stats["apple"]["offloaded"] == 0

    # Photos' facts, in the catalog
    first = store.get_photo(str(bundle / "originals" / "IMG_0.jpg"))
    assert first["taken_at"] == "2025-07-01T10:00:00"
    assert first["place_city"] == "Ubud" and first["is_photo"] == 1
    assert first["apple_text"] == "Shot 0\nBali 2025"
    assert json.loads(first["raw_exif"])["_apple"]["favorite"] is True


def test_ingest_is_off_until_it_is_asked_for(cache_dir, tmp_path, monkeypatch):
    """Reading the library needs a permission the user grants by hand, and the
    first sync of a real one is long. Both are opt-in."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    seen = install_fake(monkeypatch, photos)
    store = Store(cache_dir)

    stats = indexer.index_once(store, [str(tmp_path)], FakeEmbedder(), cache_dir)

    assert stats["added"] == 0 and "apple" not in stats
    assert seen["loads"] == 0             # PhotosDB never even opened
    assert store.path_signatures() == {}


def test_a_second_run_reads_nothing_and_re_embeds_nothing(cache_dir, tmp_path,
                                                          monkeypatch):
    """An original inside the bundle is an ordinary file with an ordinary
    (mtime, size), so it goes through the same skip-if-unchanged path as a photo
    in a folder."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    indexer.index_once(store, [], FakeEmbedder(), cache_dir)
    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["skipped"] == 2
    assert stats["added"] == stats["changed"] == stats["embedded"] == 0
    assert stats["removed"] == 0                # and nothing pruned, either


def test_an_offloaded_original_is_counted_not_indexed(cache_dir, tmp_path,
                                                      monkeypatch):
    """With "Optimise Mac Storage" on, the original is not on this machine.
    Downloading it would mean the network and the user's iCloud quota, which lens
    does not touch — so it is reported and left alone."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    photos.append(FakePhoto("u-cloud", path=None,
                            date=datetime(2025, 7, 9, 10, tzinfo=TZ)))
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["added"] == 2
    assert stats["apple"] == {"found": 3, "local": 2, "offloaded": 1,
                              "movies": 0, "error": None,
                              "seconds": stats["apple"]["seconds"],
                              "indexed": 2, "at": stats["apple"]["at"]}
    assert store.get_meta(indexer.APPLE_META)


def test_an_offload_between_two_runs_keeps_the_row(cache_dir, tmp_path,
                                                   monkeypatch):
    """Same rule as an unmounted drive: nothing was read, so nothing is known,
    and the photo is still in the user's library. The row (and its thumbnail)
    stays rather than the library losing a photo to a storage setting."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)
    indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    photos[1].path = None                     # iCloud took the original away
    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["removed"] == 0
    assert len(store.path_signatures()) == 2
    assert stats["apple"]["offloaded"] == 1


def test_a_photo_deleted_from_photos_is_pruned(cache_dir, tmp_path, monkeypatch):
    """By uuid, which is the only thing that says "this photograph left the
    library" — the file may well still be sitting in the bundle."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)
    indexer.index_once(store, [], FakeEmbedder(), cache_dir)
    gone = photos.pop()                       # removed from Photos, file kept

    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["removed"] == 1
    assert store.get_photo(gone.path) is None
    assert len(store.load_embeddings()[0]) == 1        # its vector went too


def test_a_refused_library_prunes_nothing(cache_dir, tmp_path, monkeypatch):
    """"macOS would not let us read the library" is not evidence that the library
    is empty. Treating it as such would delete every Apple row the first time
    someone moved the daemon to a terminal without Full Disk Access."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)
    indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    install_fake(monkeypatch, [], raises=PermissionError("nope"))
    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["removed"] == 0
    assert len(store.path_signatures()) == 2
    assert "Full Disk Access" in stats["apple"]["error"]
    assert json.loads(store.get_meta(indexer.APPLE_META))["error"]


def test_switching_it_off_removes_the_photos_it_added(cache_dir, tmp_path,
                                                      monkeypatch):
    """The same thing removing a folder means. The files are untouched — nothing
    here ever writes to the library — but they leave the searchable catalog."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)
    indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    _cfg(cache_dir, apple_photos=False)
    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["removed"] == 2
    assert store.path_signatures() == {}
    assert store.apple_paths() == {}
    assert store.get_meta(indexer.APPLE_META) == ""    # no stale status line
    assert all(p.exists() for p in bundle.rglob("*.jpg"))


def test_folder_photos_are_untouched_by_either_apple_path(cache_dir, tmp_path,
                                                          monkeypatch):
    """The two pruners must not reach into each other's rows: Apple rows are
    exempt from "no configured root covers this any more", and folder rows are
    exempt from the uuid diff."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    _shoot(folder / "own.jpg")
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    indexer.index_once(store, [str(folder)], FakeEmbedder(), cache_dir)
    assert len(store.path_signatures()) == 3

    # the whole Photos library empties out; the folder photo is not its business
    install_fake(monkeypatch, [])
    stats = indexer.index_once(store, [str(folder)], FakeEmbedder(), cache_dir)
    assert stats["removed"] == 2
    assert list(store.path_signatures()) == [str(folder / "own.jpg")]
    assert store.query_photos("source = 'folder'", [])[0]["apple_uuid"] is None

    # ...and now the folder goes away while Photos is unchanged
    install_fake(monkeypatch, photos)
    indexer.index_once(store, [str(folder)], FakeEmbedder(), cache_dir)
    (folder / "own.jpg").unlink()
    stats = indexer.index_once(store, [str(folder)], FakeEmbedder(), cache_dir)
    assert stats["removed"] == 1
    assert sorted(r["source"] for r in store.query_photos("1 = 1", [])) \
        == ["apple", "apple"]


def test_an_apple_original_that_cannot_be_opened_stays_an_apple_row(
        cache_dir, tmp_path, monkeypatch):
    """Or the pruner would stop recognising it as one, and the folder rule would
    delete it on the next run."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    bundle = tmp_path / "L.photoslibrary" / "originals"
    bundle.mkdir(parents=True)
    torn = bundle / "IMG_0.jpg"
    torn.write_bytes(b"not a jpeg")
    install_fake(monkeypatch, [FakePhoto("u0", path=str(torn), albums=["Bali"],
                                         date=datetime(2025, 7, 1, tzinfo=TZ))])
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    assert stats["errors"] == 1
    row = store.get_photo(str(torn))
    assert row["error"] and row["source"] == "apple" and row["apple_uuid"] == "u0"
    assert row["apple_text"] == "Bali"
    assert indexer.index_once(store, [], FakeEmbedder(), cache_dir)["removed"] == 0


def test_apple_photos_join_the_trips_they_belong_to(cache_dir, tmp_path,
                                                    monkeypatch):
    """Trips are computed from taken_at and GPS, and an Apple row carries both in
    the same columns a folder row does — so it needs no special case, only the
    dates and coordinates Photos supplied."""
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (("Ubud", "Bali", "ID") if lon > 100
                                          else ("Mumbai", "Maharashtra", "IN")))
    bundle = tmp_path / "L.photoslibrary" / "originals"
    bundle.mkdir(parents=True)
    photos = []
    # five at home, then three far away on consecutive days
    for i in range(5):
        p = _shoot(bundle / f"home{i}.jpg")
        photos.append(FakePhoto(f"h{i}", path=p, latitude=19.07, longitude=72.88,
                                date=datetime(2025, 6, 1 + i, 10, tzinfo=TZ)))
    for i in range(3):
        p = _shoot(bundle / f"away{i}.jpg")
        photos.append(FakePhoto(f"a{i}", path=p, latitude=-8.4, longitude=115.1,
                                date=datetime(2025, 7, 10 + i, 10, tzinfo=TZ),
                                albums=["Bali 2025"]))
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    indexer.index_once(store, [], FakeEmbedder(), cache_dir)

    trips = store.get_trips()
    assert len(trips) == 1 and trips[0]["place"] == "Ubud"
    assert trips[0]["name"] == "Ubud · Jul 2025"
    away = store.query_photos("place_city = 'Ubud'", [])
    assert len(away) == 3 and {r["trip_id"] for r in away} == {trips[0]["id"]}
    assert all(r["trip_id"] is None
               for r in store.query_photos("place_city = 'Mumbai'", []))


def test_the_library_is_opened_once_per_run(cache_dir, tmp_path, monkeypatch):
    """PhotosDB costs seconds to load on a real library. Once per run, never per
    photo, and never at all while the feature is off."""
    bundle, photos = _apple_library(tmp_path, monkeypatch, n=5)
    seen = install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=True)
    store = Store(cache_dir)

    indexer.index_once(store, [], FakeEmbedder(), cache_dir)
    assert seen["loads"] == 1
    indexer.index_once(store, [], FakeEmbedder(), cache_dir)
    assert seen["loads"] == 2


def test_an_explicit_flag_beats_the_config(cache_dir, tmp_path, monkeypatch):
    """`apple=` is for a caller that already knows — a test, a one-off index —
    while None (the daemon, the CLI) asks the config, which is where the settings
    panel writes the toggle."""
    bundle, photos = _apple_library(tmp_path, monkeypatch)
    install_fake(monkeypatch, photos)
    _cfg(cache_dir, apple_photos=False)
    store = Store(cache_dir)

    stats = indexer.index_once(store, [], FakeEmbedder(), cache_dir, apple=True)
    assert stats["added"] == 2


# ── the fake, against the real thing ───────────────────────────────────────
def test_the_fake_matches_the_real_osxphotos_api():
    """The shape above is only worth testing against if it is the real shape.

    Skipped where osxphotos is not installed (a non-macOS CI box), which is
    exactly where the fake cannot drift into a lie that matters — and checked
    everywhere it is."""
    osxphotos = pytest.importorskip("osxphotos")
    import inspect

    sig = inspect.signature(osxphotos.PhotosDB.photos)
    for name in ("images", "movies", "intrash"):
        assert name in sig.parameters, name
    for attr in ("uuid", "path", "date", "latitude", "longitude", "title",
                 "description", "albums", "persons", "favorite", "hidden",
                 "intrash", "ismovie", "exif_info"):
        assert hasattr(osxphotos.PhotoInfo, attr), attr
    from osxphotos.exifinfo import ExifInfo
    assert {"camera_make", "camera_model"} <= set(ExifInfo.__dataclass_fields__)
