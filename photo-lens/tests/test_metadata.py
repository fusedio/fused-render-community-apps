import json
from datetime import datetime
from fractions import Fraction

import piexif
import pytest
from PIL import Image

from lens import metadata
from tests.conftest import write_video


def _make_jpeg(path, gps=True):
    Image.new("RGB", (64, 32), "red").save(path, "JPEG")
    exif = {
        "0th": {piexif.ImageIFD.Make: b"Apple",
                piexif.ImageIFD.Model: b"iPhone 15 Pro"},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: b"2025:07:01 10:00:00",
            piexif.ExifIFD.ISOSpeedRatings: 100,
            piexif.ExifIFD.FNumber: (18, 10),
            piexif.ExifIFD.ExposureTime: (1, 120),
            piexif.ExifIFD.FocalLength: (68, 10),
            piexif.ExifIFD.LensModel: b"Main Camera",
        },
        "GPS": {},
    }
    if gps:
        exif["GPS"] = {
            piexif.GPSIFD.GPSLatitudeRef: b"S",
            piexif.GPSIFD.GPSLatitude: [(8, 1), (24, 1), (0, 1)],
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: [(115, 1), (6, 1), (0, 1)],
        }
    piexif.insert(piexif.dump(exif), str(path))


def test_extract_full(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda lat, lon: ("Ubud", "Bali", "ID"))
    p = tmp_path / "a.jpg"
    _make_jpeg(p)
    rec = metadata.extract(str(p))
    assert rec["path"] == str(p)
    assert rec["width"] == 64 and rec["height"] == 32 and rec["format"] == "JPEG"
    assert rec["taken_at"] == "2025-07-01T10:00:00"
    assert abs(rec["lat"] - (-8.4)) < 1e-6 and abs(rec["lon"] - 115.1) < 1e-6
    assert rec["place_city"] == "Ubud"
    assert rec["place_region"] == "Bali"
    assert rec["place_country"] == "ID"
    assert rec["camera"] == "Apple iPhone 15 Pro"
    assert rec["lens"] == "Main Camera"
    assert rec["iso"] == 100 and rec["f_number"] == 1.8
    assert rec["exposure"] == "1/120" and rec["focal_length"] == 6.8
    raw = json.loads(rec["raw_exif"])
    assert raw["Make"] == "Apple"            # full dump keeps everything
    assert "GPSLatitude" in raw


def test_extract_no_exif(tmp_path):
    p = tmp_path / "shot.png"
    Image.new("RGB", (10, 10)).save(p, "PNG")
    rec = metadata.extract(str(p))
    assert rec["taken_at"] is not None       # falls back to file mtime
    assert rec["lat"] is None and rec["place_city"] is None
    assert rec["place_region"] is None
    assert rec["camera"] is None


def test_hostile_exif_does_not_fail_the_row(tmp_path, monkeypatch):
    """A tag that trips one converter must cost only that column.

    Before per-field isolation each of these was a hard extract() failure —
    ISOSpeedRatings as a pair (int() on a list), and `0/0` GPS rationals that
    Pillow decodes to NaN and reverse_geocoder then rejects. The photo became
    an error row: never searchable, retried on every index run, forever."""
    def boom(lat, lon):                       # must not even be reached
        raise AssertionError(f"geocode called with {lat}, {lon}")

    monkeypatch.setattr(metadata, "geocode", boom)
    p = tmp_path / "hostile.jpg"
    Image.new("RGB", (20, 20), "red").save(p, "JPEG")
    piexif.insert(piexif.dump({
        "0th": {piexif.ImageIFD.Make: b"Weird"},
        "Exif": {piexif.ExifIFD.ISOSpeedRatings: (100, 100)},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: [(0, 0), (0, 0), (0, 0)],
            piexif.GPSIFD.GPSLongitudeRef: b"E",
            piexif.GPSIFD.GPSLongitude: [(0, 0), (0, 0), (0, 0)],
        },
    }), str(p))

    rec = metadata.extract(str(p))

    assert rec["iso"] == 100                  # first element of the pair
    assert rec["lat"] is None and rec["lon"] is None
    assert rec["place_city"] is None and rec["place_region"] is None
    assert rec["camera"] == "Weird"           # unaffected fields still promoted
    raw = json.loads(rec["raw_exif"])         # raw dump keeps the odd tags
    assert raw["ISOSpeedRatings"] == [100, 100]
    assert "GPSLatitude" in raw


def test_short_gps_coordinate_is_tolerated(tmp_path, monkeypatch):
    """Two components instead of (deg, min, sec): treat the missing seconds as
    zero rather than raising on the unpack."""
    monkeypatch.setattr(metadata, "geocode", lambda lat, lon: ("Ubud", "Bali", "ID"))
    p = tmp_path / "short.jpg"
    Image.new("RGB", (20, 20), "red").save(p, "JPEG")
    piexif.insert(piexif.dump({"GPS": {
        piexif.GPSIFD.GPSLatitudeRef: b"S",
        piexif.GPSIFD.GPSLatitude: [(8, 1), (24, 1)],
        piexif.GPSIFD.GPSLongitudeRef: b"E",
        piexif.GPSIFD.GPSLongitude: [(115, 1), (6, 1)],
    }}), str(p))

    rec = metadata.extract(str(p))
    assert abs(rec["lat"] - (-8.4)) < 1e-6
    assert abs(rec["lon"] - 115.1) < 1e-6
    assert rec["place_region"] == "Bali"


def test_geocode_failure_keeps_coordinates(tmp_path, monkeypatch):
    """The lookup is a nicety; the coordinates are data."""
    monkeypatch.setattr(metadata, "geocode",
                        lambda lat, lon: (_ for _ in ()).throw(ValueError("nope")))
    p = tmp_path / "gps.jpg"
    _make_jpeg(p)
    rec = metadata.extract(str(p))
    assert rec["lat"] is not None and rec["lon"] is not None
    assert rec["place_city"] is None and rec["place_country"] is None
    assert rec["iso"] == 100                  # the rest of the row survives


def test_nonfinite_rationals_stay_json_parseable(tmp_path, monkeypatch):
    """NaN is not JSON. raw_exif is handed to a browser, whose JSON.parse
    rejects it outright, so a 0/0 rational is stored as null."""
    monkeypatch.setattr(metadata, "geocode", lambda lat, lon: (None, None, None))
    p = tmp_path / "nan.jpg"
    Image.new("RGB", (20, 20), "red").save(p, "JPEG")
    piexif.insert(piexif.dump({
        "Exif": {piexif.ExifIFD.FNumber: (18, 0)},
    }), str(p))
    rec = metadata.extract(str(p))
    assert "NaN" not in rec["raw_exif"]
    assert json.loads(rec["raw_exif"], parse_constant=_reject)["FNumber"] is None
    assert rec["f_number"] is None


def _reject(name):
    raise AssertionError(f"non-JSON constant in raw_exif: {name}")


# ── is_photo: camera capture vs. software artwork ──────────────────────────
def test_is_photo_camera_or_gps_is_conclusive():
    assert metadata.is_photo({"camera": "Apple iPhone 15 Pro"}, {}) is True
    # a PNG a camera identified itself in is still a photo, whatever the format
    assert metadata.is_photo({"camera": "Sony A7IV", "format": "PNG"}, {}) is True
    assert metadata.is_photo({"lat": -8.4, "format": "PNG"}, {}) is True
    # 0.0 is the equator and the prime meridian, not a missing value
    assert metadata.is_photo({"lat": 0.0}, {}) is True


def test_is_photo_capture_format_plus_capture_timestamp():
    raw = {"DateTimeOriginal": "2025:07:01 10:00:00"}
    assert metadata.is_photo({"format": "JPEG"}, raw) is True
    assert metadata.is_photo({"format": "HEIF"}, raw) is True
    assert metadata.is_photo({"format": "jpeg"}, raw) is True     # case-insensitive
    # a capture format with no capture timestamp is an export, not a photo
    assert metadata.is_photo({"format": "JPEG"}, {}) is False
    # ...and a capture timestamp on a format no camera writes is metadata a
    # tool copied along, not evidence of a camera
    assert metadata.is_photo({"format": "PNG"}, raw) is False
    assert metadata.is_photo({"format": "WEBP"}, raw) is False


def test_is_photo_ignores_a_bare_file_modification_date():
    """`DateTime` is what an editor or screenshot tool sets on save. Accepting
    it would readmit the whole asset folder."""
    assert metadata.is_photo({"format": "JPEG"},
                             {"DateTime": "2025:07:01 10:00:00"}) is False


def test_is_photo_survives_a_row_with_nothing_in_it():
    assert metadata.is_photo({}, {}) is False
    assert metadata.is_photo({"camera": None, "lat": None, "format": None},
                             {}) is False


def test_extract_flags_a_camera_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "geocode", lambda lat, lon: ("Ubud", "Bali", "ID"))
    p = tmp_path / "a.jpg"
    _make_jpeg(p)
    assert metadata.extract(str(p))["is_photo"] == 1


def test_extract_flags_a_transparent_png_as_not_a_photo(tmp_path):
    """The case that motivated the column: 567 video-overlay frames outranking
    every real photograph in the user's library."""
    p = tmp_path / "stepper_0001.png"
    Image.new("RGBA", (1920, 1080), (255, 0, 0, 0)).save(p, "PNG")
    rec = metadata.extract(str(p))
    assert rec["is_photo"] == 0
    assert rec["taken_at"] is not None       # still catalogued and still dated


# ── videos ───────────────────────────────────────────────────────────────
def test_kind_is_decided_by_the_extension(tmp_path):
    assert metadata.kind_for("/x/a.jpg") == "image"
    assert metadata.kind_for("/x/a.MOV") == "video"
    assert metadata.kind_for("/x/a.mp4") == "video"
    # not a video lens indexes: a downloaded film is hours long and tens of
    # gigabytes, and is nobody's memories
    assert metadata.kind_for("/x/film.mkv") == "image"


def test_the_walker_collects_both_kinds_and_they_never_overlap():
    assert metadata.VIDEO_EXTS <= metadata.MEDIA_EXTS
    assert metadata.IMAGE_EXTS <= metadata.MEDIA_EXTS
    assert not (metadata.VIDEO_EXTS & metadata.IMAGE_EXTS)


def test_a_video_row_is_read_from_its_container(tmp_path):
    """One record shape for both kinds, so nothing downstream has two cases: the
    columns a container cannot answer (camera, exposure) are None rather than
    absent, and the ones it can are filled from the headers."""
    p = write_video(tmp_path / "clip.mp4", seconds=2.0, fps=10, size=32,
                    metadata={"creation_time": "2025-07-01T10:00:00.000000Z"})
    rec = metadata.extract(str(p))

    assert rec["kind"] == "video"
    assert rec["format"] == "MP4"
    assert (rec["width"], rec["height"]) == (32, 32)
    assert rec["duration_s"] == pytest.approx(2.0, abs=0.15)
    assert rec["taken_at"] == (
        datetime.fromisoformat("2025-07-01T10:00:00+00:00")
        .astimezone().replace(tzinfo=None).isoformat())
    assert rec["camera"] is None and rec["exposure"] is None
    assert rec["size"] == p.stat().st_size


def test_a_video_is_never_a_photograph(tmp_path, monkeypatch):
    """`is_photo` means "a camera took this still", and every scope, count and
    trip rule in lens reads it that way. A GPS fix is conclusive evidence of a
    photograph for an *image* — for a video it is just a location."""
    monkeypatch.setattr(metadata, "geocode", lambda a, b: ("Ubud", "Bali", "ID"))
    p = write_video(tmp_path / "clip.mp4", metadata={"location": "-8.4+115.1/"})
    rec = metadata.extract(str(p))
    assert rec["is_photo"] == 0
    assert rec["lat"] == pytest.approx(-8.4) and rec["lon"] == pytest.approx(115.1)
    assert rec["place_city"] == "Ubud" and rec["place_region"] == "Bali"


def test_the_whole_probe_is_kept_where_the_details_panel_can_read_it(tmp_path):
    """A movie container has no EXIF IFDs to dump, so `raw_exif` carries the probe
    instead — under `_video`, the same way apple_photos parks the facts that have
    no column. One /meta request still answers everything the panel shows."""
    p = write_video(tmp_path / "clip.mp4", seconds=1.0, fps=10)
    raw = json.loads(metadata.extract(str(p))["raw_exif"])
    assert set(raw) == {"_video"}
    assert raw["_video"]["codec"] == "h264"
    assert raw["_video"]["fps"] == pytest.approx(10.0)
    assert raw["_video"]["container"] == "MP4"


def test_a_video_with_no_container_date_falls_back_to_the_file(tmp_path):
    """Same rule as a screenshot with no EXIF: mtime is a worse answer than a
    capture time and a better one than no date at all — a row with no `taken_at`
    is in no month, no date filter and no trip."""
    p = write_video(tmp_path / "clip.webm", codec="libvpx", seconds=0.5, fps=8)
    rec = metadata.extract(str(p))
    assert rec["taken_at"] == datetime.fromtimestamp(
        p.stat().st_mtime).isoformat()


def test_a_corrupt_video_raises_like_a_corrupt_image(tmp_path):
    """Which is what the indexer turns into an error row (and retries next run),
    rather than cataloguing a file it could not read as a video with no
    dimensions and no date."""
    bad = tmp_path / "clip.mov"
    bad.write_bytes(b"not a container")
    with pytest.raises(Exception):
        metadata.extract(str(bad))
