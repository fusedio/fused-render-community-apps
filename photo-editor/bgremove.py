"""One-click background removal via macOS's native Vision framework.

`VNGenerateForegroundInstanceMaskRequest` (macOS 14+) is class-agnostic subject
lifting -- the same engine behind Finder/Preview's "Remove Background". It runs
on the Neural Engine, needs no model download, and is sub-second even at high
resolution. We only use Vision to produce the *mask*; the actual compositing is
done in Pillow so the result is a plain RGBA PNG we fully control.
"""

from __future__ import annotations

import os
import tempfile


class BackgroundRemovalError(RuntimeError):
    """Raised when Vision is unavailable or finds no subject."""


def _require_vision():
    try:
        import Quartz  # noqa: F401
        import Vision
    except ImportError as error:  # pragma: no cover - depends on host
        raise BackgroundRemovalError(
            "pyobjc-framework-Vision is not installed in this app's environment"
        ) from error
    if not hasattr(Vision, "VNGenerateForegroundInstanceMaskRequest"):
        raise BackgroundRemovalError(
            "This macOS version has no VNGenerateForegroundInstanceMaskRequest "
            "(needs macOS 14 or newer)"
        )
    return Vision


def foreground_mask(image_path: str) -> "object":
    """Return a single-channel PIL mask: 255 = subject, 0 = background.

    The mask comes back at the source image's own pixel size so it can be
    pasted straight onto the original without resampling.
    """
    from PIL import Image

    Vision = _require_vision()
    import Quartz
    from Foundation import NSURL

    image_path = os.path.abspath(image_path)
    if not os.path.exists(image_path):
        raise BackgroundRemovalError(f"No such image: {image_path}")

    url = NSURL.fileURLWithPath_(image_path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    request = Vision.VNGenerateForegroundInstanceMaskRequest.alloc().init()

    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise BackgroundRemovalError(f"Vision request failed: {error}")

    results = request.results()
    if not results:
        raise BackgroundRemovalError(
            "Vision found no distinct subject in this photo. Try an image with a "
            "clearer foreground."
        )

    observation = results[0]
    # `allInstances` is an NSIndexSet of every subject Vision segmented. Passing
    # the whole set lifts every subject at once, which is what a one-click
    # "remove background" should do (Preview lifts only the largest).
    instances = observation.allInstances()
    buffer, error = observation.generateScaledMaskForImageForInstances_fromRequestHandler_error_(
        instances, handler, None
    )
    if buffer is None:
        raise BackgroundRemovalError(f"Could not generate subject mask: {error}")

    mask = _pixel_buffer_to_mask(buffer, Quartz)
    with Image.open(image_path) as source:
        size = source.size
    if mask.size != size:
        mask = mask.resize(size, Image.LANCZOS)
    return mask


def subject_mask_at_point(image_path: str, output_path: str, x_norm: float, y_norm: float) -> dict:
    import numpy as np
    from PIL import Image

    Vision = _require_vision()
    import Quartz
    from Foundation import NSURL

    image_path = os.path.abspath(image_path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(image_path), {})
    request = Vision.VNGenerateForegroundInstanceMaskRequest.alloc().init()
    ok, error = handler.performRequests_error_([request], None)
    if not ok or not request.results():
        raise BackgroundRemovalError("Vision found no distinct subject in this photo.")
    observation = request.results()[0]
    point = (min(max(float(x_norm), 0.0), 1.0), 1.0 - min(max(float(y_norm), 0.0), 1.0))
    reply = observation.instanceAtPoint_error_(point, None)
    instances = reply[0] if isinstance(reply, tuple) else reply
    if instances is None or not instances.count() or instances.firstIndex() == 0:
        raise BackgroundRemovalError("No subject under the cursor")
    buffer, error = observation.generateScaledMaskForImageForInstances_fromRequestHandler_error_(
        instances, handler, None
    )
    if buffer is None:
        raise BackgroundRemovalError(f"Could not generate subject mask: {error}")
    mask = _pixel_buffer_to_mask(buffer, Quartz)
    with Image.open(image_path) as source:
        size = source.size
    if mask.size != size:
        mask = mask.resize(size, Image.LANCZOS)
    mask = mask.point(lambda value: 255 if value >= 128 else 0)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    mask.save(output_path + ".tmp.png", "PNG")
    os.replace(output_path + ".tmp.png", output_path)
    binary = np.asarray(mask, dtype=np.uint8) >= 128
    return {"path": output_path, "width": mask.width, "height": mask.height,
            "coverage": round(float(binary.mean()), 5), "empty": bool(not binary.any()),
            "method": "vision"}


def _pixel_buffer_to_mask(buffer, Quartz):
    """Copy a one-component CVPixelBuffer out of Vision into a PIL 'L' image."""
    import numpy as np
    from PIL import Image

    Quartz.CVPixelBufferLockBaseAddress(buffer, Quartz.kCVPixelBufferLock_ReadOnly)
    try:
        width = Quartz.CVPixelBufferGetWidth(buffer)
        height = Quartz.CVPixelBufferGetHeight(buffer)
        stride = Quartz.CVPixelBufferGetBytesPerRow(buffer)
        fmt = Quartz.CVPixelBufferGetPixelFormatType(buffer)
        base = Quartz.CVPixelBufferGetBaseAddress(buffer)
        raw = bytes(base.as_buffer(stride * height))
    finally:
        Quartz.CVPixelBufferUnlockBaseAddress(buffer, Quartz.kCVPixelBufferLock_ReadOnly)

    # Vision hands back kCVPixelFormatType_OneComponent8 ('L008') on every macOS
    # we support, but it is documented as free to return float32 ('L00f'), so
    # handle both rather than silently decoding garbage.
    if fmt == 0x4C303066:  # kCVPixelFormatType_OneComponent32Float
        array = np.frombuffer(raw, dtype=np.float32).reshape(height, stride // 4)
        array = np.clip(array[:, :width] * 255.0, 0, 255).astype(np.uint8)
    else:
        array = np.frombuffer(raw, dtype=np.uint8).reshape(height, stride)[:, :width]

    return Image.fromarray(array, mode="L")


def _flat_graphic_mask(image_path: str):
    """Chroma-key fallback for logos/icons/screenshots: flat art with a
    uniform background confuses Vision's photographic subject-lifter (it's
    tuned for depth/texture/lighting cues real photos have and flat art
    doesn't), so it reports "no subject" on exactly the images where a plain
    color-distance threshold is trivial and in fact more accurate -- it also
    correctly punches through enclosed background regions (letter counters,
    gaps in an outline icon) that a border-flood-fill would miss.

    Returns None if the image doesn't look like flat art with a near-uniform
    background, so the caller knows to give up rather than mangle a photo.
    """
    import numpy as np
    from PIL import Image

    with Image.open(image_path) as handle:
        source = handle.convert("RGB")
    array = np.asarray(source, dtype=np.float32)

    corners = np.array(
        [array[0, 0], array[0, -1], array[-1, 0], array[-1, -1]]
    )
    bg_color = np.median(corners, axis=0)
    if corners.std(axis=0).sum() > 20:
        return None  # corners disagree -- this isn't a flat solid background

    distance = np.linalg.norm(array - bg_color, axis=-1)
    background = distance < 20
    if not (0.15 < background.mean() < 0.99):
        return None  # not dominated by one background color -- likely a photo

    # Soft edge over a narrow band so text/line-art edges anti-alias instead
    # of going jagged; pixels already far past the band stay fully opaque.
    alpha = np.clip((distance - 15) / (55 - 15) * 255, 0, 255).astype(np.uint8)
    return Image.fromarray(alpha, "L")


def remove_background(image_path: str, output_path: str = "") -> dict:
    """Cut the subject out of `image_path`, writing a transparent-background PNG.

    Tries Vision's photographic subject lift first; if it finds nothing, falls
    back to a flat-art chroma-key so logos/icons/screenshots on a solid
    background still work with the same one button.

    Returns the output path plus how much of the frame survived, which the page
    uses to warn when almost nothing was kept.
    """
    import numpy as np
    from PIL import Image

    method = "vision"
    try:
        mask = foreground_mask(image_path)
    except BackgroundRemovalError:
        mask = _flat_graphic_mask(image_path)
        if mask is None:
            raise
        method = "flat-graphic-chroma-key"

    with Image.open(image_path) as handle:
        source = handle.convert("RGBA")

    cut = source.copy()
    cut.putalpha(mask)

    if not output_path:
        output_path = os.path.join(
            tempfile.gettempdir(), f"cutout-{os.getpid()}.png"
        )
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    temporary = output_path + ".tmp.png"
    cut.save(temporary, "PNG")
    os.replace(temporary, output_path)

    coverage = float(np.asarray(mask, dtype=np.float32).mean() / 255.0)
    return {
        "path": output_path,
        "width": cut.width,
        "height": cut.height,
        "coverage": round(coverage, 4),
        "method": method,
    }


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(remove_background(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""), indent=2))
