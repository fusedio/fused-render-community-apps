"""The face model's own contract: the crop geometry, and — only when asked for —
the real networks.

The models are not exercised by default. Two downloads and a few hundred
megabytes of weights, whose answers depend on photographs of real people, are not
something a unit suite should need; what everything downstream depends on is the
*shape* of their answers, and that is what the fake in tests/conftest.py provides.
`LENS_MODEL_TESTS=1` runs the one test that checks the real pair loads and
answers in that shape.
"""
import os

import numpy as np
import pytest
from PIL import Image

from lens import faces


def _img(size=(200, 100), colour=(90, 120, 200)):
    return Image.new("RGB", size, colour)


def test_crop_face_is_square_and_centred_on_the_box():
    """Square because the network wants square — and centred on the box rather
    than on the image, so a face at the edge is still a face and not a slice of
    the wall beside it."""
    img = _img((200, 100))
    for x in range(60, 100):                  # a red patch where the "face" is
        for y in range(20, 60):
            img.putpixel((x, y), (255, 0, 0))
    crop = faces.crop_face(img, (0.3, 0.2, 0.5, 0.6), size=64, margin=0.0)

    assert crop.size == (64, 64)
    # the middle of the crop is the middle of the box
    assert crop.getpixel((32, 32)) == (255, 0, 0)


def test_crop_face_takes_the_longer_side_so_a_wide_box_is_not_squashed():
    """A tall box in a wide picture must come back as a padded square, not as a
    stretched face: distortion is exactly the thing the recognition network was
    not trained on."""
    img = _img((400, 400))
    tall = faces.crop_face(img, (0.45, 0.10, 0.55, 0.90), size=80, margin=0.0)
    assert tall.size == (80, 80)


def test_crop_face_survives_a_box_that_runs_off_the_frame():
    """MTCNN regresses boxes and does not clamp them, and a face at the edge of a
    photograph is the common case. Off-frame area comes back black rather than
    the crop being shrunk to fit — shrinking would silently cut the chin off
    every edge face."""
    img = _img((100, 100), (10, 200, 10))
    crop = faces.crop_face(img, (0.0, 0.0, 0.15, 0.15), size=32, margin=0.5)
    assert crop.size == (32, 32)
    assert crop.getpixel((2, 2)) == (0, 0, 0)          # outside the picture
    assert crop.getpixel((28, 28)) == (10, 200, 10)    # inside it


def test_crop_face_never_asks_pillow_for_a_zero_sized_crop():
    """A degenerate box — two identical corners, which a damaged catalog row can
    hold — must not raise out of the middle of an index run."""
    crop = faces.crop_face(_img((50, 50)), (0.5, 0.5, 0.5, 0.5), size=16)
    assert crop.size == (16, 16)


def test_the_module_singleton_is_one_object_and_loads_nothing(real_face_lookup):
    """Constructing the model must not touch the weights: the daemon builds one
    at startup and a library of nothing but landscapes never pays for it."""
    a, b = faces.model(), faces.model()
    assert a is b
    assert a.loaded is False
    assert a.dim == faces.DIM
    # the key records the threshold as well as the two model names: changing
    # MIN_PROB changes which faces exist, so it has to invalidate a scan
    assert str(faces.MIN_PROB) in a.key


def test_a_picture_too_small_to_hold_a_face_is_answered_without_the_models():
    """An icon is not a failure, and it is not worth a model load either.

    This is the crash MIN_IMAGE exists for: MTCNN's pyramid produces no scales
    for a picture under 20px on its shorter side, and the empty candidate list
    reaches torch.cat(). The reference library hit it six times — a 2x10
    screenshot, a 7x27 sprite, a 6x6 bullet point and three 16x16 favicons — and
    those rows were retried and failed identically on every run after."""
    model = faces.FaceModel()
    for size in ((2, 10), (16, 16), (7, 27), (6, 6), (19, 400)):
        assert model.detect(_img(size)) == [], size
    assert model.loaded is False


def test_embed_of_nothing_is_an_empty_matrix_of_the_right_width():
    """The caller stacks this into the faces matrix, and a shapeless empty would
    raise on exactly the photographs that have nobody in them."""
    out = faces.FaceModel().embed([])
    assert out.shape == (0, faces.DIM) and out.dtype == np.float16


@pytest.mark.skipif(os.environ.get("LENS_MODEL_TESTS") != "1",
                    reason="set LENS_MODEL_TESTS=1 to load the real face models")
def test_the_real_models_load_and_answer_in_the_documented_shape():
    """Not a test of accuracy — a test that the wiring is real.

    Detection on a synthetic image is unreliable by design (there is no face in
    it), so what is checked here is what the rest of lens actually depends on:
    the weights load, `detect` returns a list of normalized boxes with
    probabilities, and `embed` returns unit-length float16 rows of the documented
    width for crops of whatever it is given."""
    model = faces.FaceModel()
    model.load()
    assert model.loaded

    found = model.detect(_img((512, 384)))
    assert isinstance(found, list)
    for f in found:                            # empty is a perfectly good answer
        assert len(f["bbox"]) == 4
        assert all(0.0 <= v <= 1.0 for v in f["bbox"])
        assert f["prob"] >= faces.MIN_PROB

    crops = [faces.crop_face(_img((300, 300)), (0.2, 0.2, 0.8, 0.8))] * 3
    vecs = model.embed(crops)
    assert vecs.shape == (3, faces.DIM) and vecs.dtype == np.float16
    norms = np.linalg.norm(vecs.astype(np.float32), axis=1)
    assert np.allclose(norms, 1.0, atol=0.02)
    # the same crop three times is the same vector three times: the embedding is
    # a function of the pixels and nothing else
    assert np.allclose(vecs[0].astype(np.float32), vecs[1].astype(np.float32))
