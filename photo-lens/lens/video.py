"""Video files as things lens can read: probe them, and pull frames out of them.

Decoded with **pyav** (the `av` package), which ships its own ffmpeg libraries in
the wheel — so indexing a movie needs no `ffmpeg` on PATH, no subprocess, and no
temporary files. Everything below stays inside the process.

Two facts about video shape the whole module:

  * **A video is a span, not an instant.** One frame cannot describe it, so
    `keyframes` samples several across the duration and the indexer mean-pools
    their vectors into the single vector the matrix holds (see
    indexer._pool). Six frames is the cap: it is enough to catch a scene change
    in a phone clip or a screen recording, and it bounds the cost of a library
    that turns out to hold a hundred of them.
  * **A container knows when it was shot, and often where.** `taken_at`
    everywhere else in lens is a tz-naive *local wall clock* (see
    metadata.extract, apple_photos._naive), and a container's `creation_time` is
    usually UTC — so a clip shot at 18:41 in India would land at 13:11 and sort
    among a different evening's photographs. `_capture_time` is the conversion
    back, and it prefers the keys that carry the capture-local clock outright.

`import av` happens inside the functions, not at module scope: lens must import
(and its tests must collect) on a machine that has never opened a video, and the
import pulls in ~18MB of shared libraries.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How many instants a video is sampled at, for both its vector and its
# thumbnail. Odd-vs-even matters: the thumbnail is the middle of this grid, and
# with six frames that is the fourth, ~60% of the way in — past the black frame
# and the title card that open a screen recording.
KEYFRAMES_DEFAULT = 6

# Seeking to exactly `duration` lands past the last frame and decodes nothing,
# so the last sample stops just short of the end.
_SEEK_MARGIN_S = 0.05


class VideoError(Exception):
    """A file that is not a video lens can read. Raised rather than returned:
    the indexer already turns an exception on one file into an error row and
    carries on (see indexer.index_once), which is exactly the handling this
    wants."""


def _av():
    import av
    return av


def _open(path):
    """`(container, video_stream)`. The caller closes the container.

    A file with no video stream is a VideoError rather than an IndexError: an
    .mp4 holding only audio is a real thing to find on disk, and "no video
    stream in this file" is what the error row should say."""
    av = _av()
    container = av.open(str(path))
    try:
        stream = container.streams.video[0]
    except IndexError:
        container.close()
        raise VideoError("no video stream in this file") from None
    except BaseException:
        container.close()
        raise
    # SLICE, deliberately, and never AUTO.
    #
    # AUTO adds frame threading, which is the right answer for playing a video
    # and the wrong one for this: every read here is a seek followed by *one*
    # decoded frame, and a frame-threaded decoder holds a pipeline of frames
    # before it emits the first one — so it decodes several to hand back one, and
    # keeps a pool of them alive. Measured on three 4K clips from the reference
    # library, six frames each: AUTO 3.46s and 740MB peak, SLICE 0.80s and 258MB.
    # Slice threading parallelizes *within* a frame, so it costs no latency and no
    # pool. (This machine has already had an index run OOM-killed once; a decoder
    # that triples its own footprint for a 4× slowdown is not a tradeoff.)
    stream.thread_type = "SLICE"
    return container, stream


def _hms(text):
    """"00:00:29.720000000" → 29.72. Matroska keeps the duration in the stream's
    own metadata rather than in a header field pyav exposes."""
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", str(text or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _duration_s(container, stream):
    """The video's length in seconds, or None if nothing on file says.

    Three sources in order of how much they can be trusted: the container's own
    field (in `av.time_base` units), the stream's (in its own time base), and
    Matroska's metadata string. None is a fact this module handles — the sample
    grid collapses to the first frame — not a reason to fail."""
    av = _av()
    if container.duration:
        d = container.duration / av.time_base
        if d > 0:
            return d
    if stream.duration and stream.time_base:
        d = float(stream.duration * stream.time_base)
        if d > 0:
            return d
    return _hms(stream.metadata.get("DURATION"))


def _parse_iso(text):
    """An ISO-8601 instant from container metadata, aware or naive, or None.
    Written with a trailing `Z` by every muxer in practice."""
    s = str(text or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _offset(text):
    """"+0530" → a fixed-offset timezone. Android writes the capture offset in a
    tag of its own beside a UTC `creation_time`, which is the only way to get
    that clip's local clock back."""
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", str(text or "").strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    if delta > timedelta(hours=14):
        return None
    return timezone(sign * delta)


def _capture_time(container, stream):
    """When this video was shot, as the tz-naive local wall clock the rest of
    lens stores — or None.

    The order is the whole point, because a UTC instant is not the answer to
    "when was this taken":

      1. `com.apple.quicktime.creationdate` — an iPhone writes the capture-local
         clock *with* its offset here ("2026-02-13T18:41:49+0530"), so the wall
         clock is read straight off it and the offset dropped, exactly as
         apple_photos._naive does for a Photos date.
      2. `creation_time` + `com.samsung.android.utc_offset` — Android writes UTC
         and the offset separately; applying one to the other reproduces the
         clock in the file's own name (20250712_143727 ⇄ 14:37:35 local).
      3. `creation_time` alone, carrying an offset — converted to *this*
         machine's zone. A guess, and the only one available: the container says
         when, and nothing says where.
      4. `creation_time` alone, already naive — taken as written.
    """
    md = {**dict(stream.metadata), **dict(container.metadata)}
    dt = _parse_iso(md.get("com.apple.quicktime.creationdate"))
    if dt is not None:
        return dt.replace(tzinfo=None).isoformat()
    dt = _parse_iso(md.get("creation_time"))
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.isoformat()
    tz = _offset(md.get("com.samsung.android.utc_offset"))
    return dt.astimezone(tz).replace(tzinfo=None).isoformat()


def _degrees(token, deg_digits):
    """One ISO 6709 coordinate → decimal degrees.

    The standard packs degrees, minutes and seconds into one signed number and
    leaves the reader to tell them apart by *how many digits* precede the
    decimal point: `+17.3982` is degrees, `+1723.89` is degrees and minutes,
    `+172353.4` adds seconds. `deg_digits` is 2 for a latitude and 3 for a
    longitude, which is what makes that count unambiguous."""
    m = re.fullmatch(r"([+-])(\d+)(\.\d+)?", token)
    if not m:
        return None
    sign = -1.0 if m.group(1) == "-" else 1.0
    whole, frac = m.group(2), float(m.group(3) or 0)
    if len(whole) == deg_digits:
        value = int(whole) + frac
    elif len(whole) == deg_digits + 2:
        value = int(whole[:deg_digits]) + (int(whole[deg_digits:]) + frac) / 60
    elif len(whole) == deg_digits + 4:
        value = (int(whole[:deg_digits])
                 + int(whole[deg_digits:deg_digits + 2]) / 60
                 + (int(whole[deg_digits + 2:]) + frac) / 3600)
    else:
        return None
    return sign * value


def _iso6709(value):
    """`(lat, lon)` from an ISO 6709 point string, or `(None, None)`.

    "+17.3982+078.3512+549.930/" is what an iPhone writes into
    `com.apple.quicktime.location.ISO6709` and Android into `location` — a real
    GPS fix, which is why lens reads it: with coordinates a video joins the
    trips and the place names its neighbouring photographs have, instead of
    being a row nothing can situate. The altitude is ignored; nothing shows it.
    """
    m = re.match(r"\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", str(value or ""))
    if not m:
        return None, None
    lat = _degrees(m.group(1), 2)
    lon = _degrees(m.group(2), 3)
    if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
        return None, None
    return lat, lon


def _location(container, stream):
    md = {**dict(stream.metadata), **dict(container.metadata)}
    for key in ("com.apple.quicktime.location.ISO6709", "location",
                "location-eng"):
        lat, lon = _iso6709(md.get(key))
        if lat is not None:
            return lat, lon
    return None, None


def _container_label(container, path) -> str:
    """A container name a person recognises.

    ffmpeg names one demuxer for a family of formats — an .mp4 and a .mov both
    probe as "mov,mp4,m4a,3gp,3g2,mj2", and a .webm as "matroska,webm" — so the
    file's own extension picks its member of the list when it is in there. It is
    the honest answer: the extension is the name of the container *this* file
    claims to be."""
    tokens = [t.strip().lower() for t in str(container.format.name).split(",")]
    suffix = Path(path).suffix.lstrip(".").lower()
    return (suffix if suffix in tokens else tokens[0]).upper()


def probe(path) -> dict:
    """What a video says about itself, without decoding a single frame.

    Cheap enough (one header read) that metadata.extract calls it for every
    video every time it re-reads one, and complete enough that the details panel
    needs nothing else."""
    container, stream = _open(path)
    try:
        duration = _duration_s(container, stream)
        rate = stream.average_rate or stream.guessed_rate
        lat, lon = _location(container, stream)
        return {
            "duration_s": round(duration, 3) if duration else None,
            "width": int(stream.width) or None,
            "height": int(stream.height) or None,
            "fps": round(float(rate), 3) if rate else None,
            "codec": stream.codec_context.name or None,
            "container": _container_label(container, path),
            "creation_time": _capture_time(container, stream),
            "lat": lat,
            "lon": lon,
        }
    finally:
        container.close()


def sample_times(duration_s, n: int = KEYFRAMES_DEFAULT) -> list:
    """The instants a video is sampled at: the first frame, then evenly across
    the rest of it.

    A video whose length nothing on file states is sampled once, at the start —
    the alternative is decoding the whole thing to find out how long it is, and
    a header that does not say is usually a stream that would not seek anyway.
    """
    if not duration_s or duration_s <= 0 or n <= 1:
        return [0.0]
    span = max(0.0, duration_s - _SEEK_MARGIN_S)
    return [span * i / (n - 1) for i in range(n)]


def _frame_at(container, stream, seconds):
    """The first frame at or after `seconds`, or None.

    `seek` lands on the keyframe at or before the target, and decoding resumes
    there — so this returns *a* frame near the target rather than the exact one,
    which is all a thumbnail or an embedding needs. Sparse keyframes therefore
    make two neighbouring samples the same picture, and that is deliberately not
    corrected: a keyframe that covers more of the timeline earns more weight in
    the pooled vector, and the alternative (decoding forward to the exact
    instant) is a whole GOP of work per sample.

    A failure on one sample is swallowed. A truncated file usually decodes its
    first frames and fails on the last, and the frames it *did* give up are a
    better answer than an error row — while a file that yields nothing at all
    still raises, from `keyframes`."""
    try:
        offset = int(seconds / stream.time_base) + (stream.start_time or 0)
        container.seek(offset, stream=stream)
        for frame in container.decode(stream):
            return frame
    except Exception:
        return None
    return None


def _image(frame, size=None):
    """One decoded frame as a PIL RGB image, at most `size` on its long edge.

    Downscaled here rather than by the caller because the caller is usually
    about to hold six of these: a 4K frame is 24MB of RGB, and six of them per
    video across a batch is hundreds of megabytes for pixels that are about to
    be thrown away. The indexer asks for the thumbnail size, which is also what
    it embeds photographs at — so the image encoder sees the same kind of
    picture for a video frame as for a still."""
    img = frame.to_image()
    if size:
        img.thumbnail((size, size))
    return img


def keyframes(path, max_frames: int = KEYFRAMES_DEFAULT, size=None) -> list:
    """Frames sampled evenly across the video: PIL RGB images, in order.

    One per instant in `sample_times` that decoded, so the list can be shorter
    than `max_frames` and can hold the same picture twice (see `_frame_at`).
    Raises VideoError when nothing decodes at all — a file that is a video by
    name only."""
    container, stream = _open(path)
    try:
        times = sample_times(_duration_s(container, stream), max_frames)
        frames = [_image(f, size) for f in
                  (_frame_at(container, stream, t) for t in times)
                  if f is not None]
        if not frames:
            # Nothing seekable. A stream with no index (a raw capture, a
            # growing file) still decodes from the front, so ask for that
            # before giving up on it.
            frames = [_image(f, size) for f in
                      _leading_frames(container, stream, max_frames)]
    finally:
        container.close()
    if not frames:
        raise VideoError(f"no decodable frames in {os.path.basename(str(path))}")
    return frames


def _leading_frames(container, stream, n):
    """The first `n` decodable frames, from wherever the container is now."""
    out = []
    try:
        container.seek(0, stream=stream)
    except Exception:
        pass
    try:
        for frame in container.decode(stream):
            out.append(frame)
            if len(out) >= n:
                break
    except Exception:
        pass
    return out


def middle_frame(path, size=None, max_frames: int = KEYFRAMES_DEFAULT):
    """The frame a video's thumbnail is made of.

    Defined as the middle instant of the same grid `keyframes` samples, so the
    512px thumbnail the index wrote and a 2048px one rendered later, on demand,
    for the lightbox are the same picture rather than two different moments of
    the same clip. One seek instead of six, which is what makes rendering the
    large size on a click affordable."""
    container, stream = _open(path)
    try:
        times = sample_times(_duration_s(container, stream), max_frames)
        frame = _frame_at(container, stream, times[len(times) // 2])
        if frame is None:
            frame = _frame_at(container, stream, 0.0)
        if frame is None:
            frames = _leading_frames(container, stream, 1)
            frame = frames[0] if frames else None
        if frame is None:
            raise VideoError(
                f"no decodable frames in {os.path.basename(str(path))}")
        return _image(frame, size)
    finally:
        container.close()
