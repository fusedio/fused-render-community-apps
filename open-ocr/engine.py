from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import traceback
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))

_SAFE_JOB = re.compile(r"[^A-Za-z0-9_.-]")

MODEL_CHOICES = {
    "fast": [
        {"repo": "mlx-community/PaddleOCR-VL-4bit", "label": "PaddleOCR-VL (4-bit)", "size_gb": 0.7},
        {"repo": "mlx-community/PaddleOCR-VL-8bit", "label": "PaddleOCR-VL (8-bit)", "size_gb": 1.1},
        {"repo": "mlx-community/PaddleOCR-VL-bfloat16", "label": "PaddleOCR-VL (bf16, full precision)", "size_gb": 1.8},
        {"repo": "mlx-community/PaddleOCR-VL-1.5-4bit", "label": "PaddleOCR-VL 1.5 (4-bit)", "size_gb": 0.7},
    ],
    "accurate": [
        {"repo": "mlx-community/dots.ocr-4bit", "label": "dots.ocr (4-bit)", "size_gb": 3.5},
        {"repo": "mlx-community/dots.ocr-8bit", "label": "dots.ocr (8-bit)", "size_gb": 4.4},
        {"repo": "mlx-community/dots.ocr-bf16", "label": "dots.ocr (bf16, full precision)", "size_gb": 6.1},
    ],
}

_active_repo: dict[str, str] = {
    mode: os.environ.get(f"OPENOCR_{mode.upper()}_MODEL") or choices[0]["repo"]
    for mode, choices in MODEL_CHOICES.items()
}

MARKDOWN_PROMPT = "Convert this page to clean Markdown while preserving reading order."

STRUCTURED_PROMPT = (
    "Please output the layout information from the PDF image, including each "
    "layout element's bbox, its category, and the corresponding text content "
    "within the bbox.\n\n"
    "1. Bbox format: [x1, y1, x2, y2]\n\n"
    "2. Layout Categories: The possible categories are ['Caption', 'Footnote', "
    "'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', "
    "'Section-header', 'Table', 'Text', 'Title'].\n\n"
    "3. Text Extraction & Formatting Rules:\n"
    "    - Picture: For the 'Picture' category, the text field should be omitted.\n"
    "    - Formula: Format its text as LaTeX.\n"
    "    - Table: Format its text as HTML.\n"
    "    - All Others (Text, Title, etc.): Format their text as Markdown.\n\n"
    "4. Constraints:\n"
    "    - The output text must be the original text from the image, with no translation.\n"
    "    - All layout elements must be sorted according to human reading order.\n\n"
    "5. Final Output: The entire output must be a single JSON object."
)

_models_lock = threading.Lock()
_models: dict[str, tuple] = {}
_model_state: dict[str, dict] = {
    mode: {"status": "idle", "stage": "", "error": "", "started_at": 0.0, "loaded_at": 0.0,
          "progress": 0.0, "downloaded_bytes": 0, "total_bytes": 0}
    for mode in MODEL_CHOICES
}

_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}

_mlx_queue: "queue.Queue" = queue.Queue()
_mlx_thread: "threading.Thread | None" = None
_mlx_thread_lock = threading.Lock()


def _mlx_loop() -> None:
    while True:
        task = _mlx_queue.get()
        try:
            task()
        except BaseException:  # noqa: BLE001
            print("[openocr] MLX task crashed\n" + traceback.format_exc())
        finally:
            _mlx_queue.task_done()


def _submit(task) -> None:
    global _mlx_thread
    with _mlx_thread_lock:
        if _mlx_thread is None or not _mlx_thread.is_alive():
            _mlx_thread = threading.Thread(target=_mlx_loop, name="mlx", daemon=True)
            _mlx_thread.start()
    _mlx_queue.put(task)


def _cache_home() -> str:
    return os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")


def _repo_cache_dir(repo: str) -> str:
    return os.path.join(_cache_home(), "hub", "models--" + repo.replace("/", "--"))


def is_downloaded(mode: str) -> bool:
    snapshot_dir = os.path.join(_repo_cache_dir(_active_repo[mode]), "snapshots")
    if not os.path.isdir(snapshot_dir):
        return False
    for root, _dirs, files in os.walk(snapshot_dir):
        if any(name.endswith((".safetensors", ".npz")) for name in files):
            return True
    return False


def _expected_bytes(mode: str) -> int:
    repo = _active_repo[mode]
    for choice in MODEL_CHOICES[mode]:
        if choice["repo"] == repo:
            return int(choice["size_gb"] * 1_000_000_000)
    return 0


def _downloaded_bytes(repo: str) -> int:
    """Sum of everything huggingface_hub has written for this repo so far,
    including in-progress `.incomplete` files -- a library-version-independent
    way to read live download progress off disk."""
    blobs_dir = os.path.join(_repo_cache_dir(repo), "blobs")
    if not os.path.isdir(blobs_dir):
        return 0
    total = 0
    for name in os.listdir(blobs_dir):
        try:
            total += os.path.getsize(os.path.join(blobs_dir, name))
        except OSError:
            pass
    return total


def catalog() -> dict:
    return {
        "choices": MODEL_CHOICES,
        "active": dict(_active_repo),
        "downloaded": {mode: is_downloaded(mode) for mode in MODEL_CHOICES},
    }


def select_model(mode: str, repo: str) -> dict:
    if mode not in MODEL_CHOICES:
        raise ValueError(f"Unknown mode: {mode!r}")
    valid_repos = {choice["repo"] for choice in MODEL_CHOICES[mode]}
    if repo not in valid_repos:
        raise ValueError(f"Unsupported model for {mode}: {repo!r}")
    with _models_lock:
        if _active_repo[mode] != repo:
            _active_repo[mode] = repo
            _models.pop(mode, None)
            _model_state[mode] = {"status": "idle", "stage": "", "error": "", "started_at": 0.0, "loaded_at": 0.0,
                                  "progress": 0.0, "downloaded_bytes": 0, "total_bytes": 0}
    return model_status(mode)


def model_status(mode: str) -> dict:
    if mode not in MODEL_CHOICES:
        # Non-MLX modes (Apple Vision) need no download or load step.
        return {"status": "ready", "stage": "", "error": "", "started_at": 0.0, "loaded_at": 0.0,
                "progress": 1.0, "downloaded_bytes": 0, "total_bytes": 0,
                "loaded": True, "model": "Apple Vision (on-device)", "downloaded": True}
    with _models_lock:
        state = dict(_model_state[mode])
    state["loaded"] = mode in _models
    state["model"] = _active_repo[mode]
    state["downloaded"] = is_downloaded(mode)
    if state["status"] == "loading" and state["started_at"]:
        state["elapsed"] = round(time.time() - state["started_at"], 1)
    return state


def all_model_status() -> dict:
    return {mode: model_status(mode) for mode in MODEL_CHOICES}


def _load_model_here(mode: str) -> None:
    if mode in _models:
        return
    repo = _active_repo[mode]
    job_id = f"openocr-download-{mode}::{repo}"
    stop_progress = threading.Event()
    progress_thread = None
    started_download = False
    try:
        with _models_lock:
            _model_state[mode].update(status="loading", stage="Importing mlx-vlm", error="",
                                      started_at=time.time(), progress=0.0,
                                      downloaded_bytes=0, total_bytes=0)

        from mlx_vlm import load

        if is_downloaded(mode):
            with _models_lock:
                _model_state[mode]["stage"] = f"Loading {repo}"
        else:
            started_download = True
            total_bytes = _expected_bytes(mode)

            def _poll_progress() -> None:
                # Also reported from here (not just the page's own poll loop)
                # so the Activity row keeps moving after the page is closed --
                # this download runs in the resident worker regardless.
                last_report = 0.0
                while not stop_progress.is_set():
                    downloaded = _downloaded_bytes(repo)
                    fraction = min(downloaded / total_bytes, 1.0) if total_bytes else 0.0
                    stage = f"Downloading {repo}" if fraction < 0.98 else "Finalizing download"
                    with _models_lock:
                        _model_state[mode].update(stage=stage, progress=min(fraction, 0.99),
                                                  downloaded_bytes=downloaded, total_bytes=total_bytes)
                    now = time.time()
                    if now - last_report >= 1.0:
                        last_report = now
                        reply = _report(job_id, {"state": "running", "done": downloaded,
                                                 "total": total_bytes, "detail": stage, "unit": "bytes"})
                        if reply.get("cancel_requested"):
                            stop_progress.set()
                    stop_progress.wait(0.5)

            progress_thread = threading.Thread(target=_poll_progress, daemon=True)
            progress_thread.start()

        model, processor = load(repo)
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=2)

        with _models_lock:
            if _active_repo[mode] != repo:
                return
            _models[mode] = (model, processor)
            _model_state[mode].update(status="ready", stage="Model resident", loaded_at=time.time(),
                                      progress=1.0)
        if started_download:
            _report(job_id, {"state": "done", "done": 100, "total": 100, "detail": "Ready"})
    except BaseException as error:  # noqa: BLE001
        stop_progress.set()
        if started_download:
            _report(job_id, {"state": "error", "detail": f"{type(error).__name__}: {error}"})
        with _models_lock:
            if _active_repo[mode] == repo:
                _model_state[mode].update(status="error", stage="", error=f"{type(error).__name__}: {error}")
        print(f"[openocr] {mode} model load failed\n" + traceback.format_exc())


def ensure_model(mode: str) -> dict:
    if mode not in MODEL_CHOICES:
        return model_status(mode)
    with _models_lock:
        idle = mode not in _models and _model_state[mode]["status"] not in ("loading", "queued")
        if idle:
            _model_state[mode].update(status="queued", stage="Queued for the MLX thread", error="",
                                      started_at=time.time())
    if idle:
        _submit(lambda: _load_model_here(mode))
    return model_status(mode)


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
    pass


_OTSL_RUN_RE = re.compile(r"(?:<(?:fcel|ecel|lcel|ucel|xcel|ched|rhed|srow|nl)>[^<]*)+")
_OTSL_CELL_RE = re.compile(r"<(fcel|ecel|lcel|ucel|xcel|ched|rhed|srow)>([^<]*)")


def _otsl_run_to_markdown(match: "re.Match[str]") -> str:
    chunk = match.group(0)
    rows = []
    for row_raw in chunk.split("<nl>"):
        cells = [content.strip() for _, content in _OTSL_CELL_RE.findall(row_raw)]
        if cells:
            rows.append(cells)
    if not rows:
        return chunk
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n" + "\n".join(lines) + "\n"


def _convert_otsl_tables(text: str) -> str:
    if "<fcel>" not in text and "<ecel>" not in text:
        return text
    return _OTSL_RUN_RE.sub(_otsl_run_to_markdown, text)


def _split_output(mode: str, text: str) -> tuple[str, object]:
    if mode != "accurate":
        return _convert_otsl_tables(text).strip(), None
    match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if not match:
        return _convert_otsl_tables(text).strip(), None
    try:
        structured = json.loads(match.group(0))
    except json.JSONDecodeError:
        return _convert_otsl_tables(text).strip(), None
    return _convert_otsl_tables(_structured_to_markdown(structured)), structured


_HTML_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_table_to_markdown(html: str) -> str:
    rows = []
    for row_match in _HTML_ROW_RE.finditer(html):
        cells = [_HTML_TAG_RE.sub("", cell).strip() for cell in _HTML_CELL_RE.findall(row_match.group(1))]
        if cells:
            rows.append(cells)
    if not rows:
        return html
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _structured_to_markdown(structured) -> str:
    elements = structured
    if isinstance(structured, dict):
        elements = structured.get("elements") or structured.get("layout") or [structured]
    if not isinstance(elements, list):
        return json.dumps(structured, indent=2)

    lines = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        category = str(element.get("category", "")).strip().lower()
        text = str(element.get("text", element.get("text_content", ""))).strip()

        if category == "picture":
            lines.append("*[Image]*")
            lines.append("")
            continue
        if not text:
            continue

        if category == "title":
            lines.append(f"# {text}")
        elif category == "section-header":
            lines.append(f"## {text}")
        elif category == "list-item":
            lines.append(f"- {text}")
        elif category in ("caption", "footnote", "page-header", "page-footer"):
            lines.append(f"*{text}*")
        elif category == "formula":
            lines.append(f"`{text}`")
        elif category == "table":
            lines.append(_html_table_to_markdown(text))
        else:
            lines.append(text)
        lines.append("")
    return "\n".join(lines).strip()


def _vision_ocr_page(image_path: str) -> list[dict]:
    """One page through macOS's own Vision framework -- on-device, no model
    download, no MLX involved. Bbox is converted from Vision's normalized
    bottom-left-origin rect to the same pixel/top-left [x1,y1,x2,y2] convention
    dots.ocr uses, so the existing bbox overlay and JSON viewer need no changes."""
    import Quartz
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(os.path.abspath(image_path))
    ci_image = Quartz.CIImage.imageWithContentsOfURL_(url)
    if ci_image is None:
        raise RuntimeError(f"Could not load image for Vision OCR: {image_path}")
    extent = ci_image.extent()
    image_width, image_height = extent.size.width, extent.size.height

    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci_image, {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)

    success, error = handler.performRequests_error_([request], None)
    if not success:
        raise RuntimeError(f"Vision OCR failed: {error}")

    elements = []
    for observation in (request.results() or []):
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        text = str(candidates[0].string())
        if not text.strip():
            continue
        confidence = float(candidates[0].confidence())
        box = observation.boundingBox()
        x1 = box.origin.x * image_width
        x2 = (box.origin.x + box.size.width) * image_width
        y1 = (1 - box.origin.y - box.size.height) * image_height
        y2 = (1 - box.origin.y) * image_height
        elements.append({
            "bbox": [round(x1), round(y1), round(x2), round(y2)],
            "category": "Text",
            "text": text,
            "confidence": round(confidence, 3),
        })
    elements.sort(key=lambda el: (el["bbox"][1], el["bbox"][0]))
    return elements


def _run_apple_vision(image_paths: list[str], on_stage=None) -> dict:
    total = len(image_paths)
    page_markdowns, page_structured, page_raw = [], [], []
    for index, image_path in enumerate(image_paths):
        if on_stage:
            label = f"Reading page {index + 1} of {total}" if total > 1 else "Reading with Apple Vision"
            on_stage(label, 0.1 + 0.8 * (index / max(total, 1)))
        elements = _vision_ocr_page(image_path)
        page_markdowns.append("\n".join(el["text"] for el in elements))
        page_structured.append(elements)
        page_raw.append(json.dumps(elements))

    if total == 1:
        return {"markdown": page_markdowns[0], "structured": page_structured[0], "raw": page_raw[0]}

    combined_markdown = "\n\n---\n\n".join(f"## Page {i + 1}\n\n{md}" for i, md in enumerate(page_markdowns))
    return {"markdown": combined_markdown, "structured": {"page_count": total, "pages": page_structured},
            "raw": "\n\n".join(page_raw)}


def _generate_streaming(model, processor, formatted_prompt: str, image_path: str,
                        max_tokens: int, on_tokens=None, should_stop=None) -> str:
    """Token-streamed generation so the job can report liveness (token count)
    and honour a cancel mid-page instead of only between pages."""
    from mlx_vlm import stream_generate

    pieces = []
    count = 0
    last_report = time.monotonic()
    for chunk in stream_generate(model=model, processor=processor, prompt=formatted_prompt,
                                 image=image_path, max_tokens=int(max_tokens), temperature=0.0):
        pieces.append(chunk.text)
        count += 1
        now = time.monotonic()
        if on_tokens and now - last_report >= 0.5:
            last_report = now
            on_tokens(count)
        if should_stop and should_stop():
            raise _Cancelled()
    if on_tokens:
        on_tokens(count)
    return "".join(pieces)


def run_sync(mode: str, image_paths, prompt: str = "", schema: str = "",
            max_tokens: int = 4096, on_stage=None, on_tokens=None, should_stop=None) -> dict:
    """Blocking OCR call, outside the async job queue -- for the worker's own
    background thread, and for standalone eval/testing scripts. `image_paths`
    is a single path or a list (multi-page PDF), one page per generate call
    sharing a single loaded model and formatted prompt."""
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    if not image_paths:
        raise ValueError("No image supplied")

    if mode == "apple":
        return _run_apple_vision(image_paths, on_stage=on_stage)

    if mode not in MODEL_CHOICES:
        raise ValueError(f"Unknown mode: {mode!r}")

    _load_model_here(mode)
    status = model_status(mode)
    if status["status"] != "ready":
        raise RuntimeError(status.get("error") or "Model failed to load")

    from mlx_vlm.prompt_utils import apply_chat_template

    model, processor = _models[mode]
    prompt_text = prompt or (STRUCTURED_PROMPT if mode == "accurate" else MARKDOWN_PROMPT)
    schema = schema.strip()
    if schema and mode == "accurate":
        prompt_text = f"{prompt_text}\n\nAlso extract these specific fields as top-level JSON keys: {schema}"
    formatted_prompt = apply_chat_template(processor, model.config, prompt_text, num_images=1)

    total = len(image_paths)
    page_markdowns, page_structured, page_raw = [], [], []
    for index, image_path in enumerate(image_paths):
        if on_stage:
            label = f"Reading page {index + 1} of {total}" if total > 1 else "Reading page"
            on_stage(label, 0.1 + 0.8 * (index / max(total, 1)))
        base = 0.1 + 0.8 * (index / max(total, 1))
        span = 0.8 / max(total, 1)

        def report_tokens(count: int, base=base, span=span) -> None:
            if on_tokens:
                on_tokens(count, base + span * (1 - math.exp(-count / 1200.0)))

        text_out = _generate_streaming(model, processor, formatted_prompt, image_path,
                                       max_tokens, on_tokens=report_tokens, should_stop=should_stop)
        markdown, structured = _split_output(mode, text_out)
        page_markdowns.append(markdown)
        page_structured.append(structured)
        page_raw.append(text_out)

    if total == 1:
        return {"markdown": page_markdowns[0], "structured": page_structured[0], "raw": page_raw[0]}

    combined_markdown = "\n\n---\n\n".join(f"## Page {i + 1}\n\n{md}" for i, md in enumerate(page_markdowns))
    combined_structured = (
        {"page_count": total, "pages": page_structured}
        if any(item is not None for item in page_structured) else None
    )
    return {"markdown": combined_markdown, "structured": combined_structured,
            "raw": "\n\n".join(page_raw)}


def _report(job_id: str, payload: dict) -> dict:
    """Best-effort progress POST to the shell's Activity panel, from the
    worker itself rather than the page -- so the row keeps moving (and a
    cancel click there keeps working) even after the page is closed, since
    this job's own background thread outlives it either way."""
    origin = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")
    if not origin:
        return {}
    body = {"id": job_id, "title": "OpenOCR", "kind": "task", **payload}
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


def _run_ocr(job_id: str, request: dict) -> None:
    started = time.perf_counter()
    mode = request["mode"]
    image_paths = request["image_paths"]
    try:
        _job_update(job_id, state="running", stage="Waiting for model", progress=0.05)
        if _cancel_requested(job_id):
            raise _Cancelled()

        def on_stage(stage: str, progress: float) -> None:
            _job_update(job_id, stage=stage, progress=progress, tokens=0)
            reply = _report(job_id, {"state": "running", "done": int(progress * 100),
                                     "total": 100, "detail": stage})
            if reply.get("cancel_requested"):
                _job_update(job_id, cancel=True)

        def on_tokens(count: int, progress: float) -> None:
            _job_update(job_id, tokens=count, progress=round(progress, 3))
            with _jobs_lock:
                stage = (_jobs.get(job_id) or {}).get("stage", "Generating")
            reply = _report(job_id, {"state": "running", "done": int(progress * 100),
                                     "total": 100, "detail": f"{stage} · {count} tokens"})
            if reply.get("cancel_requested"):
                _job_update(job_id, cancel=True)

        result = run_sync(mode, image_paths, prompt=request.get("prompt", ""),
                          schema=request.get("schema", ""),
                          max_tokens=int(request.get("max_tokens") or 4096),
                          on_stage=on_stage, on_tokens=on_tokens,
                          should_stop=lambda: _cancel_requested(job_id))

        if _cancel_requested(job_id):
            raise _Cancelled()

        elapsed = time.perf_counter() - started
        _job_update(job_id, state="done", stage="Complete", progress=1.0,
                    markdown=result["markdown"], structured=result["structured"],
                    raw=result["raw"], elapsed=round(elapsed, 1))
        _report(job_id, {"state": "done", "done": 100, "total": 100,
                         "detail": f"Finished in {elapsed:.1f}s"})
    except _Cancelled:
        _job_update(job_id, state="cancelled", stage="Cancelled", progress=0)
        _report(job_id, {"state": "cancelled", "detail": "Cancelled"})
    except BaseException as error:  # noqa: BLE001
        message = f"{type(error).__name__}: {error}"
        _job_update(job_id, state="error", stage="Failed", error=message)
        _report(job_id, {"state": "error", "detail": message})
        print("[openocr] ocr failed\n" + traceback.format_exc())


def start_ocr(image_paths, mode: str = "fast", prompt: str = "", schema: str = "",
             max_tokens: int = 4096, job_id: str = "") -> dict:
    if mode not in MODEL_CHOICES and mode != "apple":
        raise ValueError(f"Unknown mode: {mode!r}")
    if isinstance(image_paths, str):
        image_paths = [image_paths]
    if not image_paths:
        raise ValueError("No image supplied")
    for image_path in image_paths:
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"No such image: {image_path}")
    if mode in MODEL_CHOICES and mode not in _models and not is_downloaded(mode):
        raise RuntimeError(
            f"The {mode} model ({_active_repo[mode]}) is not downloaded yet. "
            "Use the model picker to download it first."
        )

    job_id = _SAFE_JOB.sub("", job_id)[:96] or f"ocr-{int(time.time() * 1000)}"
    with _jobs_lock:
        _jobs[job_id] = {"job_id": job_id, "state": "queued", "stage": "Queued", "progress": 0.0,
                        "created_at": time.time(), "updated_at": time.time(), "mode": mode}
    request = {"mode": mode, "image_paths": image_paths, "prompt": prompt, "schema": schema,
              "max_tokens": max_tokens}
    _submit(lambda: _run_ocr(job_id, request))
    return {"job_id": job_id, "queued_behind": max(_mlx_queue.qsize() - 1, 0)}


def poll(job_id: str) -> dict:
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        return {"state": "unknown", "stage": "No such job", "progress": 0.0}
    job["model"] = model_status(job.get("mode", "fast"))
    return job


def cancel(job_id: str) -> dict:
    _job_update(job_id, cancel=True)
    return poll(job_id)
