# Local Image

![Local Image — a prompt panel beside a generated image](preview.png)

**Text-to-image with FLUX.2 Klein, generated entirely on your machine.** Type
a prompt, hit Generate, and watch the render come together — no cloud API, no
account, no key. Four tabs, one shared model:

## Studio

One prompt, one image. Full parameter panel (width, height, steps, guidance
scale, seed with a randomize button), a live denoising progress card, and a
Cancel button that actually stops the job. When it settles you get the exact
parameters used — including the seed — so any result is reproducible.

## Sweep

One prompt, one axis, N images. Vary seed, steps, or guidance across a range
and get a contact sheet showing what that single knob really does.

## Prompt Lab

Hand it a rough idea; `fused.ai` (text) expands it into N detailed prompts and
renders each one. It's the only surface that uses both AI destinations — text
*and* image — which is also why this page can't be exported to a standalone
file.

## Gallery

Every generated image is saved to `./gallery/` as a PNG with a JSON sidecar.
The Gallery tab lists them, searches them, reopens one back in Studio with its
settings intact, and deletes the misses. Backed by `gallery.py` — the app's
only Python.

---

The model is curated and held resident by fused-render's `fused.ai.image(...)`
runtime, so loading, download progress, and unloading are shared with the rest
of the local-ai apps.
