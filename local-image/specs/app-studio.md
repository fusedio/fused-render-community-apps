# Studio

> **Status — target.** Defers to `app.md` for the shell, params wiring, model
> strip and error routing. This file owns the **single-render surface**: the
> parameter panel, the denoising progress card, the settled-params readout, seed
> reuse, and auto-save. Implementing symbols: `index.html` (`generate`,
> `drawResult`, `drawProgress`, `sizePreset`, `reuseSeed`). Assumes
> `assumptions.md §1–§3`.

## 1. What it demonstrates

The whole of `fused.ai.image` in one screen: a prompt goes in, a PNG comes back
minutes later, and everything the server decided about the render is on screen
next to it. It is the surface a first-time reader opens, so it is the one that
must make the **settled-params rule** (`assumptions.md §1`) visible rather than
merely obey it.

## 2. The parameter panel (aside)

| Control | Param | Range | Notes |
|---|---|---|---|
| Prompt | `prompt` | non-empty | `<textarea>`, 4 rows, in `<main>` not the aside — it is the subject, not a setting. |
| Size preset | `w`,`h` | 1024², 1024×1536, 1536×1024, 768², 512² | Buttons; each writes both params. |
| Width / Height | `w`,`h` | 256–2048 | Number inputs for anything the presets miss. |
| Steps | `steps` | 1–100, default 28 | Range slider + numeric readout. |
| Guidance | `guidance` | 0–20 step 0.5, default 4 | Range slider + numeric readout. |
| Seed | `seed` | 0–2147483647, or empty | Empty means **let the server choose**. |

**The snap is shown, not hidden.** Sides are snapped down to a multiple of 16
server-side. The panel renders the effective size beside the inputs
(`1000 × 1000 → 992 × 992`) as soon as a non-multiple is typed, so the reply's
`width`/`height` are never a surprise. The page does **not** rewrite the param —
the server owns the clamp, and duplicating the arithmetic here is a second
definition that will drift (`assumptions.md §1`).

Guidance and steps show their defaults as the slider's initial position and label
the extremes (`steps: faster ← → finer`). No control is unlabelled.

## 3. Seed

Three affordances, because reproducibility is the point of the field:

- **Dice** — writes a fresh random integer into `seed`, so a user can pin a seed
  without inventing one.
- **Clear** — empties `seed`, returning to server-chosen.
- **Reuse** — appears on a result; writes that result's `seed` into the param.
  Since the reply always carries a seed (`assumptions.md §1`), this works for
  renders the user never seeded.

Changing only the seed with everything else fixed is the cheapest demonstration
that the pipeline is deterministic; the result meta says so in one line
(`same prompt + same seed + same settings → the same image`).

## 4. Generating

`generate()`:

1. Refuse early if `prompt` is blank or `IS_LOCAL` is false or the runner is
   unavailable — no POST, and a banner saying which (`app.md §6`).
2. Disable Generate, enable Stop, show the progress card. **`fused.ai.image` has
   no stale-cancel**, so a double-click would fire two real renders; the disabled
   button is the only thing preventing it.
3. Call with the six params plus `onProgress`.
4. On resolve, `drawResult(res)`; on reject, route by `.type` (`app.md §6`).
5. `finally` — re-enable Generate, hide the progress card, clear Stop.

**Progress (`drawProgress`)** reads the record as **denoising steps**
(`assumptions.md §2`):

- `job.total` present and > 0 → determinate bar, `step 7 / 28`, plus elapsed.
- `job.total` absent or 0 → **indeterminate** bar and the words
  `waiting for the model…`. This is the cold-start case: the model's bytes are on
  their own row in the download manager, not this one. Rendering it as `0%` would
  read as a stalled render, which is the single most confusing thing this surface
  could do on a first run.

**Stop** calls `fused.ai.cancel("text-to-image")` — named, because the argless form
stops text generation instead (`assumptions.md §4`). The subsequent rejection is
`cancelled`, which is not a failure and produces no banner.

## 5. The result

`drawResult(res)` renders `<img src="${res.url}">` at natural aspect ratio, and
beneath it a meta row read **entirely off the resolved object** — model, the
settled `width × height`, `steps`, `guidance`, `seed`, and wall-clock elapsed.
Where a settled value differs from what was requested, the row says so
(`992 × 992 (asked 1000 × 1000)`). This is the surface's core claim; a meta row
built from the request instead of the reply is a spec violation, not a cosmetic bug.

Actions on a result: **Reuse seed** (§3), **Save to gallery**, **Copy prompt**, and
**Open the PNG's folder path** as selectable monospace text (`res.path`) so the
file is findable outside the app.

**Auto-save** (`autosave`, default on) runs the same save as the button, once, as
soon as the render resolves — `app-gallery.md §2` owns the copy. Failure to save is
a banner, never a lost image: the PNG still exists at `res.path` and the meta row
still shows it.

## Non-goals

- Multiple renders in one action — `app-sweep.md`.
- Writing files — `app-gallery.md §2` owns `gallery.py`.
- Prompt authoring help — `app-prompt-lab.md`.

## See also

- `app.md §4` — the model strip this surface's Generate button depends on.
- `app-sweep.md` — reuses this surface's params as its fixed baseline.
