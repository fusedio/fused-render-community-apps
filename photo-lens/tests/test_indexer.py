import json
import os
from datetime import datetime

import numpy as np
import pytest
import piexif
from PIL import Image

from lens import indexer, memguard, metadata
from lens.store import Store
from lens.thumbs import thumb_path
from tests.conftest import FakeFaceModel, face_photo, write_video


class FakeEmbedder:
    dim = 4
    key = "fake"

    def embed_images(self, imgs):
        return np.full((len(imgs), 4), 0.5, dtype=np.float16)


class WideEmbedder:
    """A different model: different key, different dimensionality."""
    dim = 8
    key = "fake-wide"

    def embed_images(self, imgs):
        return np.full((len(imgs), 8), 0.25, dtype=np.float16)


class RaiseOnceEmbedder:
    """Fails the first batch (e.g. the first 16-photo flush), succeeds after."""
    dim = 4
    key = "fake"

    def __init__(self):
        self.calls = 0

    def embed_images(self, imgs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return np.full((len(imgs), 4), 0.5, dtype=np.float16)


def _shoot(path):
    Image.new("RGB", (32, 32), "green").save(path, "JPEG")


def test_incremental_lifecycle(cache_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    _shoot(root / "b.jpg")
    (root / "notes.txt").write_text("not a photo")

    store = Store(cache_dir)
    fe = FakeEmbedder()

    s1 = indexer.index_once(store, [str(root)], fe, cache_dir)
    assert s1["added"] == 2 and s1["embedded"] == 2
    ids, mat = store.load_embeddings()
    assert len(ids) == 2 and mat.shape == (2, 4)

    # steady state: nothing re-read, nothing re-embedded
    s2 = indexer.index_once(store, [str(root)], fe, cache_dir)
    # Run-metrics fields (duration_s, stages, rate, mem_peak_gb) are on every
    # stats dict now (see test_run_metrics_are_reported below) and vary by
    # definition — they are timings and a memory reading — so this exact
    # equality is over everything *else*, which a steady-state run must still
    # reproduce byte for byte.
    metrics_keys = {"duration_s", "stages", "rate", "mem_peak_gb"}
    assert {k: v for k, v in s2.items() if k not in metrics_keys} == {
                  "added": 0, "changed": 0, "removed": 0, "moved": 0,
                  "skipped": 2, "embedded": 0, "errors": 0,
                  # the face pass has nothing left to do either: both rows were
                  # scanned on the first run and stamped with the model's key
                  "faces": 0, "face_photos": 0, "face_errors": 0,
                  "people": {"people": 0, "clustered": 0, "named": 0}}

    # move keeps embedding (sha1 match), no re-embed
    (root / "a.jpg").rename(root / "sub_a.jpg")
    s3 = indexer.index_once(store, [str(root)], fe, cache_dir)
    assert s3["moved"] == 1 and s3["embedded"] == 0
    assert store.get_photo(str(root / "sub_a.jpg")) is not None
    assert store.get_photo(str(root / "a.jpg")) is None

    # delete prunes catalog + embedding row
    (root / "b.jpg").unlink()
    s4 = indexer.index_once(store, [str(root)], fe, cache_dir)
    assert s4["removed"] == 1
    ids, mat = store.load_embeddings()
    assert len(ids) == 1 and mat.shape == (1, 4)


def test_corrupt_file_flagged_not_fatal(cache_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    (root / "broken.jpg").write_bytes(b"not really a jpeg")
    _shoot(root / "ok.jpg")
    store = Store(cache_dir)
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert stats["errors"] == 1 and stats["added"] == 2
    assert store.get_photo(str(root / "broken.jpg"))["error"] is not None


def test_embed_batch_failure_is_isolated(cache_dir, tmp_path, monkeypatch):
    """An embedder exception on one batch must not (a) mark unrelated
    healthy photos as corrupt, (b) leak past batches, or (c) abort the run
    before embeddings/trips are saved for the rest of the catalog."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(20):
        _shoot(root / f"p{i:02d}.jpg")
    store = Store(cache_dir)

    stats = indexer.index_once(store, [str(root)], RaiseOnceEmbedder(), cache_dir)

    assert stats["added"] == 20
    assert stats["errors"] == 16     # first (failed) batch of 16
    assert stats["embedded"] == 4    # second (successful) batch of 4

    ids, mat = store.load_embeddings()
    assert len(ids) == 4 and mat.shape == (4, 4)

    errored = store.query_photos("error IS NOT NULL", [])
    ok = store.query_photos("error IS NULL", [])
    assert len(errored) == 16 and len(ok) == 4
    # metadata for the failed batch wasn't clobbered, just flagged
    assert all(r["sha1"] for r in errored)


def test_errored_files_retried_next_run(cache_dir, tmp_path, monkeypatch):
    """Rows flagged with an embed error must be retried on the next run even
    though their on-disk (mtime, size) signature hasn't changed."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(20):
        _shoot(root / f"p{i:02d}.jpg")
    store = Store(cache_dir)

    indexer.index_once(store, [str(root)], RaiseOnceEmbedder(), cache_dir)
    assert len(store.query_photos("error IS NOT NULL", [])) == 16

    stats2 = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats2["errors"] == 0
    assert stats2["embedded"] == 16   # only the previously-errored ones
    assert stats2["skipped"] == 4     # already-good ones untouched

    assert store.query_photos("error IS NOT NULL", []) == []
    ids, mat = store.load_embeddings()
    assert len(ids) == 20 and mat.shape == (20, 4)


def test_model_swap_re_embeds_the_library(cache_dir, tmp_path, monkeypatch):
    """Switching models used to brick the index: the surviving vectors had the
    old dimensionality, so np.stack raised on the mix and no run could ever
    complete again. Re-embed everything instead — the old vectors live in a
    different coordinate space regardless of their width."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for name in ("a.jpg", "b.jpg"):
        _shoot(root / name)
    store = Store(cache_dir)

    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    ids, mat = store.load_embeddings()
    assert mat.shape == (2, 4)
    assert store.get_meta("embed_model") == "fake"

    stats = indexer.index_once(store, [str(root)], WideEmbedder(), cache_dir)

    assert stats["embedded"] == 2 and stats["skipped"] == 0
    ids, mat = store.load_embeddings()
    assert len(ids) == 2 and mat.shape == (2, 8)
    assert store.get_meta("embed_model") == "fake-wide"

    # and the run after the swap is a normal no-op again
    stats2 = indexer.index_once(store, [str(root)], WideEmbedder(), cache_dir)
    assert stats2["embedded"] == 0 and stats2["skipped"] == 2

    # the shape that actually crashed: one new photo embedded at the new width
    # while the stored matrix is still at the old one, met in a single np.stack
    # ("all input arrays must have the same shape").
    _shoot(root / "c.jpg")
    stats3 = indexer.index_once(store, [str(root)], WideEmbedder(), cache_dir)
    assert stats3["added"] == 1
    ids, mat = store.load_embeddings()
    assert len(ids) == 3 and mat.shape == (3, 8)


def test_thumb_version_bump_re_embeds_the_library(cache_dir, tmp_path,
                                                  monkeypatch):
    """Vectors are computed from the thumbnail, so a change to how thumbs are
    rendered makes every stored vector describe an image lens no longer
    produces. (The v1→v2 case was real: transparency composited onto black, so
    hundreds of PNGs were embedded as black rectangles.)"""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for name in ("a.jpg", "b.jpg"):
        _shoot(root / name)
    store = Store(cache_dir)

    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["embedded"] == 2
    assert store.get_meta("thumb_version") == str(indexer.THUMB_VERSION)
    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["embedded"] == 0

    monkeypatch.setattr(indexer, "THUMB_VERSION", 99)
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert stats["embedded"] == 2 and stats["skipped"] == 0
    assert store.get_meta("thumb_version") == "99"

    # ...and the run after it is a no-op again
    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["embedded"] == 0


def test_a_catalog_predating_thumb_versioning_is_re_embedded(cache_dir, tmp_path,
                                                            monkeypatch):
    """No recorded version + rows already in the catalog = built by the old
    thumb pipeline. Absence is the signal; there is nothing else to compare."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    store.set_meta("thumb_version", None)          # as if it had never been set
    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["embedded"] == 1


def test_first_run_is_not_reported_as_a_thumb_change(cache_dir, tmp_path,
                                                     monkeypatch, capsys):
    """A fresh catalog has no stale vectors to redo, so it must not announce a
    re-embed it is not doing."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    indexer.index_once(Store(cache_dir), [str(root)], FakeEmbedder(), cache_dir)
    assert "thumbnail rendering changed" not in capsys.readouterr().out


def test_is_photo_is_persisted_per_file(cache_dir, tmp_path, monkeypatch):
    """The scope filter is a plain SQL column, so the derivation has to land in
    the catalog — not just in the record the indexer built."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    Image.new("RGBA", (32, 32), (255, 0, 0, 0)).save(root / "overlay.png", "PNG")
    shot = root / "shot.jpg"
    Image.new("RGB", (32, 32), "green").save(shot, "JPEG")
    piexif.insert(piexif.dump({"Exif": {
        piexif.ExifIFD.DateTimeOriginal: b"2025:07:01 10:00:00"}}), str(shot))

    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert store.get_photo(str(shot))["is_photo"] == 1
    assert store.get_photo(str(root / "overlay.png"))["is_photo"] == 0
    assert store.scope_counts() == {"all": 2, "photos": 1, "videos": 0}


def test_a_killed_run_keeps_the_vectors_it_had(cache_dir, tmp_path, monkeypatch):
    """Vectors used to be written once, at the very end. A first index of a real
    library is tens of minutes of GPU work, and the daemon was OOM-killed two
    thirds of the way through — every vector it had computed died with the
    process, so the next run started from zero and hit the same wall.

    Checkpoints bound the loss to one interval, and the run that follows resumes
    instead of restarting."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    monkeypatch.setattr(indexer, "CHECKPOINT_EVERY", 16)
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(48):
        _shoot(root / f"p{i:02d}.jpg")

    class DiesEmbedder(FakeEmbedder):
        """Killed, not failed: a SIGKILL has no exception, and the nearest thing
        a test can do is raise something the indexer's own `except Exception`
        handlers cannot swallow."""

        def __init__(self):
            self.seen = 0

        def embed_images(self, imgs):
            self.seen += len(imgs)
            if self.seen > 32:
                raise KeyboardInterrupt("killed mid-run")
            return super().embed_images(imgs)

    store = Store(cache_dir)
    with pytest.raises(KeyboardInterrupt):
        indexer.index_once(store, [str(root)], DiesEmbedder(), cache_dir)

    ids, mat = store.load_embeddings()
    assert len(ids) == 32, len(ids)          # two checkpoints' worth, kept
    assert mat.shape == (32, 4)
    # the versions were recorded up front, so the next run treats the vectors on
    # disk as valid and backfills the rest — rather than declaring the whole
    # library stale again and starting over into the same kill
    assert store.get_meta("thumb_version") == str(indexer.THUMB_VERSION)

    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats["embedded"] == 16            # only the ones that were missing
    assert stats["skipped"] == 32
    ids, mat = store.load_embeddings()
    assert len(ids) == 48


def test_a_rebuild_discards_stale_vectors_before_it_starts(cache_dir, tmp_path,
                                                          monkeypatch):
    """The invariant the resume relies on: the stored matrix only ever holds
    vectors valid for the recorded versions. So a rebuild wipes first and works
    second — otherwise a run killed before its first checkpoint would leave the
    old vectors on disk *and* the new version recorded, and those stale vectors
    would never be redone."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for name in ("a.jpg", "b.jpg"):
        _shoot(root / name)
    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert len(store.load_embeddings()[0]) == 2

    class DiesImmediately(FakeEmbedder):
        def embed_images(self, imgs):
            raise KeyboardInterrupt("killed before the first checkpoint")

    monkeypatch.setattr(indexer, "THUMB_VERSION", 99)
    with pytest.raises(KeyboardInterrupt):
        indexer.index_once(store, [str(root)], DiesImmediately(), cache_dir)

    assert len(store.load_embeddings()[0]) == 0      # stale vectors are gone
    assert store.get_meta("thumb_version") == "99"

    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert stats["embedded"] == 2                    # redone against v99 thumbs
    assert len(store.load_embeddings()[0]) == 2


def test_graphics_do_not_bridge_two_trips(cache_dir, tmp_path, monkeypatch):
    """A trip is derived from when and where pictures were taken. A graphic's
    `taken_at` is its file mtime, so a screenshot saved between two trips used
    to fill the 48-hour gap that separates them and merge them into one."""
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (("Ubud", "Bali", "ID") if lat < 0
                                          else ("Mumbai", "Maharashtra", "IN")))
    root = tmp_path / "photos"
    root.mkdir()

    def shot(name, when, lat_ref, lat, lon_ref, lon):
        p = root / name
        Image.new("RGB", (32, 32), "green").save(p, "JPEG")
        piexif.insert(piexif.dump({
            "Exif": {piexif.ExifIFD.DateTimeOriginal: when.encode()},
            "GPS": {piexif.GPSIFD.GPSLatitudeRef: lat_ref,
                    piexif.GPSIFD.GPSLatitude: lat,
                    piexif.GPSIFD.GPSLongitudeRef: lon_ref,
                    piexif.GPSIFD.GPSLongitude: lon}}), str(p))

    HOME = (b"N", [(19, 1), (4, 1), (0, 1)], b"E", [(72, 1), (52, 1), (0, 1)])
    AWAY = (b"S", [(8, 1), (24, 1), (0, 1)], b"E", [(115, 1), (6, 1), (0, 1)])
    # home (the majority, which is how compute_trips finds it), then two Bali
    # trips a fortnight apart, then home again. Three photographs each, because
    # that is now the fewest that make a trip (trips.MIN_PHOTOS).
    shot("home1.jpg", "2025:06:01 09:00:00", *HOME)
    shot("home2.jpg", "2025:06:02 09:00:00", *HOME)
    shot("t1a.jpg", "2025:07:01 09:00:00", *AWAY)
    shot("t1b.jpg", "2025:07:02 09:00:00", *AWAY)
    shot("t1c.jpg", "2025:07:02 18:00:00", *AWAY)
    shot("t2a.jpg", "2025:07:20 09:00:00", *AWAY)
    shot("t2b.jpg", "2025:07:21 09:00:00", *AWAY)
    shot("t2c.jpg", "2025:07:22 09:00:00", *AWAY)
    # home has to stay the *majority* city, or compute_trips reads Bali as home
    # and calls these two Mumbai stays the trips instead
    shot("home3.jpg", "2025:08:01 09:00:00", *HOME)
    shot("home4.jpg", "2025:08:02 09:00:00", *HOME)
    shot("home5.jpg", "2025:08:03 09:00:00", *HOME)
    shot("home6.jpg", "2025:08:04 09:00:00", *HOME)
    shot("home7.jpg", "2025:08:05 09:00:00", *HOME)

    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert len(store.get_trips()) == 2

    # a graphic dated into the middle of the gap, which would splice the two
    # segments together if trips looked at anything but photographs
    for day in range(4, 19):
        g = root / f"screenshot_{day:02d}.png"
        Image.new("RGB", (32, 32), "white").save(g, "PNG")
        ts = datetime(2025, 7, day, 12, 0).timestamp()
        os.utime(g, (ts, ts))

    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    trips_after = store.get_trips()
    assert len(trips_after) == 2, [t["name"] for t in trips_after]
    assert trips_after[0]["end"].startswith("2025-07-02")   # not stretched
    assert trips_after[1]["start"].startswith("2025-07-20")  # still two trips
    # and the graphics are in no trip at all
    assert store.get_photo(str(root / "screenshot_10.png"))["trip_id"] is None


def test_rows_missing_a_vector_are_backfilled(cache_dir, tmp_path, monkeypatch):
    """The catalog and the embedding matrix are separate files and can fall out
    of step — a model swap performed while a root was offline drops the vectors
    of photos that were never rescanned, and a run killed mid-flight leaves the
    same gap. Those rows still match on signature, so without an explicit check
    they are never re-embedded and stay invisible to semantic search."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        _shoot(root / f"p{i}.jpg")
    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    ids, mat = store.load_embeddings()
    assert len(ids) == 3
    # simulate the gap: keep one vector, drop the other two
    keep = ids[:1]
    store.save_embeddings(keep, mat[:1])

    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats["embedded"] == 2      # only the two missing ones
    assert stats["skipped"] == 1      # the one that still had a vector
    ids2, mat2 = store.load_embeddings()
    assert sorted(ids2.tolist()) == sorted(ids.tolist())
    assert mat2.shape == (3, 4)


def test_checkpoints_embeddings_during_a_long_run(cache_dir, tmp_path, monkeypatch):
    """A run over the real library can be killed (OOM, crash) long before the
    single save_embeddings() at the end of index_once ever executes — without
    a mid-run checkpoint the whole run's work vanishes with the process. This
    pins that a checkpoint actually lands on disk partway through, at the
    documented cadence (every 8 flushes of 16 = 128 images)."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    n_images = 150
    for i in range(n_images):
        _shoot(root / f"p{i:03d}.jpg")
    store = Store(cache_dir)

    seen_at_checkpoint = {}

    def progress(done, total, stage=indexer.STAGE_INDEX):
        if stage != indexer.STAGE_INDEX:
            return                          # the face sweep counts separately
        if done == 128:                     # right after the 8th flush
            ids, _ = store.load_embeddings()
            seen_at_checkpoint["n"] = len(ids)

    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir,
                               progress=progress)

    assert stats["added"] == n_images and stats["embedded"] == n_images
    assert seen_at_checkpoint.get("n") == 128, (
        "expected a checkpoint on disk with 128 vectors right after the 8th "
        "flush, not just at the end of the run")
    ids, mat = store.load_embeddings()
    assert len(ids) == n_images and mat.shape == (n_images, 4)


def test_checkpoint_partial_run_backfills_the_rest_on_restart(cache_dir, tmp_path,
                                                               monkeypatch):
    """The scenario the checkpoint exists for: a run is killed partway through,
    losing only the in-memory tail. On restart, rows the checkpoint already
    covers must keep their vectors untouched, and only the rest gets embedded —
    this is the same `pid not in emb` backfill path that recovers a catalog
    whose embedding file fell out of step with the catalog for any reason."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    n_images = 40
    for i in range(n_images):
        _shoot(root / f"p{i:03d}.jpg")
    store = Store(cache_dir)

    # Simulate the kill: only the first checkpoint (128... here we only have 40
    # images, so simulate directly) made it to disk, then the process died —
    # the catalog has all 40 rows (upserted before embedding) but the
    # embedding file only has a subset, as a mid-run checkpoint would leave it.
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    ids, mat = store.load_embeddings()
    assert len(ids) == n_images
    kept = ids[:16]                      # what an interrupted run's last
    store.save_embeddings(kept, mat[:16])  # checkpoint would have saved

    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats["embedded"] == n_images - 16   # only the missing tail
    assert stats["skipped"] == 16               # checkpointed rows untouched
    ids2, mat2 = store.load_embeddings()
    assert sorted(ids2.tolist()) == sorted(ids.tolist())
    assert mat2.shape == (n_images, 4)


def test_same_model_does_not_re_embed(cache_dir, tmp_path, monkeypatch):
    """The model-change check must not fire on a first run with no recorded
    model, nor on an unchanged one."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    store = Store(cache_dir)
    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["embedded"] == 1
    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["embedded"] == 0


def test_configured_but_missing_root_does_not_wipe_catalog(cache_dir, tmp_path,
                                                           monkeypatch):
    """An unmounted drive is still a configured root: nothing was scanned
    there, so "absent from disk" says nothing about its photos and they have to
    survive to be found again when it comes back."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    store = Store(cache_dir)
    fe = FakeEmbedder()

    indexer.index_once(store, [str(root)], fe, cache_dir)
    assert store.get_photo(str(root / "a.jpg")) is not None

    # the drive goes away, but the root stays in the config
    root.rename(tmp_path / "unplugged")
    stats = indexer.index_once(store, [str(root)], fe, cache_dir)

    assert stats["removed"] == 0
    assert store.get_photo(str(root / "a.jpg")) is not None
    ids, mat = store.load_embeddings()
    assert len(ids) == 1

    # and a root the user never had photos under can't disturb them either
    stats = indexer.index_once(
        store, [str(root), str(tmp_path / "does-not-exist")], fe, cache_dir)
    assert stats["removed"] == 0
    assert store.get_photo(str(root / "a.jpg")) is not None


def test_root_dropped_from_config_is_pruned(cache_dir, tmp_path, monkeypatch):
    """Removing a folder from the library has to actually empty it out. Its
    files are still on disk and still readable — only the config changed — so
    the "was it scanned this run" test alone would keep them catalogued
    forever, and the photos of a folder the user removed would stay searchable."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    keep, drop = tmp_path / "keep", tmp_path / "drop"
    keep.mkdir()
    drop.mkdir()
    _shoot(keep / "a.jpg")
    _shoot(drop / "b.jpg")
    store = Store(cache_dir)
    fe = FakeEmbedder()

    indexer.index_once(store, [str(keep), str(drop)], fe, cache_dir)
    ids, _ = store.load_embeddings()
    assert len(ids) == 2

    stats = indexer.index_once(store, [str(keep)], fe, cache_dir)

    assert stats["removed"] == 1
    assert store.get_photo(str(drop / "b.jpg")) is None
    assert store.get_photo(str(keep / "a.jpg")) is not None
    ids, mat = store.load_embeddings()
    assert len(ids) == 1 and mat.shape == (1, 4)

    # dropping the last root empties the library rather than stranding it
    assert indexer.index_once(store, [], fe, cache_dir)["removed"] == 1
    assert store.path_signatures() == {}


def test_walk_skips_hidden_and_junk_directories(cache_dir, tmp_path, monkeypatch):
    """A home directory is a reasonable root now that folders are chosen in the
    UI, so the scan must not disappear into caches and dependency trees."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "home"
    for sub in (".hidden", "node_modules", "Library", "__pycache__", "venv",
                "Applications", "System", "real"):
        (root / sub).mkdir(parents=True)
    _shoot(root / ".hidden" / "x.jpg")
    _shoot(root / "node_modules" / "y.jpg")
    _shoot(root / "Library" / "cached.jpg")
    _shoot(root / "__pycache__" / "p.jpg")
    _shoot(root / "venv" / "v.jpg")
    _shoot(root / "Applications" / "app.jpg")
    _shoot(root / "System" / "s.jpg")
    _shoot(root / "real" / "z.jpg")

    found, scanned = indexer.scan_roots([str(root)])

    assert sorted(found) == [str(root / "real" / "z.jpg")]
    assert scanned == [str(root)]

    store = Store(cache_dir)
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert stats["added"] == 1
    assert store.get_photo(str(root / "real" / "z.jpg")) is not None

    # the rule applies to subdirectories, not to the root itself: a root that
    # is literally called "Library" is a deliberate choice and must still scan
    lib = tmp_path / "Library"
    lib.mkdir()
    _shoot(lib / "in-root.jpg")
    found, _ = indexer.scan_roots([str(lib)])
    assert sorted(found) == [str(lib / "in-root.jpg")]


def test_walk_skips_photos_library_bundles(cache_dir, tmp_path, monkeypatch):
    """An Apple Photos library is a directory, but it holds every original and
    every derivative render, under paths the user never chose. Walking into one
    indexes the same photo several times over."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "Pictures"
    for sub in ("Photos Library.photoslibrary/originals",
                "old.PhotosLibrary/originals", "loose"):
        (root / sub).mkdir(parents=True)
    _shoot(root / "Photos Library.photoslibrary/originals/a.jpg")
    _shoot(root / "old.PhotosLibrary/originals/b.jpg")
    _shoot(root / "loose" / "c.jpg")

    found, _ = indexer.scan_roots([str(root)])

    assert sorted(found) == [str(root / "loose" / "c.jpg")]


def test_under_root_handles_the_filesystem_root(tmp_path):
    """normpath leaves "/" as "/", so the old `root + os.sep` was "//" — a
    prefix of nothing, which made a configured "/" prune the whole catalog."""
    assert indexer._under_root("/a/b.jpg", "/")
    assert indexer._under_root("/a/b.jpg", "//")
    assert indexer._under_root("/", "/")
    assert not indexer._under_root("relative/b.jpg", "/")

    # and a trailing slash on an ordinary root is still just that root
    assert indexer._under_root("/a/b.jpg", "/a/")
    assert indexer._under_root("/a/b.jpg", "/a")
    assert not indexer._under_root("/ab/c.jpg", "/a")


# ── videos ───────────────────────────────────────────────────────────────
class FrameEmbedder:
    """One distinct unit vector per image handed to it, recorded in order — so a
    test can compute what the pooled vector *should* be and compare."""
    dim = 4
    key = "frames"

    def __init__(self):
        self.batches = []

    def embed_images(self, imgs):
        self.batches.append(len(imgs))
        n = sum(self.batches[:-1])
        out = np.zeros((len(imgs), 4), dtype=np.float32)
        for i in range(len(imgs)):
            out[i, (n + i) % 4] = 1.0            # a different axis per image
        return out.astype(np.float16)

    @property
    def images(self):
        return sum(self.batches)


def test_a_video_is_indexed_like_everything_else(cache_dir, tmp_path, monkeypatch):
    """It flows through the same todo/skip/thumbnail/embed machinery a photograph
    does — the only things that differ are which decoder read it and how many
    images its one vector was pooled from."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "media"
    root.mkdir()
    _shoot(root / "a.jpg")
    clip = write_video(root / "clip.mp4", seconds=2.0, fps=10, size=32)

    store = Store(cache_dir)
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats["added"] == 2 and stats["errors"] == 0
    row = store.get_photo(str(clip))
    assert row["kind"] == "video" and row["error"] is None
    assert row["duration_s"] == pytest.approx(2.0, abs=0.15)
    assert row["format"] == "MP4" and row["is_photo"] == 0
    # one thumbnail, under the video file's own hash
    assert thumb_path(cache_dir, row["sha1"], indexer.THUMB_SIZE).exists()
    # ...and one vector, in the same matrix as the photograph's
    ids, mat = store.load_embeddings()
    assert sorted(ids) == sorted([row["id"], store.get_photo(str(root / "a.jpg"))["id"]])
    assert mat.shape == (2, 4)
    assert store.scope_counts() == {"all": 2, "photos": 0, "videos": 1}


def test_a_videos_vector_is_its_frames_pooled(cache_dir, tmp_path, monkeypatch):
    """Six frames in, one vector out: the mean of the frame vectors, re-normalized.

    Both halves matter. The mean is the direction the clip is *about*; the
    re-normalization is what puts it on the same scale as a photograph's vector,
    because ranking is a bare dot product (query.rank) and a short vector would
    lose to every still in the library for the same content."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "media"
    root.mkdir()
    write_video(root / "clip.mp4", seconds=2.0, fps=10, size=32)

    store = Store(cache_dir)
    fe = FrameEmbedder()
    indexer.index_once(store, [str(root)], fe, cache_dir)

    assert fe.images == indexer.VIDEO_FRAMES        # one call per sampled frame
    ids, mat = store.load_embeddings()
    assert mat.shape == (1, 4)                       # ...and a single row for it
    vec = mat[0].astype(np.float32)
    assert np.linalg.norm(vec) == pytest.approx(1.0, abs=1e-2)
    # six frames over four axes: two axes were hit twice, two once
    want = np.array([2, 2, 1, 1], dtype=np.float32) / 6
    assert vec == pytest.approx(want / np.linalg.norm(want), abs=1e-2)


def test_pooling_one_frame_changes_nothing():
    """A still is a one-frame case of the same code path, and it must come out
    bit-for-bit as the encoder produced it — not through a float32 mean and a
    re-normalize that would round it."""
    v = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float16)
    assert indexer._pool([v]) is v


def test_a_degenerate_pool_stays_zero_rather_than_exploding():
    """Frame vectors that cancel out have no direction. Dividing by ~0 would make
    a garbage unit vector that outranks real answers; a zero vector scores 0
    against every query, which is the honest answer."""
    a = np.array([1, 0, 0, 0], dtype=np.float16)
    pooled = indexer._pool([a, -a])
    assert np.linalg.norm(pooled.astype(np.float32)) == 0.0


def test_frames_are_batched_by_image_rather_than_by_row(cache_dir, tmp_path,
                                                       monkeypatch):
    """A batch is a queue of rows, and a video row is six images. Counting rows
    would have sent sixteen videos to the encoder as ninety-six frames in one
    call — several times the peak a batch of stills costs, on the machine this has
    already been OOM-killed on."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "media"
    root.mkdir()
    for n in range(4):
        write_video(root / f"clip{n}.mp4", seconds=1.0, fps=10, size=32)

    fe = FrameEmbedder()
    indexer.index_once(Store(cache_dir), [str(root)], fe, cache_dir)

    assert fe.images == 4 * indexer.VIDEO_FRAMES
    assert max(fe.batches) <= indexer.EMBED_BATCH_IMAGES + indexer.VIDEO_FRAMES


def test_an_unchanged_video_is_not_decoded_again(cache_dir, tmp_path, monkeypatch):
    """Decoding is the expensive half for a video — a 4K clip is a second or two —
    so the skip that makes re-indexing cheap has to cover it, not just the
    embedding."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "media"
    root.mkdir()
    write_video(root / "clip.mp4", seconds=1.0, fps=10, size=32)

    store = Store(cache_dir)
    fe = FakeEmbedder()
    indexer.index_once(store, [str(root)], fe, cache_dir)

    calls = []
    real = indexer.video.keyframes
    monkeypatch.setattr(indexer.video, "keyframes",
                        lambda *a, **kw: calls.append(a) or real(*a, **kw))
    stats = indexer.index_once(store, [str(root)], fe, cache_dir)

    assert stats["skipped"] == 1 and stats["embedded"] == 0
    assert calls == []


def test_a_moved_video_keeps_its_vector_and_its_thumbnail(cache_dir, tmp_path,
                                                          monkeypatch):
    """Same rule as a moved photograph: the file is identified by its sha1, so
    renaming it is a path change and nothing else. Re-decoding six frames and
    re-embedding them to arrive at the vector we already hold would be the whole
    cost of indexing it, paid for a rename."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "media"
    root.mkdir()
    clip = write_video(root / "clip.mp4", seconds=1.0, fps=10, size=32)

    store = Store(cache_dir)
    fe = FakeEmbedder()
    indexer.index_once(store, [str(root)], fe, cache_dir)
    before = store.get_photo(str(clip))
    thumb = thumb_path(cache_dir, before["sha1"], indexer.THUMB_SIZE)
    assert thumb.exists()

    moved = root / "holiday.mp4"
    clip.rename(moved)
    calls = []
    monkeypatch.setattr(indexer.video, "keyframes",
                        lambda *a, **kw: calls.append(a) or [])
    stats = indexer.index_once(store, [str(root)], fe, cache_dir)

    assert stats["moved"] == 1 and stats["embedded"] == 0
    assert calls == []                        # nothing was decoded again
    assert store.get_photo(str(clip)) is None
    after = store.get_photo(str(moved))
    assert after["sha1"] == before["sha1"] and after["kind"] == "video"
    ids, _ = store.load_embeddings()
    assert [int(i) for i in ids] == [after["id"]]
    assert thumb.exists()                     # named by the hash, not the path


def test_a_video_nothing_can_decode_becomes_an_error_row(cache_dir, tmp_path,
                                                         monkeypatch):
    """Flagged and retried next run, like a corrupt JPEG — and flagged as a
    *video*: a row that claimed to be an image would sit in the photographs'
    scope counts describing a file that is not one."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "media"
    root.mkdir()
    _shoot(root / "a.jpg")
    bad = root / "torn.mov"
    bad.write_bytes(b"not a container at all")

    store = Store(cache_dir)
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats["errors"] == 1 and stats["added"] == 2
    row = store.get_photo(str(bad))
    assert row["error"] and row["kind"] == "video"
    ids, _ = store.load_embeddings()
    assert row["id"] not in [int(i) for i in ids]
    # unreadable, so in no scope's count — the counts promise search results
    assert store.scope_counts() == {"all": 1, "photos": 0, "videos": 0}
    # ...and it is retried rather than skipped for ever
    assert indexer.index_once(store, [str(root)], FakeEmbedder(),
                              cache_dir)["errors"] == 1


def test_a_video_joins_the_trip_it_was_shot_on(cache_dir, tmp_path, monkeypatch):
    """Trips are computed from photographs *and* videos (trips.TRIP_ROWS_WHERE).
    A clip shot in the middle of a trip belonged to no trip at all while the
    computation ran over stills only."""
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (("Ubud", "Bali", "ID") if lat < 0
                                          else ("Mumbai", "MH", "IN")))
    root = tmp_path / "media"
    root.mkdir()

    def shot(name, when, gps):
        p = root / name
        _shoot(p)
        piexif.insert(piexif.dump({
            "Exif": {piexif.ExifIFD.DateTimeOriginal: when.encode()},
            "GPS": {piexif.GPSIFD.GPSLatitudeRef: gps[0],
                    piexif.GPSIFD.GPSLatitude: gps[1],
                    piexif.GPSIFD.GPSLongitudeRef: gps[2],
                    piexif.GPSIFD.GPSLongitude: gps[3]}}), str(p))

    HOME = (b"N", [(19, 1), (4, 1), (0, 1)], b"E", [(72, 1), (52, 1), (0, 1)])
    AWAY = (b"S", [(8, 1), (24, 1), (0, 1)], b"E", [(115, 1), (6, 1), (0, 1)])
    for n in range(3):                      # home
        shot(f"home{n}.jpg", f"2025:06:0{n + 1} 10:00:00", HOME)
    for n in range(3):                      # away, 48h+ later and 4,000km off
        shot(f"away{n}.jpg", f"2025:07:1{n} 10:00:00", AWAY)
    clip = write_video(root / "clip.mp4", seconds=1.0, fps=10, size=32,
                       metadata={"creation_time": "2025-07-11T04:30:00.000000Z"})

    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    trips = store.get_trips()
    assert len(trips) == 1 and trips[0]["place"] == "Ubud"
    assert store.get_photo(str(clip))["trip_id"] == trips[0]["id"]


def test_a_checkpoint_is_paid_for_in_work_rather_than_in_rows(cache_dir, tmp_path,
                                                             monkeypatch):
    """CHECKPOINT_EVERY has always been counted in images, and with videos that
    stops being the same thing as rows: one video row is six frames and a decode,
    tens of times a photograph's cost. Counted in rows, the gap between two saves
    on a library of clips stretches from a minute to half an hour — and this
    daemon has already been killed mid-run once, losing everything since the last
    save."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    monkeypatch.setattr(indexer, "CHECKPOINT_EVERY", 12)
    root = tmp_path / "media"
    root.mkdir()
    for n in range(3):                      # 3 rows, 18 frames
        write_video(root / f"clip{n}.mp4", seconds=1.0, fps=10, size=32)

    store = Store(cache_dir)
    saves = []
    real = store.save_embeddings
    monkeypatch.setattr(store, "save_embeddings",
                        lambda ids, mat: saves.append(len(ids)) or real(ids, mat))
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    # a save mid-run and the authoritative one at the end, not just the end
    assert len(saves) >= 2, saves
    assert saves[-1] == 3


# ── faces ──────────────────────────────────────────────────────────────────
# Identity is colour here: `face_photo` writes bands, FakeFaceModel reads one
# face per band and gives each colour its own vector (see tests/conftest.py).
def _index(store, root, cache_dir, faces_model, **kw):
    return indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir,
                              face_model=faces_model, **kw)


def test_faces_are_found_clustered_and_named_after_the_photographs_are_indexed(
        cache_dir, tmp_path, monkeypatch):
    """The whole stage, end to end: three photographs of one person and three of
    another become two people, and a landscape stays a landscape.

    Three of each because that is the rule (persons.MIN_CLUSTER) — a stranger
    seen once is a face, not a card in the People view."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
        face_photo(root / f"ben{i}.jpg", ["ben"])
    face_photo(root / "beach.jpg", [])              # nobody in it
    store = Store(cache_dir)
    faces_model = FakeFaceModel()

    stats = _index(store, root, cache_dir, faces_model)

    assert stats["face_photos"] == 7 and stats["faces"] == 6
    assert stats["face_errors"] == 0
    assert stats["people"] == {"people": 2, "clustered": 6, "named": 0}
    # the models are loaded once for the run, not once per photograph
    assert faces_model.loads == 1

    counts = store.person_counts()
    assert sorted(counts.values()) == [(3, 1 * 3), (3, 3)]
    ids, mat = store.load_faces()
    assert len(ids) == 6 and mat.shape == (6, faces_model.dim)
    # every face row has a vector and a person, and the landscape has neither
    rows = store.all_faces()
    assert len(rows) == 6 and all(r["cluster_id"] is not None for r in rows)
    assert store.get_photo(str(root / "beach.jpg"))["faces_v"] == faces_model.key


def test_a_second_run_re_detects_nothing_and_still_reclusters(cache_dir, tmp_path,
                                                              monkeypatch):
    """A stamped row is not scanned again — that is what makes the face pass
    incremental — but the people are recomputed every run, because a cluster is a
    fact about all the faces at once."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    store = Store(cache_dir)
    fm = FakeFaceModel()
    _index(store, root, cache_dir, fm)
    before = [(p["id"], p["name"]) for p in store.get_persons()]
    calls = fm.detect_calls

    stats = _index(store, root, cache_dir, fm)
    assert stats["face_photos"] == 0 and fm.detect_calls == calls
    assert stats["people"]["people"] == 1
    assert [(p["id"], p["name"]) for p in store.get_persons()] == before


def test_a_new_face_model_re_detects_the_whole_library(cache_dir, tmp_path,
                                                       monkeypatch):
    """The stamp is the model's key, not a boolean: a different detector
    threshold changes which faces exist and a different network changes what
    "the same person" means, so neither can move without a re-detect."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    store = Store(cache_dir)
    _index(store, root, cache_dir, FakeFaceModel())
    first = {r["id"] for r in store.all_faces()}

    stats = _index(store, root, cache_dir, FakeFaceModel(key="fake-faces-v2"))
    assert stats["face_photos"] == 3 and stats["faces"] == 3
    rows = store.all_faces()
    assert {r["id"] for r in rows} != first          # rows replaced, not added
    ids, _ = store.load_faces()
    # ...and the old vectors went with the old rows: nothing orphaned
    assert sorted(int(i) for i in ids) == sorted(r["id"] for r in rows)


def test_deleting_a_photograph_takes_its_face_vectors_with_it(cache_dir, tmp_path,
                                                              monkeypatch):
    """The rows go with the photo (store.remove_paths); the vectors are the face
    pass's own file, so the ids are read while the link still exists."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    store = Store(cache_dir)
    fm = FakeFaceModel()
    _index(store, root, cache_dir, fm)
    assert len(store.load_faces()[0]) == 3

    (root / "ana0.jpg").unlink()
    _index(store, root, cache_dir, fm)
    ids, mat = store.load_faces()
    assert len(ids) == 2 and mat.shape == (2, fm.dim)
    assert sorted(int(i) for i in ids) == sorted(r["id"] for r in store.all_faces())
    # two sightings are below the cluster minimum, so the person keeps their row
    # (and their name, had there been one) but no longer holds any face
    assert store.person_counts() == {}
    assert [p["id"] for p in store.get_persons()] == [1]


def test_a_group_shot_is_several_faces_in_one_photograph(cache_dir, tmp_path,
                                                         monkeypatch):
    """Both people are found in it, and it counts once for each of them — a
    photograph is in two people's grids, and a person's face count and photo
    count are different numbers."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"both{i}.jpg", ["ana", "ben"])
    store = Store(cache_dir)
    stats = _index(store, root, cache_dir, FakeFaceModel())

    assert stats["faces"] == 6 and stats["face_photos"] == 3
    assert stats["people"]["people"] == 2
    assert sorted(store.person_counts().values()) == [(3, 3), (3, 3)]


def test_a_face_pass_that_raises_costs_that_photograph_and_nothing_else(
        cache_dir, tmp_path, monkeypatch):
    """A photograph whose faces cannot be read is still searchable and still has
    a thumbnail — so it keeps both, is counted, and is left unstamped so the next
    run tries again. It is never flagged as an unreadable *file*."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    store = Store(cache_dir)

    class Flaky(FakeFaceModel):
        seen = 0

        def detect(self, img):
            self.seen += 1
            if self.seen == 2:
                raise RuntimeError("detector fell over")
            return FakeFaceModel.detect(self, img)

    fm = Flaky()
    stats = _index(store, root, cache_dir, fm)
    assert stats["face_errors"] == 1 and stats["face_photos"] == 2
    assert stats["errors"] == 0                      # not a file-level failure
    unstamped = [r for r in store.faces_pending(fm.key)]
    assert len(unstamped) == 1
    assert store.get_photo(unstamped[0]["path"])["error"] is None

    fm2 = FakeFaceModel()
    stats = _index(store, root, cache_dir, fm2)       # the retry
    assert stats["face_photos"] == 1 and stats["face_errors"] == 0
    assert store.faces_pending(fm2.key) == []


def test_a_missing_face_model_does_not_fail_the_index_run(cache_dir, tmp_path,
                                                          monkeypatch):
    """facenet-pytorch is an optional extra. A machine without it indexes and
    searches exactly as before, the run says why once, and no photograph is
    marked as anything."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    face_photo(root / "ana.jpg", ["ana"])
    store = Store(cache_dir)

    class NoModel(FakeFaceModel):
        def load(self):
            raise RuntimeError(indexer.faces.INSTALL_HINT)

    stats = _index(store, root, cache_dir, NoModel())
    assert stats["added"] == 1 and stats["embedded"] == 1
    assert "facenet-pytorch" in stats["faces_error"]
    assert stats["face_photos"] == 0 and stats["face_errors"] == 0
    assert store.all_faces() == []
    assert store.get_photo(str(root / "ana.jpg"))["faces_v"] is None


def test_the_face_pass_reports_its_own_progress_stage(cache_dir, tmp_path,
                                                      monkeypatch):
    """A run is two sweeps over the library now, and a bar that fills, resets and
    fills again reads as a bug unless it can say which sweep it is watching."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    store = Store(cache_dir)
    seen = []
    _index(store, root, cache_dir, FakeFaceModel(),
           progress=lambda done, total, stage: seen.append((done, total, stage)))

    stages = [s for _, _, s in seen]
    assert indexer.STAGE_INDEX in stages and indexer.STAGE_FACES in stages
    # the faces sweep comes second: the library is searchable before it starts
    assert stages.index(indexer.STAGE_FACES) > stages.index(indexer.STAGE_INDEX)
    assert (3, 3, indexer.STAGE_FACES) in seen


def test_face_vectors_are_checkpointed_during_a_long_pass(cache_dir, tmp_path,
                                                          monkeypatch):
    """Same protection the embeddings have: a first face pass over a real library
    is minutes of work, and a kill must not discard all of it. The row and the
    vector are written per photograph, so a checkpoint is the only moment they
    are both on disk."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    monkeypatch.setattr(indexer, "FACE_CHECKPOINT_EVERY", 4)
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(9):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    store = Store(cache_dir)

    seen = {}

    def progress(done, total, stage):
        if stage == indexer.STAGE_FACES and done == 4:
            seen["n"] = len(store.load_faces()[0])

    _index(store, root, cache_dir, FakeFaceModel(), progress=progress)
    assert seen.get("n") == 4, "no checkpoint landed after the 4th photograph"
    assert len(store.load_faces()[0]) == 9


def test_a_video_gets_its_faces_from_the_keyframe_it_was_thumbnailed_from(
        cache_dir, tmp_path, monkeypatch):
    """One source for both kinds: the 512px thumbnail. For a video that is the
    middle keyframe the index already decoded, so the face pass never seeks into
    a 4K clip to find a face it can see perfectly well at 512px."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    # a clip whose every frame is one person's colour
    colour = [(200, 40, 40)]
    for i in range(3):
        write_video(root / f"clip{i}.mp4", seconds=1.0, fps=8, size=64,
                    bands=colour)
    store = Store(cache_dir)
    stats = _index(store, root, cache_dir, FakeFaceModel())

    assert stats["face_photos"] == 3 and stats["faces"] == 3
    assert stats["people"]["people"] == 1
    rows = store.all_faces()
    kinds = {store.get_photo_by_id(r["photo_id"])["kind"] for r in rows}
    assert kinds == {"video"}


def test_a_name_the_photos_library_already_holds_is_taken_after_three_agreements(
        cache_dir, tmp_path, monkeypatch):
    """Apple Photos knows who is in a photograph. That is evidence, not
    instruction: it is taken only from photographs with exactly one face on them
    and only once the cluster has agreed with itself three times — a group shot's
    five names against five faces is a permutation nobody should guess."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(3):
        face_photo(root / f"ana{i}.jpg", ["ana"])
    face_photo(root / "group.jpg", ["ben", "cleo"])
    store = Store(cache_dir)
    fm = FakeFaceModel()
    _index(store, root, cache_dir, fm)

    # …as apple_photos.merge would have left them
    for i in range(3):
        row = store.get_photo(str(root / f"ana{i}.jpg"))
        row["source"] = "apple"
        row["raw_exif"] = '{"_apple": {"persons": ["Ana Costa"]}}'
        store.upsert_photo(row)
    group = store.get_photo(str(root / "group.jpg"))
    group["source"] = "apple"
    group["raw_exif"] = '{"_apple": {"persons": ["Ben", "Cleo"]}}'
    store.upsert_photo(group)

    indexer.recluster(store)
    named = {p["name"] for p in store.get_persons() if p["name"]}
    assert named == {"Ana Costa"}                    # the group shot named nobody

    # and a name the user typed is never overwritten by a later run
    ana = next(p for p in store.get_persons() if p["name"] == "Ana Costa")
    store.set_person_name(ana["id"], "Ana")
    indexer.recluster(store)
    assert store.get_persons()[0]["name"] == "Ana"


# ── run metrics + memory guard ──────────────────────────────────────────────

def _seq_footprint(*values):
    """A footprint_fn that returns each of `values` in order, then keeps
    returning the last one — so a test can name exactly which flush sees
    which reading without having to count how many flushes the run
    actually makes."""
    it = iter(values)
    last = [values[-1]]

    def fn():
        try:
            v = next(it)
        except StopIteration:
            return last[0]
        last[0] = v
        return v
    return fn


def test_run_metrics_are_reported_and_appended_to_history(cache_dir, tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(5):
        _shoot(root / f"p{i}.jpg")
    store = Store(cache_dir)

    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)

    assert stats["duration_s"] >= 0
    assert set(stats["stages"]) == {"walk_s", "metadata_s", "thumbs_s",
                                    "embed_s", "faces_s", "trips_s", "apple_s"}
    assert all(v >= 0 for v in stats["stages"].values())
    assert stats["rate"] >= 0
    assert stats["mem_peak_gb"] > 0     # this very process is using *some* RAM
    assert "error" not in stats

    lines = (cache_dir / "runs.jsonl").read_text().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["duration_s"] == stats["duration_s"]
    assert line["error"] is None

    # a second run appends, it does not replace
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert len((cache_dir / "runs.jsonl").read_text().splitlines()) == 2


def test_run_history_trims_to_the_configured_max(cache_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    monkeypatch.setattr(indexer, "RUNS_HISTORY_MAX", 3)
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    store = Store(cache_dir)
    fe = FakeEmbedder()
    for _ in range(5):
        indexer.index_once(store, [str(root)], fe, cache_dir)
    lines = (cache_dir / "runs.jsonl").read_text().splitlines()
    assert len(lines) == 3


def test_soft_breach_checkpoints_releases_and_the_run_keeps_going(
        cache_dir, tmp_path, monkeypatch, capsys):
    """A breach that clears on its own (the checkpoint + release actually
    freed enough) must not abort anything — "soft" means "handled", and a run
    that stops on every momentary spike would never finish a real library."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    monkeypatch.setattr(indexer, "EMBED_BATCH_IMAGES", 1)   # one flush/photo
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(4):
        _shoot(root / f"p{i}.jpg")
    store = Store(cache_dir)

    guard = memguard.MemGuard(8, footprint_fn=_seq_footprint(
        1.0, 20.0, 1.0, 1.0))                    # breach only on flush #2
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir,
                               mem_guard=guard)

    assert "error" not in stats
    assert stats["embedded"] == 4
    assert stats["mem_peak_gb"] == pytest.approx(20.0, abs=0.5)
    ids, mat = store.load_embeddings()
    assert len(ids) == 4

    out = capsys.readouterr().out
    assert "memory guard" in out and "checkpointed and released" in out


def test_hard_breach_aborts_the_run_and_the_next_run_backfills(
        cache_dir, tmp_path, monkeypatch, capsys):
    """Still over the limit right after the checkpoint-and-release that a
    soft breach already tried: freeing did not help, so the run stops on
    purpose rather than waiting for the OS to do it with an OOM kill."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    monkeypatch.setattr(indexer, "EMBED_BATCH_IMAGES", 1)
    root = tmp_path / "photos"
    root.mkdir()
    for i in range(4):
        _shoot(root / f"p{i}.jpg")
    store = Store(cache_dir)

    guard = memguard.MemGuard(8, footprint_fn=_seq_footprint(1.0, 20.0, 20.0))
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir,
                               mem_guard=guard)

    assert "memory limit hit" in stats["error"]
    assert "aborted safely" in stats["error"]
    assert "max_index_memory_gb" in stats["error"]
    assert stats["embedded"] == 3                 # p0, p1, p2 — not p3
    assert store.get_photo(str(root / "p3.jpg")) is None
    ids, mat = store.load_embeddings()
    assert len(ids) == 3                           # every embedded one saved

    out = capsys.readouterr().out
    assert "memory limit hit" in out

    lines = (cache_dir / "runs.jsonl").read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["error"] and "memory limit hit" in last["error"]

    # the daemon stays alive — a plain reindex, no special handling required,
    # picks up exactly the file the aborted run never reached
    stats2 = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert "error" not in stats2
    assert stats2["added"] == 1
    assert store.get_photo(str(root / "p3.jpg")) is not None


def test_a_disabled_limit_never_breaches(cache_dir, tmp_path, monkeypatch):
    """`max_index_memory_gb: 0` (or any non-positive value) is "no limit" —
    some machines have no ceiling worth enforcing, and a guard that can
    never be satisfied is not a safe default for that case."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    store = Store(cache_dir)
    guard = memguard.MemGuard(0, footprint_fn=lambda: 999.0)
    stats = indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir,
                               mem_guard=guard)
    assert "error" not in stats


def test_memory_limit_is_read_from_config_when_no_guard_is_passed(
        cache_dir, tmp_path, monkeypatch):
    """The panel writes `max_index_memory_gb` through /config; a run that
    starts without an explicit `mem_guard=` (every real caller) must read
    that setting fresh rather than trusting a value baked in at daemon
    startup."""
    from lens import config
    monkeypatch.setattr(metadata, "geocode", lambda a, b: (None, None, None))
    cfg = config.load_config(cache_dir)
    cfg["max_index_memory_gb"] = 4321
    config.save_config(cfg, cache_dir)

    seen = {}
    real_init = memguard.MemGuard.__init__

    def spy_init(self, limit_gb, **kw):
        seen["limit_gb"] = limit_gb
        real_init(self, limit_gb, **kw)
    monkeypatch.setattr(memguard.MemGuard, "__init__", spy_init)

    root = tmp_path / "photos"
    root.mkdir()
    _shoot(root / "a.jpg")
    store = Store(cache_dir)
    indexer.index_once(store, [str(root)], FakeEmbedder(), cache_dir)
    assert seen["limit_gb"] == 4321
