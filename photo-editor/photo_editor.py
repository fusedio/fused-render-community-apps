"""Cheap, stateless operations for the page -- everything that is not a model.

These run through `fused.runPython`, i.e. a fresh subprocess with a 60 s budget,
so nothing here may load weights. Uploads, mask normalisation, Pillow
adjustments, background removal and export all finish in well under a second to
a few seconds. Upscale and smart select live in the warm worker (`worker.py` /
`engine.py`); generative edits run through `fused.ai.image` from the page.
"""

from __future__ import annotations

import os
import time

import docstore
import imaging

APP_DIR = os.path.dirname(os.path.abspath(__file__))
AI_IMAGES_DIR = os.path.realpath(os.path.expanduser("~/.fused-render/ai/images"))


def main(action: str = "status", session_id: str = "", image_data: str = "",
         name: str = "photo", source: str = "", values_json: str = "",
         fmt: str = "png", quality: int = 92, background: str = "#ffffff",
         grow: float = 0.0, filter_name: str = "", filter_strength: float = 100.0,
         x: float = 0.5, y: float = 0.5, mode: str = "", generated: str = "",
         mask: str = "", feather: float = 3.0, dpi: float = 0.0,
         doc_id: str = "", snap_json: str = "", label: str = "", base_rev: int = -1,
         rev: int = 0, thumb: str = "", save: bool = False, file_name: str = "",
         path: str = "", request_id: str = "", prev_doc: str = "") -> dict:
    if action == "status":
        return _status()

    if action == "view_save":
        import json

        payload = json.loads(values_json or "{}")
        return {"ok": True, "view": docstore.set_view(payload.get("view") or {}, "user",
                                                      base_seq=payload.get("base_seq", ""))}

    if action == "screenshot_save":
        import json

        return {"ok": True, **docstore.save_screenshot(request_id, docstore.decode_png(image_data) if image_data else b"",
                                                        json.loads(values_json or "{}"))}

    if action.startswith("doc_") or action == "reveal":
        return _documents(action, doc_id=doc_id, name=name, snap_json=snap_json, label=label,
                          base_rev=base_rev, rev=rev, thumb=thumb, path=path, prev_doc=prev_doc)

    if action == "new_session":
        session_id = session_id or imaging.new_session_id()
        result = imaging.save_source(session_id, image_data, name=name)
        return {**result, "url_path": result["path"]}

    if action == "save_mask":
        return imaging.save_mask(session_id, image_data,
                                 source_path=_resolve(session_id, source), grow=grow)

    if action == "ai_prepare":
        prepared = imaging.prepare_ai_edit(session_id, _resolve(session_id, source), mode=mode or "remove")
        return {**prepared, "marked_rel": os.path.relpath(prepared["marked"], APP_DIR)}

    if action == "ai_composite":
        generated_path = os.path.realpath(generated)
        if os.path.commonpath([generated_path, AI_IMAGES_DIR]) != AI_IMAGES_DIR:
            raise ValueError("Refusing to read a generated image from outside fused-render's AI folder")
        folder = imaging.session_dir(session_id, create=True)
        mask_path = _resolve(session_id, mask)
        output = os.path.join(folder, f"result-{int(time.time() * 1000)}.png")
        return imaging.feathered_composite(_resolve(session_id, source), generated_path,
                                           mask_path, output, feather=float(feather),
                                           mode=mode or "remove")

    if action == "remove_background":
        import bgremove

        folder = imaging.session_dir(session_id, create=True)
        output = os.path.join(folder, f"cutout-{int(time.time() * 1000)}.png")
        try:
            return {"ok": True, **bgremove.remove_background(_resolve(session_id, source), output)}
        except bgremove.BackgroundRemovalError as error:
            # An expected, explainable outcome (no subject found, old macOS) --
            # the page shows this as a message, not a traceback overlay.
            return {"ok": False, "message": str(error)}

    if action == "subject_at_point":
        import bgremove

        folder = imaging.session_dir(session_id, create=True)
        output = os.path.join(folder, "smart-mask.png")
        try:
            return {"ok": True, **bgremove.subject_mask_at_point(
                _resolve(session_id, source), output, float(x), float(y))}
        except bgremove.BackgroundRemovalError as error:
            return {"ok": False, "message": str(error)}

    if action == "adjust":
        import json

        folder = imaging.session_dir(session_id, create=True)
        output = os.path.join(folder, f"adjusted-{int(time.time() * 1000)}.png")
        values = json.loads(values_json or "{}")
        return {"ok": True, **imaging.apply_adjustments(_resolve(session_id, source), output, values)}

    if action == "filter":
        folder = imaging.session_dir(session_id, create=True)
        output = os.path.join(folder, f"filtered-{int(time.time() * 1000)}.png")
        return {"ok": True, **imaging.apply_filter_preset(
            _resolve(session_id, source), output, filter_name, float(filter_strength))}

    if action == "export":
        folder = imaging.session_dir(session_id, create=True)
        suffix = {"jpeg": "jpg", "tiff": "tif"}.get(fmt, fmt)
        if suffix not in ("png", "jpg", "webp", "pdf", "tif"):
            raise ValueError(f"Unsupported export format: {fmt!r}")
        output = (docstore.save_path(file_name or name, suffix) if save
                  else os.path.join(folder, f"export-{int(time.time() * 1000)}.{suffix}"))
        return {"ok": True, **imaging.export_image(_resolve(session_id, source), output,
                                                   fmt=fmt, quality=int(quality),
                                                   background=background, dpi=float(dpi))}

    if action == "save_frame":
        # The page hands back a canvas it composed itself (crop, flip, rotate),
        # which then becomes the new working image for every later server op.
        folder = imaging.session_dir(session_id, create=True)
        output = os.path.join(folder, f"frame-{int(time.time() * 1000)}.png")
        imaging.write_atomic(output, imaging.decode_data_url(image_data))
        return {"ok": True, "path": output}

    raise ValueError(f"Unknown action: {action!r}")


def _documents(action: str, doc_id: str, name: str, snap_json: str, label: str,
               base_rev: int, rev: int, thumb: str, path: str, prev_doc: str = "") -> dict:
    """Autosave, history and the document list (see docstore.py)."""
    import json

    png = docstore.decode_png(thumb) if thumb else None
    if action == "doc_create":
        record = docstore.create(name or "Untitled", json.loads(snap_json), source="user",
                                 label=label or "new document", thumb_png=png, doc_id=doc_id,
                                 claim_from=prev_doc)
        return {"ok": True, "id": record["id"], "rev": record["rev"]}
    if action == "doc_save":
        result = docstore.commit(doc_id, json.loads(snap_json), label, "user",
                                 base_rev=None if base_rev < 0 else base_rev, thumb_png=png)
        if not result["ok"]:
            return {"ok": False, "conflict": True, "rev": result["rev"], "doc": result["doc"]}
        return {"ok": True, "id": doc_id, "rev": result["rev"]}
    if action == "doc_load":
        record = docstore.load(doc_id)
        docstore.set_current(doc_id, record["rev"], "user")
        return {"ok": True, "doc": record}
    if action == "doc_rename":
        return {"ok": True, "name": docstore.rename(doc_id, name)["name"]}
    if action == "doc_list":
        return {"documents": docstore.list_documents(), "current": docstore.get_current(),
                "save_folder": docstore.SAVE_DIR}
    if action == "doc_history":
        return {"steps": docstore.history(doc_id)}
    if action == "doc_restore":
        result = docstore.restore(doc_id, rev, "user")
        return {"ok": True, "doc": result["doc"]}
    if action == "doc_delete":
        docstore.delete(doc_id)
        return {"ok": True}
    if action == "reveal":
        import subprocess

        target = os.path.realpath(path or docstore.SAVE_DIR)
        root = os.path.realpath(docstore.SAVE_DIR)
        if os.path.commonpath([target, root]) != root:
            raise ValueError("Only files in the save folder can be revealed")
        os.makedirs(root, exist_ok=True)
        subprocess.run(["open", "-R", target] if os.path.isfile(target) else ["open", target], check=False)
        return {"ok": True}
    raise ValueError(f"Unknown action: {action!r}")


def _resolve(session_id: str, source: str) -> str:
    """Pick the working image: whatever the page names, else the session source."""
    return imaging.resolve_source(session_id, source)


def _status() -> dict:
    """What the page needs to decide which features to offer."""
    import importlib.util

    report = {"vision": False, "vision_message": "",
              "sam": False, "seedvr2": False}

    try:
        import Vision

        report["vision"] = hasattr(Vision, "VNGenerateForegroundInstanceMaskRequest")
        if not report["vision"]:
            report["vision_message"] = "Needs macOS 14 or newer for subject lifting"
    except ImportError as error:
        report["vision_message"] = f"Vision framework unavailable: {error}"

    for key, module in (("sam", "mlx_sam"), ("seedvr2", "mflux.models.seedvr2")):
        try:
            report[key] = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            report[key] = False

    return report
