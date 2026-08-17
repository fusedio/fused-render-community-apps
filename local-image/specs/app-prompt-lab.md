# Prompt Lab

> **Status — target.** Defers to `app.md` for the shell. This file owns the
> **text→prompts→images surface**: the expansion call, the strict-JSON parse, the
> editable variant list, the batch render, and the export consequence. Implementing
> symbols: `index.html` (`expand`, `parseVariants`, `renderVariants`, `EXPAND_SYSTEM`).
> Assumes `assumptions.md §1–§5`.

## 1. What it demonstrates

Both AI destinations in one flow. A rough idea (`idea`) goes to **`fused.ai`** —
text, via the `claude` CLI — which returns `variants` (2–6, default 3) fully-written
image prompts; each then goes to **`fused.ai.image`**. It is the surface that shows
the two calls are the same bridge, and that a language model is a good way to author
input for an image model.

Controls: `idea` (textarea), `variants` (2–6), and a **style** select
(`none` | `photographic` | `illustration` | `3D render` | `technical diagram`) that
is appended to the system prompt, not to the user's idea — a style is a constraint
on the expander, not part of what the user asked for.

## 2. The expansion call

```js
fused.ai(prompt, { systemPrompt: EXPAND_SYSTEM, effort: "low" })
```

- **Claude path deliberately** — no `model` is passed. `temperature`, `history`,
  `raw`, `topP` and `maxTokens` are **local-model-only and rejected with a 400 on
  the Claude path** (`assumptions.md §1` context), so none are sent. Using the
  default destination also means no cold multi-GB load stands between pressing the
  button and seeing prompts, which is the right trade for a step that exists to be
  fast.
- `effort: "low"` — this is prompt-writing, not reasoning.
- **The button is disabled while the call is in flight.** `fused.ai` has no
  stale-cancel; a double-click is two real calls.
- The reply's `res.model` and `res.usage` are shown in a small meta line —
  `{input_tokens, output_tokens}`, **Anthropic names**; there is no
  `prompt_tokens`/`completion_tokens` and reading them would silently print
  `undefined`.

`EXPAND_SYSTEM` asks for a **bare JSON array of N strings and nothing else**, each
a self-contained image prompt of roughly 25–50 words, materially different from the
others (not the same scene reworded), with no numbering and no commentary.

## 3. Parsing, defensively

`parseVariants(text)` must not let a chatty model break the surface:

1. Strip a leading/trailing markdown code fence if present.
2. Take the substring from the first `[` to the last `]` and `JSON.parse` it.
3. Require an array; keep only non-empty strings; trim; cap at `variants`.
4. **On any failure, fall back to splitting on newlines** and stripping leading
   list markers (`1.`, `-`, `*`).
5. If that still yields nothing, show a banner with the raw reply and stop.

A model that returns prose is a normal outcome to handle, not an exception to
propagate. The raw reply is always available behind a "show raw response" toggle,
because a parse that silently produced two variants from a reply that contained
four is worse than one that says what it saw.

## 4. `ai_unavailable` is a first-class state

The `claude` CLI may simply not be installed. `err.type === "ai_unavailable"` gets a
friendly banner carrying `err.message` (which names what to install or set), and the
Expand button stays disabled until a retry — **not** a red traceback overlay. The
rest of the app is unaffected: image generation does not go through the CLI, so
Studio, Sweep and Gallery keep working. The banner says that too, so a user without
Claude Code does not conclude the app is broken.

Other rejections route normally (`app.md §6`); `timeout` (600 s) offers a retry.

## 5. Variants and rendering

Parsed variants render as an **editable list** — each row a textarea holding one
prompt, with per-row **Render**, **Send to Studio** (writes `prompt`, sets
`tab=studio`), and **Remove**. The user is expected to edit them; a read-only list
would make the model's output final, which is the opposite of a lab.

**Render all** runs the rows sequentially under the same rules as a sweep
(`app-sweep.md §3`): one call in flight, overall `render 2 / 3` progress, a failed
row does not abort the rest, Stop cancels the current row and abandons the queue.
Each row's image appears in the row itself with its settled seed. Size, steps,
guidance and model come from the shared params — the variable here is the prompt,
so nothing else may vary.

Auto-save applies as everywhere else (`app-studio.md §5`).

## 6. This surface makes the page non-exportable

The exporter rejects any file containing the literal string `fused.ai(`
(`assumptions.md §5`), matched **textually** — so `IS_LOCAL` guards, aliasing, or
computing the property name would not change the verdict, and the last two would
only trade a clear export-time refusal for a page that ships broken.

**Accepted, and stated in the UI.** A one-line footnote on this tab says the app is
local-only by design and names this call as the reason. Aliasing the call to sneak
past the check is explicitly forbidden. If an exportable build is ever wanted, the
fix is to remove this surface from `index.html` — not to disguise it.

## Non-goals

- Local text models. `history`/`raw`/`temperature` and the `model_loading` retry
  dance they require are real, specified upstream, and not what this surface is
  for; it demonstrates the *pairing* of the two calls, and the Claude path makes
  that pairing fast. Recorded here so the omission reads as a choice.
- Chat history — a single-shot expansion is the whole interaction.

## See also

- `app-studio.md` — where a variant goes to be tuned.
- `assumptions.md §5` — the export rule this surface trips.
