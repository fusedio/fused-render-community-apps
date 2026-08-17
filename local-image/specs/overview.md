# Spec registry — flux-image-demo

The capability index for this app. Every spec earns exactly one bullet here,
ending in its owning filename. To find the owner of a concept, read this list
first — never grep the code.

## Capabilities

- **Assumptions** — the `fused` bridge facts and machine facts every spec in this
  folder takes as given: the `fused.ai.image` contract, catalog/runtime shape,
  export rules, param typing (`assumptions.md`).
- **App shell** — the hub: the single entry page, its four tabs, the params that
  are the app's whole state, the shared runtime/model strip, the notice surface,
  and theming (`app.md`).
- **Studio** — one prompt, one image: the parameter panel, the denoising progress
  card, the settled-params result readout, seed reuse, auto-save (`app-studio.md`).
- **Sweep** — one prompt, one axis, N images: the contact sheet that shows what a
  single knob (seed / steps / guidance) actually does (`app-sweep.md`).
- **Prompt Lab** — a rough idea expanded by `fused.ai` (text) into N detailed
  prompts, each rendered; the one surface that uses both AI destinations, and the
  reason the page cannot be exported (`app-prompt-lab.md`).
- **Gallery** — the on-disk record: `./gallery/` PNGs with JSON sidecars, listed,
  searched, reopened in Studio, deleted. The only surface backed by Python
  (`app-gallery.md`).

## Reading order

`assumptions.md` → `app.md` → whichever surface spec you are changing.
A surface spec never restates the hub; it opens by deferring to it.
