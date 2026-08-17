# Sweep

> **Status — target.** Defers to `app.md` for the shell and `app-studio.md` for
> the baseline parameters it holds fixed. This file owns the **contact sheet**:
> the axis model, cell scheduling, per-cell and overall progress, and handoff back
> to Studio. Implementing symbols: `index.html` (`sweepValues`, `runSweep`,
> `drawSheet`, `openInStudio`). Assumes `assumptions.md §1–§3`.

## 1. What it demonstrates

One prompt, one knob, N images side by side. It answers the question the Studio
cannot: *what does this parameter actually do?* — and, on the `seed` axis, *how
much of the image is the prompt and how much is the noise?*

Controls: `axis` (`seed` | `steps` | `guidance`), `n` (2–8, default 4), and
`from` / `to`. Everything else — prompt, model, size, and the two parameters not
being swept — is taken from the **Studio params verbatim**. There is no second copy
of the parameter panel, and no sweep-local prompt: the shared `prompt` param is the
baseline, which is what makes "tune it in Studio, then sweep it" a two-click flow.

## 2. The axis

`sweepValues()` is pure and total — given `axis`, `n`, `from`, `to` it returns
exactly `n` values, and every surface reads the cells from it:

| Axis | `from` / `to` defaults | Values | Clamp |
|---|---|---|---|
| `seed` | `from` = current `seed` or a fresh random; `to` unused | `from + i` for `i` in `0…n-1` | wrapped into 0–(2³¹−1) |
| `steps` | 8 → 40 | `n` values linearly spaced, **rounded to integers, deduplicated** | 1–100 |
| `guidance` | 1 → 10 | `n` values linearly spaced, rounded to 1 decimal | 0–20 |

**Deduplication shortens the sheet rather than rendering the same image twice.**
`n=8` across steps 8→10 yields three distinct integers; the sheet shows three
cells and says so (`3 of 8 requested — the rest were duplicates`). Silently
rendering duplicates would spend minutes proving nothing.

The `seed` axis is **sequential, not random**: consecutive seeds are as
uncorrelated as random ones for this purpose, and a reproducible range means the
whole sheet can be recreated from the URL — which is the params-are-the-state rule
paying off (`app.md §2`).

## 3. Running

`runSweep()` renders the full grid of placeholder cells **first**, each labelled
with its axis value, then fills them one at a time:

- **Strictly sequential.** One `fused.ai.image` call in flight at a time
  (`app.md` Open questions). The active cell shows its own step progress
  (`assumptions.md §2`); the others stay placeholders.
- **Overall progress** above the sheet: `render 3 / 6`, plus elapsed and a running
  mean per render so the user can predict the end.
- **Stop** cancels the in-flight render (`fused.ai.cancel("text-to-image")`) **and
  abandons the remaining cells.** The already-finished cells stay on screen — a
  half-swept sheet is a real result, not wreckage to clear. The abandoned cells are
  marked `skipped`, not `failed`.
- **A failed cell does not abort the sweep.** Its cell shows the typed error
  (`assumptions.md §3`) and the run continues to the next value; one bad seed
  should not cost the other five renders. `cancelled` is the sole exception — it
  means the user asked to stop, so it ends the run.
- Each finished cell is auto-saved to the gallery when `autosave` is on, exactly as
  Studio does (`app-studio.md §5`).

## 4. The sheet

A responsive grid of cells, each: the image, its axis value as the caption, and
its settled seed. Cells keep a fixed aspect box from `w`/`h` so the grid does not
reflow as images land.

**Clicking a cell opens it in Studio** — `openInStudio(cell)` writes that cell's
settled parameters (`seed`, `steps`, `guidance`, and the shared prompt/size) into
params and sets `tab=studio`. It writes params only; it does not call Studio's
functions. That is the whole reason the surfaces share a param namespace, and it
means the resulting URL is a shareable "this exact image" link.

## Non-goals

- Choosing the prompt — the shared `prompt` param, tuned in `app-studio.md`.
- Sweeping two axes at once. A 2-D grid is 16–64 renders, which is an hour on a
  laptop; recorded under Open questions rather than built.
- Saving the sheet as one composite image.

## Open questions

- **2-D sweeps.** Deferred on wall-clock grounds, not design ones. If revisited,
  the axis model in §2 already generalises — it is `sweepValues` twice and a
  nested loop; the cost is the render budget, not the code.

## See also

- `app-studio.md` — owns the baseline parameters and the settled-params rule.
- `app-gallery.md` — where auto-saved cells land.
