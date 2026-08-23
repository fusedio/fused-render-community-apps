import pytest
from PIL import Image

from lens.thumbs import (THUMB_SIZE_DEFAULT, THUMB_VERSION, ensure_media_thumb,
                         ensure_thumb, ensure_thumb_from_image,
                         ensure_video_thumb, thumb_path)
from lens import video
from tests.conftest import band_of, write_video


def test_thumb_path_is_where_ensure_thumb_actually_writes(tmp_path):
    """The name is spelled in one place because the version suffix is part of
    it. A caller that rebuilt `{sha1}-512.webp` for itself would report every
    thumbnail in the cache as missing the moment THUMB_VERSION moved — which is
    exactly what validate.embedding_integrity looks photos up by, so the audit
    would fail a perfectly good library."""
    cache = tmp_path / "cache"
    src = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64), "blue").save(src, "JPEG")

    p = thumb_path(cache, "abc", 64)
    assert p.name == f"abc-64-v{THUMB_VERSION}.webp"
    assert p.parent == cache / "thumbs"
    assert ensure_thumb(str(src), cache, "abc", 64) == p
    assert p.exists()

    # the default is the one size the indexer renders, and so the only size
    # anything else may assume exists on disk
    assert thumb_path(cache, "abc") == thumb_path(cache, "abc", THUMB_SIZE_DEFAULT)
    assert ensure_thumb(str(src), cache, "abc") == thumb_path(cache, "abc")
    assert thumb_path(cache, "abc").exists()


def test_thumb_created_and_cached(tmp_path):
    src = tmp_path / "big.jpg"
    Image.new("RGB", (2000, 1000), "blue").save(src, "JPEG")
    out = ensure_thumb(str(src), tmp_path / "cache", "deadbeef", size=512)
    assert out.name == f"deadbeef-512-v{THUMB_VERSION}.webp" and out.exists()
    with Image.open(out) as t:
        assert max(t.size) == 512 and t.size == (512, 256)
    m1 = out.stat().st_mtime_ns
    assert ensure_thumb(str(src), tmp_path / "cache", "deadbeef", 512).stat().st_mtime_ns == m1


def _corner(path):
    with Image.open(path) as t:
        return t.convert("RGB").getpixel((2, 2))


def test_transparency_composites_onto_white(tmp_path):
    """`.convert("RGB")` on an RGBA image keeps whatever is under the alpha,
    which Pillow zero-fills — so every transparent PNG became a black
    rectangle, in the grid and in its embedding alike."""
    src = tmp_path / "overlay.png"
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 0))          # fully clear red
    img.putpixel((32, 32), (255, 0, 0, 255))                   # one opaque dot
    img.save(src, "PNG")

    out = ensure_thumb(str(src), tmp_path / "cache", "clear", size=64)
    r, g, b = _corner(out)
    assert (r, g, b) == (255, 255, 255), (r, g, b)
    assert r + g + b > 60                    # emphatically not black


def test_semi_transparent_pixels_lighten_rather_than_darken(tmp_path):
    """A 50%-alpha red over white is pink; over black it is maroon. The point
    of the white canvas is that partial coverage stays visible."""
    src = tmp_path / "half.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(src, "PNG")
    r, g, b = _corner(ensure_thumb(str(src), tmp_path / "cache", "half", size=64))
    assert r > 200 and g > 90 and b > 90     # pink, not maroon (≈127, 0, 0)


def test_palette_transparency_is_composited_too(tmp_path):
    """A GIF/PNG in mode "P" carries its transparency in `info`, not in a
    channel, so the mode check alone would miss it."""
    src = tmp_path / "p.gif"
    img = Image.new("P", (64, 64), 0)
    img.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    img.save(src, "GIF", transparency=0)
    r, g, b = _corner(ensure_thumb(str(src), tmp_path / "cache", "pal", size=64))
    assert (r, g, b) == (255, 255, 255), (r, g, b)


def test_opaque_images_are_untouched_by_the_composite(tmp_path):
    src = tmp_path / "solid.png"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(src, "PNG")
    # lossy WEBP, so within a couple of levels of the original dark colour —
    # the point is that it did not get washed toward white
    px = _corner(ensure_thumb(str(src), tmp_path / "cache", "solid", 64))
    assert all(abs(a - b) <= 3 for a, b in zip(px, (10, 20, 30))), px


def test_version_bump_regenerates_and_retires_the_old_thumb(tmp_path):
    """The version lives in the file name, so old output can never be served:
    the new name simply isn't there yet. Earlier renderings are dropped as we
    pass them — globbed, not named, so the next bump cleans up after this one
    without anyone having to remember."""
    cache = tmp_path / "cache"
    src = tmp_path / "a.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(src, "PNG")

    thumbs = cache / "thumbs"
    thumbs.mkdir(parents=True)
    legacy = thumbs / "abc-64.webp"                    # what v1 wrote
    prev = thumbs / "abc-64-v1.webp"                   # a hypothetical earlier v
    other = thumbs / "abc-512-v1.webp"                 # a different size
    keep = thumbs / "def-64-v1.webp"                   # a different photo
    for f in (legacy, prev, other, keep):
        Image.new("RGB", (64, 64), "black").save(f, "WEBP")

    out = ensure_thumb(str(src), cache, "abc", size=64)

    assert out.name == f"abc-64-v{THUMB_VERSION}.webp" and out.exists()
    assert not legacy.exists() and not prev.exists()
    assert other.exists() and keep.exists()            # untouched
    assert _corner(out) == (255, 255, 255)


def test_thumb_appears_atomically(tmp_path, monkeypatch):
    """`out.exists()` is the only gate, and the daemon's /thumb handler races
    the index thread for the same file. A reader that caught the half-written
    WEBP got a truncated image — a broken tile, or a bogus "cannot identify
    image file" error row on a perfectly good photo."""
    cache = tmp_path / "cache"
    src = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64), "blue").save(src, "JPEG")

    seen = []
    real_save = Image.Image.save

    def watching_save(self, fp, *a, **kw):
        # whatever a concurrent reader could see at the moment bytes are written
        seen.append(sorted(p.name for p in (cache / "thumbs").iterdir()))
        return real_save(self, fp, *a, **kw)

    monkeypatch.setattr(Image.Image, "save", watching_save)
    out = ensure_thumb(str(src), cache, "abc", size=64)

    assert seen, "save was never called"
    # the final name never exists while it is being written
    assert all(out.name not in names for names in seen), seen
    assert out.exists()


def test_a_failed_render_leaves_no_partial_file(tmp_path):
    """Half a thumb on disk would be served forever after: `out.exists()` would
    be true and nothing would ever rewrite it."""
    cache = tmp_path / "cache"
    src = tmp_path / "broken.jpg"
    src.write_bytes(b"not really a jpeg")

    with pytest.raises(Exception):
        ensure_thumb(str(src), cache, "abc", size=64)

    leftovers = list((cache / "thumbs").iterdir())
    assert leftovers == [], leftovers


# ── videos ───────────────────────────────────────────────────────────────
def test_a_thumb_can_be_made_from_an_image_already_in_memory(tmp_path):
    """The route a video takes: the indexer has just decoded the frames it is
    about to embed, so re-opening the file to render the middle one would decode
    the same frame twice. Same name, same cache, same everything else — `sha1` is
    still the hash of the file on disk."""
    cache = tmp_path / "cache"
    img = Image.new("RGB", (200, 100), "green")
    out = ensure_thumb_from_image(img, cache, "vid1", 64)
    assert out == thumb_path(cache, "vid1", 64)
    with Image.open(out) as t:
        assert t.size == (64, 32) and t.format == "WEBP"


def test_the_callers_image_survives_being_thumbnailed(tmp_path):
    """`Image.thumbnail` shrinks in place, and the frame handed in here is on its
    way to the image encoder next — embedding a 64px crop of a 512px frame would
    silently make a video's vector describe something else."""
    img = Image.new("RGB", (200, 100), "green")
    ensure_thumb_from_image(img, tmp_path / "cache", "vid2", 32)
    assert img.size == (200, 100)


def test_an_existing_thumb_is_not_rendered_again(tmp_path):
    cache = tmp_path / "cache"
    out = ensure_thumb_from_image(Image.new("RGB", (64, 64), "red"),
                                  cache, "vid3", 32)
    stamp = out.stat().st_mtime_ns
    same = ensure_thumb_from_image(Image.new("RGB", (64, 64), "blue"),
                                   cache, "vid3", 32)
    assert same == out and out.stat().st_mtime_ns == stamp


def test_a_video_thumb_is_the_frame_the_index_picked(tmp_path):
    """The tile and the large render have to be the same picture: the lightbox
    asks the daemon for 2048px on a click, and that size was never rendered at
    index time. Both come from the middle of the same sampling grid."""
    src = write_video(tmp_path / "clip.mp4", seconds=2.0, fps=10, size=64)
    out = ensure_video_thumb(str(src), tmp_path / "cache", "clip", 64)
    frames = video.keyframes(str(src))
    with Image.open(out) as t:
        assert band_of(t) == band_of(frames[len(frames) // 2])


def test_ensure_media_thumb_reads_a_video_and_a_still_the_same_way(tmp_path):
    """One entry point, so everything that serves thumbnails — the /thumb route,
    the lightbox's large render, the audit — is indifferent to which kind of file
    it is holding."""
    cache = tmp_path / "cache"
    still = tmp_path / "a.jpg"
    Image.new("RGB", (64, 64), "blue").save(still, "JPEG")
    clip = write_video(tmp_path / "clip.mp4", seconds=1.0, fps=10, size=64)

    assert ensure_media_thumb(str(still), cache, "s1", 32).exists()
    assert ensure_media_thumb(str(clip), cache, "v1", 32, "video").exists()
    # without the catalog's column the extension decides, which is the same rule
    # that filled the column
    assert ensure_media_thumb(str(clip), cache, "v2", 32).exists()
    # ...and the wrong kind is a failure, not a silently blank tile
    with pytest.raises(Exception):
        ensure_media_thumb(str(clip), cache, "v3", 32, "image")
