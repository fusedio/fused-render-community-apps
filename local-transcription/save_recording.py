"""Land a browser recording on disk as bytes.

The page records with MediaRecorder and gets a Blob; `fused.writeFile` only
writes UTF-8 text, so the bytes come through here as base64 and get decoded into
./recordings/. Large recordings arrive in several appended chunks (each a whole
number of base64 quads, so every chunk decodes on its own).
"""

SAFE_EXT = {"webm", "m4a", "mp4", "ogg", "oga", "opus", "wav", "aac", "mp3"}


def main(b64: str = "", name: str = "", ext: str = "webm", append: bool = False):
    """Decode `b64` into ./recordings/<name>.<ext>; append=True adds to it.

    Returns {"path", "name", "size"} — the absolute path is what the page hands
    to fused.ai.transcribe.
    """
    import base64
    import binascii
    import os
    import re

    if not b64:
        raise ValueError("no audio data was sent")

    name = (name or "recording").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name) or name.startswith("."):
        raise ValueError(f"unsafe recording name: {name!r}")

    ext = (ext or "webm").lstrip(".").lower()
    if ext not in SAFE_EXT:
        raise ValueError(f"unsupported audio extension: {ext!r} (allowed: {sorted(SAFE_EXT)})")

    try:
        chunk = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"audio data was not valid base64: {exc}") from exc

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name}.{ext}")

    with open(path, "ab" if append else "wb") as fh:
        fh.write(chunk)

    return {"path": path, "name": f"{name}.{ext}", "size": os.path.getsize(path)}
