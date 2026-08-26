import hashlib
import os
from pathlib import Path

from PIL import Image, ImageOps

from lens import video
from lens.faces import crop_face
from lens.metadata import _ensure_heif, kind_for

# Bumped whenever the pixels this module produces change, because the cached
# file name carries it: a bump makes every thumb miss the cache and regenerate
# instead of serving output made by the old code. It is also what the indexer
# keys re-embedding on (see indexer.index_once) — the image encoder only ever
# sees thumbs, so a different thumb is a different vector.
#   v2: transparency composites onto white instead of black.
THUMB_VERSION = 2

# The size the indexer renders for every photo, and so the only size anything
# else may assume exists on disk (the lightbox's 2048 is generated on demand).
THUMB_SIZE_DEFAULT = 512


def thumb_path(cache, sha1: str, size: int = THUMB_SIZE_DEFAULT) -> Path:
    """Where this photo's thumbnail lives — the one spelling of the name.

    Named here rather than rebuilt by each caller because the version suffix is
    part of it: an audit that looked for `{sha1}-512.webp` would report every
    thumbnail in the cache as missing the moment THUMB_VERSION moved."""
    return Path(cache) / "thumbs" / f"{sha1}-{size}-v{THUMB_VERSION}.webp"


def _flatten(img):
    """`img` as RGB, with any transparency composited onto white.

    A plain `.convert("RGB")` drops the alpha channel and leaves whatever was
    underneath it — which Pillow initialises to black. Every transparent PNG
    (icons, logos, video overlay frames) therefore became a black rectangle:
    black in the grid, and black in the vector, since the embedder is fed the
    thumb. White is the correct backdrop: it is what these images are drawn to
    sit on, and it keeps the subject visible."""
    transparent = (img.mode in ("RGBA", "LA", "PA")
                   or (img.mode in ("P", "L", "I", "1")
                       and "transparency" in img.info))
    if not transparent:
        return img.convert("RGB")
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, img).convert("RGB")


def _retire_old_versions(out_dir: Path, out: Path, sha1: str, size: int):
    """Delete this thumb's earlier renderings, as we pass them.

    Otherwise every version bump leaves its whole predecessor on disk (13MB for
    the reference library) that nothing will ever read again. Globbed rather
    than named, so v2→v3 cleans up v2 without anyone remembering to add it —
    and the unversioned name, which is what v1 wrote."""
    for old in (*out_dir.glob(f"{sha1}-{size}-v*.webp"),
                out_dir / f"{sha1}-{size}.webp"):
        if old != out:
            old.unlink(missing_ok=True)


def _target(cache, sha1: str, size: int) -> Path:
    """Where this thumb belongs, with its directory in place.

    The mkdir happens before anything is decoded, not after: the cache directory
    existing is what every reader (and the audit) tests the cache's presence
    with, and a file that turns out to be unreadable should not be the reason
    there is no cache at all."""
    out = thumb_path(cache, sha1, size)
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _write_thumb(img, out: Path, sha1: str, size: int) -> Path:
    """`img` — already RGB, and ours to shrink — saved as this thumbnail.

    Written to a private name and moved into place, because `out.exists()` is
    the only gate and two threads race for it: the daemon's /thumb handler
    calls this for the photo the index thread is writing right now. A reader
    that opened the half-written file got a truncated WEBP — a broken tile, or
    an "cannot identify image" error row on a perfectly good photo. os.replace
    is atomic, so a reader sees either nothing or the finished image. The pid
    keeps two processes (daemon + CLI index) off each other's temp file."""
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        img.thumbnail((size, size))
        img.save(tmp, "WEBP", quality=85)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)          # never leave a partial file behind
        raise
    _retire_old_versions(out.parent, out, sha1, size)
    return out


def ensure_thumb(src: str, cache: Path, sha1: str,
                 size: int = THUMB_SIZE_DEFAULT) -> Path:
    _ensure_heif()
    out = _target(cache, sha1, size)
    if out.exists():
        return out
    with Image.open(src) as img:
        return _write_thumb(_flatten(ImageOps.exif_transpose(img)),
                            out, sha1, size)


def ensure_thumb_from_image(img, cache: Path, sha1: str,
                            size: int = THUMB_SIZE_DEFAULT) -> Path:
    """Same thumbnail, from an image already in memory rather than a file.

    This is how a video gets one: the indexer has just decoded six frames to
    embed and holds the middle one, so re-opening the file to render it would
    mean decoding the same frame twice. `sha1` is still the hash of the *video
    file*, so the cache name, the /thumb route and the audit all work on a video
    exactly as they do on a photograph.

    The image is copied first: shrinking is in place, and the caller's frame is
    on its way to the image encoder."""
    out = _target(cache, sha1, size)
    if out.exists():
        return out
    return _write_thumb(_flatten(img).copy(), out, sha1, size)


def ensure_video_thumb(src: str, cache: Path, sha1: str,
                       size: int = THUMB_SIZE_DEFAULT) -> Path:
    """A video's thumbnail, decoding only the one frame it is made of.

    The route the *daemon* takes: the lightbox asks for 2048px on a click, and
    that size was never rendered at index time. video.middle_frame picks the same
    instant the index picked, so the large render is the same picture as the tile
    it grew out of rather than a different second of the same clip."""
    out = _target(cache, sha1, size)
    if out.exists():
        return out
    return _write_thumb(_flatten(video.middle_frame(src, size=size)),
                        out, sha1, size)


# ── face crops ────────────────────────────────────────────────────────────
# The size a person's cover face is served at. Small on purpose: it is a card
# avatar, and a 512px crop of a 512px thumbnail is upscaled mush.
FACE_SIZE_DEFAULT = 200

# How much wider than the detector's box a *displayed* crop is taken.
#
# Deliberately larger than faces.MARGIN, which is what the recognition network
# sees. The network wants the features and nothing else; a person looking at a
# card wants to recognise a face, and MTCNN's box is tight enough that at 0.12
# the crop is eyes-to-mouth with the top of the head cut off. Two different jobs,
# two different crops of the same stored box — which is exactly why the box is
# stored normalized rather than as a crop.
FACE_COVER_MARGIN = 0.35


def face_thumb_path(cache, sha1: str, bbox, size: int = FACE_SIZE_DEFAULT) -> Path:
    """Where one face's crop lives.

    Named from the *content* — the photo's hash and the box within it — rather
    than from the face's row id, and that is not a detail: re-detecting a library
    replaces every face row (new ids for the same faces), so an id-keyed cache
    would be invalidated wholesale by a run that changed nothing anybody can see.
    The box is folded into a short digest because it is four floats and a file
    name is not the place for `0.123456_0.654321_…`."""
    key = hashlib.sha1(
        (",".join(f"{float(v):.5f}" for v in bbox)).encode()).hexdigest()[:10]
    return (Path(cache) / "faces"
            / f"{sha1}-{key}-{size}-v{THUMB_VERSION}.webp")


def ensure_face_thumb(src: str, cache: Path, sha1: str, bbox,
                      size: int = FACE_SIZE_DEFAULT, kind: str = None) -> Path:
    """A square crop of one face, rendered from the thumbnail lens already has.

    From the 512px thumbnail rather than from the original file: that thumbnail
    is what the detector looked at, so the box means exactly what it says there,
    and re-decoding a 48-megapixel HEIC (or seeking into a video) to crop a 200px
    avatar would make opening the People view cost more than the index run did.
    The upper bound on quality is the same 512px the box was found in, which is
    the honest ceiling for a face that was never bigger than that."""
    out = _target_in(Path(cache) / "faces", face_thumb_path(cache, sha1, bbox, size))
    if out.exists():
        return out
    base = ensure_media_thumb(src, cache, sha1, THUMB_SIZE_DEFAULT, kind)
    with Image.open(base) as img:
        crop = crop_face(img, bbox, size=size, margin=FACE_COVER_MARGIN)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        crop.save(tmp, "WEBP", quality=88)
        os.replace(tmp, out)                 # same atomic swap as _write_thumb
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return out


def _target_in(directory: Path, out: Path) -> Path:
    """`out`, with its directory in place. Same reason as `_target`: the
    directory existing is what readers test the cache's presence with, and a
    file that turns out to be unreadable should not be why there is none."""
    directory.mkdir(parents=True, exist_ok=True)
    return out


def ensure_media_thumb(src: str, cache: Path, sha1: str,
                       size: int = THUMB_SIZE_DEFAULT, kind: str = None) -> Path:
    """This file's thumbnail, whichever kind of file it is.

    One entry point so that everything serving thumbnails — the /thumb route,
    the lightbox's large render, the audit — is indifferent to stills versus
    videos. `kind` is the catalog's column when the caller has the row; without
    it the extension decides, which is the same rule that filled the column."""
    if (kind or kind_for(src)) == "video":
        return ensure_video_thumb(src, cache, sha1, size)
    return ensure_thumb(src, cache, sha1, size)
