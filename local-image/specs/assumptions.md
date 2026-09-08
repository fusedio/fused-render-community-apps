# Assumptions

> **Status — shipped (fused-render runtime, not this app).** This file owns the
> **external contracts this app is written against**: the `fused.ai.image` reply,
> the catalog/runtime shapes, the export rule, and param typing. Nothing here is
> ours to change — it is recorded so no surface spec has to restate it and no
> reader has to go spelunking `runtime.js`. Implementing modules (upstream):
> `fused_render/static/runtime.js` (`aiImage`, `aiModels`, `ai.cancel`),
> `fused_render/server/routers/ai_runtime.py` (`api_ai_image`).

## 1. `fused.ai.image(opts)` — the one call this app is built on

**Request.** Only these keys are sent; anything else is dropped by the bridge.

| Key | Type | Default | Server clamp |
|---|---|---|---|
| `prompt` | string, non-empty | — (required) | trimmed; empty rejects `bad_request` **client-side**, before any POST |
| `model` | HF repo id | catalog default for `text-to-image` | absent runner → `unavailable` (409) |
| `width` / `height` | number | `1024` | clamped 256–2048, **snapped down to a multiple of 16** |
| `steps` | number | `28` | clamped 1–100 |
| `guidance` | number | `4.0` | clamped 0–20 |
| `seed` | number | invented server-side | clamped 0–(2³¹−1) |
| `onProgress` | `(job) => void` | — | not sent; fires locally per job tick |

**Reply.** Resolves with the **settled** request — the render that actually
happened, not the one that was asked for:

```js
{ jobId, path, url, model, prompt, width, height, steps, guidance, seed }
```

- `path` is absolute, under `<home>/ai/images/<YYYYmmdd-HHMMSS>-<uid>.png` — **not**
  beside this page. Copying one into `./gallery/` is therefore a real file copy
  (`app-gallery.md §2`), not a rename.
- `url` is a ready-made `/api/fs/raw` address. Point an `<img src>` at it; never
  build the URL by hand.
- `seed` comes back **whether or not one was passed**. This is what makes "render
  that one again" one call away, and it is the axis `app-sweep.md` sweeps.

**Rule: echo the reply, never the request.** A panel showing `1000×1000` when the
server snapped to `992×992`, or `steps 500` when it clamped to `100`, mislabels
the picture on screen. Every surface reads its result meta off the resolved
object. This is the single most load-bearing assumption in the app.

## 2. Progress: an image waits for its own model

`onProgress(job)` fires with the download-manager record. **`job.done` / `job.total`
are DENOISING STEPS** — not bytes (that is a model download) and not seconds (that
is transcription).

**Unlike `fused.ai` (text), an image never rejects with `model_loading`.** A cold
multi-GB load happens *inside* the job: the render row simply reports that it is
waiting while a separate row (`sys:ai-model:<repo>`) carries the bytes. So this app
has **no cold-start retry dance for images** — a first render is just a slow render.
A `job.total` of 0 or absent means "still waiting for the model"; surfaces render
that as an indeterminate state, never as 0%.

One row per render (`sys:ai-image:<uid>`), and that row's ✕ genuinely stops the
work — the work is the server's, not the page's.

## 3. Rejections

Every rejection is an `Error` with `.type` (and `.jobId` where a row existed):

| `.type` | Meaning here | Required UI response |
|---|---|---|
| `cancelled` | The user pressed Stop, or the row's ✕. | **Not a failure.** Clear the progress card, say nothing alarming. |
| `unavailable` | 409 — a fact about this machine ("the Diffusers runner is not built yet"). | Show `err.message` verbatim; it explains itself. Disable Generate. |
| `ai_error` | Ran and failed (bad repo id, OOM, worker crash). | Show `err.message`. |
| `bad_request` | Our bug — empty prompt or a malformed number. | Show it; it means the page shipped broken. |
| `ai_unavailable` | `fused.ai` text only: the `claude` CLI is missing. | `app-prompt-lab.md §4` only. |

## 4. Model discovery — always ask, never hard-code

- `fused.ai.models.catalog()` → `{capabilities: [{capability, runner, runnerLabel,
  available, reason, default, models: [{id, label, size_gb, note}]}]}`.
- `fused.ai.models.list()` → `{runners: [{code, capability, label, note, available,
  reason}], loaded: [...], downloading: [...], totalResidentBytes}`.

**A repo id belongs to a backend, not to a capability.** The MLX FLUX runner and the
Diffusers runner take *different repos for the same model*
(`mlx-community/FLUX.2-Klein-4B-4bit` vs `black-forest-labs/FLUX.2-klein-4B`), so a
hard-coded id becomes an unloadable download the moment the other engine is serving.
Every model list in this app is read from `catalog()` at load time.

- `fused.ai.models.load(id)` → `{jobId}` — a job, **not a loaded model**. Watch it
  with `fused.watchJob(jobId)`.
- `fused.ai.models.unload({capability: "text-to-image"})` — **by capability, never by
  id.** The resident model may not be the one our dropdown shows (the AI Models page
  or another app can load a different one); passing our id would unload nothing and
  leave the real model in memory.
- `fused.ai.cancel("text-to-image")` — stops generation, keeps the weights.
  **Must be named**; the argless form defaults to `"text-generation"` and would stop
  a chat instead of our render. Resolving `false` is not an error.

**`/api/ai/runtime` reports ONE runner entry per capability — the active one.** There
is no `active` field to read in this build; the payload's single `text-to-image` row
*is* the answer to "what is serving this capability now". Finding that row by
capability is therefore correct, and would keep being correct if a future build
listed both and added the flag.

**Observed on this machine (2026-08-16), recorded as a warning, not a constant:**
two probes minutes apart disagreed. The first reported `diffusers-image`
("Diffusers (PyTorch)") offering `black-forest-labs/FLUX.2-klein-4B` at 2.6 GB; the
second, after a Preferences engine switch, reported MLX FLUX offering a 4.6 GB repo
with a different id. Both are real, and **a build that had hard-coded either one
would have been wrong within the hour.** This is the concrete reason §4 says *always
ask*: not portability across machines in the abstract, but the fact that the answer
changes under a running page. A switch also **evicts** whatever was resident, so the
strip and the model list are re-read after every load and unload (`app.md §4`).

## 5. Export: this app is local-only, deliberately

- The exporter **rejects any page containing the literal string `fused.ai(`**
  (SPEC RH-11) — a *textual* match, so an `if (fused.env === "local")` guard does not
  make it exportable. `app-prompt-lab.md` uses text generation, so **the whole entry
  page is non-exportable**. Accepted, and stated in the UI rather than worked around.
- The **dotted** calls (`fused.ai.image(`, `fused.ai.models.*`) slip past that match
  and would export cleanly, then fail at the reader — a hosted page has no worker.
  Nothing stops us at export time, so **every dotted call is gated on
  `fused.env === "local"` by hand** (`app.md §5`).
- `fused.trackJob` exports fine and no-ops when hosted. We do not need it: image and
  load jobs already own their rows.

## 6. Params are strings; Python coerces

`fused.params.get()` always returns a string or `undefined`, and `set()` **throws on
a non-string**. Numbers are `String(n)` on the way out and parsed on the way in.
Python-side coercion is driven by annotations on `main()` — `steps: int` receives
`int("28")`, an unannotated parameter receives `"28"`.

## See also

- `app.md` — the shell that puts these contracts to work.
