import numpy as np
import pytest
from PIL import Image

from lens import faces


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "lens-cache"
    monkeypatch.setenv("LENS_CACHE", str(d))
    return d


# ── faces, without the models ──────────────────────────────────────────────
# A face detector and a recognition network are two model downloads and a few
# hundred megabytes of weights, and their answers depend on real photographs of
# real people — which a test suite has none of, and should not need. So identity
# is expressed as *colour*: `face_photo` writes an image of flat vertical bands,
# and FakeFaceModel says "one face per band, and two bands of the same colour are
# the same person".
#
# That is not a stub of the models; it is a stub of the *facts* they produce (a
# box, and a vector whose distances mean identity), which is exactly the input
# everything downstream — the pipeline stage, the clustering, the stability
# rules, the endpoints — is written against. The real models are exercised
# separately, and only when asked for (see tests/test_faces.py,
# LENS_MODEL_TESTS=1).
FACE_COLOURS = {
    "ana": (200, 40, 40),
    "ben": (40, 200, 40),
    "cleo": (40, 40, 200),
    "dee": (200, 200, 40),
}
_BY_RGB = {v: k for k, v in FACE_COLOURS.items()}
FACE_DIM = 8
_NAME_AXIS = {name: i for i, name in enumerate(sorted(FACE_COLOURS))}


def face_photo(path, people=("ana",), size: int = 64,
               taken: str = "2025:07:14 10:00:00"):
    """An image whose vertical bands are the people in it. Returns `path`.

    `people=()` writes a plain grey picture — a photograph of a landscape, as far
    as FakeFaceModel is concerned.

    Written with a capture timestamp, so `metadata.is_photo` calls it a
    photograph and it lands in the default search scope. That is not decoration:
    a picture of somebody *is* a camera photo, and without the tag every test
    below would have to ask for `scope=all` to see its own fixtures."""
    import piexif

    people = list(people)
    img = Image.new("RGB", (size, size), (128, 128, 128))
    if people:
        band = size // len(people)
        for i, name in enumerate(people):
            x1 = size if i == len(people) - 1 else (i + 1) * band
            for x in range(i * band, x1):
                for y in range(size):
                    img.putpixel((x, y), FACE_COLOURS[name])
    img.save(path, "JPEG", quality=95)
    piexif.insert(piexif.dump(
        {"Exif": {piexif.ExifIFD.DateTimeOriginal: taken.encode()}}), str(path))
    return path


def _who(px, tolerance: int = 40):
    for rgb, name in _BY_RGB.items():
        if all(abs(a - b) <= tolerance for a, b in zip(px[:3], rgb)):
            return name
    return None


class FakeFaceModel:
    """One face per band of a `face_photo`, with a vector per person.

    `key` and `dim` are the real model's interface, because the indexer keys
    re-detection on the first and sizes the empty matrix with the second."""

    dim = FACE_DIM

    def __init__(self, key: str = "fake-faces", jitter: float = 0.0):
        self.key = key
        # How far a vector is pushed off its person's axis. 0 means two
        # photographs of one person are bit-identical; a small value is what a
        # test uses to check the clustering is a threshold and not an equality.
        self.jitter = jitter
        self.detect_calls = 0
        self.embed_calls = 0
        self.loads = 0

    def load(self):
        self.loads += 1

    def detect(self, img):
        """Left to right, one face per run of same-coloured columns."""
        self.detect_calls += 1
        w, h = img.size
        cols = [_who(img.getpixel((x, h // 2))) for x in range(w)]
        out, start = [], None
        for x in range(w + 1):
            here = cols[x] if x < w else None
            if start is not None and (here != cols[start]):
                if cols[start] is not None:
                    out.append({"bbox": (start / w, 0.2, x / w, 0.8),
                                "prob": 0.99})
                start = x if x < w else None
            elif start is None:
                start = x if x < w else None
        return out

    def embed(self, crops):
        self.embed_calls += 1
        if not crops:
            return np.zeros((0, FACE_DIM), dtype=np.float16)
        out = []
        for c in crops:
            v = np.zeros(FACE_DIM, dtype=np.float32)
            name = _who(c.convert("RGB").getpixel((c.width // 2, c.height // 2)))
            if name is None:
                v[-1] = 1.0                  # a face of nobody in particular
            else:
                v[_NAME_AXIS[name]] = 1.0
                if self.jitter:
                    v[-2] = self.jitter
            v /= np.linalg.norm(v)
            out.append(v)
        return np.stack(out).astype(np.float16)


# Captured before anything can patch it, so a test that is *about* the lazy
# singleton can put the real lookup back (see real_face_lookup).
_REAL_FACE_LOOKUP = faces.model


@pytest.fixture(autouse=True)
def no_real_face_model(monkeypatch):
    """Nothing in the suite reaches the real models by accident.

    `index_once` falls back to `faces.model()` when no face model is passed, and
    most tests are about something else entirely — so without this every one of
    them would download and load two networks to find no faces in a 32×32 green
    square. Tests that are about faces pass their own FakeFaceModel explicitly;
    the ones that want the real thing build it themselves."""
    stub = FakeFaceModel(key="autouse-fake-faces")
    monkeypatch.setattr(faces, "model", lambda: stub)
    return stub


@pytest.fixture
def real_face_lookup(monkeypatch):
    """`faces.model` as the module defines it — for the one test that is about
    the lazy singleton rather than about what it returns. Still loads nothing:
    constructing a FaceModel touches no weights, which is the property being
    checked."""
    monkeypatch.setattr(faces, "model", _REAL_FACE_LOOKUP)
    return _REAL_FACE_LOOKUP


# Eight flat colours, in order, for the test videos below: a clip is a stack of
# these bands, so *which frame* a decoder handed back is a question a test can
# answer by looking at one pixel. That is what makes "sampled across the
# duration" and "the thumbnail is the middle frame" checkable rather than
# asserted about lengths.
BANDS = [(220, 20, 20), (20, 220, 20), (20, 20, 220), (220, 220, 20),
         (220, 20, 220), (20, 220, 220), (240, 240, 240), (10, 10, 10)]


def band_of(img, tolerance: int = 12) -> int:
    """Which BANDS index this frame is, by its centre pixel.

    A tolerance because these frames have been through a lossy codec and a
    yuv420p round trip: (220, 20, 20) comes back as (220, 20, 19). -1 for a
    colour that is none of the bands."""
    px = img.convert("RGB").getpixel((img.width // 2, img.height // 2))
    for i, want in enumerate(BANDS):
        if all(abs(a - b) <= tolerance for a, b in zip(px, want)):
            return i
    return -1


def write_video(path, seconds: float = 2.0, fps: int = 10, size: int = 32,
                codec: str = "libx264", metadata: dict = None, bands=None):
    """A real, tiny video file on disk. Returns `path`.

    Real rather than faked, because everything lens does with a video happens
    inside a decoder — the container's headers, seeking, the frames that come
    back — and a stub for any of that would be a test of the stub. Two seconds
    of 32×32 costs about a millisecond to write and a millisecond to read.

    `gop_size = 1` makes every frame a keyframe, which is deliberate: a seek
    lands on the keyframe at or before the target, so with a default GOP six
    samples of a two-second clip come back as two distinct pictures and nothing
    can be said about *where* they were taken from. Every frame being seekable is
    what lets a test assert the sampling grid itself. (Real files are not like
    this, and lens.video documents what it does about that.)"""
    import av

    bands = list(bands if bands is not None else BANDS)
    with av.open(str(path), "w") as container:
        stream = container.add_stream(codec, rate=fps)
        stream.width = stream.height = size
        stream.pix_fmt = "yuv420p"
        stream.codec_context.gop_size = 1
        if metadata:
            container.metadata.update(metadata)
        total = max(1, int(round(seconds * fps)))
        for n in range(total):
            colour = bands[min(len(bands) - 1, int(n / total * len(bands)))]
            frame = np.zeros((size, size, 3), dtype=np.uint8)
            frame[:, :] = colour
            for packet in stream.encode(
                    av.VideoFrame.from_ndarray(frame, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.fixture
def video_file(tmp_path):
    """`make(name, **kw)` → a written video under tmp_path."""
    def make(name="clip.mp4", **kw):
        return write_video(tmp_path / name, **kw)
    return make
