"""Warm-worker entry point, declared by `[tool.fused-render.app] main =`.

Every call here must return in well under a minute: the shipped worker's HTTP
handler times out at 65 s, and a render can still run past that. So this
file only ever starts work and reads status -- `engine.py` owns the threads and
keeps the model resident between calls.
"""

from __future__ import annotations

import json

import engine


def main(action: str = "status", session_id: str = "", job_id: str = "",
         source: str = "", points: str = "[]", labels: str = "[]",
         scale: str = "2x", softness: float = 0.0) -> dict:
    if action == "status":
        return {"upscaler": engine.upscaler_status(), "sam": engine.sam_status()}

    if action == "warmup":
        # Fire-and-forget: returns the current state, does not wait for weights.
        return {"sam": engine.ensure_sam()}

    if action == "sam_status":
        return engine.sam_status()

    if action == "smart_mask":
        return engine.smart_mask(
            session_id=session_id, source=source,
            points=_as_list(points), labels=_as_list(labels),
        )

    if action == "upscaler_status":
        return engine.upscaler_status()

    if action == "upscale_start":
        started = engine.start_upscale(
            session_id=session_id, source=source, scale=str(scale),
            softness=float(softness), job_id=job_id,
        )
        return {**started, "upscaler": engine.upscaler_status()}

    if action == "edit_poll":
        return engine.poll(job_id)

    if action == "edit_cancel":
        return engine.cancel(job_id)

    raise ValueError(f"Unknown action: {action!r}")


def _as_list(value) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    return json.loads(value or "[]")
