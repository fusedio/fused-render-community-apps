"""The resident SeedVR2 upscaler and SAM smart-select engine.

This module is deliberately separate from `worker.py`. The shipped warm worker
re-imports its target module whenever the file's mtime changes, which would
throw away a loaded model on every save of the dispatcher. `import engine`
resolves from `sys.modules` instead, so the weights below survive edits to
`worker.py` and live for the whole lifetime of the worker process.

Generative remove/insert does not live here: the page calls fused-render's
own `fused.ai.image` edit runner, so the FLUX weights are shared with every
other app instead of being loaded a second time in this process.

Nothing here downloads weights on its own. The page shows what is missing and
the user starts `start_download` explicitly; until then SAM falls back to the
page's Vision path and the upscaler refuses to start.
"""

from __future__ import annotations

from collections import OrderedDict
import gc
import json
import os
import queue
import re
import threading
import time
import traceback
import urllib.request

import imaging

APP_DIR = os.path.dirname(os.path.abspath(__file__))

#: Job ids come from the page and end up in dict keys and JSON, so keep them to
#: a boring alphabet rather than trusting the caller.
_SAFE_JOB = re.compile(r"[^A-Za-z0-9_.-]")

UPSCALER_REPO = os.environ.get("PHOTO_EDITOR_UPSCALER") or "AbstractFramework/seedvr2-3b-8bit"
SAM_REPO = os.environ.get("PHOTO_EDITOR_SAM") or "avbiswas/sam2.1-hiera-base-plus-mlx"
SAM_FILE = "sam2.1_hiera_base_plus_image_segmenter.safetensors"
SAM_MODEL_ID = "facebook/sam2.1-hiera-base-plus"
SAM_IMAGE_SIZE = 1024

#: Shown on the Download button before the first byte arrives; the real total
#: comes from the Hub once the download starts.
UPSCALER_SIZE_GB = 4.7
SAM_SIZE_GB = 0.35

MAX_UPSCALE_EDGE = 8192
MAX_UPSCALE_PIXELS = 48_000_000
UPSCALE_SCALES = {"2x": 2, "3x": 3, "2160": 2160}

_model_lock = threading.Lock()
_upscaler = None
_upscaler_state = {"status": "idle", "stage": "", "error": "", "started_at": 0.0, "loaded_at": 0.0}

_sam_lock = threading.Lock()
_sam = None
_sam_state = {"status": "idle", "stage": "", "error": "", "started_at": 0.0, "loaded_at": 0.0}
_sam_features: "OrderedDict[tuple, tuple]" = OrderedDict()
_sam_queue: "queue.Queue" = queue.Queue()
_sam_thread: "threading.Thread | None" = None
_sam_thread_lock = threading.Lock()

_downloads_lock = threading.Lock()
_downloads: dict[str, dict] = {}

_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Every MLX call -- loading the weights AND every render -- runs on this one
# thread. MLX binds an array to the thread that created it, so evaluating on a
# different thread from the one that built the weights fails outright with
# "There is no Stream(gpu, 0) in current thread". Serialising is also simply
# correct: two renders cannot share this GPU usefully anyway.
_mlx_queue: "queue.Queue" = queue.Queue()
_mlx_thread: "threading.Thread | None" = None
_mlx_thread_lock = threading.Lock()


def _mlx_loop() -> None:
    while True:
        task = _mlx_queue.get()
        try:
            task()
        except BaseException:  # noqa: BLE001 - one bad task must not kill the thread
            print("[photo_editor] MLX task crashed\n" + traceback.format_exc())
        finally:
            _mlx_queue.task_done()


def _submit(task) -> None:
    """Queue work onto the single MLX thread, starting it on first use."""
    global _mlx_thread
    with _mlx_thread_lock:
        if _mlx_thread is None or not _mlx_thread.is_alive():
            _mlx_thread = threading.Thread(target=_mlx_loop, name="mlx", daemon=True)
            _mlx_thread.start()
    _mlx_queue.put(task)


def _sam_loop() -> None:
    while True:
        task = _sam_queue.get()
        try:
            task()
        except BaseException:
            print("[photo_editor] SAM task crashed\n" + traceback.format_exc())
        finally:
            _sam_queue.task_done()


def _submit_sam(task) -> None:
    global _sam_thread
    with _sam_thread_lock:
        if _sam_thread is None or not _sam_thread.is_alive():
            _sam_thread = threading.Thread(target=_sam_loop, name="sam", daemon=True)
            _sam_thread.start()
    _sam_queue.put(task)


def _free_mlx_memory() -> None:
    gc.collect()
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass


def unload_upscaler(timeout: float = 45.0) -> dict:
    """Drop the upscaler so the page's FLUX edit has the memory to itself.

    Always queued on the MLX thread, even when nothing is resident yet: a
    cancelled upscale may still be loading weights there, and only a task
    queued behind it sees the model it ends up holding. `freed` is False when
    that thread is still busy after `timeout`; the page asks again rather than
    starting FLUX next to a resident SeedVR2.
    """
    done = threading.Event()

    def task() -> None:
        global _upscaler
        try:
            if _upscaler is not None:
                with _model_lock:
                    _upscaler = None
                    _upscaler_state.update(status="idle", stage="Unloaded to make room for AI edit", loaded_at=0.0)
                _free_mlx_memory()
        finally:
            done.set()

    _submit(task)
    freed = done.wait(timeout)
    return {**upscaler_status(), "freed": freed}


# --- Weights on disk ---------------------------------------------------------

def _upscaler_patterns() -> list[str]:
    from mflux.models.common.config import ModelConfig
    from mflux.models.seedvr2.weights.seedvr2_weight_definition import SeedVR2WeightDefinition

    return SeedVR2WeightDefinition.get_download_patterns_for_source(ModelConfig.seedvr2_3b(), UPSCALER_REPO)


def upscaler_downloaded() -> bool:
    """True when mflux would load the upscaler without touching the network."""
    try:
        from mflux.models.common.resolution.path_resolution import PathResolution

        patterns = _upscaler_patterns()
        return any(PathResolution._check(check, UPSCALER_REPO, patterns)
                   for check in ("exists_locally", "has_local_prepared_hf", "is_hf_cached"))
    except Exception:
        return False


def sam_downloaded() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache(SAM_REPO, SAM_FILE), str)
    except Exception:
        return False


def _cache_bytes(repo: str) -> int:
    """Bytes of `repo` in the Hub cache so far, partial files included."""
    from huggingface_hub import constants

    blobs = os.path.join(constants.HF_HUB_CACHE, "models--" + repo.replace("/", "--"), "blobs")
    total = 0
    try:
        with os.scandir(blobs) as entries:
            for entry in entries:
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def download_status(which: str) -> dict:
    with _downloads_lock:
        return dict(_downloads.get(which) or {"state": "idle"})


def start_download(which: str) -> dict:
    """Fetch the upscaler or SAM weights -- only ever on the user's click."""
    if which not in ("upscaler", "sam"):
        raise ValueError(f"Unknown download: {which!r}")
    with _downloads_lock:
        current = _downloads.get(which)
        if current and current["state"] == "running":
            return dict(current)
        _downloads[which] = {"state": "running", "done": 0, "total": 0, "error": "",
                             "job_id": f"photo-editor-download-{which}", "started_at": time.time()}
    threading.Thread(target=_run_download, args=(which,), name=f"download-{which}", daemon=True).start()
    return download_status(which)


def _run_download(which: str) -> None:
    import fnmatch

    from huggingface_hub import HfApi, hf_hub_download, snapshot_download

    job_id = f"photo-editor-download-{which}"
    title = "Upscaler weights" if which == "upscaler" else "Smart select weights"
    repo = UPSCALER_REPO if which == "upscaler" else SAM_REPO

    def update(**fields) -> None:
        with _downloads_lock:
            _downloads[which].update(fields)

    try:
        patterns = _upscaler_patterns() if which == "upscaler" else [SAM_FILE]
        try:
            info = HfApi().model_info(repo, files_metadata=True)
            total = sum(f.size or 0 for f in info.siblings
                        if any(fnmatch.fnmatch(f.rfilename, pattern) for pattern in patterns))
        except Exception:
            total = 0
        baseline = _cache_bytes(repo)
        update(total=total)
        finished = threading.Event()

        def progress() -> None:
            while not finished.wait(1.0):
                done = max(0, _cache_bytes(repo) - baseline)
                done = min(done, total) if total else done
                update(done=done)
                _report(job_id, {"state": "running", "unit": "bytes", "done": done,
                                 "total": total or None, "detail": repo}, title)

        ticker = threading.Thread(target=progress, daemon=True)
        ticker.start()
        try:
            if which == "upscaler":
                snapshot_download(repo_id=repo, allow_patterns=patterns)
            else:
                hf_hub_download(repo, SAM_FILE)
        finally:
            finished.set()
            # A late "running" tick must not land after the terminal report.
            ticker.join(timeout=5)
        update(state="done", done=total)
        _report(job_id, {"state": "done", "unit": "bytes", "done": total, "total": total or None,
                         "detail": "Downloaded"}, title)
    except BaseException as error:
        message = f"{type(error).__name__}: {error}"
        update(state="error", error=message)
        _report(job_id, {"state": "error", "detail": message}, title)
        print(f"[photo_editor] {which} download failed\n" + traceback.format_exc())


# --- Job-row reporting -----------------------------------------------------

def _report(job_id: str, payload: dict, title: str) -> dict:
    """Best-effort progress POST to the shell's download manager.

    The worker outlives the page, so it -- not the page -- is the only thing
    that can keep the row honest once the user navigates away. Every failure is
    swallowed: a progress row must never be able to break a render.
    """
    origin = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
    if not origin:
        return {}
    body = {"id": job_id, "title": title, "kind": "task", **payload}
    request = urllib.request.Request(
        f"{origin}/api/jobs",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Fused": "1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except Exception:
        return {}


def upscaler_status() -> dict:
    with _model_lock:
        state = dict(_upscaler_state)
    state["loaded"] = _upscaler is not None
    state["model"] = UPSCALER_REPO
    state["downloaded"] = state["loaded"] or upscaler_downloaded()
    state["size_gb"] = UPSCALER_SIZE_GB
    state["download"] = download_status("upscaler")
    if state["status"] == "loading" and state["started_at"]:
        state["elapsed"] = round(time.time() - state["started_at"], 1)
    return state


def _load_upscaler_here() -> None:
    global _upscaler
    if _upscaler is not None:
        return
    try:
        with _model_lock:
            _upscaler_state.update(status="loading", stage="Importing MLX runtime", error="",
                                   started_at=time.time())

        from mflux.models.common.config import ModelConfig
        from mflux.models.seedvr2 import SeedVR2

        with _model_lock:
            _upscaler_state["stage"] = f"Loading {UPSCALER_REPO}"

        upscaler = SeedVR2(model_config=ModelConfig.seedvr2_3b(), model_path=UPSCALER_REPO)
        with _model_lock:
            _upscaler = upscaler
            _upscaler_state.update(status="ready", stage="Upscaler resident", loaded_at=time.time())
    except BaseException as error:
        with _model_lock:
            _upscaler_state.update(status="error", stage="", error=f"{type(error).__name__}: {error}")
        print("[photo_editor] upscaler load failed\n" + traceback.format_exc())


def sam_status() -> dict:
    with _sam_lock:
        state = dict(_sam_state)
    state["loaded"] = _sam is not None
    state["model"] = SAM_REPO
    state["downloaded"] = state["loaded"] or sam_downloaded()
    state["size_gb"] = SAM_SIZE_GB
    state["download"] = download_status("sam")
    if state["status"] == "loading" and state["started_at"]:
        state["elapsed"] = round(time.time() - state["started_at"], 1)
    return state


def _sam_checkpoint() -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(SAM_REPO, SAM_FILE, local_files_only=True)


def _load_sam_here() -> None:
    global _sam
    if _sam is not None:
        return
    try:
        with _sam_lock:
            _sam_state.update(status="loading", stage="Locating Smart select weights", error="",
                              started_at=time.time())
        checkpoint = _sam_checkpoint()
        with _sam_lock:
            _sam_state["stage"] = f"Loading {SAM_REPO}"

        from mlx_sam.weights import load_image_segmenter

        model = load_image_segmenter(checkpoint, model_id=SAM_MODEL_ID)
        with _sam_lock:
            _sam = model
            _sam_state.update(status="ready", stage="Smart select resident", loaded_at=time.time())
    except BaseException as error:
        with _sam_lock:
            _sam_state.update(status="error", stage="", error=f"{type(error).__name__}: {error}")
        print("[photo_editor] SAM load failed\n" + traceback.format_exc())


def ensure_sam() -> dict:
    if not sam_downloaded():
        return sam_status()
    with _sam_lock:
        idle = _sam is None and _sam_state["status"] not in ("loading", "queued")
        if idle:
            _sam_state.update(status="queued", stage="Queued for the SAM thread", error="",
                              started_at=time.time())
    if idle:
        _submit_sam(_load_sam_here)
    return sam_status()


def _sam_features_for(source_path: str):
    import mlx.core as mx
    from PIL import Image
    from mlx_sam.preprocess import preprocess_image

    key = (source_path, os.path.getmtime(source_path))
    entry = _sam_features.get(key)
    if entry is not None:
        _sam_features.move_to_end(key)
        return entry
    with Image.open(source_path) as handle:
        # A cut-out's transparent pixels would otherwise read as black.
        image, _ = imaging.split_alpha(handle)
    width, height = image.size
    encoded = _sam.encode_image(mx.array(preprocess_image(image, SAM_IMAGE_SIZE)))
    mx.eval(encoded["vision_features"], *encoded["high_res_features"])
    entry = (encoded, width, height)
    _sam_features[key] = entry
    while len(_sam_features) > 2:
        _sam_features.popitem(last=False)
    return entry


def _smart_mask_here(session_id: str, source_path: str, points, labels) -> dict:
    import mlx.core as mx
    import numpy as np
    from PIL import Image
    from mlx_sam.video_predictor import _best_low_mask, _original_to_sam

    encoded, width, height = _sam_features_for(source_path)
    pts = np.array(points, dtype=np.float32).reshape(-1, 2)
    lbl = np.array(labels, dtype=np.int32).reshape(-1)
    if pts.shape[0] == 0 or pts.shape[0] != lbl.shape[0]:
        raise ValueError("Smart select needs one label per point")
    sam_points = _original_to_sam(pts, width, height, SAM_IMAGE_SIZE)
    out = _sam.predict_from_encoded(
        encoded, mx.array(sam_points[None]), mx.array(lbl[None]),
        multimask_output=(pts.shape[0] == 1),
    )
    low = np.array(_best_low_mask(out), dtype=np.float32)[0, 0]
    score = float(np.array(out["object_score_logits"]).ravel()[0])
    logits = Image.fromarray(low, mode="F").resize((width, height), Image.BILINEAR)
    binary = np.asarray(logits, dtype=np.float32) > 0
    mask = Image.fromarray((binary.astype(np.uint8) * 255), mode="L")
    path = os.path.join(imaging.session_dir(session_id, create=True), "smart-mask.png")
    mask.save(path + ".tmp.png", "PNG")
    os.replace(path + ".tmp.png", path)
    return {
        "path": path, "width": width, "height": height,
        "coverage": round(float(binary.mean()), 5), "score": round(score, 3),
        "empty": bool(not binary.any()),
    }


def smart_mask(session_id: str, source: str = "", points=None, labels=None) -> dict:
    status = ensure_sam()
    if status["status"] != "ready":
        return {"ready": False, "downloaded": status["downloaded"], "status": status}
    source_path = imaging.resolve_source(session_id, source)
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"No such image: {source_path}")
    done = threading.Event()
    box: dict = {}

    def task() -> None:
        try:
            box["result"] = _smart_mask_here(session_id, source_path, points or [], labels or [])
        except BaseException as error:
            box["error"] = error
        finally:
            done.set()

    _submit_sam(task)
    if not done.wait(40):
        raise TimeoutError("Smart select took too long; try again")
    if "error" in box:
        raise box["error"]
    return {"ready": True, **box["result"]}


# --- Generation ------------------------------------------------------------

def _job_update(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated_at"] = time.time()


def _cancel_requested(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return bool(job and job.get("cancel"))


class _Cancelled(Exception):
    """Raised inside the progress callback to unwind a cancelled render."""


def _run_upscale(job_id: str, request: dict) -> None:
    started = time.perf_counter()
    source_path = request["source_path"]
    result_path = request["result_path"]
    title = "Upscale"

    try:
        _job_update(job_id, state="running", stage="Loading upscaler", progress=0.02)
        _report(job_id, {"state": "running", "detail": "Loading upscaler", "done": 2, "total": 100}, title)

        _load_upscaler_here()
        status = upscaler_status()
        if status["status"] != "ready":
            raise RuntimeError(status.get("error") or "Upscaler failed to load")
        if _cancel_requested(job_id):
            raise _Cancelled()

        from mflux.utils.scale_factor import ScaleFactor

        scale = request["scale"]
        resolution = UPSCALE_SCALES[scale] if scale == "2160" else ScaleFactor(UPSCALE_SCALES[scale])
        seed = int(request["seed"])
        _job_update(job_id, stage="Encoding", progress=0.08, seed=seed)
        _report(job_id, {"state": "running", "detail": "Encoding", "done": 8, "total": 100}, title)

        last_report = [0.0]

        def on_progress(event) -> None:
            if getattr(event, "phase", "") != "denoise":
                return
            step = int(getattr(event, "step", 0) or 0)
            total = int(getattr(event, "total_steps", 1) or 1)
            fraction = 0.1 + 0.75 * (step / max(total, 1))
            _job_update(job_id, stage=f"Denoising step {step}/{total}",
                        progress=round(fraction, 4), step=step, total_steps=total)
            now = time.time()
            if now - last_report[0] >= 1.0:
                last_report[0] = now
                reply = _report(job_id, {
                    "state": "running", "done": int(fraction * 100), "total": 100,
                    "detail": f"Step {step}/{total}",
                }, title)
                if reply.get("cancel_requested"):
                    _job_update(job_id, cancel=True)
            if _cancel_requested(job_id):
                raise _Cancelled()

        model = _upscaler
        unsubscribe = None
        try:
            unsubscribe = model.callbacks.subscribe_progress(on_progress)
        except Exception:
            unsubscribe = None

        try:
            generated = model.generate_image(
                seed=seed,
                image_path=source_path,
                resolution=resolution,
                softness=float(request.get("softness") or 0.0),
                num_inference_steps=None,
            )
        finally:
            if callable(unsubscribe):
                try:
                    unsubscribe()
                except Exception:
                    pass

        if _cancel_requested(job_id):
            raise _Cancelled()

        _job_update(job_id, stage="Decoding", progress=0.9)
        _report(job_id, {"state": "running", "detail": "Decoding", "done": 90, "total": 100}, title)
        _job_update(job_id, stage="Saving", progress=0.96)
        generated.save(path=result_path, overwrite=True)

        from PIL import Image

        alpha_path = request.get("alpha_path")
        if alpha_path:
            with Image.open(result_path) as handle:
                upscaled = handle.convert("RGB")
            with Image.open(alpha_path) as handle:
                alpha = handle.convert("L").resize(upscaled.size, Image.LANCZOS)
            upscaled.putalpha(alpha)
            upscaled.save(result_path + ".tmp.png", "PNG")
            os.replace(result_path + ".tmp.png", result_path)
            for extra in (source_path, alpha_path):
                try:
                    os.remove(extra)
                except OSError:
                    pass

        with Image.open(result_path) as handle:
            width, height = handle.size

        elapsed = time.perf_counter() - started
        _job_update(job_id, state="done", stage="Complete", progress=1.0,
                    result_path=result_path, width=width, height=height,
                    elapsed=round(elapsed, 1))
        _report(job_id, {"state": "done", "done": 100, "total": 100,
                         "detail": f"Finished in {elapsed:.0f}s"}, title)

    except _Cancelled:
        _job_update(job_id, state="cancelled", stage="Cancelled", progress=0)
        _report(job_id, {"state": "cancelled", "detail": "Cancelled"}, title)
    except BaseException as error:
        message = f"{type(error).__name__}: {error}"
        _job_update(job_id, state="error", stage="Failed", error=message,
                    traceback=traceback.format_exc())
        _report(job_id, {"state": "error", "detail": message}, title)
        print("[photo_editor] upscale failed\n" + traceback.format_exc())


def upscale_target(width: int, height: int, scale: str) -> tuple[int, int, float]:
    if scale not in UPSCALE_SCALES:
        raise ValueError(f"Unknown upscale factor: {scale!r}")
    factor = UPSCALE_SCALES[scale] / min(width, height) if scale == "2160" else float(UPSCALE_SCALES[scale])
    if factor <= 1.0:
        raise ValueError("This image's short edge is already 2160 px or larger")
    out_w, out_h = round(width * factor), round(height * factor)
    if max(out_w, out_h) > MAX_UPSCALE_EDGE or out_w * out_h > MAX_UPSCALE_PIXELS:
        raise ValueError(
            f"{out_w} x {out_h} is too large to upscale here (limit {MAX_UPSCALE_EDGE} px on the "
            f"long edge or {MAX_UPSCALE_PIXELS // 1_000_000} megapixels)"
        )
    return out_w, out_h, factor


def start_upscale(session_id: str, source: str = "", scale: str = "2x", softness: float = 0.0,
                  job_id: str = "") -> dict:
    from PIL import Image

    if _upscaler is None and not upscaler_downloaded():
        raise RuntimeError("The upscaler weights are not downloaded yet -- use Download in the Upscale panel")
    folder = imaging.session_dir(session_id)
    source_path = imaging.resolve_source(session_id, source)
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"This session has no photo yet ({source_path})")
    with Image.open(source_path) as handle:
        width, height = handle.size
        flat, alpha = imaging.split_alpha(handle)
    out_w, out_h, factor = upscale_target(width, height, str(scale))
    softness = max(0.0, min(1.0, float(softness or 0.0)))
    seed = int.from_bytes(os.urandom(4), "big") % (2**31)

    job_id = _SAFE_JOB.sub("", job_id)[:96] or f"photo-upscale-{session_id}-{int(time.time() * 1000)}"
    request = {
        "source_path": source_path, "result_path": os.path.join(folder, f"upscaled-{job_id}.png"),
        "scale": str(scale), "softness": softness, "seed": seed,
    }
    if alpha is not None:
        request["source_path"] = os.path.join(folder, f"upscale-input-{job_id}.png")
        request["alpha_path"] = os.path.join(folder, f"upscale-alpha-{job_id}.png")
        flat.save(request["source_path"], "PNG")
        alpha.save(request["alpha_path"], "PNG")
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id, "state": "queued", "stage": "Queued", "progress": 0.0,
            "created_at": time.time(), "updated_at": time.time(), "seed": seed,
            "kind": "upscale", "scale": str(scale), "target_width": out_w, "target_height": out_h,
        }
    _submit(lambda: _run_upscale(job_id, request))
    return {"job_id": job_id, "seed": seed, "scale": str(scale), "factor": round(factor, 4),
            "target_width": out_w, "target_height": out_h,
            "queued_behind": max(_mlx_queue.qsize() - 1, 0)}


def poll(job_id: str) -> dict:
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return {"state": "unknown", "stage": "No such job", "progress": 0.0}
    if job.get("kind") == "upscale":
        job["upscaler"] = upscaler_status()
    return job


def cancel(job_id: str) -> dict:
    _job_update(job_id, cancel=True)
    return poll(job_id)
