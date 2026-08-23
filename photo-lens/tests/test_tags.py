"""The concept vocabulary behind /tags.

A tag chip is a claim about what is in a picture, and it is clickable — the
score behind the chip and the score behind the search it runs have to be the
same number. That makes two things load-bearing: the labels must be embedded
through the *same* sentence the search embeds, and the vocabulary must be
embedded once rather than once per panel that opens.
"""

import threading
import time

import numpy as np
import pytest

from lens import query
from lens.tags import TAG_RATIO, TOP_K, VOCAB, TagIndex

SMALL = ["dog", "cat", "beach", "sunset", "food", "city street", "boat"]


class LabelEmbedder:
    """A distinct unit vector per label, so a ranking is actually checkable.

    FakeEmbedder's constant vector scores every label alike, which is right for
    the daemon's plumbing tests and useless for the ordering ones: with one-hot
    labels a photo vector's components *are* its per-label scores, so a test can
    name the answer it expects. Calls are recorded (under a lock — two threads
    can ask at once) because "the vocabulary is embedded once" is a claim about
    how many times this was called."""

    def __init__(self, vocab=SMALL, delay=0.0):
        self.vocab = list(vocab)
        self.prompts = [query.text_prompt(v) for v in self.vocab]
        self.seen = []
        self.delay = delay
        self._lock = threading.Lock()

    def embed_text(self, text):
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.seen.append(text)
        v = np.zeros(len(self.vocab), dtype=np.float32)
        v[self.prompts.index(text)] = 1.0
        return v


def _vec(*scores):
    """A photo vector that scores label i exactly `scores[i]` (see LabelEmbedder)."""
    return np.array(scores, dtype=np.float32)


def test_top_ranks_labels_and_cuts_relative_to_the_best():
    """`[{"label", "score"}]`, best first, and everything kept is within
    TAG_RATIO of the top score.

    The cut is proportional for the same reason query.RELEVANCE_RATIO is: these
    cosine scores are small positive numbers whose absolute range belongs to the
    model, so a fixed threshold would transfer to no other model and no other
    corpus. Six chips of which four describe the photo and two are the
    vocabulary's noise floor is the failure this prevents."""
    emb = LabelEmbedder()
    idx = TagIndex(emb, vocab=SMALL)
    out = idx.top(_vec(0.9, 0.8, 0.7, 0.6, 0.2, 0.1, 0.05))

    assert [t["label"] for t in out] == ["dog", "cat", "beach", "sunset"]
    assert all(set(t) == {"label", "score"} for t in out)
    scores = [t["score"] for t in out]
    assert scores == sorted(scores, reverse=True)
    assert all(s >= out[0]["score"] * TAG_RATIO for s in scores)
    # the 0.2 plateau is behind the cut, not merely last
    assert "food" not in [t["label"] for t in out]


def test_top_is_capped_at_top_k():
    """Six is what fits beside a photo, so a vector that likes everything
    equally still gets six chips rather than the whole vocabulary."""
    idx = TagIndex(LabelEmbedder(), vocab=SMALL)
    out = idx.top(np.ones(len(SMALL), dtype=np.float32))
    assert len(SMALL) > TOP_K                      # there was something to cut
    assert len(out) == TOP_K
    assert len({t["label"] for t in out}) == TOP_K


def test_a_non_positive_best_score_returns_exactly_one_label():
    """No positive score is no signal to take a fraction of: `top × ratio` is
    then *above* the top, so the ratio cut cannot decide anything (the code
    sets the cut to -inf, which would let the entire top-k through as if every
    label had earned it). One label — honestly the closest — is the answer, and
    returning none would be a claim we cannot make either."""
    idx = TagIndex(LabelEmbedder(), vocab=SMALL)
    out = idx.top(_vec(-0.3, -0.1, -0.2, -0.4, -0.5, -0.6, -0.7))
    assert [t["label"] for t in out] == ["cat"]    # the least-bad one
    assert out[0]["score"] < 0


def test_a_vector_of_the_wrong_shape_is_refused():
    """A vector from another model's tower, or a whole matrix passed by
    mistake, would otherwise broadcast into scores that mean nothing."""
    idx = TagIndex(LabelEmbedder(), vocab=SMALL)
    with pytest.raises(ValueError):
        idx.top(np.ones(3, dtype=np.float32))
    with pytest.raises(ValueError):
        idx.top(np.ones((2, len(SMALL)), dtype=np.float32))


def test_labels_are_embedded_as_captions_not_bare_nouns():
    """The tag score and the score of the search the chip runs must be the same
    number, so the label goes through query.text_prompt exactly as the search's
    residual does. A bare noun is also off SigLIP's caption distribution."""
    emb = LabelEmbedder()
    TagIndex(emb, vocab=SMALL).build()
    assert emb.seen == ["a photo of a dog", "a photo of a cat",
                        "a photo of a beach", "a photo of a sunset",
                        "a photo of a food", "a photo of a city street",
                        "a photo of a boat"]
    assert "dog" not in emb.seen


def test_the_vocabulary_is_embedded_once_under_concurrent_callers():
    """~70 forward passes through the text tower is a few seconds. The daemon
    pays it once at start-up, on a thread per connection — so two /tags calls
    arriving together must make the second *wait* for the first build, not start
    a second one."""
    emb = LabelEmbedder(delay=0.002)               # long enough to overlap
    idx = TagIndex(emb, vocab=SMALL)
    assert idx.built is False

    ready = threading.Barrier(2)
    outs = []

    def worker():
        ready.wait()
        outs.append(idx.top(_vec(0.9, 0.8, 0.1, 0.1, 0.1, 0.1, 0.1)))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    for _ in range(5):                             # and every later caller
        idx.build()
        idx.top(np.ones(len(SMALL), dtype=np.float32))

    assert len(emb.seen) == len(SMALL), emb.seen
    assert idx.built is True
    # both threads got the same answer off the one matrix
    assert outs[0] == outs[1]


def test_the_shipped_vocabulary_has_no_duplicate_labels():
    """A duplicate would spend two of the six chips saying one thing, and the
    second copy would be unreachable noise in the matrix."""
    assert len(set(VOCAB)) == len(VOCAB)
    assert all(v and v == v.strip() for v in VOCAB)
    # the default is a copy, so an index cannot edit the module's list
    idx = TagIndex(LabelEmbedder(vocab=VOCAB))
    assert idx.labels == VOCAB
    idx.labels.append("something else")
    assert "something else" not in VOCAB
