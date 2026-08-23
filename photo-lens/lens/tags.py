"""Auto-tags: what a photo looks like, named from a fixed vocabulary.

The details panel can say when and where a photo was taken because the camera
wrote it down. Nothing in EXIF says *what is in the picture* — and the library
already holds a vector for every photo that does. Scoring that vector against a
small set of pre-embedded concept labels turns the embedding the search already
depends on into something readable.

The labels are deliberately the sort of thing a person types into the search
box, and they are embedded through `query.text_prompt` — the very same sentence
the search does. That is what makes a tag chip clickable: the score behind the
chip and the score behind the search it runs are the same number.
"""

import threading

import numpy as np

from lens import query

# ~70 concepts, chosen to cover what a personal library actually holds: the
# subjects (people, animals, food), the places (indoors, cities, landscape),
# and — because most of a home folder is not photographs — the software-made
# images too, so a screenshot is labelled a screenshot rather than mislabelled
# a document.
VOCAB = [
    # people
    "people", "portrait", "selfie", "baby", "wedding", "crowd",
    # animals
    "dog", "cat", "bird", "horse", "fish",
    # food and drink
    "food", "coffee", "cocktail", "restaurant", "fruit", "cake",
    # water and landscape
    "beach", "ocean", "waves", "mountains", "forest", "waterfall", "lake",
    "river", "desert", "snow", "sunset", "sunrise", "night sky", "clouds",
    "rain", "flowers", "palm trees", "garden",
    # built environment
    "city street", "skyline", "building", "temple", "church", "market",
    "bridge", "road", "airport", "train", "boat", "car", "motorbike",
    "bicycle",
    # interiors
    "indoor room", "kitchen", "bedroom", "office desk", "swimming pool",
    "hotel room",
    # doing something
    "concert", "sports", "surfing", "hiking", "dancing", "yoga",
    # made by software, not by a camera
    "screenshot", "document", "chart", "logo", "artwork", "poster", "map",
    "computer screen",
    # odds and ends that are unmistakable when they are there
    "statue", "fireworks",
]

# How many chips the panel gets. Six is what fits on two lines beside a photo,
# and past the sixth the scores are indistinguishable from the vocabulary's
# noise floor anyway.
TOP_K = 6

# ...and how far behind the best label the sixth is allowed to be.
#
# Same reasoning as query.RELEVANCE_RATIO, for the same reason: these cosine
# scores are small positive numbers whose absolute range belongs to the model,
# so a fixed threshold would transfer to no other model and to no other corpus.
# The *proportion* of this photo's own best label does.
#
# Measured over all 86 photographs of the reference library, this cut yields
# 1/2/3/4/5/6 labels for 11/9/19/11/12/24 of them — a mean of 3.9, and the
# spread is the point. A photo of an airport concourse gets one label because
# one concept dominates it; a group shot on a terrace gets six because six
# genuinely apply. A fixed count would have printed five wrong labels under the
# first and truncated the second.
TAG_RATIO = 0.66


class TagIndex:
    """The vocabulary, embedded once.

    Built lazily and at most once: the first call pays for ~70 forward passes
    through the text tower (a few seconds on MPS), and the daemon spends that
    at start-up, on the same background thread that warms the text encoder, so
    it never lands on a click. `_lock` makes a second caller wait for the first
    rather than start a second build — the daemon serves requests on a thread
    per connection, and two /tags calls can arrive together."""

    def __init__(self, embedder, vocab=None):
        self.embedder = embedder
        self.labels = list(vocab if vocab is not None else VOCAB)
        self._mat = None
        self._lock = threading.Lock()

    @property
    def built(self) -> bool:
        return self._mat is not None

    def build(self):
        """The (len(labels), dim) matrix of label vectors, computed once."""
        if self._mat is not None:
            return self._mat
        with self._lock:
            if self._mat is None:               # another thread may have won
                vecs = [self.embedder.embed_text(query.text_prompt(lab))
                        for lab in self.labels]
                self._mat = np.stack(vecs).astype(np.float32)
        return self._mat

    def top(self, vec, k: int = TOP_K, ratio: float = TAG_RATIO):
        """`[{"label", "score"}]` for one photo vector, best first.

        The best label always survives — it is this vocabulary's answer to
        "what is this", and returning nothing would be a claim we cannot make.
        Everything behind it has to earn its place against that best score."""
        mat = self.build()
        v = np.asarray(vec, dtype=np.float32)
        if v.ndim != 1 or v.shape[0] != mat.shape[1]:
            raise ValueError("vector does not match the label matrix")
        scores = mat @ v
        order = np.argsort(-scores)[:max(1, k)]
        top = float(scores[order[0]])
        cut = top * ratio if top > 0 else float("-inf")
        out = [{"label": self.labels[i], "score": float(scores[i])}
               for i in order if float(scores[i]) >= cut]
        # a non-positive best score means the vocabulary matched nothing at
        # all; `cut` is then -inf and the whole top-k would come back as if it
        # had. One label, honestly the closest, is the answer there.
        return out if top > 0 else out[:1]
