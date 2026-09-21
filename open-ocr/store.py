from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
HISTORY_DIR = APP_DIR / ".fused" / "data" / "history"

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def _record_dir(record_id: str, create: bool = False) -> Path:
    safe_id = _SAFE_ID.sub("", record_id)
    if not safe_id:
        raise ValueError(f"Invalid history record id: {record_id!r}")
    folder = HISTORY_DIR / safe_id
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def save(mode: str, source_paths: list[str], markdown: str, structured, raw: str, elapsed: float,
        model_repo: str = "") -> dict:
    """Keeps a full-resolution copy of every source page (not just a thumbnail)
    under this record, so a history entry can be re-run later -- possibly with
    a different model -- without the original scratch file still existing."""
    from PIL import Image

    record_id = time.strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
    folder = _record_dir(record_id, create=True)
    pages_dir = folder / "pages"

    saved_paths = []
    has_thumb = False
    for index, source_path in enumerate(source_paths):
        if not source_path or not os.path.exists(source_path):
            continue
        try:
            image = Image.open(source_path).convert("RGB")
        except Exception:
            continue
        pages_dir.mkdir(parents=True, exist_ok=True)
        dest = pages_dir / f"page-{index + 1}.png"
        image.save(dest, "PNG")
        saved_paths.append(str(dest))
        if index == 0:
            thumb = image.copy()
            thumb.thumbnail((360, 360))
            thumb.save(folder / "thumb.jpg", "JPEG", quality=82)
            has_thumb = True

    record = {
        "id": record_id,
        "mode": mode,
        "model_repo": model_repo,
        "created_at": time.time(),
        "elapsed": elapsed,
        "snippet": " ".join(markdown.split())[:160],
        "markdown": markdown,
        "structured": structured,
        "raw": raw,
        "has_thumb": has_thumb,
        "image_paths": saved_paths,
    }
    with (folder / "record.json").open("w", encoding="utf-8") as file:
        json.dump(record, file)
    return {"id": record_id, "has_thumb": has_thumb, "page_count": len(saved_paths)}


def list_records(query: str = "", limit: int = 200) -> list[dict]:
    if not HISTORY_DIR.exists():
        return []
    query = query.strip().lower()
    records = []
    for folder in sorted(HISTORY_DIR.iterdir(), reverse=True):
        record_path = folder / "record.json"
        if not record_path.exists():
            continue
        with record_path.open("r", encoding="utf-8") as file:
            record = json.load(file)
        haystack = (record.get("snippet", "") + " " + record.get("markdown", "")).lower()
        if query and query not in haystack:
            continue
        thumb = folder / "thumb.jpg"
        records.append({
            "id": record["id"],
            "mode": record["mode"],
            "model_repo": record.get("model_repo", ""),
            "created_at": record["created_at"],
            "elapsed": record.get("elapsed", 0),
            "snippet": record["snippet"],
            "thumb_path": str(thumb) if thumb.exists() else "",
        })
        if len(records) >= limit:
            break
    return records


def get(record_id: str) -> dict:
    record_path = _record_dir(record_id) / "record.json"
    if not record_path.exists():
        raise FileNotFoundError(f"No such history record: {record_id}")
    with record_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def delete(record_id: str) -> None:
    import shutil

    folder = _record_dir(record_id)
    if folder.exists():
        shutil.rmtree(folder)
