from __future__ import annotations

import base64
import importlib.util
import json
import re
import time
from pathlib import Path

import store

APP_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = APP_DIR / ".fused" / "cache" / "scratch"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def main(action: str = "status", image_data: str = "", path: str = "",
         query: str = "", record_id: str = "", content: str = "",
         fmt: str = "md", name: str = "ocr-result", mode: str = "fast",
         markdown: str = "", structured_json: str = "", raw: str = "",
         elapsed: float = 0.0, image_paths: str = "", model: str = "") -> dict:
    if action == "status":
        return _status()

    if action == "ingest":
        return _ingest(image_data=image_data, path=path)

    if action == "history_list":
        return {"records": store.list_records(query=query)}

    if action == "history_get":
        return store.get(record_id)

    if action == "history_save":
        structured = json.loads(structured_json) if structured_json else None
        paths = json.loads(image_paths) if image_paths else ([path] if path else [])
        return store.save(mode=mode, source_paths=paths, markdown=markdown,
                          structured=structured, raw=raw, elapsed=float(elapsed),
                          model_repo=model)

    if action == "history_delete":
        store.delete(record_id)
        return {"ok": True}

    if action == "export":
        return _export(content=content, fmt=fmt, name=name)

    raise ValueError(f"Unknown action: {action!r}")


def _status() -> dict:
    return {
        "app_dir": str(APP_DIR),
        "mlx_vlm": importlib.util.find_spec("mlx_vlm") is not None,
    }


def _ingest(image_data: str, path: str) -> dict:
    from PIL import Image

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    if path:
        raw_bytes = Path(path).read_bytes()
    elif image_data:
        raw_bytes = base64.b64decode(image_data.split(",", 1)[-1])
    else:
        raise ValueError("No image supplied")

    if raw_bytes[:5] == b"%PDF-":
        return _ingest_pdf(raw_bytes)

    tmp = SCRATCH_DIR / "incoming.bin"
    tmp.write_bytes(raw_bytes)
    image = Image.open(tmp).convert("RGB")
    dest = SCRATCH_DIR / f"scan-{int(time.time() * 1000)}.png"
    image.save(dest, "PNG")
    return {"paths": [str(dest)], "page_count": 1, "width": image.width, "height": image.height}


def _ingest_pdf(pdf_bytes: bytes) -> dict:
    import pymupdf

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        paths = []
        width = height = 0
        for index, page in enumerate(doc):
            pixmap = page.get_pixmap(dpi=200)
            dest = SCRATCH_DIR / f"scan-{stamp}-p{index + 1}.png"
            pixmap.save(str(dest))
            paths.append(str(dest))
            width, height = pixmap.width, pixmap.height
        if not paths:
            raise ValueError("That PDF has no pages")
        return {"paths": paths, "page_count": len(paths), "width": width, "height": height}
    finally:
        doc.close()


def _export(content: str, fmt: str, name: str) -> dict:
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "json" if fmt == "json" else "md"
    safe_name = _SAFE_NAME.sub("-", name).strip("-") or "ocr-result"
    dest = SCRATCH_DIR / f"{safe_name}.{suffix}"
    dest.write_text(content, encoding="utf-8")
    return {"path": str(dest), "name": dest.name}
