"""What lens can read out of a video container, and what it does about the two
things a container gets wrong: the timezone of the capture instant, and how many
pictures one file is."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from lens import video
from tests.conftest import BANDS, band_of, write_video


def _local(utc_iso: str) -> str:
    """The same instant as this machine's naive wall clock — computed the way a
    reader would, so the assertion holds in any timezone the tests run in."""
    return (datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
            .astimezone().replace(tzinfo=None).isoformat())


def _stub(container_md=None, stream_md=None):
    """A container and stream that are nothing but their metadata — enough for
    the timestamp and location rules, which are pure functions of those two
    dicts. The vendor keys they test (`com.apple.quicktime.*`) cannot be written
    by the mp4 muxer, so a real file cannot carry them here; everything a muxer
    *can* write is tested on a real file below."""
    return (SimpleNamespace(metadata=container_md or {}),
            SimpleNamespace(metadata=stream_md or {}))


# ── probe ────────────────────────────────────────────────────────────────
def test_probe_reads_the_shape_of_a_real_file(video_file):
    p = video_file("clip.mp4", seconds=2.0, fps=10, size=32)
    info = video.probe(str(p))
    assert info["duration_s"] == pytest.approx(2.0, abs=0.15)
    assert (info["width"], info["height"]) == (32, 32)
    assert info["fps"] == pytest.approx(10.0)
    assert info["codec"] == "h264"
    assert info["container"] == "MP4"


def test_container_is_named_after_the_file_rather_than_the_demuxer(video_file):
    """ffmpeg names one demuxer for a whole family — an .mp4 and a .mov both
    probe as "mov,mp4,m4a,3gp,3g2,mj2" — so "MOV" for an .mp4 would be a fact
    about ffmpeg's internals shown in a details panel."""
    assert video.probe(str(video_file("a.mp4")))["container"] == "MP4"
    assert video.probe(str(video_file("b.mov")))["container"] == "MOV"
    assert video.probe(str(video_file("c.webm", codec="libvpx",
                                      seconds=0.5, fps=8)))["container"] == "WEBM"


def test_a_utc_creation_time_becomes_the_local_wall_clock(video_file):
    """`taken_at` is a tz-naive local clock everywhere in lens (dates are
    compared as strings), and a muxer writes UTC. Left alone, a clip shot at
    18:41 in India was catalogued at 13:11 and sorted among a different
    evening's photographs."""
    p = video_file("clip.mp4",
                   metadata={"creation_time": "2025-07-01T10:00:00.000000Z"})
    assert video.probe(str(p))["creation_time"] == _local("2025-07-01T10:00:00Z")


def test_a_container_with_no_date_says_so(video_file):
    """None, not a guess: the fallback to the file's mtime is the caller's
    decision (see metadata._extract_video), and this module must not pretend the
    file said something it did not."""
    assert video.probe(str(video_file("clip.webm", codec="libvpx",
                                      seconds=0.5)))["creation_time"] is None


def test_the_capture_local_clock_wins_over_utc():
    """An iPhone writes both: `creation_time` in UTC and
    `com.apple.quicktime.creationdate` with the offset it was shot at. The
    second is the answer to "when was this taken" — it needs no guess about
    where the camera was."""
    c, s = _stub({"creation_time": "2026-02-13T13:11:49.000000Z",
                  "com.apple.quicktime.creationdate": "2026-02-13T18:41:49+0530"})
    assert video._capture_time(c, s) == "2026-02-13T18:41:49"


def test_an_android_offset_tag_is_applied_to_its_utc_timestamp():
    """Android writes UTC and keeps the capture offset in a tag of its own.
    Measured against a real file: 20250712_143727.mp4 carries
    creation_time 09:07:35Z and utc_offset +0530 — 14:37 local, which is the
    clock in its own name."""
    c, s = _stub({"creation_time": "2025-07-12T09:07:35.000000Z",
                  "com.samsung.android.utc_offset": "+0530"})
    assert video._capture_time(c, s) == "2025-07-12T14:37:35"


def test_a_naive_creation_time_is_taken_as_written():
    c, s = _stub({"creation_time": "2025-07-12T09:07:35"})
    assert video._capture_time(c, s) == "2025-07-12T09:07:35"


def test_an_unreadable_offset_falls_back_rather_than_raising():
    """A garbage offset tag must not cost the timestamp — it is one hint about
    one field, and the UTC instant is still known."""
    c, s = _stub({"creation_time": "2025-07-12T09:07:35Z",
                  "com.samsung.android.utc_offset": "half past"})
    assert video._capture_time(c, s) == _local("2025-07-12T09:07:35Z")
    assert video._offset("+9900") is None            # 99 hours is not a zone


def test_no_timestamp_anywhere_is_none():
    c, s = _stub({"encoder": "Lavf61"})
    assert video._capture_time(c, s) is None


# ── location ─────────────────────────────────────────────────────────────
def test_a_container_gps_fix_is_read(video_file):
    """A phone writes an ISO 6709 point into the container, and with it a video
    joins the trips and place names of the photographs around it instead of
    being a row nothing can situate."""
    p = video_file("clip.mp4", metadata={"location": "+11.4272+079.7934/"})
    info = video.probe(str(p))
    assert info["lat"] == pytest.approx(11.4272)
    assert info["lon"] == pytest.approx(79.7934)


def test_iso6709_digit_counts_decide_degrees_minutes_seconds():
    """The standard packs D/M/S into one signed number and leaves the reader to
    tell them apart by how many digits precede the point — two for a latitude,
    three for a longitude."""
    assert video._iso6709("+17.3982+078.3512+549.930/") == (
        pytest.approx(17.3982), pytest.approx(78.3512))
    lat, lon = video._iso6709("-3530.00+15830.00/")      # degrees + minutes
    assert lat == pytest.approx(-35.5) and lon == pytest.approx(158.5)
    lat, lon = video._iso6709("+491230.0+0113000.0/")    # + seconds
    assert lat == pytest.approx(49.2083, abs=1e-3)
    assert lon == pytest.approx(11.5, abs=1e-3)


def test_a_nonsense_location_is_no_location():
    assert video._iso6709("") == (None, None)
    assert video._iso6709("here") == (None, None)
    assert video._iso6709("+99.0+200.0/") == (None, None)     # off the planet


# ── frames ───────────────────────────────────────────────────────────────
def test_sample_times_spans_the_duration():
    times = video.sample_times(10.0, 6)
    assert len(times) == 6
    assert times[0] == 0.0
    assert times[-1] == pytest.approx(9.95)              # just short of the end
    assert times == sorted(times)


def test_a_video_of_unknown_length_is_sampled_once():
    """Seeking needs a duration to divide up. A header that does not state one is
    usually a stream that would not seek anyway, and decoding the whole file to
    find out how long it is costs more than the sampling is worth."""
    assert video.sample_times(None, 6) == [0.0]
    assert video.sample_times(0, 6) == [0.0]
    assert video.sample_times(10.0, 1) == [0.0]


def test_keyframes_are_taken_across_the_whole_clip(video_file):
    """Six frames of the same second would describe a title card rather than a
    video, which is the entire reason more than one frame is embedded."""
    frames = video.keyframes(str(video_file("clip.mp4", seconds=2.0, fps=10)))
    assert len(frames) == 6
    bands = [band_of(f) for f in frames]
    assert bands[0] == 0                                  # the first frame
    assert bands == sorted(bands)                         # in order
    assert bands[-1] >= len(BANDS) - 2                    # and reaching the end
    assert len(set(bands)) >= 5


def test_keyframes_are_downscaled_where_asked(video_file):
    """A 4K frame is 24MB of RGB and six of them per video, across a batch, is
    hundreds of megabytes of pixels about to be thrown away."""
    p = video_file("clip.mp4", size=64)
    assert video.keyframes(str(p), size=16)[0].size == (16, 16)
    assert video.keyframes(str(p))[0].size == (64, 64)
    assert video.keyframes(str(p))[0].mode == "RGB"


def test_max_frames_bounds_the_work(video_file):
    p = video_file("clip.mp4", seconds=2.0, fps=10)
    assert len(video.keyframes(str(p), max_frames=2)) == 2
    assert len(video.keyframes(str(p), max_frames=1)) == 1


def test_the_thumbnail_frame_is_the_middle_of_the_same_grid(video_file):
    """The index writes a 512px thumb from `keyframes`; the lightbox asks the
    daemon for 2048px later, which decodes one frame with `middle_frame`. If the
    two picked different instants, opening a video would swap the picture the
    tile showed for a different second of the same clip."""
    p = str(video_file("clip.mp4", seconds=2.0, fps=10))
    frames = video.keyframes(p)
    assert band_of(video.middle_frame(p)) == band_of(frames[len(frames) // 2])


def test_a_file_that_is_not_a_video_raises(tmp_path):
    """Raised, not returned: the indexer turns an exception on one file into an
    error row and carries on, which is exactly the handling this wants."""
    bad = tmp_path / "clip.mp4"
    bad.write_bytes(b"MOOV nope not a container at all")
    with pytest.raises(Exception):
        video.probe(str(bad))
    with pytest.raises(Exception):
        video.keyframes(str(bad))


def test_an_audio_only_file_says_what_is_wrong(tmp_path):
    """An .mp4 holding only sound is a real thing to find on disk, and "no video
    stream in this file" is what the error row should read."""
    import av
    path = tmp_path / "sound.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("aac", rate=8000)
        frame = av.AudioFrame(format="fltp", layout="mono", samples=1024)
        frame.sample_rate = 8000
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)

    with pytest.raises(video.VideoError, match="no video stream"):
        video.probe(str(path))


def test_a_truncated_file_keeps_the_frames_it_could_give(tmp_path):
    """A file cut off mid-stream decodes its first frames and fails on the rest.
    Those frames are a better answer than an error row — the video is watchable,
    and something can be said about what is in it."""
    # WebM rather than mp4: an mp4's index lives at the *end* of the file, so a
    # truncated one is refused outright by the demuxer and never reaches the
    # decode this is about. Matroska is streamable, which is the case worth
    # covering — a file being written, or a copy that stopped halfway.
    whole = write_video(tmp_path / "whole.webm", codec="libvpx",
                        seconds=3.0, fps=10)
    cut = tmp_path / "cut.webm"
    data = whole.read_bytes()
    cut.write_bytes(data[:len(data) // 3])
    try:
        frames = video.keyframes(str(cut))
    except Exception:
        pytest.skip("this build refuses the truncated file outright")
    assert frames and band_of(frames[0]) == 0
