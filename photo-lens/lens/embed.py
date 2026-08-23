import json
import os
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request

import numpy as np

MODELS = {
    "siglip2": ("google/siglip2-so400m-patch14-384", 1152),
    "clip-b32": ("openai/clip-vit-base-patch32", 512),
}


# One thread at a time may bring the weights up (see Embedder.load).
#
# Module-level rather than per-instance because what needs serializing is not
# this object's fields — it is the `import transformers` inside the load, which
# is process-global state. Two threads entering that import together caught it
# half-initialized and one of them raised `cannot import name 'AutoModel'`; on
# the reference library that flagged five perfectly good photographs as
# unreadable, because an embed failure is recorded on the rows it was for (see
# indexer.flush). The daemon has exactly two threads that can reach it — the
# index run, and the first /query to arrive while it is still going — and that
# is precisely the collision.
_LOAD_LOCK = threading.Lock()


def _normalize(x):
    x = x / np.clip(np.linalg.norm(x, axis=-1, keepdims=True), 1e-8, None)
    return x.astype(np.float16)


def _as_tensor(out):
    # transformers versions differ: get_image_features/get_text_features may
    # return a plain tensor or a ModelOutput (with pooler_output) depending
    # on model class/version. Normalize to a plain tensor either way.
    return out.pooler_output if hasattr(out, "pooler_output") else out


class Embedder:
    def __init__(self, key: str = "siglip2"):
        if key not in MODELS:
            raise ValueError(f"unknown model {key!r}; options: {sorted(MODELS)}")
        self.key = key
        self.model_id, self.dim = MODELS[key]
        self._model = self._processor = self._device = None

    @property
    def loaded(self) -> bool:
        """False until the weights are in memory. The first load downloads a
        few GB from HuggingFace, which the UI needs to be able to explain."""
        return self._model is not None

    def load(self):
        """Bring the weights up, once, however many threads ask at once.

        Checked twice around the lock deliberately: the common call is a hot path
        (every batch, every query) and must not take a lock to discover there is
        nothing to do; the check *inside* is what stops the second thread through
        the door from loading a second copy of a multi-gigabyte model."""
        if self._model is not None:
            return
        with _LOAD_LOCK:
            if self._model is not None:
                return
            self._load_now()

    def _load_now(self):
        import torch
        from transformers import AutoModel, AutoProcessor
        self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        self._processor = AutoProcessor.from_pretrained(self.model_id)
        # `_model` last, and only once everything it needs is in place: it is
        # what `loaded` and the double-check above read, so assigning it earlier
        # would publish a half-built embedder to the other thread.
        self._model = AutoModel.from_pretrained(self.model_id).to(self._device).eval()

    def _release(self):
        """Hand the backend's cached blocks back after a batch.

        MPS caches every block it allocates and never returns it on its own, so
        a long index run climbs: indexing the reference library reached ~11GB
        resident on a 16GB machine, exhausted swap, and the daemon was killed
        two thirds of the way through — losing the run's work. Freeing per batch
        keeps the footprint flat. Best-effort: older torch builds have no such
        entry point, and failing to free is not a reason to fail the embed."""
        try:
            import torch
            if self._device == "mps" and hasattr(torch, "mps"):
                torch.mps.empty_cache()
        except Exception:
            pass

    def embed_images(self, imgs) -> np.ndarray:
        self.load()
        import torch
        out = []
        for i in range(0, len(imgs), 16):
            batch = self._processor(images=imgs[i:i + 16], return_tensors="pt")
            batch = {k: v.to(self._device) for k, v in batch.items()}
            with torch.no_grad():
                feats = _as_tensor(self._model.get_image_features(**batch))
            out.append(feats.float().cpu().numpy())
            del batch, feats
            self._release()
        return _normalize(np.concatenate(out, axis=0))

    def embed_text(self, text: str) -> np.ndarray:
        self.load()
        import torch
        batch = self._processor(
            text=[text], return_tensors="pt", padding="max_length", truncation=True)
        batch = {k: v.to(self._device) for k, v in batch.items()}
        with torch.no_grad():
            feats = _as_tensor(self._model.get_text_features(**batch))
        return _normalize(feats.float().cpu().numpy())[0]


# ── the embedder that has no model of its own ──────────────────────────────

# `POST /api/ai/embed` refuses a batch larger than this (fused-render's
# `ai/runners/embed_common.MAX_ITEMS`). Restated rather than discovered from a
# 400, because the indexer's own batch is counted in *images* and a video row
# can be six of them — the chunking below is what keeps a legal row from
# becoming an illegal request.
API_MAX_ITEMS = 64

# How long to keep waiting for weights that are still loading. so400m is
# 4.55GB: a cold load is tens of seconds even from a warm disk cache, and the
# first one on a fresh machine is a download. An index run has just decided to
# spend minutes of work, so the wrong answer here is to give up early — the
# only thing this bounds is a load that is never going to finish.
API_LOAD_DEADLINE_S = 20 * 60

# Gap between `/api/ai/runtime` polls while waiting for that load. Long enough
# that a twenty-minute wait is not a thousand requests, short enough that the
# progress line moves.
API_LOAD_POLL_S = 2.0

# The temp file's format, and it has to be a LOSSLESS one.
#
# JPEG was the obvious choice and it is measurably wrong. The trip through the
# API is an extra encode the daemon never did — the indexer holds decoded
# pixels and the endpoint takes paths — and re-embedding photographs this store
# already holds vectors for is a direct measurement of what that encode costs:
#
#   handed the thumbnail's own .webp   cosine 1.000000  (the reference)
#   re-encoded as PNG                  cosine 1.000000
#   re-encoded as JPEG quality 100     cosine 0.9985
#   re-encoded as JPEG quality 92      cosine 0.95 – 0.99
#
# 0.95 is not a rounding error in this space; it is the same order as the gap
# between two *different photographs of the same scene*, which means a vector
# written that way is a slightly different photograph as far as every future
# search is concerned. A PNG of a 512px thumb is a few hundred KB written to
# tmpfs and thrown away, which costs nothing next to a forward pass through a
# 4.55GB tower — so the lossless format is simply the correct one.
API_TEMP_FORMAT = ("PNG", "png")


def _flatten_white(img):
    """`thumbs._flatten`, restated for the one caller below.

    Not imported from `lens.thumbs`: that module pulls in `lens.video` and
    `lens.faces` (and through them av and torch) to do its own job, and this
    file is imported by anything that merely wants to embed a string. Six lines
    duplicated against a dependency chain that size is the right trade — the
    original is the definition, and this comment is the pointer back to it.
    """
    from PIL import Image
    transparent = (img.mode in ("RGBA", "LA", "PA")
                   or (img.mode in ("P", "L", "I", "1")
                       and "transparency" in img.info))
    if not transparent:
        return img.convert("RGB")
    img = img.convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, img).convert("RGB")


class EmbedApiError(RuntimeError):
    """The API refused, or answered something this store cannot use.

    A distinct type because the indexer's caller has to be able to tell "this
    photo could not be read" (recorded on the row, run continues) from "the
    embedding service is not usable" (the run must stop before it writes a
    single vector). Every raise below is the second kind.
    """


class EmbedCancelled(EmbedApiError):
    """The caller asked to stop while this class was waiting on a model load.

    A subclass so that anything catching `EmbedApiError` still stops — but a
    distinct type because it is not a failure: an index run cancelled during a
    cold load must report `cancelled`, not `error`. Worth having at all because
    that wait is the longest thing this class does (a 4.55GB download, the first
    time), and a ✕ pressed during it that did nothing for twenty minutes would
    be the worst cancel in the app.
    """


class ApiEmbedder:
    """`Embedder`'s interface, served by fused-render's own resident model.

    Why this exists: the local `Embedder` above owns a multi-gigabyte torch
    model, and the process that used to hold it — lens's daemon — is gone. The
    weights now live in fused-render, which already keeps exactly one copy
    resident for every page on the machine, and exposes them at
    `POST /api/ai/embed`. Indexing through that endpoint means an index run
    costs no second copy of so400m and no torch import at all.

    Three mismatches between that endpoint and what the indexer hands us, and
    each one is handled here so that `lens/indexer.py` needs no change:

      * **The API takes paths; the indexer has decoded images.** It has to:
        it decodes HEIC through pillow-heif and video frames through av, and a
        video row is frames that were never files. So every incoming PIL image
        is written to a temp JPEG and the *paths* are sent. One directory per
        batch, removed in a `finally` — a run of a real library is thousands of
        batches and a leak here fills a disk.
      * **The API caps a batch at `API_MAX_ITEMS`.** The indexer's batch is
        counted in images and can exceed it, so requests are chunked.
      * **A cold model is a 409, not a failure.** `model_loading` means the
        load has *started*; the honest response is to wait for it (see
        `_wait_for_model`), not to fail a run that is about to spend minutes.

    Accuracy note, because "same model, different engine" deserves a number
    rather than a promise: fused-render serves so400m through MLX on Apple
    Silicon while the torch `Embedder` used MPS, and re-embedding photographs
    this store already holds vectors for reproduces them at cosine 0.999999.
    The two engines are interchangeable for this index in the only sense that
    matters — a vector written by one ranks correctly against the other's.
    """

    def __init__(self, key: str = "siglip2", origin: str = None,
                 expect_dim: int = None, progress=None, should_stop=None):
        if key not in MODELS:
            raise ValueError(f"unknown model {key!r}; options: {sorted(MODELS)}")
        # `key` stays the SAME string the local embedder uses ("siglip2"), and
        # deliberately so: the indexer compares it against the catalog's
        # `embed_model` meta and re-embeds the entire library when it differs
        # (index_once's `model_changed`). Naming this embedder "siglip2-api"
        # would have thrown away 3,115 perfectly good vectors on the first run.
        self.key = key
        self.model_id, self.dim = MODELS[key]
        # The store's own dimensionality, when the caller knows it. A store
        # written by another model is not something to detect halfway through a
        # run by watching np.stack raise — it is a refusal before the first
        # vector, which is what `_check_dim` makes it.
        self.expect_dim = int(expect_dim) if expect_dim else None
        if self.expect_dim and self.expect_dim != self.dim:
            raise EmbedApiError(
                f"this library's vectors are {self.expect_dim}-dimensional but "
                f"{self.model_id} produces {self.dim} — indexing it with this "
                "model would corrupt the index")
        self.origin = (origin or api_origin() or "").rstrip("/")
        if not self.origin:
            raise EmbedApiError(
                "no fused-render origin to embed against — FUSED_RENDER_ORIGIN "
                "is unset (a page's subprocess inherits it; a shell does not, "
                "so set it, or pass --origin)")
        # Called with one sentence whenever this class is doing something the
        # caller's own progress line cannot see — a model load, chiefly, which
        # is otherwise thirty silent seconds before the first photo.
        self._progress = progress
        # Asked between polls of a model load, and nowhere else: a batch that
        # is already in flight is one forward pass and there is nothing to gain
        # by abandoning it. `None` means "never stop", which is what every
        # caller that is not a cancellable job wants.
        self._should_stop = should_stop
        self._loaded = False

    # ── the Embedder interface ────────────────────────────────────────────
    @property
    def loaded(self) -> bool:
        """True once this process has had an answer out of the model.

        Not "the weights are resident": that is knowable (`/api/ai/runtime`
        says so) but it is a request, and `loaded` is read to decide whether to
        print "loading model…". The first successful embed is the only
        observation this class needs, and it is free."""
        return self._loaded

    def load(self):
        """Make sure the weights are up, waiting for a load already underway.

        Sends the cheapest legal request there is — one short text — precisely
        so the 409 that means "loading" arrives here, at a moment nothing is
        waiting on it, rather than in the middle of the first image batch."""
        if self._loaded:
            return
        self._post({"texts": ["a photograph"]})
        self._loaded = True

    def embed_images(self, imgs) -> np.ndarray:
        """(N, dim) float16, unit-normalized, for N decoded PIL images."""
        if not len(imgs):
            return np.zeros((0, self.dim), dtype=np.float16)
        out = []
        for i in range(0, len(imgs), API_MAX_ITEMS):
            chunk = imgs[i:i + API_MAX_ITEMS]
            tmp = tempfile.mkdtemp(prefix="lens-embed-")
            try:
                paths = [self._write_image(img, tmp, n)
                         for n, img in enumerate(chunk)]
                out.append(self._vectors(self._post({"paths": paths}),
                                         len(paths)))
            finally:
                # Unconditional: the failure paths above are the ones that
                # matter, because a run that dies on a bad photo will be
                # started again and again.
                shutil.rmtree(tmp, ignore_errors=True)
        # Normalized here even though the API's vectors are already unit: the
        # concatenation is what goes to disk, `_normalize` is also the float16
        # cast the stored matrix is defined in, and one code path for both means
        # the API's rows and the torch embedder's rows are the same rows.
        return _normalize(np.concatenate(out, axis=0))

    def embed_text(self, text: str) -> np.ndarray:
        return _normalize(self._vectors(self._post({"texts": [str(text)]}), 1))[0]

    # ── internals ─────────────────────────────────────────────────────────
    def _write_image(self, img, directory: str, n: int) -> str:
        """One image, on disk, losslessly (see API_TEMP_FORMAT).

        Written rather than passed by path even when the caller *has* a path,
        because the caller often does not: a video row's frames were never
        files, and a HEIC arrives here already decoded. One code path for both
        is what keeps "what the encoder saw" a single answer.

        Transparency is composited onto *white* rather than dropped, which is
        `thumbs._flatten`'s rule and has to be: `convert("RGB")` leaves
        whatever Pillow initialised the backdrop to, which is black, and that
        turned every transparent PNG into a black rectangle in the vector once
        already. In practice the indexer hands this method flattened thumbs and
        RGB video frames, so the branch is a guard rather than a hot path — but
        it is the guard that stops one caller from writing rows nothing else in
        the store agrees with.
        """
        fmt, ext = API_TEMP_FORMAT
        path = os.path.join(directory, f"{n:04d}.{ext}")
        _flatten_white(img).save(path, fmt)
        return path

    def _vectors(self, result: dict, want: int) -> np.ndarray:
        dim = int(result.get("dim") or 0)
        self._check_dim(dim)
        rows = result.get("vectors") or []
        if len(rows) != want:
            raise EmbedApiError(
                f"asked {self.model_id} for {want} vectors and got {len(rows)}")
        vecs = np.asarray(rows, dtype=np.float32)
        if vecs.ndim != 2 or vecs.shape[1] != dim:
            raise EmbedApiError(
                f"{self.model_id} answered a {vecs.shape} block for {want} "
                f"items of {dim} dimensions")
        return vecs

    def _check_dim(self, dim: int):
        """Refuse loudly on the wrong width, always, never write the rows.

        This is the one failure that is silent if it is not checked: a
        differently-shaped vector appended to `embeddings.npz` either raises
        somewhere unrelated (np.stack, much later) or — if the widths happen to
        agree — ranks as noise forever with nothing to see. The store's
        dimensionality is not a preference, it is what every vector already in
        it means.
        """
        if dim != self.dim:
            raise EmbedApiError(
                f"{self.model_id} answered {dim}-dimensional vectors but this "
                f"index is {self.dim}-dimensional — refusing to write rows "
                "that would corrupt it")
        if self.expect_dim and dim != self.expect_dim:
            raise EmbedApiError(
                f"this library's vectors are {self.expect_dim}-dimensional and "
                f"the embedding service answered {dim} — refusing to mix them")

    def _post(self, payload: dict) -> dict:
        """One `/api/ai/embed` call, waiting out a load, returning `result`."""
        body = dict(payload)
        body["model"] = self.model_id
        deadline = time.monotonic() + API_LOAD_DEADLINE_S
        said = False
        while True:
            answer, error, status = self._post_once(body)
            if answer is not None:
                self._loaded = True
                return answer
            kind = (error or {}).get("type") or ""
            message = (error or {}).get("message") or f"HTTP {status}"
            if kind != "model_loading":
                # `unavailable` (no embeddings runner on this machine),
                # `bad_request`, `ai_error` — none of them get better by being
                # retried, and an index run must stop rather than log thousands
                # of them.
                raise EmbedApiError(f"{kind or 'embed failed'}: {message}")
            if time.monotonic() > deadline:
                raise EmbedApiError(
                    f"{self.model_id} was still loading after "
                    f"{API_LOAD_DEADLINE_S // 60} minutes: {message}")
            self._check_stop()
            if not said:
                # Said once, not per poll: this is the thirty-second silence at
                # the start of a run, and the caller's own progress line has no
                # way to know why nothing is happening yet.
                self._note(f"loading {self.model_id} ({message})")
                said = True
            self._wait_for_model(deadline)

    def _post_once(self, body: dict):
        """`(result, error, status)` — exactly one of the first two is set.

        The 409 that carries `model_loading` is an HTTPError with a JSON body,
        so the body is read off the exception rather than treated as a network
        failure; a genuine network failure (nothing listening) is the one case
        that comes back as an error dict this class invented, because there is
        no server answer to quote."""
        request = urllib.request.Request(
            f"{self.origin}/api/ai/embed",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Fused": "1"})
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                parsed = json.load(response)
            return (parsed.get("result") or {}), None, 200
        except urllib.error.HTTPError as exc:
            try:
                parsed = json.loads(exc.read() or b"{}")
            except ValueError:
                parsed = {}
            error = parsed.get("error")
            if not isinstance(error, dict):
                error = {"type": "", "message": f"{exc.code} {exc.reason}"}
            return None, error, exc.code
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, {"type": "no_server",
                          "message": f"{self.origin} did not answer ({exc})"}, 0

    def _wait_for_model(self, deadline: float):
        """Sleep until `/api/ai/runtime` calls this model ready (or time is up).

        Polling the runtime rather than just re-POSTing on a timer because the
        runtime answer distinguishes the two ways a wait ends badly: a load
        that FAILED reports `state: "error"` with a reason, which is a sentence
        worth showing, and would otherwise present as twenty minutes of 409s.
        """
        while time.monotonic() < deadline:
            time.sleep(API_LOAD_POLL_S)
            self._check_stop()
            entry = self._runtime_entry()
            if entry is None:
                return                       # gone from the list: retry the POST
            state = entry.get("state") or ""
            if state == "ready":
                return
            if state == "error":
                raise EmbedApiError(
                    f"{self.model_id} failed to load: "
                    f"{entry.get('error') or 'no reason given'}")
            detail = entry.get("detail")
            if detail:
                self._note(f"loading {self.model_id}: {detail}")

    def _runtime_entry(self):
        """This model's entry in `/api/ai/runtime`'s `loaded` list, or None."""
        try:
            with urllib.request.urlopen(
                    f"{self.origin}/api/ai/runtime", timeout=30) as response:
                described = json.load(response)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        for entry in described.get("loaded") or []:
            if entry.get("model") == self.model_id:
                return entry
        return None

    def _check_stop(self):
        if self._should_stop and self._should_stop():
            raise EmbedCancelled(
                f"stopped while waiting for {self.model_id} to load")

    def _note(self, message: str):
        if self._progress:
            self._progress(message)
        else:
            print(f"lens: {message}", flush=True)


def api_origin() -> str:
    """Where fused-render is, for a process that wants to embed.

    `FUSED_RENDER_ORIGIN` is set by the server on itself, so anything it spawns
    — a `fused.runPython` data file, and in turn anything *that* spawns —
    inherits it. Nothing here guesses a port: a wrong origin is either a
    connection refused (fine, if noisy) or, far worse, a *different*
    fused-render on the same machine serving a different model.
    """
    return (os.environ.get("FUSED_RENDER_ORIGIN")
            or os.environ.get("LENS_FUSED_ORIGIN") or "").strip()
