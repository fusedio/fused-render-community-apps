"""Faces: found in the thumbnails lens already rendered, described as vectors.

Two models, one after the other, both running on this machine and neither of
them ever told who anybody is:

  * **MTCNN** finds the faces — a box per face and a confidence with it. It is
    the detector, and it knows nothing about identity.
  * **InceptionResnetV1** (vggface2 weights) turns each face crop into a
    512-dimensional unit vector. Two crops of the same person land close
    together on that sphere; two different people land apart. That is the whole
    of "recognition" here: a distance, clustered afterwards (lens/persons.py).

Both come from `facenet-pytorch`, which is chosen over dlib/face_recognition
for one reason worth stating: dlib is a C++ build with CMake and Boost in front
of it, and a photo indexer that cannot be installed is not an indexer. These are
torch modules and arrive as a wheel.

Two facts about running them on an Apple laptop shape the module:

  * **The detector runs on the CPU, always.** MTCNN builds an image pyramid with
    `interpolate(mode="area")`, which lands on `adaptive_avg_pool2d`, and MPS
    raises `Adaptive pool MPS: input sizes must be divisible by output sizes` on
    every non-divisible scale — i.e. essentially every real photo. Measured on
    the reference library it costs 10–160ms per 512px thumbnail on the CPU
    anyway, so this is a limitation with no price attached. The *embedder* does
    run on MPS: its input is a fixed 160×160, where no such gap exists.
  * **MPS never gives memory back on its own**, so the cache is emptied after
    every batch — the same rule, and the same reason, as lens/embed.py.

Nothing here reads a file: it is handed the PIL image the indexer already has
(the 512px thumbnail, or a video's middle keyframe rendered to the same size).
"""

import threading

import numpy as np

# What a face vector is, and what generated it. Recorded per photo row
# (photos.faces_v) so that changing any of it re-detects the library instead of
# silently mixing two generations of vectors in one cluster: the detector's
# threshold decides *which* faces exist, and the embedder's weights decide what
# "close" means, so neither can move without invalidating the other's output.
DETECT_MODEL = "mtcnn"
EMBED_MODEL = "vggface2"
DIM = 512

# How sure the detector has to be that a thing is a face.
#
# High on purpose. A false positive is not a cosmetic problem here: it becomes a
# vector in the clustering, and a handful of "faces" that are actually door
# handles cluster together happily and arrive in the UI as a person. MTCNN's own
# scores on the reference library are bimodal — real faces come back at 0.95–1.0
# (measured: 0.998, 1.0, 1.0, 0.997 on portraits; 0.949/0.875/0.798 for the
# three faces in a group shot, the weakest of them a half-turned profile at the
# edge of frame) — so a cut here keeps the faces and drops the guesses.
MIN_PROB = 0.92

# Face crops are square, 160px, which is what the vggface2 network was trained
# on, and they are taken slightly wider than the detector's box: MTCNN boxes are
# tight around the features, and the recognition network was trained on crops
# that include some hair and jaw. Measured in fractions of the box rather than
# pixels so it means the same thing on a 40px face and a 400px one.
CROP = 160
MARGIN = 0.12

# Smallest picture worth handing to the detector, on its shorter side.
#
# Not a quality rule — a crash. MTCNN builds an image pyramid down from
# `12 / minsize` and stops at 12px, so a picture whose shorter side is under
# `minsize` (20) yields *no scales at all*, and the empty list of candidate boxes
# reaches `torch.cat()`, which raises "expected a non-empty list of Tensors". A
# real library is full of these: the first face pass over the reference library
# hit it six times, on a 2×10 screenshot, a 7×27 sprite, a 6×6 bullet point and
# three 16×16 favicons. Those rows were then retried, and failed identically, on
# every subsequent run.
#
# Anything smaller than this cannot contain a detectable face anyway, so the
# honest answer for it is "no faces" rather than an error counted against the
# photograph.
MIN_IMAGE = 20

# Faces per forward pass. Same size as the image embedder's batch and for the
# same reason: it is the point where a batch stops being a win and starts being
# peak memory (see lens/embed.py).
BATCH = 16

# Fed to the network after the crop: what facenet calls
# `fixed_image_standardization`. Restated here rather than imported so that the
# preprocessing lives beside the crop it applies to — the two together are what
# defines the vector, and importing half of it from a library whose pins we
# already override would be one indirection too many.
_PIXEL_MEAN = 127.5
_PIXEL_SCALE = 128.0

# The one thing to say when the dependency is not installed. facenet-pytorch
# declares pins that are two years stale (torch<2.3, numpy<2, Pillow<10.3), and
# honouring them downgrades torch, numpy and Pillow underneath the rest of lens
# — pillow-heif then refuses to load and HEIC photos stop being readable. The
# code itself runs fine on current torch, so it is installed without its
# declared dependencies, which are all satisfied by lens's own.
INSTALL_HINT = ("Face detection needs facenet-pytorch. Install it without its "
                "stale pins:  pip install --no-deps facenet-pytorch")

# One thread at a time may bring the weights up — module-level, for exactly the
# reason lens/embed.py's lock is: what needs serializing is the `import
# facenet_pytorch` inside the load, which is process-global state, not any field
# on the object below.
_LOAD_LOCK = threading.Lock()


def _normalize(x):
    """Unit-length rows, in float16 — the same storage contract as an image
    embedding (see embed._normalize), so cosine similarity is a bare dot
    product for faces too."""
    x = x / np.clip(np.linalg.norm(x, axis=-1, keepdims=True), 1e-8, None)
    return x.astype(np.float16)


def crop_face(img, bbox, size: int = CROP, margin: float = MARGIN):
    """The square crop a face vector is computed from — and the same crop the
    UI shows as a person's cover.

    `bbox` is normalized (x0, y0, x1, y1) in 0–1, never pixels: a box is stored
    once and then read against a 512px thumbnail today and a 2048px render
    tomorrow, and pixel coordinates would silently describe the wrong part of
    the second one.

    Square because the network wants square: taking the longer side and
    centring on the box means a wide box is padded rather than a tall face being
    squashed. The crop is allowed to run off the edge of the image — PIL fills
    that with black, which is the honest thing for a face at the frame's edge,
    and cheaper than the alternative of shrinking the box until it fits (which
    would silently cut the chin off every edge face)."""
    w, h = img.size
    x0, y0, x1, y1 = (float(v) for v in bbox)
    px0, py0, px1, py1 = x0 * w, y0 * h, x1 * w, y1 * h
    cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
    side = max(px1 - px0, py1 - py0) * (1 + 2 * margin)
    side = max(side, 8.0)                  # a 2px "face" is not a crop
    half = side / 2
    box = (int(round(cx - half)), int(round(cy - half)),
           int(round(cx + half)), int(round(cy + half)))
    return img.convert("RGB").crop(box).resize((size, size))


class FaceModel:
    """Detector + embedder, loaded once, on first use.

    `key` is what the catalog records against every row this model scanned, so a
    change to the models or to MIN_PROB is a re-detect rather than a library
    holding two incompatible generations of vectors (see indexer._index_faces).
    """

    dim = DIM

    def __init__(self):
        self._mtcnn = None
        self._resnet = None
        self._device = None
        self.key = f"{DETECT_MODEL}@{MIN_PROB}+{EMBED_MODEL}"

    @property
    def loaded(self) -> bool:
        return self._resnet is not None

    def load(self):
        """Checked twice around the lock: the common call is per-photo and must
        not take a lock to learn there is nothing to do, and the check inside is
        what stops two threads loading two copies of the weights."""
        if self._resnet is not None:
            return
        with _LOAD_LOCK:
            if self._resnet is not None:
                return
            self._load_now()

    def _load_now(self):
        try:
            from facenet_pytorch import MTCNN, InceptionResnetV1
        except ImportError as exc:
            raise RuntimeError(f"{INSTALL_HINT} ({exc})") from exc
        import torch
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        # keep_all: every face in the frame, not the biggest one — a photo of
        # three people is three faces or it is nothing. post_process=False
        # because we never use MTCNN's own crops: the boxes are what we want,
        # and crop_face above is what produces what the embedder sees.
        # The detector is CPU-only whatever the embedder runs on (see module
        # docstring: MPS has no non-divisible adaptive pool). Measured cost of
        # the pair on the reference library: 0.127s per photograph, detection
        # and embedding together.
        self._mtcnn = MTCNN(keep_all=True, post_process=False, device="cpu")
        # `_resnet` last, and only once the detector exists: it is what `loaded`
        # and the double-check above read, so assigning it earlier would publish
        # a half-built model to the other thread.
        self._resnet = (InceptionResnetV1(pretrained=EMBED_MODEL)
                        .to(self._device).eval())

    def _release(self):
        """Hand MPS's cached blocks back after a batch. Best-effort, same as
        embed.Embedder._release — failing to free is not a reason to fail."""
        try:
            import torch
            if self._device == "mps" and hasattr(torch, "mps"):
                torch.mps.empty_cache()
        except Exception:
            pass

    def detect(self, img) -> list:
        """`[{"bbox": (x0, y0, x1, y1), "prob": p}]` for one image, boxes
        normalized to 0–1 and sorted left-to-right.

        Sorted so that the order is a property of the picture rather than of the
        detector's internals: the boxes are about to become rows in a table, and
        "the second face in this photo" should mean the same thing on two runs.

        A photo with no faces comes back as `[]`, which is a fact about it, not
        a failure — most of a photo library is not portraits. So does a picture
        too small to hold one (see MIN_IMAGE), and that case is checked before
        the model is even loaded: it is the answer for an icon, and it must not
        cost the weights on a library made entirely of them."""
        if min(img.size) < MIN_IMAGE:
            return []
        self.load()
        boxes, probs = self._mtcnn.detect(img)
        if boxes is None:
            return []
        w, h = img.size
        out = []
        for box, prob in zip(boxes, probs if probs is not None else []):
            if prob is None or float(prob) < MIN_PROB:
                continue
            x0, y0, x1, y1 = (float(v) for v in box)
            # A box can extend past the frame (MTCNN regresses it, it does not
            # clamp it), and a normalized coordinate outside 0–1 would make
            # every later crop and every stored fraction meaningless.
            x0, x1 = max(0.0, x0 / w), min(1.0, x1 / w)
            y0, y1 = max(0.0, y0 / h), min(1.0, y1 / h)
            if x1 <= x0 or y1 <= y0:
                continue                    # a box entirely off the frame
            out.append({"bbox": (x0, y0, x1, y1), "prob": float(prob)})
        out.sort(key=lambda f: (f["bbox"][0], f["bbox"][1]))
        return out

    def embed(self, crops) -> np.ndarray:
        """`(N, 512)` unit-length float16 for a list of face crops.

        `(0, 512)` for no crops rather than an empty array of no shape: the
        caller stacks this into the faces matrix, and a shapeless empty would
        make that raise on exactly the photos that have nobody in them."""
        if not crops:
            return np.zeros((0, DIM), dtype=np.float16)
        self.load()
        import torch
        out = []
        for i in range(0, len(crops), BATCH):
            batch = np.stack([
                np.asarray(c.convert("RGB").resize((CROP, CROP)),
                           dtype=np.float32)
                for c in crops[i:i + BATCH]])
            t = torch.from_numpy(
                (batch - _PIXEL_MEAN) / _PIXEL_SCALE).permute(0, 3, 1, 2)
            t = t.to(self._device)
            with torch.no_grad():
                feats = self._resnet(t)
            out.append(feats.float().cpu().numpy())
            del t, feats
            self._release()
        return _normalize(np.concatenate(out, axis=0))


_MODEL = None
_MODEL_LOCK = threading.Lock()


def model() -> FaceModel:
    """The process-wide face model. Constructing one loads nothing — the weights
    arrive on the first detect — so this is safe to call from anywhere,
    including a daemon that may never index a photo with a face in it."""
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                _MODEL = FaceModel()
    return _MODEL


def detect_faces(img) -> list:
    return model().detect(img)


def embed_faces(crops) -> np.ndarray:
    return model().embed(crops)
