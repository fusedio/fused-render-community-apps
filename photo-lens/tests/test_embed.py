import os
import threading
import time

import numpy as np
import pytest
from PIL import Image

# The real model is several gigabytes and a download; these run on request.
model_test = pytest.mark.skipif(
    os.environ.get("LENS_MODEL_TESTS") != "1",
    reason="set LENS_MODEL_TESTS=1 to run model tests")


@model_test
def test_shapes_norms_and_sanity():
    from lens.embed import Embedder
    e = Embedder("siglip2")
    red = Image.new("RGB", (384, 384), "red")
    blue = Image.new("RGB", (384, 384), "blue")
    mat = e.embed_images([red, blue])
    assert mat.shape == (2, e.dim) and mat.dtype == np.float16
    assert np.allclose(np.linalg.norm(mat.astype(np.float32), axis=1), 1.0, atol=1e-2)
    t_red = e.embed_text("a solid red image").astype(np.float32)
    sims = mat.astype(np.float32) @ t_red
    assert sims[0] > sims[1]          # red image closer to "red" text


def test_only_one_thread_ever_brings_the_weights_up(monkeypatch):
    """`load()` used to be a bare `if self._model is None` and a `from
    transformers import AutoModel` — and the daemon has exactly two threads that
    reach it at once: the index run, and the first /query to arrive while that run
    is still going. Both entered the import together, one of them caught the
    module half-initialised, and it raised `cannot import name 'AutoModel'`.

    That is not a loading error where it lands: an embed failure is recorded on
    the rows the batch was for (see indexer.flush), so five perfectly good
    photographs in the reference library were flagged unreadable by a race.

    The load itself is stubbed — the point under test is the mutual exclusion, not
    what transformers does inside it — but `load()` is the real one, so the
    double-check and the lock are both exercised."""
    from lens.embed import Embedder
    e = Embedder("clip-b32")
    started, done = [], []

    def slow_load():
        started.append(time.monotonic())
        time.sleep(0.05)                 # wide enough for a second thread to race
        e._processor = object()
        e._model = object()               # published last, as the real one does
        done.append(time.monotonic())

    monkeypatch.setattr(e, "_load_now", slow_load)
    threads = [threading.Thread(target=e.load) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not any(t.is_alive() for t in threads)
    assert len(started) == 1, "the weights were loaded more than once"
    assert e.loaded


def test_a_loaded_embedder_reloads_nothing(monkeypatch):
    """The common call is a hot path — every batch, every query — so it must not
    take the lock just to find there is nothing to do."""
    from lens.embed import Embedder
    e = Embedder("clip-b32")
    e._model = object()
    monkeypatch.setattr(e, "_load_now",
                        lambda: pytest.fail("reloaded an already-loaded model"))
    e.load()
