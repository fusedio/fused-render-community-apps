"""Pillow-side image work: session storage, mask normalisation, adjustments.

Everything here is pure CPU/Pillow and cheap enough to run inside a 60 s
`runPython` call. The expensive model work lives in `engine.py`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# In data/, not cache/: saved documents (docstore.py) point at these files, so
# clearing them would break every document that uses the image.
SESSIONS_DIR = os.path.join(APP_DIR, ".fused", "data", "sessions")

#: Bump when the on-disk session layout changes so stale folders are ignored
#: rather than half-read.
SESSION_LAYOUT_VERSION = 1

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def session_dir(session_id: str, create: bool = False) -> str:
    """Resolve a session folder, refusing anything that could escape the root."""
    if not _SAFE_ID.match(session_id or ""):
        raise ValueError(f"Invalid session id: {session_id!r}")
    path = os.path.join(SESSIONS_DIR, f"v{SESSION_LAYOUT_VERSION}-{session_id}")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def new_session_id() -> str:
    return hashlib.sha1(f"{time.time_ns()}-{os.getpid()}".encode()).hexdigest()[:16]


def resolve_source(session_id: str, source: str = "") -> str:
    folder = os.path.abspath(session_dir(session_id))
    if not source:
        return os.path.join(folder, "source.png")
    path = os.path.abspath(source)
    if os.path.commonpath([path, folder]) != folder:
        raise ValueError("Refusing to read outside the session folder")
    return path


def write_atomic(path: str, data: bytes) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)
    return path


def write_json(path: str, value: dict) -> str:
    return write_atomic(path, json.dumps(value).encode("utf-8"))


def decode_data_url(data_url: str) -> bytes:
    """Accept either a full `data:` URL or a bare base64 payload."""
    if not data_url:
        raise ValueError("No image data was supplied")
    return base64.b64decode(data_url.split(",", 1)[-1])


def save_source(session_id: str, data_url: str, name: str = "photo") -> dict:
    """Persist an uploaded photo as the session's immutable source image."""
    return save_source_bytes(session_id, decode_data_url(data_url), name=name)


def save_source_path(session_id: str, path: str, name: str = "") -> dict:
    """Same as `save_source`, for a file on disk (the agent tools' `add_image`)."""
    path = os.path.expanduser(path)
    with open(path, "rb") as handle:
        raw = handle.read()
    return save_source_bytes(session_id, raw, name=name or os.path.splitext(os.path.basename(path))[0])


def save_source_bytes(session_id: str, raw: bytes, name: str = "photo") -> dict:
    from PIL import Image, ImageOps

    folder = session_dir(session_id, create=True)
    with Image.open(io.BytesIO(raw)) as handle:
        # Phone photos carry rotation in EXIF; bake it in now so every later
        # stage (mask alignment above all) agrees on which way is up.
        image = ImageOps.exif_transpose(handle)
        # Keep transparency (cutouts, logos, stickers); flatten everything else.
        has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
        image = image.convert("RGBA" if has_alpha else "RGB")
        path = os.path.join(folder, "source.png")
        image.save(path + ".tmp.png", "PNG")
        os.replace(path + ".tmp.png", path)
        size = image.size

    write_json(
        os.path.join(folder, "session.json"),
        {"name": name, "width": size[0], "height": size[1], "created_at": time.time()},
    )
    return {"session_id": session_id, "path": path, "width": size[0], "height": size[1]}


def grow_mask(mask, pixels: int):
    """Dilate (pixels > 0) or erode (pixels < 0) a grayscale mask.

    Pillow's rank filters are naive, so a 40-pixel kernel over a 12-megapixel
    photo would take seconds. The mask is applied to a ~1 megapixel model
    canvas and feathered on the way back, so we do the morphology on a
    quarter-scale copy, which is both fast and far finer than anything the
    edit can resolve.
    """
    from PIL import Image, ImageFilter

    if not pixels:
        return mask

    scale = 4
    small = mask.resize((max(mask.width // scale, 1), max(mask.height // scale, 1)), Image.BILINEAR)
    radius = max(1, round(abs(pixels) / scale))
    kernel = min(2 * radius + 1, 15)
    passes = max(1, round(radius / ((kernel - 1) / 2)))
    step = ImageFilter.MaxFilter(kernel) if pixels > 0 else ImageFilter.MinFilter(kernel)
    for _ in range(passes):
        small = small.filter(step)
    return small.resize(mask.size, Image.BILINEAR)


def save_mask(session_id: str, data_url: str, source_path: str = "", grow: float = 0.0) -> dict:
    """Normalise a page-drawn mask into plain grayscale: white = change.

    The page draws the selection into a canvas's alpha channel, and everything
    downstream reads luminance thresholded at 128 -- so a canvas export with
    transparent "keep" pixels would read as solid white and repaint the whole
    frame. We therefore flatten onto black, drop alpha, and store plain 8-bit
    grayscale at the source image's exact resolution.
    """
    from PIL import Image

    folder = session_dir(session_id, create=True)
    source_path = source_path or os.path.join(folder, "source.png")
    with Image.open(source_path) as handle:
        target_size = handle.size

    raw = decode_data_url(data_url)
    with Image.open(io.BytesIO(raw)) as handle:
        drawn = handle.convert("RGBA")
        flattened = Image.new("RGBA", drawn.size, (0, 0, 0, 255))
        flattened.alpha_composite(drawn)
        mask = flattened.convert("L")

    if mask.size != target_size:
        mask = mask.resize(target_size, Image.LANCZOS)

    # Expanding slightly is the usual fix when a removal leaves a halo of the
    # old object behind; contracting protects a subject the brush overshot.
    if grow:
        mask = grow_mask(mask, int(grow))

    path = os.path.join(folder, "mask.png")
    mask.save(path + ".tmp.png", "PNG")
    os.replace(path + ".tmp.png", path)

    # Coverage is measured after the same >=128 threshold the model applies, so
    # the page can tell the user "nothing is selected" before a 3-minute render.
    import numpy as np

    binary = np.asarray(mask, dtype=np.uint8) >= 128
    return {
        "path": path,
        "width": mask.width,
        "height": mask.height,
        "coverage": round(float(binary.mean()), 5),
        "empty": bool(not binary.any()),
    }


AI_EDIT_LONG_EDGE = 1024
AI_EDIT_MIN_EDGE = 256
AI_EDIT_MAX_EDGE = 2048


def split_alpha(image, backdrop=(255, 255, 255)):
    if "A" not in image.getbands() and not (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGB"), None
    from PIL import Image

    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] == 255:
        return rgba.convert("RGB"), None
    flat = Image.new("RGB", rgba.size, backdrop)
    flat.paste(rgba, mask=alpha)
    return flat, alpha


#: Insert paints the selection this colour so the model knows where to draw;
#: Remove never shows it to the model (see `prepare_ai_edit`).
AI_MARK_COLOR = (255, 0, 255)
REMOVE_EXPAND_CELLS = 1.0


def remove_expansion_px(width: int, height: int) -> int:
    return max(8, round(16 * REMOVE_EXPAND_CELLS * (width * height) ** 0.5 / 1024))


def _push_pull(values, known):
    """Fill the unknown cells of an HxWxC float array from the known ones.

    Push-pull: average the known cells down a 2x pyramid until every cell has
    a value, then walk back up, keeping real cells and taking the coarser
    level's smooth estimate wherever a cell was unknown. Known cells come back
    unchanged; unknown ones become a seamless continuation of their
    surroundings.
    """
    import numpy as np
    from PIL import Image

    channels = values.shape[2]
    levels = []
    color, weight = values * known[..., None], known
    while True:
        levels.append((color, weight))
        h, w = weight.shape
        if (weight > 0).all() or (h == 1 and w == 1):
            break
        color = np.pad(color, ((0, h % 2), (0, w % 2), (0, 0)))
        weight = np.pad(weight, ((0, h % 2), (0, w % 2)))
        h2, w2 = weight.shape[0] // 2, weight.shape[1] // 2
        color = color.reshape(h2, 2, w2, 2, channels).sum(axis=(1, 3))
        weight = weight.reshape(h2, 2, w2, 2).sum(axis=(1, 3))
        # Premultiplied colour: keep the average, cap the confidence at 1.
        capped = np.minimum(weight, 1.0)
        color *= (capped / np.maximum(weight, 1e-6))[..., None]
        weight = capped

    color, weight = levels[-1]
    filled = color / np.maximum(weight, 1e-6)[..., None]
    for color, weight in reversed(levels[:-1]):
        h, w = weight.shape
        up = np.stack([
            np.asarray(Image.fromarray(np.ascontiguousarray(filled[..., c], dtype=np.float32), "F")
                       .resize((w, h), Image.BILINEAR))
            for c in range(channels)
        ], axis=-1)
        filled = color + up * (1.0 - weight)[..., None]
    return np.where(known[..., None] > 0, values, filled)


def fill_from_surroundings(image, hole):
    """Fill the white pixels of `hole` with colour pulled in from around them.

    What a clone-free "content-aware" fill looks like before any detail is
    added: a soft continuation of the surrounding colours with no seam and no
    flat patch. Pixels outside the hole come back unchanged.
    """
    import numpy as np
    from PIL import Image

    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    known = (np.asarray(hole.convert("L").resize(image.size)) < 128).astype(np.float32)
    if known.all() or not known.any():
        return image.convert("RGB")
    out = _push_pull(rgb, known)
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


def match_tone(generated, source, mask):
    """Undo the model's overall colour drift inside the selection.

    FLUX re-renders the whole frame and comes back a little lighter or warmer
    than it went in, so a patch pasted back from it reads as a pale ghost of
    the selection. The fix is a smooth offset field: the difference between
    the original and the model's output, measured everywhere OUTSIDE the
    selection and interpolated across it. Adding it matches the patch's edge
    to the photo exactly while keeping the model's detail. It is computed at
    quarter scale because it only has to carry low frequencies.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    size = generated.size
    small = (max(1, size[0] // 4), max(1, size[1] // 4))
    g = np.asarray(generated.convert("RGB").resize(small, Image.BOX), dtype=np.float32)
    s = np.asarray(source.convert("RGB").resize(small, Image.BOX), dtype=np.float32)
    hole = mask.convert("L").resize(small, Image.BILINEAR).filter(ImageFilter.MaxFilter(3))
    known = (np.asarray(hole) < 8).astype(np.float32)
    if not known.any():
        return generated.convert("RGB")
    offset = _push_pull(s - g, known)
    up = np.stack([
        np.asarray(Image.fromarray(np.ascontiguousarray(offset[..., c]), "F").resize(size, Image.BILINEAR))
        for c in range(3)
    ], axis=-1)
    out = np.asarray(generated.convert("RGB"), dtype=np.float32) + up
    return Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8), "RGB")


def ai_canvas_size(width: int, height: int) -> tuple[int, int]:
    """The model canvas for a photo: long edge <= 1024, short edge >= 256.

    Both sides share ONE scale so the model sees the photo's real aspect
    ratio; flooring each side at 256 separately would stretch a thin crop or
    banner. Only past fused.ai's 2048 px limit (an aspect beyond 8:1) does the
    short side have to give.
    """
    scale = min(1.0, AI_EDIT_LONG_EDGE / max(width, height))
    scale = max(scale, AI_EDIT_MIN_EDGE / min(width, height))
    scale = min(scale, AI_EDIT_MAX_EDGE / max(width, height))

    def snap(side: float) -> int:
        return int(min(AI_EDIT_MAX_EDGE, max(AI_EDIT_MIN_EDGE, round(side * scale / 16) * 16)))

    return snap(width), snap(height)


def prepare_ai_edit(session_id: str, source_path: str, mode: str) -> dict:
    """Build the downscaled picture `fused.ai.image` edits.

    `fused.ai.image` takes an image and an instruction but no mask, so the
    selection has to be visible in the picture itself; `feathered_composite`
    then keeps everything outside the mask bit-identical.

    Insert paints the selection solid magenta -- the model needs to know where
    to draw, and an opaque fill stops it restoring the old object underneath.

    Remove does NOT: FLUX.2 klein regularly hands a magenta patch straight back
    instead of filling it, and that patch is then blended into the photo. So the
    selection is expanded (the outline of the subject must not survive) and
    pre-filled from the surrounding pixels. The model is asked to repair that
    soft patch, and even if it changes nothing the result is already blended
    with its neighbourhood rather than painted over.
    """
    from PIL import Image

    folder = session_dir(session_id, create=True)
    with Image.open(source_path) as handle:
        source, _ = split_alpha(handle)
    with Image.open(os.path.join(folder, "mask.png")) as handle:
        mask = handle.convert("L")
    if mask.size != source.size:
        mask = mask.resize(source.size, Image.LANCZOS)
    mask = mask.point(lambda value: 255 if value >= 128 else 0)
    if mode == "remove":
        mask = grow_mask(mask, remove_expansion_px(*mask.size))
    stamp = int(time.time() * 1000)
    mask_path = os.path.join(folder, f"ai-mask-{stamp}.png")
    mask.save(mask_path + ".tmp.png", "PNG")
    os.replace(mask_path + ".tmp.png", mask_path)

    size = ai_canvas_size(*source.size)
    small = source.resize(size, Image.LANCZOS)
    small_mask = mask.resize(size, Image.LANCZOS).point(lambda value: 255 if value >= 128 else 0)
    if mode == "remove":
        small = fill_from_surroundings(small, small_mask)
    else:
        small.paste(AI_MARK_COLOR, (0, 0, *size), mask=small_mask)
    marked_path = os.path.join(folder, f"ai-marked-{stamp}.png")
    small.save(marked_path + ".tmp.png", "PNG")
    os.replace(marked_path + ".tmp.png", marked_path)
    return {"marked": marked_path, "mask": mask_path, "width": size[0], "height": size[1]}


def scrub_marker(generated, mask, reach: int = 4):
    """Replace any magenta the model left inside the selection.

    Only pixels close to the pure marker colour and inside the (slightly
    grown) selection count, so a pink subject elsewhere in the photo is never
    touched. They are filled from the non-magenta pixels around them. Returns
    the cleaned image and the fraction of the selection that was magenta.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    arr = np.asarray(generated.convert("RGB"), dtype=np.int32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    distance = (255 - r) ** 2 + g ** 2 + (255 - b) ** 2
    magenta = (distance < 110 ** 2) & (np.minimum(r, b) - g > 90)
    area = mask.resize(generated.size, Image.BILINEAR).point(lambda value: 255 if value >= 64 else 0)
    if reach:
        area = area.filter(ImageFilter.MaxFilter(2 * reach + 1))
    inside = np.asarray(area) > 0
    magenta &= inside
    if not magenta.any():
        return generated.convert("RGB"), 0.0
    # Catch the antialiased fringe around each leftover blob too.
    hole = Image.fromarray((magenta * 255).astype(np.uint8), "L").filter(ImageFilter.MaxFilter(5))
    fraction = float(magenta.sum()) / max(int(inside.sum()), 1)
    return fill_from_surroundings(generated, hole), round(fraction, 4)


def feathered_composite(source_path: str, generated_path: str, mask_path: str,
                        output_path: str, feather: float = 3.0, mode: str = "remove") -> dict:
    """Paste the model's output back into the full-resolution original.

    The model renders onto its own ~1 megapixel canvas, so returning its output
    directly would silently downscale the user's photo. Instead we scale the
    result back up and blend it in through the mask only: untouched pixels stay
    bit-identical to the upload, and a small blur on the mask edge hides the
    seam. At the model's own resolution, Insert's magenta marker is scrubbed
    first so none of it can be blended in, then `match_tone` removes the
    model's colour drift so the patch does not read as a pale ghost.
    """
    from PIL import Image, ImageFilter

    with Image.open(source_path) as handle:
        source, alpha = split_alpha(handle)
    with Image.open(generated_path) as handle:
        generated = handle.convert("RGB")
    with Image.open(mask_path) as handle:
        mask = handle.convert("L")

    unfilled = 0.0
    if mode == "insert":
        generated, unfilled = scrub_marker(generated, mask, reach=max(4, round(feather)))
    generated = match_tone(generated, source.resize(generated.size, Image.LANCZOS), mask)

    if generated.size != source.size:
        generated = generated.resize(source.size, Image.LANCZOS)
    if mask.size != source.size:
        mask = mask.resize(source.size, Image.LANCZOS)

    mask = mask.point(lambda value: 255 if value >= 128 else 0)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=float(feather)))

    blended = Image.composite(generated, source, mask)
    if alpha is not None:
        # Insert may draw where the source was transparent (a cut-out's empty
        # background); make the selection opaque so the new object shows.
        # Remove keeps the source's own transparency.
        if mode == "insert":
            from PIL import ImageChops

            alpha = ImageChops.lighter(alpha, mask)
        blended.putalpha(alpha)
    blended.save(output_path + ".tmp.png", "PNG")
    os.replace(output_path + ".tmp.png", output_path)
    return {"path": output_path, "width": blended.width, "height": blended.height, "unfilled": unfilled}


# --- Adjustments -----------------------------------------------------------
#
# These mirror the CSS filters the page uses for live preview, so "commit"
# produces roughly what the user was already looking at. Each value is a
# percentage where 0 means "no change".

ADJUSTMENT_KEYS = (
    "brightness", "contrast", "saturation", "temperature", "tint",
    "highlights", "shadows", "sharpness", "vignette", "blur",
)


def apply_adjustments(source_path: str, output_path: str, values: dict) -> dict:
    """Bake slider values into real pixels with Pillow."""
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter

    def amount(key: str) -> float:
        try:
            return float(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    with Image.open(source_path) as handle:
        # Bake RGB only; a cutout's transparency is carried across untouched.
        image, alpha = split_alpha(handle)

    for key, enhancer in (
        ("brightness", ImageEnhance.Brightness),
        ("contrast", ImageEnhance.Contrast),
        ("saturation", ImageEnhance.Color),
    ):
        level = amount(key)
        if level:
            image = enhancer(image).enhance(1.0 + level / 100.0)

    temperature, tint = amount("temperature"), amount("tint")
    if temperature or tint:
        array = np.asarray(image, dtype=np.float32)
        # Warm = push red up and blue down; tint trades green against magenta.
        array[..., 0] += temperature * 0.6
        array[..., 2] -= temperature * 0.6
        array[..., 1] += tint * 0.5
        image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")

    highlights, shadows = amount("highlights"), amount("shadows")
    if highlights or shadows:
        array = np.asarray(image, dtype=np.float32) / 255.0
        luma = array @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        # Weight each correction toward the tonal range it names, so lifting
        # shadows does not also blow out an already-bright sky.
        high_weight = np.clip((luma - 0.5) * 2.0, 0, 1)[..., None]
        low_weight = np.clip((0.5 - luma) * 2.0, 0, 1)[..., None]
        array += high_weight * (highlights / 100.0) * 0.5
        array += low_weight * (shadows / 100.0) * 0.5
        image = Image.fromarray(np.clip(array * 255.0, 0, 255).astype(np.uint8), "RGB")

    sharpness = amount("sharpness")
    if sharpness > 0:
        image = image.filter(ImageFilter.UnsharpMask(radius=2, percent=int(sharpness * 2), threshold=3))
    elif sharpness < 0:
        image = image.filter(ImageFilter.GaussianBlur(radius=abs(sharpness) / 40.0))

    blur = amount("blur")
    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(radius=blur / 10.0))

    vignette = amount("vignette")
    if vignette:
        array = np.asarray(image, dtype=np.float32) / 255.0
        height, width = array.shape[:2]
        y = np.linspace(-1, 1, height, dtype=np.float32)[:, None]
        x = np.linspace(-1, 1, width, dtype=np.float32)[None, :]
        radius = np.sqrt(x * x + y * y) / np.sqrt(2.0)
        falloff = np.clip(1.0 - (vignette / 100.0) * radius**2, 0, 2)[..., None]
        image = Image.fromarray(np.clip(array * falloff * 255.0, 0, 255).astype(np.uint8), "RGB")

    if alpha is not None:
        image.putalpha(alpha)
    image.save(output_path + ".tmp.png", "PNG")
    os.replace(output_path + ".tmp.png", output_path)
    return {"path": output_path, "width": image.width, "height": image.height}


# --- Filter presets ----------------------------------------------------

FILTER_RECIPES = {
    "vivid": {
        "curve": [(0, 0), (64, 45), (128, 128), (192, 215), (255, 255)],
        "saturation": 1.45,
    },
    "mono": {
        "curve": [(0, 0), (64, 55), (128, 128), (192, 200), (255, 250)],
        "gray_weights": (0.4, 0.4, 0.2),
    },
    "noir": {
        "curve": [(0, 0), (40, 0), (90, 60), (128, 140), (190, 210), (255, 255)],
        "gray_weights": (0.35, 0.35, 0.3),
        "vignette": 55,
        "grain": 9,
    },
    "warm": {
        "curve": [(0, 15), (64, 80), (128, 140), (192, 200), (255, 235)],
        "saturation": 1.15,
        "shadow_tint": (42, 53, 64),
        "highlight_tint": (245, 217, 168),
        "tint_strength": 0.35,
    },
    "cool": {
        "curve": [(0, 5), (64, 70), (128, 128), (192, 190), (255, 250)],
        "saturation": 0.85,
        "shadow_tint": (30, 58, 74),
        "highlight_tint": (245, 240, 230),
        "tint_strength": 0.35,
    },
    "faded": {
        "curve": [(0, 35), (64, 90), (128, 150), (192, 200), (255, 225)],
        "saturation": 0.75,
        "shadow_tint": (58, 53, 48),
        "highlight_tint": (232, 226, 213),
        "tint_strength": 0.3,
        "grain": 6,
    },
}


def _curve_lut(points):
    import numpy as np

    xs, ys = zip(*points)
    lut = np.interp(np.arange(256), xs, ys)
    return np.clip(lut, 0, 255).astype(np.uint8)


def _split_tone(array, shadow_color, highlight_color, strength):
    import numpy as np

    luma = (array[..., 0] * 0.299 + array[..., 1] * 0.587 + array[..., 2] * 0.114) / 255.0
    shadow_w = (np.clip(1.0 - luma * 2.0, 0, 1) * strength)[..., None]
    highlight_w = (np.clip(luma * 2.0 - 1.0, 0, 1) * strength)[..., None]
    shadow = np.array(shadow_color, dtype=np.float32)
    highlight = np.array(highlight_color, dtype=np.float32)
    out = array.astype(np.float32)
    out = out * (1 - shadow_w) + shadow * shadow_w
    out = out * (1 - highlight_w) + highlight * highlight_w
    return out


def apply_filter_preset(source_path: str, output_path: str, name: str, strength: float = 100.0) -> dict:
    """Bake a named look into real pixels -- the same recipe the live CSS
    preview only approximates."""
    import numpy as np
    from PIL import Image, ImageEnhance

    with Image.open(source_path) as handle:
        original, alpha = split_alpha(handle)

    recipe = FILTER_RECIPES.get(name)
    if not recipe:
        original.save(output_path + ".tmp.png", "PNG")
        os.replace(output_path + ".tmp.png", output_path)
        return {"path": output_path, "width": original.width, "height": original.height}

    image = original
    weights = recipe.get("gray_weights")
    if weights:
        array = np.asarray(image, dtype=np.float32)
        gray = array[..., 0] * weights[0] + array[..., 1] * weights[1] + array[..., 2] * weights[2]
        gray = np.clip(gray, 0, 255).astype(np.uint8)
        image = Image.fromarray(np.stack([gray, gray, gray], axis=-1), "RGB")

    lut = _curve_lut(recipe["curve"])
    array = lut[np.asarray(image, dtype=np.uint8)]

    if "shadow_tint" in recipe or "highlight_tint" in recipe:
        array = _split_tone(
            array,
            recipe.get("shadow_tint", (0, 0, 0)),
            recipe.get("highlight_tint", (255, 255, 255)),
            recipe.get("tint_strength", 0.3),
        )

    image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")

    saturation = recipe.get("saturation")
    if saturation and saturation != 1.0:
        image = ImageEnhance.Color(image).enhance(saturation)

    grain = recipe.get("grain")
    if grain:
        rng = np.random.default_rng(7)
        noise = rng.normal(0, grain, size=image.size[::-1])[..., None]
        array = np.asarray(image, dtype=np.float32) + noise
        image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")

    vignette = recipe.get("vignette")
    if vignette:
        array = np.asarray(image, dtype=np.float32) / 255.0
        height, width = array.shape[:2]
        y = np.linspace(-1, 1, height, dtype=np.float32)[:, None]
        x = np.linspace(-1, 1, width, dtype=np.float32)[None, :]
        radius = np.sqrt(x * x + y * y) / np.sqrt(2.0)
        falloff = np.clip(1.0 - (vignette / 100.0) * radius**2, 0, 2)[..., None]
        image = Image.fromarray(np.clip(array * falloff * 255.0, 0, 255).astype(np.uint8), "RGB")

    blend = max(0.0, min(1.0, float(strength) / 100.0))
    if blend < 1.0:
        image = Image.blend(original, image, blend)

    if alpha is not None:
        image.putalpha(alpha)
    image.save(output_path + ".tmp.png", "PNG")
    os.replace(output_path + ".tmp.png", output_path)
    return {"path": output_path, "width": image.width, "height": image.height}


def export_image(source_path: str, output_path: str, fmt: str = "png",
                 quality: int = 92, background: str = "#ffffff", dpi: float = 0) -> dict:
    """Write the final file, flattening transparency for formats that lack it.

    `dpi` is written into the file so print software opens it at its intended
    physical size; for PDF it sets the page size (pixels / dpi inches).
    """
    from PIL import Image

    fmt = (fmt or "png").lower()
    dpi = float(dpi or 0)
    with Image.open(source_path) as handle:
        image = handle.convert("RGBA")

    def flat():
        base = Image.new("RGB", image.size, background)
        base.paste(image, mask=image.split()[3])
        return base

    extra = {"dpi": (dpi, dpi)} if dpi > 0 else {}
    temporary = output_path + ".tmp"
    if fmt in ("jpg", "jpeg"):
        flat().save(temporary, "JPEG", quality=int(quality), subsampling=0, **extra)
    elif fmt == "pdf":
        flat().save(temporary, "PDF", resolution=dpi if dpi > 0 else 72.0)
    elif fmt in ("tif", "tiff"):
        image.save(temporary, "TIFF", compression="tiff_lzw", **extra)
    elif fmt == "webp":
        image.save(temporary, "WEBP", quality=int(quality))
    else:
        image.save(temporary, "PNG", **extra)
    os.replace(temporary, output_path)
    return {"path": output_path, "format": fmt, "bytes": os.path.getsize(output_path),
            "width": image.width, "height": image.height, "dpi": dpi}


def export_pdf_pages(source_paths: list[str], output_path: str, background: str = "#ffffff",
                     dpi: float = 0) -> dict:
    """One PDF with a page per image, in order. Pages may differ in size; each
    is pixels / dpi inches, like export_image's single-page PDF."""
    from PIL import Image

    if not source_paths:
        raise ValueError("No pages to export")
    pages = []
    for path in source_paths:
        with Image.open(path) as handle:
            image = handle.convert("RGBA")
        base = Image.new("RGB", image.size, background)
        base.paste(image, mask=image.split()[3])
        pages.append(base)
    dpi = float(dpi or 0)
    temporary = output_path + ".tmp"
    pages[0].save(temporary, "PDF", resolution=dpi if dpi > 0 else 72.0, save_all=True, append_images=pages[1:])
    os.replace(temporary, output_path)
    return {"path": output_path, "format": "pdf", "bytes": os.path.getsize(output_path),
            "pages": len(pages), "width": pages[0].width, "height": pages[0].height, "dpi": dpi}
