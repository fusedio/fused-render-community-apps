from __future__ import annotations

import json

import capture
import engine


def main(action: str = "status", mode: str = "fast", image_paths: str = "",
         prompt: str = "", schema: str = "", max_tokens: int = 4096,
         job_id: str = "", repo: str = "") -> dict:
    if action == "status":
        return {"models": engine.all_model_status()}

    if action == "screenshot_start":
        return capture.start()

    if action == "screenshot_poll":
        return capture.poll(job_id)

    if action == "screenshot_cancel":
        return capture.cancel(job_id)

    if action == "catalog":
        return engine.catalog()

    if action == "select_model":
        return {"model": engine.select_model(mode, repo)}

    if action == "warmup":
        return {"model": engine.ensure_model(mode)}

    if action == "ocr_start":
        paths = json.loads(image_paths) if image_paths else []
        started = engine.start_ocr(image_paths=paths, mode=mode, prompt=prompt,
                                   schema=schema, max_tokens=int(max_tokens), job_id=job_id)
        return {**started, "model": engine.model_status(mode)}

    if action == "ocr_poll":
        return engine.poll(job_id)

    if action == "ocr_cancel":
        return engine.cancel(job_id)

    raise ValueError(f"Unknown action: {action!r}")
