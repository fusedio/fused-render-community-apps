import json
import math
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from PIL import ExifTags, Image

from lens import video

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
              ".avif", ".heic", ".heif", ".dng"}

# Videos lens indexes. Deliberately the four containers a phone, a screen
# recorder or a browser actually produces — and deliberately not `.mkv` or
# `.avi`, which on a real machine are downloaded films: hours long, tens of
# gigabytes, and not anybody's memories. A separate set from IMAGE_EXTS because
# almost everything downstream has to know which of the two it is holding.
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

# What the walker collects. Both kinds, one scan (see indexer.scan_roots).
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Formats a camera writes. A screenshot, an app icon, a chart export or a video
# overlay frame is a PNG/GIF/WEBP/BMP; nobody's camera produces one.
_PHOTO_FORMATS = {"JPEG", "JPEG2000", "MPO", "HEIF", "HEIC", "AVIF", "DNG"}

# Tags only a capture device writes. `DateTime` is deliberately absent: editors
# and screenshot tools set it on files they merely saved.
_PHOTO_DATE_TAGS = ("DateTimeOriginal", "DateTimeDigitized")


def kind_for(path) -> str:
    """"video" or "image", off the extension alone.

    The catalog's `kind` column, decided in one place because four modules ask
    the question: the extractor picks which reader to use, the thumbnailer which
    renderer, the query builder which scope a row belongs to, and the error path
    still has to label a row for a file nothing could open."""
    return "video" if Path(path).suffix.lower() in VIDEO_EXTS else "image"


def is_photo(rec: dict, raw: dict) -> bool:
    """Was this file taken by a camera, as opposed to made by software?

    A real library is mostly not photographs. Indexing a home folder pulls in
    hundreds of PNG assets, screenshots and rendered frames, and they crowd out
    the actual pictures in every semantic search — so lens keeps them (they are
    still findable) but separates them, and searches photos by default.

    Camera identification and GPS are conclusive on their own. Failing those,
    a capture format carrying a capture timestamp is the signal that remains
    for a photo whose EXIF has been partly stripped (many sharing pipelines
    remove Make/Model but keep DateTimeOriginal)."""
    if rec.get("camera") or rec.get("lat") is not None:
        return True
    fmt = str(rec.get("format") or "").upper()
    return fmt in _PHOTO_FORMATS and any(raw.get(t) for t in _PHOTO_DATE_TAGS)

_HEIF_REGISTERED = False


def _ensure_heif():
    global _HEIF_REGISTERED
    if not _HEIF_REGISTERED:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _HEIF_REGISTERED = True


_RG = None


def geocode(lat: float, lon: float):
    """Offline reverse geocode. Returns (city, region, country_code).

    `region` is the GeoNames admin1 division — the level people actually name
    when they talk about a trip ("Bali", "Tuscany", "Bavaria"). Without it a
    search for "bali" finds nothing, because the nearest populated place to a
    Bali coordinate may be a hamlet nobody has heard of."""
    global _RG
    if _RG is None:
        import reverse_geocoder
        _RG = reverse_geocoder
    hit = _RG.search([(lat, lon)], mode=1)[0]
    return hit.get("name"), hit.get("admin1") or None, hit.get("cc")


def _num(x: float):
    """NaN and infinity have no JSON representation, and a `0/0` EXIF
    rational — which Pillow decodes to NaN — is a non-value anyway. Store it
    as one so raw_exif stays parseable by a browser's JSON.parse()."""
    return x if math.isfinite(x) else None


def _jsonable(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace").strip("\x00")
    if isinstance(v, Fraction):
        return _num(float(v))
    if isinstance(v, (tuple, list)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if v is None or isinstance(v, (int, str)):        # bool is an int
        return v
    if isinstance(v, float):
        return _num(v)
    try:
        return _num(float(v))    # IFDRational and friends
    except Exception:
        return str(v)


def _dump_exif(img) -> dict:
    """Every tag from the base IFD + Exif/GPS sub-IFDs, by name."""
    out = {}
    exif = img.getexif()
    for tag, val in exif.items():
        out[ExifTags.TAGS.get(tag, str(tag))] = _jsonable(val)
    for ifd_id, tag_names in ((ExifTags.IFD.Exif, ExifTags.TAGS),
                              (ExifTags.IFD.GPSInfo, ExifTags.GPSTAGS)):
        try:
            ifd = exif.get_ifd(ifd_id)
        except Exception:
            continue
        for tag, val in ifd.items():
            out[tag_names.get(tag, str(tag))] = _jsonable(val)
    return out


def _gps_deg(coord, ref):
    """(degrees, minutes, seconds) → signed decimal degrees.

    Real-world files break the spec in every direction: two components
    instead of three, a bare scalar, `0/0` rationals that decode to NaN.
    Take whatever is there, treat the missing tail as zero, and return None
    rather than a non-finite number the caller would have to re-check."""
    if coord is None or coord == "" or coord == []:
        return None
    if not isinstance(coord, (list, tuple)):
        coord = [coord]
    parts = []
    for x in list(coord)[:3]:
        try:
            parts.append(float(x))
        except (TypeError, ValueError, ZeroDivisionError):
            parts.append(float("nan"))
    parts += [0.0] * (3 - len(parts))
    d, m, s = parts
    deg = d + m / 60 + s / 3600
    if not math.isfinite(deg):
        return None
    return -deg if ref in ("S", "W") else deg


def _text(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = " ".join(str(x) for x in v if x is not None)
    return str(v).strip() or None


def _scalar(v):
    """First element of a tag some cameras write as a list where the spec
    says scalar (ISOSpeedRatings is a SHORT, but tuples turn up in the wild
    and int() on a list is a TypeError)."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return v


def _as_float(v):
    v = _scalar(v)
    if v is None or isinstance(v, bool):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _as_int(v):
    f = _as_float(v)
    return None if f is None else int(f)


def _rounded(v, digits):
    f = _as_float(v)
    return None if f is None else round(f, digits)


def _exposure(v):
    ex = _as_float(v)
    if ex is None:
        return None
    return f"1/{round(1 / ex)}" if 0 < ex < 1 else str(ex)


def _camera(raw):
    make, model = _text(raw.get("Make")), _text(raw.get("Model"))
    return " ".join(x for x in (make, model) if x).strip() or None


def _promote(rec, field, fn):
    """Derive one indexed column from raw EXIF, in isolation.

    Without this a single unreadable tag costs the whole photo: extract()
    raises, the indexer stores an error row, and the file is invisible to
    search *and* retried on every run, forever. Dropping just the column it
    could not parse loses nothing that matters — raw_exif still has the
    original tag, byte for byte."""
    try:
        rec[field] = fn()
    except Exception:
        rec[field] = None


def _parse_dt(raw):
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").isoformat()
    except (TypeError, ValueError):
        return None


def _blank(p, st, kind: str) -> dict:
    """The columns every row has, empty, plus the four the filesystem answers.

    Shared by both readers so a video row and a photo row are the same shape —
    the indexer, the upsert and the details panel all handle one record type,
    and a column one reader never fills is None rather than absent."""
    rec = {c: None for c in (
        "sha1", "width", "height", "format", "taken_at", "lat", "lon",
        "place_city", "place_region", "place_country", "camera", "lens", "iso",
        "f_number", "exposure", "focal_length", "duration_s")}
    rec.update(path=str(p), size=st.st_size, mtime=st.st_mtime, raw_exif="{}",
               kind=kind)
    return rec


def _place_from(rec: dict, lat: float, lon: float):
    """Record a coordinate and the names it geocodes to.

    A failed lookup loses the place names, not the coordinates.
    reverse_geocoder raises on non-finite input (hence the isfinite gates in the
    callers), but a bogus-yet-finite coordinate can still make it throw."""
    rec["lat"], rec["lon"] = lat, lon
    try:
        city, region, country = geocode(lat, lon)
    except Exception:
        return
    rec["place_city"] = _text(city)
    rec["place_region"] = _text(region)
    rec["place_country"] = _text(country)


def extract(path: str) -> dict:
    """One catalog row for one file on disk, whichever kind it is.

    Raises for a file its reader cannot open — that is the signal the indexer
    turns into an error row, and it must not be swallowed here: a "successful"
    extract of a corrupt file would be catalogued as a photograph with no
    dimensions and no date."""
    return (_extract_video(Path(path)) if kind_for(path) == "video"
            else _extract_image(Path(path)))


def _extract_video(p: Path) -> dict:
    """A video row, read from the container's own headers (see lens/video.py).

    No EXIF: a movie container has no IFDs to dump, so the whole probe goes into
    `raw_exif` under `_video` — the same trick apple_photos uses for the facts
    that have no column, and it keeps `/meta` a single request for everything
    the details panel shows.

    `is_photo` is 0, always. It means "a camera took this still", and every
    scope, count and trip rule in lens reads it that way; a video is separated by
    `kind` instead (see store.scope_counts, query.build_where)."""
    st = p.stat()
    info = video.probe(str(p))
    rec = _blank(p, st, "video")
    rec.update(width=info["width"], height=info["height"],
               format=info["container"], duration_s=info["duration_s"],
               taken_at=(info["creation_time"]
                         or datetime.fromtimestamp(st.st_mtime).isoformat()))
    lat, lon = info.get("lat"), info.get("lon")
    if lat is not None and lon is not None:
        _place_from(rec, lat, lon)
    try:
        rec["raw_exif"] = json.dumps({"_video": info}, ensure_ascii=False)
    except (TypeError, ValueError):
        rec["raw_exif"] = "{}"
    rec["is_photo"] = 0
    return rec


def _extract_image(p: Path) -> dict:
    _ensure_heif()
    st = p.stat()
    rec = _blank(p, st, "image")

    # Only the image itself is load-bearing: a file Pillow cannot open is
    # genuinely broken and *should* become an error row. Everything derived
    # from EXIF below is optional and fails per field.
    with Image.open(p) as img:
        rec["width"], rec["height"], rec["format"] = img.width, img.height, img.format
        try:
            raw = _dump_exif(img)
        except Exception:
            raw = {}
    try:
        rec["raw_exif"] = json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        rec["raw_exif"] = "{}"

    rec["taken_at"] = (_parse_dt(raw.get("DateTimeOriginal"))
                       or _parse_dt(raw.get("DateTime"))
                       or datetime.fromtimestamp(st.st_mtime).isoformat())

    try:
        lat = _gps_deg(raw.get("GPSLatitude"), raw.get("GPSLatitudeRef"))
        lon = _gps_deg(raw.get("GPSLongitude"), raw.get("GPSLongitudeRef"))
    except Exception:
        lat = lon = None
    if lat is not None and lon is not None:
        _place_from(rec, lat, lon)

    _promote(rec, "camera", lambda: _camera(raw))
    _promote(rec, "lens", lambda: _text(raw.get("LensModel")))
    _promote(rec, "iso", lambda: _as_int(raw.get("ISOSpeedRatings")))
    _promote(rec, "f_number", lambda: _rounded(raw.get("FNumber"), 2))
    _promote(rec, "exposure", lambda: _exposure(raw.get("ExposureTime")))
    _promote(rec, "focal_length", lambda: _rounded(raw.get("FocalLength"), 2))
    # derived last: it reads the columns promoted above
    rec["is_photo"] = 1 if is_photo(rec, raw) else 0
    return rec
