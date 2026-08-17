# App shell

> **Status — target.** This file owns the **shell**: the single entry page, the
> four-tab surface switch, the params that constitute the app's entire state, the
> shared runtime/model strip, the notice surface, and theming. It owns nothing
> about what any individual tab renders — each surface owns itself. Implementing
> modules: `index.html` (`draw`, `setTab`, `refreshRuntime`, `fillModels`,
> `notice`, `P`), `gallery.py` (`main`). Assumes `assumptions.md` throughout.

## 1. Family index

| Surface | Tab param | Spec |
|---|---|---|
| Studio | `tab=studio` (default) | `app-studio.md` |
| Sweep | `tab=sweep` | `app-sweep.md` |
| Prompt Lab | `tab=lab` | `app-prompt-lab.md` |
| Gallery | `tab=gallery` | `app-gallery.md` |

**One folder, one entry page.** `index.html` carries `<meta name="fused-app" />`
near the top of `<head>` — the only thing that makes this folder an app. There is
no second top-level `.html`; the four surfaces are sections of the one page,
shown and hidden by `setTab`. This is a packaging decision, not a rendering one:
each surface is independently specified and independently readable.

## 2. Params are the state

Every piece of view state lives in `fused.params`, so any view is refresh-proof
and bookmarkable, and so Sweep and Gallery can hand settings to Studio by writing
params rather than reaching into its DOM.

| Param | Default | Owner | Meaning |
|---|---|---|---|
| `tab` | `studio` | §1 | Which surface is visible. |
| `model` | catalog default | §4 | HF repo id for `text-to-image`. |
| `prompt` | `""` | `app-studio.md`, shared with `app-sweep.md` | The render prompt. |
| `w` / `h` | `1024` | `app-studio.md` §2 | Requested size, pre-snap. |
| `steps` | `28` | `app-studio.md` §2 | Denoising steps. |
| `guidance` | `4` | `app-studio.md` §2 | Guidance scale. |
| `seed` | `""` (= let the server choose) | `app-studio.md` §3 | Fixed seed. |
| `autosave` | `1` | `app-studio.md` §5 | Copy each render into `./gallery/`. |
| `axis` | `seed` | `app-sweep.md` §1 | `seed` \| `steps` \| `guidance`. |
| `n` | `4` | `app-sweep.md` §1 | Cells in the sweep, 2–8. |
| `from` / `to` | axis-dependent | `app-sweep.md` §2 | Sweep endpoints. |
| `idea` | `""` | `app-prompt-lab.md` §1 | The rough idea to expand. |
| `variants` | `3` | `app-prompt-lab.md` §1 | Prompts to generate, 2–6. |
| `q` | `""` | `app-gallery.md` §3 | Gallery search text. |

**The wiring rule, without exception:** a control writes its param and does
nothing else; `fused.params.onChange(draw)` is the single re-render path; `draw()`
reads params, never `input.value`. A control that reads its own DOM to decide
what to render is a state fork, and refresh loses it.

**`draw()` must be idempotent and cheap.** It reflects params into controls, shows
the active section, and hides the rest. It never starts a render, never calls
`fused.ai.*`, and never writes a param — a `set()` inside `draw` is a render loop,
and the call log will show it as calls with no interactions
(`fused-render calls --page <page>`).

## 3. Layout

Deliberately the same skeleton as the transcription demo it is a sibling to, so
someone who has read one can read the other:

- `<header>` — title, a one-line subtitle naming the bridge call, and a **runtime
  strip** on the right (§4): a status dot plus the active runner's label.
- **Tab bar** directly under the header — four buttons, `aria-current` on the
  active one, each writing `tab`.
- `.layout` grid: a fixed-width `<aside>` control panel on the left, `<main>` on
  the right. The aside's contents change per tab; the model group and the
  runtime-dependent controls are shared and always present.
- `<main>` holds the notice surface (§6) then the active surface's cards.

## 4. Runtime and model strip (shared)

On load, and after every load/unload, in this order:

1. `fused.ai.models.list()` → find the entry in `runners` whose `capability` is
   `text-to-image`. Record it (`available`, `reason`, `label`) and whether a model
   is currently `loaded` for that capability.
2. `fused.ai.models.catalog()` → find the `text-to-image` capability block; fill
   the model `<select>` from its `models` (label + `size_gb` + `note`), and take
   its `default` as the fallback for the `model` param.

Both are read **every time**, never cached across a load/unload, because the
resident model can change under us (the AI Models page, another app, an engine
switch in Preferences, which evicts).

**Three honest states, and the strip says which:**

| State | Dot | Text | Effect on Generate |
|---|---|---|---|
| Runner available, model resident | on (accent) | `Diffusers (PyTorch) · FLUX.2 klein 4B resident` | enabled |
| Runner available, nothing resident | on | `Diffusers (PyTorch) · first render loads 2.6 GB` | enabled — the wait is inside the job (`assumptions.md §2`) |
| No runner | off (err) | the runner's `reason`, verbatim | **disabled**, with the reason in a banner |

If `catalog()` fails or returns no `text-to-image` block, the select falls back to
a single hard-coded entry and the strip says the catalog could not be read. A
fallback is a last resort, not the default path (`assumptions.md §4`).

**Preload / Unload** live under the model select and say what they *would* do
before you press them:

- Preload → `fused.ai.models.load(id)` → `{jobId}` → `fused.watchJob(jobId).watch(cb)`,
  drawing bytes (`unit: "bytes"`), then re-runs step 1.
- Unload → `fused.ai.models.unload({capability: "text-to-image"})`, **by capability**
  (`assumptions.md §4`), then re-runs step 1. The button's caption names what is
  actually resident, not what the dropdown shows, and is disabled when nothing is.

## 5. The local-only gate

```js
const IS_LOCAL = fused.env === "local";
```

Every `fused.ai.*` call site checks it. The dotted calls export cleanly and then
fail at the reader, so nothing but this check stops them (`assumptions.md §5`).
When `IS_LOCAL` is false the page renders its chrome, disables every generate
control, and shows one banner explaining that local AI is not available on a
hosted copy. In practice this page cannot be exported at all (Prompt Lab's
`fused.ai(` is a textual match), so the gate is belt-and-braces — kept because the
gate is the correct habit and cheap, not because we expect to hit it.

## 6. Notices and errors

One `#notices` container above the surface cards. `notice(html, kind)` renders a
banner (`warn` default, `error` for failures) and replaces the previous one for
the same cause rather than stacking.

Failures are **typed and routed** (`assumptions.md §3`), never rethrown into the
red traceback overlay:

- `cancelled` → no banner at all; just clear the progress card.
- `unavailable` → error banner with `err.message` verbatim, Generate disabled.
- `ai_error` / `bad_request` → error banner with `err.message`.
- `ai_unavailable` → `app-prompt-lab.md §4` handles it; other surfaces cannot raise it.

A `runPython` rejection (Gallery only) is caught and shown as a banner with
`err.type + ": " + err.message`; the traceback stays in the console.

## 7. Theming

`<html data-fused-theme="shell">` — the runtime resolves the app's Light/Dark
setting and writes `data-theme` before our stylesheet parses. Two `:root` blocks
define **the same token set** and no colour literal appears anywhere else in the
stylesheet. There is no in-page theme switcher (it would silently lose to the app
setting). No colour is handed to JS, so there is nothing to redraw on a theme flip.

## Non-goals

- What any tab renders — see the four surface specs (§1).
- The `fused.ai.image` contract, model discovery, export rules — `assumptions.md`.
- Persisting images to disk — `app-gallery.md` owns `./gallery/` and `gallery.py`.

## Open questions

- **Concurrency.** The shell serialises renders app-wide: one image job at a time,
  Generate disabled while any surface is rendering. Whether the server would
  happily run two is untested, and a laptop that OOMs mid-sweep is a worse demo
  than one that waits. Revisit only with a measurement.

## See also

- `assumptions.md` — the bridge contracts this shell wires up.
- `app-studio.md`, `app-sweep.md`, `app-prompt-lab.md`, `app-gallery.md` — the surfaces.
