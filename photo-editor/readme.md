# Photo Editor

A local, Canva-inspired photo editor. Every pixel stays on this Mac: the
generative editing runs FLUX.2 Klein 4B on Apple MLX, click-to-select runs
SAM 2.1 on MLX, upscaling runs SeedVR2 3B on MLX, and background removal uses
macOS's own Vision framework. Nothing is uploaded and, apart from the model
weights, nothing is downloaded.

![Photo Editor with a sticker layer selected on a custom 1536 x 1024 page](preview.png)

> **Apple Silicon + macOS 14 only.** The AI tools need several GB of model
> weights from Hugging Face (see [Requirements](#requirements)); the page also
> loads Konva and its fonts from public CDNs (unpkg, Google Fonts).

## What it does

| Tool | How it works |
|---|---|
| **Upload** | Drop a photo anywhere on the canvas. EXIF rotation is baked in on import so masks always line up. |
| **Adjust** | Ten sliders (brightness, contrast, saturation, temperature, tint, highlights, shadows, sharpness, vignette, blur). Live CSS preview, then baked into pixels with Pillow. |
| **Filters** | Seven presets with an intensity slider. |
| **Crop** | Freeform plus the usual ratios. |
| **AI Edit** | Paint a mask (brush / lasso / box / eraser) with brush-size, feather and expand/contract controls, optionally describe the change, press Generate. Runs FLUX.2 Klein 4B locally. |
| **Smart select** | Fifth mode of the mask tool (`S`). Click a subject and SAM 2.1 (hiera base-plus, MLX) returns its mask; Shift-click adds a positive point, Option-click a negative one, and Brush / Erase refine the result. Image features are cached per source file so repeat clicks take tens of milliseconds. Falls back to Vision's `instanceAtPoint` (single click) when `mlx-sam` is missing. |
| **Upscale & restore** | 2x, 3x or "short edge 2160" with a 0-50 % denoise slider. SeedVR2 3B on MLX, same start-then-poll job flow as AI Edit. The layer keeps its on-canvas size; only the pixel count grows. Refuses outputs over 8192 px on the long edge or 48 megapixels. |
| **Background Remover** | One click. `VNGenerateForegroundInstanceMaskRequest` on the Neural Engine — class-agnostic, sub-second, no model download. |
| **Page sizes** | New document dialog with Paper (A3-A6, B4/B5, Letter, Legal, Tabloid), Photo prints, Cards & flyers, Posters, Screen & social and Custom sizes, plus resolution (DPI), bleed and safe margin. Opened photos keep their pixels and show the size they print at. Page setup in Properties changes any of it later. |
| **Rulers, grid & guides** | View menu: rulers in mm / cm / in / px, a measurement grid, bleed / trim / safe-area overlay with crop marks, guides dragged out of the rulers, and snapping to page edges, margins and guides. |
| **Export** | Print: original size or any paper size (fit / fill), 150-600 DPI, bleed, crop marks with a page-info slug, optionally centred on an A4 / Letter / A3 sheet, as PDF, PNG, JPEG or TIFF with DPI written into the file. Digital: original, 2x, 1/2x, custom width or a screen preset as PNG, JPEG or WebP. Warns about photos under 150 ppi and missing bleed. |

Undo/redo, zoom, View (rulers, grid, guides) and Export are in the top bar.

### Remove works with the hint left blank

Selecting something and pressing Remove is complete on its own. Remove runs a
different recipe from Insert, because FLUX.2 Klein's masked-edit route feeds
the *unmasked* source image back to the model as conditioning at every step,
and a polite "remove the object and fill naturally" prompt loses to that
signal: the model simply repaints what was there (measured: the output differs
from the input by about 2/255 inside the mask, the same as outside, i.e. VAE
round-trip noise). Two things fix it:

- The prompt is always the forceful scene-level instruction "Remove all people
  and foreground objects from this photo. Show only the empty background..."
  A typed hint is appended as "The uncovered area shows: ..." rather than
  replacing it, so a two-word hint like "tiled floor" still carries the full
  instruction.
- The mask is expanded by about one latent cell (16 px at the model's ~1 MP
  canvas, so 17 px on a 1280x960 photo) before the model and the composite see
  it. Without this the boundary cells still condition on the subject's outline
  and a translucent ghost survives around the head and shoulders.

Dropping the masked cells out of the conditioning tokens (the trick mflux's
outpaint route uses) was also tested; it works on busy indoor scenes but
degrades to a blurred blob on simpler outdoor ones, so the stock conditioning
is kept. The Expand / contract slider still applies on top.

## How it fits together

```
index.html          the whole UI (vanilla JS + Konva via CDN, no build step)
photo_editor.py     runPython entry: uploads, masks, adjustments, crop, export,
                    background removal -- everything that finishes in seconds
worker.py           warm-worker entry (main(**params)): starts work, reads status
engine.py           resident models + generation threads; the weights live here
imaging.py          Pillow: session storage, mask normalisation, compositing
bgremove.py         macOS Vision subject lifting (and the Smart select fallback)
vendor/             the patched mlx-sam wheel, see Requirements
```

Two things drive that split:

- **`runPython` is killed at 60 s** and gets a fresh subprocess every call, so it
  can never hold a loaded model resident between calls.
- **The warm worker's HTTP handler times out at 65 s**, so `worker.py` only ever
  *starts* a render and reports progress; `engine.py` runs it on a thread and the
  page polls. Progress is also POSTed to the shell's download manager from the
  worker itself, so the row keeps moving after the page is closed.

`engine.py` is deliberately a separate module from `worker.py`: the shipped
worker re-imports its target whenever the file's mtime changes, which would
discard the model on every edit of the dispatcher. `import engine` resolves from
`sys.modules`, so the weights survive.

### Threads and the heavy slot

MLX binds every array to the thread that created it, so each model lives on
one thread for its whole life. FLUX and SeedVR2 share the `_mlx_queue` thread:
they are both multi-gigabyte and only one of them fits comfortably in 32 GB
alongside the rest of the system, so before either loads it evicts the other
(`gc.collect()` + `mx.clear_cache()`). Serialising them is also simply correct;
two heavy renders cannot share the GPU usefully. SAM is small and interactive,
so it gets its own thread and queue and never waits behind a render. The worker
answers `smart_mask` with `{"ready": false}` while the weights are still loading
and the page polls `sam_status` instead of blocking the HTTP handler.

### Masks

The page authors masks on an offscreen canvas in **source-pixel space**, so
zooming or panning can never shift what gets repainted. The alpha channel is the
mask; the pink tint is cosmetic. On export, alpha becomes luminance and the file
is written as opaque grayscale — **white = repaint, black = preserve** — because
mflux reads the mask as luminance and ignores alpha entirely. A transparent
"keep" area would otherwise read as white and repaint the whole photo.

Expand/contract runs the morphology on a quarter-scale copy — the model
downsamples the mask 8x and binarises it anyway, so that is far finer than
anything it can act on, and it keeps a 12-megapixel photo under a quarter second.

`imaging.feathered_composite` scales the model's output back to the upload's
resolution if needed and blends it in through the mask only. Untouched pixels
stay bit-identical to the upload, and a small blur hides the seam the model's
hard-thresholded mask leaves behind.

## Requirements

- Apple Silicon (MLX is the whole point) and macOS 14+ for Vision.
- `mlx-community/FLUX.2-Klein-4B-4bit` weights in `~/.cache/huggingface`. Fetch
  them once: `mlxgen download --model mlx-community/FLUX.2-Klein-4B-4bit`.
- Smart select weights (352 MB, Apache-2.0):
  `.venv/bin/hf download avbiswas/sam2.1-hiera-base-plus-mlx`. Override the repo
  with `PHOTO_EDITOR_SAM`. If the file is not cached the worker downloads it on
  first use.
- Upscaler weights (about 4.7 GB):
  `mlxgen download --model AbstractFramework/seedvr2-3b-8bit`. Override with
  `PHOTO_EDITOR_UPSCALER`; `numz/SeedVR2_comfyUI` (fp16, 7.3 GB) also works.
- Roughly 20-30 GB of memory free while the model loads; it settles back down
  to under 20 GB once generation starts.

The first render of a session loads the model, which takes on the order of a
few seconds on an M5. Opening the AI Edit tab starts that load in the
background so the wait overlaps with drawing the mask. The same warmup kicks
off the SAM load, which takes about 30 s the first time.

### About `vendor/mlx_sam-0.3.0-py3-none-any.whl`

`mlx-sam` 0.3.0 from PyPI declares `Requires-Python >=3.14`, but FusedRender
ships Python 3.12 and the package is pure Python that compiles and runs there
unchanged. The vendored wheel is the upstream 0.3.0 wheel with only that one
metadata line relaxed to `>=3.12`; `[tool.uv.sources]` in `pyproject.toml`
points uv at it. Only the image segmenter (`load_image_segmenter`,
`encode_image`, `predict_from_encoded`) is used; none of the video code paths
are touched.

## Known gaps

- Crop is centred on the chosen ratio; there is no draggable crop rectangle yet.
- Filter presets are CSS filter stacks rather than real LUTs, and baking one
  applies the equivalent brightness/contrast/saturation move through Pillow.
- Magic Expand (mflux's outpaint/reframe padding) is deliberately out of scope.
