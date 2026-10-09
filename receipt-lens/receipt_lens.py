# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow>=11",
#   "torch>=2.7",
#   "torchvision>=0.22",
#   "transformers>=5,<6",
# ]
# ///

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


JOBS = Path(tempfile.gettempdir()) / "fused-receipt-lens"

# All three are Idefics3-family checkpoints loadable through the same
# AutoModelForImageTextToText / AutoProcessor pair below, so picking a
# different size never changes the generation code path.
CURATED_MODELS = [
    {
        "id": "HuggingFaceTB/SmolVLM-256M-Instruct",
        "label": "SmolVLM 256M",
        "size_gb": 0.6,
        "note": "Fastest on CPU; best for short, clean receipts.",
    },
    {
        "id": "HuggingFaceTB/SmolVLM-500M-Instruct",
        "label": "SmolVLM 500M",
        "size_gb": 1.1,
        "note": "Balanced speed and accuracy for most receipts.",
    },
    {
        "id": "HuggingFaceTB/SmolVLM-Instruct",
        "label": "SmolVLM 2.2B",
        "size_gb": 4.4,
        "note": "Most accurate; slowest on a CPU-only machine.",
    },
]
DEFAULT_MODEL = CURATED_MODELS[0]["id"]


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(value, file)
    temporary.replace(path)


def _job_path(job_id: str) -> Path:
    return JOBS / str(uuid.UUID(job_id))


def _hf_cache_dir() -> Path:
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(os.environ["HUGGINGFACE_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _is_cached(model_id: str) -> bool:
    snapshots = _hf_cache_dir() / ("models--" + model_id.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


def _generate(request: dict, job_dir: Path) -> str:
    import torch
    from PIL import Image
    from transformers import AutoModelForImageTextToText, AutoProcessor

    torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
    model_id = request["model_id"]
    _write_json(
        job_dir / "status.json",
        {"status": "running", "stage": f"Loading {model_id}", "progress": 5, "started_at": request["started_at"]},
    )
    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(model_id, dtype=torch.float32).eval()
    image = Image.open(job_dir / "receipt.jpg").convert("RGB")
    instruction = (
        "Respond with ONLY a single JSON object, no other text before or after it. "
        'Use this shape: {"merchant": string, "date": string, "receipt_id": string, '
        '"items": [{"name": string, "quantity": number, "amount": number}], '
        '"subtotal": number, "tax": number, "tip": number, "total": number}. '
        "Use 0 for any amount you cannot read."
    )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": request["prompt"].strip() + " " + instruction},
            ],
        }
    ]
    text = processor.apply_chat_template(messages, add_generation_prompt=True)
    inputs = processor(text=text, images=[image], return_tensors="pt")
    _write_json(
        job_dir / "status.json",
        {
            "status": "running",
            "stage": f"Reading receipt with {model_id.split('/')[-1]}",
            "progress": 25,
            "started_at": request["started_at"],
        },
    )
    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=int(request["max_new_tokens"]),
            do_sample=False,
            use_cache=True,
        )
    prompt_length = inputs["input_ids"].shape[1]
    return processor.decode(output[0, prompt_length:], skip_special_tokens=True)


def _json_from_text(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"The VLM did not return JSON. Raw response: {text[:700]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise ValueError(f"The VLM returned incomplete JSON. Raw response: {text[:700]}") from error


def _number(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else 0.0


def _nested_number(value, *names: str) -> float:
    if not isinstance(value, dict):
        return _number(value)
    for name in names:
        if name in value:
            return _number(value[name])
    return 0.0


def _document(value: dict) -> dict:
    source_items = value.get("items") or value.get("menu") or value.get("line_items") or []
    items = []
    pending = [source_items] if isinstance(source_items, dict) else list(source_items)
    while pending:
        item = pending.pop(0)
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("sub"), dict):
            pending.append(item["sub"])
        name = str(item.get("name") or item.get("nm") or item.get("item") or "Unknown item")
        items.append(
            {
                "name": name,
                "qty": max(1, int(_number(item.get("quantity", item.get("qty", item.get("cnt", item.get("num", 1))))) or 1)),
                "amount": _number(item.get("amount", item.get("price", item.get("total", 0)))),
                "confidence": 80,
            }
        )

    sub_total = value.get("sub_total", {})
    subtotal = _number(value.get("subtotal")) or _nested_number(sub_total, "subtotal", "subtotal_price")
    subtotal = subtotal or sum(item["amount"] for item in items)
    tax = _number(value.get("tax")) or _nested_number(sub_total, "tax", "tax_price")
    tip = _number(value.get("tip")) or _number(value.get("gratuity")) or _nested_number(sub_total, "tip", "tip_price")
    total = _nested_number(value.get("total"), "total", "total_price", "amount") or subtotal + tax + tip
    if not tip and total > subtotal + tax:
        tip = round(total - subtotal - tax, 2)
    return {
        "merchant": str(value.get("merchant") or value.get("restaurant") or value.get("store") or "Unknown restaurant"),
        "date": str(value.get("date") or "Date not detected"),
        "receiptId": str(value.get("receipt_id") or value.get("receiptId") or "--"),
        "confidence": 80,
        "items": items,
        "summary": {"subtotal": subtotal, "tax": tax, "tip": tip, "total": total},
    }


def _worker(job_dir: Path) -> None:
    started = time.perf_counter()
    try:
        with (job_dir / "request.json").open("r", encoding="utf-8") as file:
            request = json.load(file)
        _write_json(
            job_dir / "status.json",
            {"status": "running", "stage": "Importing CPU model runtime", "progress": 2, "started_at": request["started_at"]},
        )
        response = _generate(request, job_dir)
        _write_json(
            job_dir / "result.json",
            {
                "status": "complete",
                "document": _document(_json_from_text(response)),
                "raw_response": response,
                "elapsed_seconds": time.perf_counter() - started,
            },
        )
    except Exception as error:
        _write_json(job_dir / "result.json", {"status": "error", "message": str(error)})


def _spawn_worker(job_dir: Path, script_path: Path) -> int:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to start the local vision model")
    log = (job_dir / "worker.log").open("ab")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "cwd": script_path.parent}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([uv, "run", "--script", str(script_path), "--worker", str(job_dir)], **kwargs)
    log.close()
    return process.pid


def main(
    action: str = "status",
    image_data: str = "",
    model_id: str = DEFAULT_MODEL,
    prompt: str = "Extract the merchant, date, receipt number, every purchased item, subtotal, tax, tip, and total.",
    job_id: str = "",
    max_new_tokens: int = 384,
) -> dict:
    if action == "models":
        return {
            "models": [dict(model, downloaded=_is_cached(model["id"])) for model in CURATED_MODELS],
            "default": DEFAULT_MODEL,
        }

    if action == "status":
        ready = shutil.which("uv") is not None
        cached = _is_cached(model_id)
        if not ready:
            message = "Install Astral uv to run the local vision model"
        elif cached:
            message = f"{model_id} is downloaded and ready"
        else:
            message = f"{model_id} will download on first extraction"
        return {"ready": ready, "cached": cached, "message": message}

    if action == "poll":
        job_dir = _job_path(job_id)
        result_path = job_dir / "result.json"
        status_path = job_dir / "status.json"
        path = result_path if result_path.exists() else status_path
        if not path.exists():
            return {"status": "running", "stage": "Starting CPU worker", "progress": 0}
        with path.open("r", encoding="utf-8") as file:
            result = json.load(file)
        if result.get("status") == "running" and time.time() - result.get("started_at", time.time()) > 1200:
            return {"status": "error", "message": "CPU inference exceeded 20 minutes"}
        return result

    if action != "start":
        raise ValueError(f"Unknown action: {action}")
    if not image_data:
        raise ValueError("No receipt image was supplied")

    JOBS.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())
    job_dir = JOBS / job_id
    job_dir.mkdir()
    encoded_image = image_data.split(",", 1)[-1]
    with (job_dir / "receipt.jpg").open("wb") as file:
        file.write(base64.b64decode(encoded_image))
    started_at = time.time()
    _write_json(
        job_dir / "request.json",
        {
            "model_id": model_id,
            "prompt": prompt,
            "max_new_tokens": max_new_tokens,
            "started_at": started_at,
        },
    )
    _write_json(
        job_dir / "status.json",
        {"status": "running", "stage": "Starting CPU worker", "progress": 0, "started_at": started_at},
    )
    pid = _spawn_worker(job_dir, Path.cwd() / "receipt_lens.py")
    return {
        "job_id": job_id,
        "cleaned_image": encoded_image,
        "cleaned_mime": "image/jpeg",
        "model_id": model_id,
        "worker_pid": pid,
    }


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    _worker(Path(sys.argv[2]))
