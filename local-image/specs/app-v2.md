# Local Image v2 — build spec

The app today is generate-only across three tabs (Studio, Prompt Lab, Gallery).
This spec takes it to a full local image studio: editing, several takes at
once, semantic gallery search, and a redesign — built for a beginner, not for
someone who already knows what a seed is. §1.5 is the rule that governs all of
it.

Read `specs/app.md`, `specs/assumptions.md` and the existing `index.html`
before changing anything. The conventions in this app are deliberate and every
builder must keep them.

---

## 0. Runtime facts — verified on this machine, do not re-derive

These were checked against `/api/ai/runtime` on 2026-08-28 and against the
`fused-render-ai` skill. Building against a wrong assumption here wastes a
whole phase.

- **Active text-to-image runner is `mflux-image` (MLX FLUX, Apple Silicon).**
  The model in use is `mlx-community/FLUX.2-Klein-4B-4bit`.
- **`fused.ai.image({image})` — base-image editing — is mflux-only.** The
  Diffusers engines refuse it with `bad_request` naming the Engines tab. Every
  edit call must catch that and degrade with a message, never a raw overlay.
- **Edit defaults differ from a plain render**: `steps` 4 and `guidance` 1.0,
  and `width`/`height` derive from the base image (fit to a longest side of
  1024, snapped down to a multiple of 16, each side floored at 256). **Do not
  inject Studio's width/height/steps/guidance into an edit call.**
- **The request envelope is closed.** There is no `negative_prompt`, no
  `strength`, no `mask`, no batch count, no scheduler, no LoRA. Passing an
  option outside the documented set is `bad_request`, not a silent ignore. Do
  not invent one.
- **On mflux a repeated seed does NOT reproduce the same picture.** Two renders
  with identical seed, prompt, steps and guidance came back different. Studio's
  current line "Same prompt + same seed + same settings → the same image" is
  **false on this engine and must be corrected**, not carried forward. Say the
  seed reproduces *the request*, not the pixels.
- **Renders serialize.** One at a time per worker; a second call waits with no
  queue message on its row. The app already gates on `running` — every new
  surface must respect the same gate and say so in its own UI when it queues.
- **`fused.ai.image` never rejects `model_loading`.** A cold model loads inside
  the render's own job with `done`/`total` null. Guard the division.
- **Embeddings**: `mlx-embed` is active. `fused.ai.embed({paths})` works only on
  a model whose `catalog()` entry reports `acceptsPaths === true`; `kind` works
  only where `promptScheme` is non-null. Test both, per model, before drawing a
  control. Persist the model id with the vectors and refuse a query embedded
  under a different one.
- **No network at runtime.** No web fonts, no CDN, no remote assets. System
  faces only.
- **The page is local-only and cannot be exported.** It already contains
  `fused.ai(`, which the exporter refuses textually. Keep the existing
  `IS_LOCAL` gating on the dotted calls anyway.

## 1. Conventions this app already holds — keep them

- **Params are the state.** `draw()` reads `P.*`, never a control's value.
  Controls write params; `fused.params.onChange(draw)` is the single re-render
  path. New view state goes in a param unless it genuinely cannot (see §3.2).
- **Never name a param with a leading `_`**, and note `model` is taken by the
  shell — the app uses `imageModel`. Same care for any new key.
- **Read the settled reply, never echo the request.** Where the server settled
  on something different, show both, as `drawResult` already does.
- **Errors are typed and routed.** Use `reportError`/`notice`; `cancelled` is
  never a failure. Python returns `{error}` rather than raising, so a failed
  copy reads as a banner, not a traceback.
- **One place talks to each API.** `render()` is the only `fused.ai.image`
  caller — keep it that way; add options to it rather than calling around it.
- **Comments explain WHY, not what.** Match the existing density and voice.
- **Commit per logical unit** with short imperative subjects. Never leave the
  tree dirty.

---

## 1.5 The vocabulary rule — this is a beginner's app

**Nothing technical appears anywhere in the default UI.** Not a parameter name,
not a file path, not a model id, not an error code, not an engine name. If a
beginner would have to look something up, it does not belong on the surface.

This is the app owner's explicit direction and it outranks every convenience in
this spec. When in doubt, hide it.

**Hidden — reachable only inside Advanced:**

- seed, steps, guidance — the words as well as the controls
- model selection, preload/unload, "resident", megabytes, runner and engine
  names (mflux, MLX FLUX, Diffusers, CUDA)
- pixel dimensions
- the on-disk path a render was written to
- typed error codes (`bad_request`, `ai_error`, `model_loading`)
- the `fused.ai` / `fused.ai.image` API names that currently appear in the
  page's own copy, and the footer about the exporter

**Visible, because it is plain language, not jargon:**

- a **Shape** chooser — Square, Portrait, Landscape, Wide. *Interpretation
  note*: the owner said "size" goes to Advanced. Pixel numbers do. A beginner
  still needs to choose a shape, so the named shapes stay on the surface and
  the numbers behind them do not appear. Flag this if it reads wrong.
- style presets, by their plain names
- how long a render took, in seconds
- what went wrong, in a sentence a person can act on

**Rewriting rules for copy:**

- Progress says **"Working…"** and shows a bar. It does not say "denoising
  step 14 / 28".
- Errors say what happened and what to do. The typed code may be kept inside a
  collapsed "Details" disclosure for debugging, never in the sentence.
- The result caption says **"Made in 12s"**, not a parameter dump. Everything
  else moves into a "Details" disclosure that is closed by default.
- The header currently reads "text → image on this machine, via
  `fused.ai.image`". Replace it with something a person understands.
- Buttons name what happens: "Generate", "Show me options", "More like this",
  "Use this photo". An action keeps the same name through the whole flow.

Keep the honesty rules from §1 — where the server settled on something other
than what was asked, that is still recorded, it just lives in Details rather
than on the surface. Hiding jargon must never become lying about what happened.

---

## 2. Phase 1 — the Edit tab

A new `edit` tab, added to `TABS`. This is the app's first surface that
consumes an image rather than producing one from text alone.

### 2.1 Choosing a base image

Four sources, all resolving to one absolute path on disk:

1. **From the last Studio render** — a button on the Studio result toolbar,
   "Edit this", which sets the base and switches tab.
2. **From the gallery** — a button on the gallery detail card, "Edit this".
3. **Upload** — a file input and a drop target on the Edit tab. Accepts
   PNG/JPEG/WebP.
4. **Webcam** — see §2.5.

Upload and webcam both go through `files.py` (already committed):
`fused.runPython("./files.py", {action: "import", data: <data URL>, name, source})`
returns `{path}` or `{error}`. Downscale client-side to a longest side of 1536
before encoding — the model fits to 1024 anyway and a 12 MP phone photo makes a
needlessly large POST body. Say nothing about the downscale in the UI; it is
below the model's own fit.

The base image path lives in the param `base`, so a refresh restores it.

### 2.2 The edit call

```js
const res = await render({ prompt: instruction, image: basePath }, onTick, { bare: true });
```

`render()` currently injects `width`/`height`/`steps`/`guidance` from params.
Add a third argument so an edit can opt out — the edit defaults are the
server's and must not be overridden (§0). Keep `model` and an optional `seed`.

Failure handling: on `bad_request` whose message names the engine, show a
banner explaining that editing needs the MLX FLUX engine and pointing at the
AI Models page's Engines tab. Do not retry.

### 2.3 The edit stack, with undo and redo

A linear history with a pointer — the standard undo/redo shape:

- `stack` is an array of steps. Step 0 is the base image.
  Each step: `{path, url, width, height, prompt|null, seed|null, elapsedMs|null}`.
- `at` is the index of the step currently shown.
- **Undo** decrements `at`; **Redo** increments it. Neither destroys anything.
- **A new edit made while rewound truncates the redo tail** (`stack.length = at + 1`)
  and pushes the new step. This is what every editor does; do it silently.
- Both buttons disable at the ends of the range. Show position as `3 / 5`.
- `⌘Z` / `⇧⌘Z` (and `Ctrl` equivalents) bind to undo/redo, but only while the
  Edit tab is active and no textarea has focus.

Each step renders as a row in a visible history strip — thumbnail, the
instruction that produced it, and a click to jump to that step. The strip is
the undo stack made legible; it is not a separate feature.

**Why the stack is not in params**: it is a list of absolute paths that grows
per edit, and a URL is the wrong container for it. `base` and the current
instruction go in params; the stack is session memory. Say so in a comment —
this is a deliberate exception to §1, not an oversight.

Nothing is destroyed by undo: every step is a real PNG the server wrote and
never overwrites, so a rewound branch's files stay on disk.

### 2.4 Before/after compare

A slider over the plate comparing the current step against the previous one
(step 0 compares against nothing — hide the control there). Implement as two
stacked images with `clip-path: inset()` driven by pointer position. Keyboard
accessible: arrow keys move the divider when it has focus.

### 2.5 Webcam capture

`getUserMedia` in a modal, a live `<video>`, a capture button that draws the
current frame to a canvas and sends `toDataURL("image/png")` to `files.py`.

**The shell frames app pages in an `<iframe>` with no `allow` attribute**, so
Permissions Policy blocks the camera there. This is verified, not speculative.
So:

- Feature-detect `navigator.mediaDevices?.getUserMedia` first.
- Catch the rejection. On `NotAllowedError` **while framed**
  (`window.parent !== window`), show a specific message: the camera is blocked
  in this frame, and the page can be opened in its own tab to use it — with the
  `/render?path=<abs path>` URL as a real link. Do not show a generic
  "permission denied", which sends the user to their browser settings for a
  problem that is not there.
- On `NotFoundError`, say there is no camera on this machine.
- Stop every track when the modal closes. A camera light left on after a
  dialog closes is a bug users notice and do not forgive.

---

## 3. Phase 2 — Sweep, batch, variations, presets, history

### 3.1 Style presets

A rail of chips above the prompt. Each preset appends a suffix to the prompt at
render time.

**There is no system prompt for an image model** — this is prompt
concatenation, and the UI must not pretend otherwise. Show the effective prompt
(or at least the appended suffix) so the user can see exactly what is sent.
Selected preset lives in the param `look` — **not** `style`, which Prompt Lab
already owns.

Presets, written as descriptive terms rather than artist or studio names, which
is both better prompting for FLUX and avoids leaning on a real studio's brand:

| id | label | suffix |
|---|---|---|
| `photo` | Photoreal | photorealistic, natural light, shallow depth of field, 35mm photograph, fine detail |
| `cinematic` | Cinematic | cinematic film still, anamorphic lens, dramatic key light, muted teal and amber grade |
| `lineart` | Line art | clean black line art, ink on white, uniform stroke weight, no shading, no colour |
| `anime` | Anime film | hand-painted anime film still, soft watercolour backgrounds, warm gentle light, cel-shaded characters |
| `cartoon` | Cartoon | bold cartoon illustration, thick outlines, flat saturated colour, exaggerated shapes |
| `watercolour` | Watercolour | loose watercolour painting, visible paper grain, bleeding pigment, soft edges |
| `oil` | Oil paint | oil painting, thick impasto brushwork, canvas texture, rich chiaroscuro |
| `render3d` | 3D render | 3D render, physically based materials, soft studio HDRI lighting, subtle ambient occlusion |
| `pixel` | Pixel art | 16-bit pixel art, limited palette, crisp dithering, sprite on a plain background |
| `blueprint` | Blueprint | technical blueprint, white line work on deep blue, orthographic projection, dimension lines |
| `riso` | Risograph | risograph print, two-colour overprint, halftone grain, slight misregistration, matte paper |

Presets apply to Studio and Sweep. They do **not** apply to an edit
instruction — appending "clean black line art" to "remove the car" is wrong.

### 3.2 "Show me options"

**The Sweep tab is CUT.** A sweep is an axis over seed, steps and guidance —
precisely the vocabulary §1.5 forbids. Its actual value to a beginner is
"show me a few takes so I can pick one", and that is what gets built instead.
Do not add a Sweep tab, an axis selector, or from/to inputs. If you find
leftover Sweep markup, delete it.

One button beside Generate: **"Show me options"**, which renders N takes of the
same prompt into a contact sheet. N lives in the param `batch` (2–8, default 4)
and is set in Advanced; the button's label says the current count ("Show me 4
options") so the number is visible without a control on the surface.

Renders serialize, so this is a loop, not a server feature. The UI must say
which take is running ("Making 2 of 4…") because a queued render's job row says
nothing about queueing. Stop abandons the remainder, reusing the existing
`abandon` flag.

Clicking a take promotes it to the main plate. Each take can be saved or sent
to Edit. No take shows a seed or any other parameter on its face.

### 3.3 "More like this"

On any finished render or any take in the sheet: **"More like this"** — reruns
the same prompt and settings with fresh seeds into the same contact sheet.

**Do not describe this as reproducing or varying the image** (§0): on this
engine a repeated seed does not reproduce a picture, so "more like this" is an
honest promise and "the same image with small changes" is not.

### 3.5 Prompt history

The last ~30 prompts actually rendered, most recent first, deduped, in
`.fused/data/` via a small Python action. Distinct from the gallery: it is
prompts, not images, and it includes prompts whose renders were never saved.
Click one to load it into the prompt box.

---

## 4. Phase 3 — Gallery: semantic search, find-similar, tags

The gallery searches prompt substrings today. Add search over the *pixels*.

- **Index**: embed each gallery PNG with `fused.ai.embed({paths})`. Store
  vectors plus the embedding model id in `.fused/data/`. Index incrementally —
  only images with no vector — and never block the gallery listing on it.
- **Guard the model** (§0): if the stored model id differs from what
  `embed` returns now, the index is invalid. Refuse to search it and offer a
  rebuild. Do not rank with mismatched vectors.
- **Check `acceptsPaths === true`** on the chosen model before offering image
  search at all; a prose encoder cannot see pixels. If no such model is
  available, hide the feature and say why.
- **Semantic search**: embed the query text, rank by dot product (vectors come
  back unit-length, so no normalisation). Keep the existing substring search as
  a separate, always-available mode — do not replace it.
- **Find similar**: rank the rest of the gallery against one image's vector.
- **Auto-tags**: zero-shot — embed a fixed label vocabulary once, rank each
  image against it, keep labels above a threshold. Render as filter chips.
  These are guesses; label them as such in the UI.

---

## 5. Phase 4 — the redesign: "The Enlarger"

The current look is a dark developer settings panel: a permanent sidebar of
form fields, uppercase letterspaced labels, mono everywhere. Replace it.

**Chosen direction — the page is a print easel.** The image is the subject; the
machinery is put away.

### 5.1 Information architecture

- **The plate** — one large image on a recessed mat, centered, dominating the
  page. Every surface that shows an image uses the same plate component.
- **The prompt bar** sits beneath the plate like a caption card on a gallery
  wall: the prompt, the preset rail, and the single primary action.
- **The sidebar is DELETED — not restyled, not collapsed, not narrowed.** The
  `<aside>` element does not survive this phase. Of everything it holds today,
  **exactly one control stays on the surface: the aspect ratio.** Every other
  thing in it — the model picker, Preload, Unload, the width and height number
  inputs, the pixel-size chips, Steps, Guidance, Seed, Random/Clear, the takes
  count, autosave, and Prompt Lab's Variants and Style selects — moves into a
  **right-edge Advanced drawer** that slides over the content without
  reflowing it. Per §1.5 these are not merely tucked away for tidiness — a
  beginner must never meet them. The drawer is the only place any of that
  vocabulary is allowed to appear.
- **The aspect ratio chooser names the USE, not the number.** `512²`, `3:2`,
  `912x512` and the width/height inputs are all pixel vocabulary and all
  belong in the drawer. The chooser sets the same underlying `w`/`h` params;
  only its labels change. An explicit size set in the drawer wins and the
  chooser reflects it.

  Midjourney's own beginner docs (§5.5) never make a newcomer reason about
  the ratio itself — they attach each one to something the reader already
  owns: 1:1 is "social media profile pictures", 16:9 is "HD videos", 9:16 is
  "mobile content". Do the same. Four options, each a shape word with the
  familiar use as its supporting line:

  | Label | Sets | Supporting line |
  |---|---|---|
  | Square | 1024×1024 | Profile pictures and posts |
  | Portrait | 768×1024 | Phone screens and prints |
  | Landscape | 1024×768 | Photos and slides |
  | Wide | 1024×576 | Video and banners |

  Render them as four labelled shape swatches whose proportions are drawn to
  scale, so the control shows what it does before it is read.
- **On the surface**: the prompt, the style presets, the aspect ratio chooser,
  Generate, and "Show me options". Nothing else. If a control is not on that
  list, it is in the drawer — there is no third place.
- **The caption under the plate says "Made in 12s"** and nothing more. The
  settled facts (size, seed, steps, guidance, path) move into a closed
  "Details" disclosure — still recorded, never on the surface (§1.5).
- **Three modes, not four** — Create, Edit, Gallery. Prompt Lab stops being a
  destination (§5.7). They render as a thin icon-only rail per §5.6, not a row
  of text buttons. Runtime state is a small lozenge, and since "which model is
  resident" is drawer vocabulary per §1.5, the lozenge shows only whether the
  app is busy or idle — never a model id, never a memory figure.

### 5.2 Tokens

**The ground is LIGHT.** Revised against the user's reference screenshot
(§5.6): a soft neutral field (around `#F5F5F7`) with the image itself on plain
white, not the dark graphite darkroom this section originally called for. A
dark panel reads as a tool for operators; the reference reads as a place to
look at a picture, and that is the brief. Dark mode still ships — it is the
alternate, no longer the primary.

Two accents with **distinct jobs**, which is what keeps this from being
decoration:

- Selenium violet — identity and every interactive state: focus rings, the
  active shape swatch, the selected preset. (Selenium toning genuinely shifts
  a print purple-brown; the accent is drawn from the subject, and it survives
  the move to a light ground unchanged.)
- Safelight amber — **reserved exclusively for work in progress**. If it is
  amber, something is developing. Never use it for an ordinary control.

Surfaces are separated by tone and soft shadow, not by borders and rules.
Radii ~10px on cards and the prompt bar, ~8px on controls. No hairline boxes
around everything — the current app's biggest tell after the uppercase labels.

Define the complete light palette on bare `:root` and redefine tokens under
both `@media (prefers-color-scheme: dark)` and `[data-theme="dark"]`, per
`fused-render-theming`. Keep `data-fused-theme="shell"` on `<html>`.

### 5.3 Type

System faces only (§0). Avenir Next / Avenir Next Condensed for display and
labels, system-ui for body, SF Mono for data and paths, each with a real
fallback stack. Drop the uppercase letterspaced micro-labels — they are the
single biggest contributor to the "developer panel" read.

### 5.4 Signature

A hairline rule that traces the plate's perimeter as denoising steps land — an
SVG rounded rect driven by `stroke-dashoffset`, replacing the corner brackets
and the `<progress>` bar. Exposure in progress, tied to the frame that holds
the picture. This is the one flourish; keep everything else quiet, and let the
existing blur-to-sharp preview transition survive unchanged — it is the best
thing in the current app.

Respect `prefers-reduced-motion` throughout, keep visible keyboard focus, and
make the layout work down to a narrow pane — the app is often opened in a
split.

### 5.5 What the mainstream tools do, and what we take from them

Surveyed August 2026: Midjourney's web Create page, and the one-tap editors
(Canva Magic Eraser / BG Remover, Photoroom AI Retouch). Three patterns are
worth copying, one is worth rejecting.

**Take: the prompt bar carries its own tools.** Midjourney's "Imagine bar" is
a single wide input with two icons inside it — add an image, and open
settings — and that is the entire visible control surface. Our caption card
becomes the same thing: the prompt, the preset rail, an image button, a
settings button that opens the drawer, and the primary action. This is the
strongest confirmation of §5.1 available: the most-used generator on the
market ships one input and a gear.

**Take: name the use, not the parameter.** Folded into the aspect table above.

**Take: one-tap edit actions instead of a blank instruction box.** Canva and
Photoroom lead with *named jobs* — Remove background, Erase object, Retouch —
not with a free-text field, because a beginner facing an empty box does not
know what the tool can do. The Edit tab has exactly this problem today. Add a
rail of canned instructions above the instruction box, mechanically identical
to §3.1's presets but writing the whole instruction rather than a suffix:

  Remove the background · Make it brighter · Black and white · Change the sky ·
  Make it look like a painting · Remove the text

Clicking one fills the box, so it stays editable and teaches the format by
example. The box remains for anything not on the rail.

**Reject: Midjourney puts aspect ratio in the settings panel too.** We
deliberately keep it on the surface — their audience arrives already knowing
what `--ar 16:9` means, and ours does not. This is the user's explicit call
and it outranks the reference.

**Note but do not build: the creation feed.** Midjourney's Create page is a
scrolling feed of past generations rather than a single result pane, which is
close to what our Gallery, contact sheet and prompt history each do a third
of. Worth unifying one day; out of scope for this phase, and listed here so
the next person knows it was considered rather than missed.

### 5.6 The reference screenshot — the target for "clean and simple"

The user supplied a screenshot of Kittl's editor as the standard to hit. What
to take from it, concretely — this is the controlling reference for the look,
and it outranks §5.2's original darkroom palette wherever they disagree:

- **Light neutral ground, image on plain white.** A large empty field around
  the artwork; the picture is the only thing with weight. Not dark.
- **One floating prompt pill, bottom-centre.** Rounded, full-width-ish,
  generous vertical padding, a small leading icon, and placeholder copy
  written as an invitation — theirs is "Describe what you want to create".
  Our caption card becomes this. It floats over the field rather than sitting
  in a bordered card.
- **A thin icon-only left rail** for navigation, glyphs with no labels. Our
  three modes become this instead of a row of text buttons.
- **A small floating toolbar** of icon actions grouped in a pill — undo/redo
  sit here. Our Edit tab's undo/redo and the result actions adopt this shape.
- **Soft radii (~8–10px), tone-and-shadow separation, no visible borders**,
  and sentence-case labels at normal weight and tracking.
- **Every property control is off in a right panel**, which is exactly the
  drawer §5.1 already requires — the screenshot is dense there and that is
  fine, because a beginner never opens it.

Do not copy its right-panel density onto our surface: what makes the reference
feel clean is that the canvas half is nearly empty, and that half is the whole
of our default view.

### 5.7 Prompt Lab stops being a tab

A whole destination for "write me some prompt variants" is backwards: it is
not a place you go, it is help you want **while typing**, in the box you are
already typing in. Delete the tab and fold the capability into the prompt bar.

- A small **"Help me write this"** affordance sits in the prompt bar (a
  sparkle, per §5.6's leading icon). It is enabled once there is some text —
  it needs an idea to work from.
- Pressing it expands a panel **directly beneath the prompt bar** with the
  rewritten variants. Each is one tap to adopt: the text replaces what is in
  the box, the panel closes, and the user is back where they started with a
  better prompt. Nothing else about the page changes.
- Variants render as text first. The Lab's existing per-variant preview
  renders are kept but become opt-in, not automatic — automatically firing N
  image renders because someone asked for wording help is the kind of
  expensive surprise a beginner cannot predict.
- `variants` and `style` move to the drawer with everything else (§5.1). The
  panel is not a settings surface.
- Keep `fused.ai(` guarded exactly as today, and keep the app's local-only
  export note intact — folding the feature in must not change where it runs.

**Flagged, not built: Edit could fold in the same way.** The reference and
Midjourney both attach a base image via a button *in the prompt bar*, which
turns the same box into an edit instruction. That would take the app to two
modes — Create and Gallery. It is the logical conclusion of this note and it
is very likely right, but it is a larger consolidation than was asked for, so
this phase keeps Edit as a mode and makes the prompt bar's image button the
way you reach it. Revisit once the redesign has landed.

### 5.8 Branch `edit-tab`: the flagged merge, built — and two other decisions

Three decisions from this branch, recorded so they don't get re-litigated:

**512, not 1024, is the default render size.** `SHAPES` in the script sizes
its four swatches around 512² rather than 1024² for a load-bearing reason: on
this machine a 512² render lands in a few seconds, where 1024² takes minutes.
A beginner judging whether a prompt worked needs that answer fast far more
than they need the extra pixels — bigger sizes are still one tap away in the
Advanced drawer for anyone who wants them. This is a machine-specific
performance fact, not a taste choice; re-check it before ever moving the
default back up.

**The composer has one anatomy that both modes wear differently, not two
composers.** `.composer` is always: a `.composer-settings` row, then the
`.pill` with the text field and primary button. Every mode built after §5.6 —
Create's shapes-and-style-chips row, Edit's job chips — is that same anatomy
with a different `.composer-settings` slotted in, never a second box stacked
underneath. That is what made the next decision cheap to build.

**Create and Edit merged into one workspace, exactly as §5.7's flag predicted
— with one addition it didn't anticipate: Start Over is explicit, not
inferred.** One plate, one composer; which content shows depends only on
`stack.length` (empty plate + "Generate" vs. a picture + "Change it"). The
rail dropped to two modes (the workspace, Your pictures); `tab=edit` is kept
as a silent alias for `tab=studio` so an old bookmark still lands on the
workspace. The one design choice worth naming: the reference tools that
inspired §5.7 let typing alone decide whether you're making something new or
changing what's there. This app's owner was offered exactly that — one field,
two buttons — and asked for the explicit version instead: with a picture on
the plate, the composer is unambiguously in "change it" mode, and the *only*
way back to a blank plate is a named **Start over** action on the plate's own
action pill. Typing never silently decides between the two intents. Also
folded in while merging: a gear on the prompt bar itself (the rail's own gear
sits at the bottom of a thin icon rail, nowhere near where typing happens),
and the plate's action pill now shows on hover/focus rather than sitting
permanently over the picture's lower edge.

---

## 6. Verification

**No UI tests. No test framework. Do not add one, and do not work test-first.**
This app is optimised for moving fast, and a browser-automation suite around a
single-file page would cost more than it caught. That is a deliberate call by
the app's owner, not a gap to fill.

There is no test suite and no CI. Verification is:

1. `python3 -c "import gallery, files; print(...)"` style direct calls for any
   `.py` change — each `main()` is an ordinary function.
2. The page must be opened in a browser; nothing runs its JavaScript from a
   terminal. Load it top-level at
   `http://127.0.0.1:1777/render?path=/Users/iamsdas/Fused/showcase/local-image/index.html`
   (top-level, not `/explorer/embed/`, so the webcam is testable).
3. `fused-render calls --page /Users/iamsdas/Fused/showcase/local-image/index.html --since 15m`
   after opening it. **Zero records means the page's JS died before reaching
   Python** — a different bug from a failing `main()`, and they look identical
   without the log.
4. A render takes real time and real memory. Do not fire renders in a loop to
   test; one is enough to prove a path.
5. **Rendering works again as of 2026-08-29** — the fork-safety crash that
   killed every worker with `code -11` is fixed, and a 512² four-step render
   was confirmed landing a real PNG. So the redesign must be judged against a
   real picture on the plate, not an empty frame: generate at least once, and
   look at the result before calling the phase done.
