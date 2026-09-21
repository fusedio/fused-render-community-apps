# OpenOCR

A local, Raycast-inspired OCR tool. Drop a screenshot or scan and get back
clean Markdown or schema-driven structured JSON — entirely on this Mac, via
small purpose-built document-OCR vision models running on Apple MLX. Nothing
is uploaded and, apart from the model weights, nothing is downloaded.

## What it does

| Feature | How it works |
|---|---|
| **Capture** | Take a screenshot (`fused.capture.screenshot`) or drop/choose an image. OCR runs automatically the moment an image lands. |
| **Model picker** | Each mode lists its available models with size; picking one just switches which weights the mode will use next — it does not download anything by itself. |
| **Explicit downloads** | Nothing is fetched until you click Download next to a not-yet-downloaded model. `engine.start_ocr` also refuses to run a model that isn't already on disk, so a UI bug can't silently trigger a multi-GB download either. |
| **Fast mode** | `PaddleOCR-VL-4bit` (0.9B, ~0.7 GB) — top-ranked on OmniDocBench for its size, clean Markdown with tables, formulas and reading order preserved. `PaddleOCR-VL-1.5-4bit` is offered as an alternative. |
| **Accurate mode** | `dots.ocr-4bit` (3B, ~3.5 GB) — one model, prompt-switchable output: Markdown, HTML tables, LaTeX formulas, and full structured JSON with per-element bounding boxes and categories. |
| **Apple mode** | macOS's own Vision framework (`VNRecognizeTextRequest`, accurate recognition level) — no download, no model weights, near-instant (~0.1–0.3s/page). Great for plain text and receipts; unlike Fast/Accurate it has no document-structure understanding, so it doesn't reconstruct tables — it just reads text lines with bounding boxes. |
| **Custom extraction** | In Accurate mode, name specific fields ("invoice_number, total, date") and they're folded into the JSON prompt. |
| **Dual output** | Markdown and JSON tabs side by side, with copy and `.md`/`.json` export. |
| **History & search** | Every run is saved locally with a thumbnail and searchable text, so past scans stay one click away. |

Both models are dedicated document-OCR VLMs rather than general chat models —
on OmniDocBench, sub-1B specialist OCR models like these outrank general
vision-language models more than 100x their size, so bigger was not the goal.

## How it fits together

```
index.html     the whole UI (vanilla JS, no build step, Ollama-style layout)
OpenOCR.py     runPython entry: image ingest, history list/get/save/delete, export
worker.py      warm-worker entry (main(**params)): starts an OCR job, reads status
engine.py      resident models + generation thread; the weights live here
store.py       history persistence under .fused/data/history
```

Two things drive that split:

- **`runPython` is killed at 60 s** and gets a fresh subprocess every call, so it
  can never hold a loaded model resident between calls.
- **The warm worker's HTTP handler times out at 65 s**, so `worker.py` only ever
  *starts* an OCR run; `engine.py` runs it on MLX's single dedicated thread and
  the page polls until it's done.

Fast and Accurate models load independently and stay resident once warmed, so
switching modes mid-session doesn't reload weights you've already paid for.

## Models

Override either model via environment variable before the worker starts:

```
OPENOCR_FAST_MODEL=mlx-community/PaddleOCR-VL-1.5-4bit
OPENOCR_ACCURATE_MODEL=mlx-community/dots.ocr-4bit
```

## Evaluating quality

`eval/run_eval.py` runs both models over a handful of local fixtures (synthetic
documents with known ground truth, plus real scanned samples) and writes the
extracted Markdown/JSON to `eval/results/` for inspection — see `eval/README.md`.
