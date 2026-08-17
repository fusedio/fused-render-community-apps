# Assumptions

> **Status — shipped (fused-render runtime, not this app).** This file owns the
> **external contracts this app is written against**: the `fused.ai` text reply on
> the local path, the cold-start rejection, cancel semantics, the catalog/runtime
> shapes, the export rule, and param typing. Nothing here is ours to change — it is
> recorded so no surface spec has to restate it and no reader has to go spelunking
> `runtime.js`. Implementing modules (upstream):
> `fused_render/static/runtime.js` (`ai`, `ai.cancel`, `aiModels`, `watchJob`),
> `fused_render/server/ai.py` (`_local_relay`, `_sampling_problem`,
> `_history_problem`), `fused_render/ai/supervisor.py` (`generate_text`,
> `cancel_generation`), `fused_render/ai/runners/mlx_text/worker.py` (`generate`).

## 1. `fused.ai(prompt, opts)` — the one call this app is built on

**The destination is decided by the model id, and by nothing else:** an id
containing a `/` is a Hugging Face repo served by a worker process on this
machine; anything else goes to the `claude` CLI. **Every call in this app names a
slashed id**, so this app is entirely the local path. That is not a detail — half
the options below do not exist on the other one.

**Request.** Only these keys are sent; anything else the bridge drops.

| Key | Type | Default | Local path |
|---|---|---|---|
| `prompt` | string, non-empty | — (required) | the turn being asked **now**; empty rejects `bad_request` **client-side**, before any POST |
| `model` | HF repo id | user's configured default | absent runner → `ai_unavailable`; not resident → `model_loading` (§2) |
| `systemPrompt` | string | `"You are a helpful assistant."` | sent as a `system` message only when it **differs from that exact default string** |
| `history` | `[{role, content}]` | `[]` | **local only.** `role` ∈ `user` \| `assistant` — any other role, a non-string `content`, or a non-list is `bad_request` naming the offending index |
| `raw` | boolean | `false` | **local only.** Prompt goes to the model verbatim, no chat template. Mutually exclusive with `history` — sending both is `bad_request` |
| `temperature` | number | `0.7` (worker) | **local only.** Range **0.0–2.0**; outside → `bad_request` |
| `topP` | number | `0.95` (worker) | **local only.** Range **0.0–1.0** |
| `maxTokens` | number | `1024` (worker) | **local only.** Range **1–32768** |
| `effort` | string | — | Claude path only; **ignored** here. Never sent by this app |
| `onChunk` | `(text) => void` | — | not sent; opts the request into NDJSON streaming and fires per delta |

Bools are refused where a number is expected (`temperature: true` is
`bad_request`, not `1`), and sampling is validated **only** on the local path.

**Reply.** Resolves with the same shape streaming or not:

```js
{ text, model, usage }
```

- `text` is the **complete** completion even when streaming — the server
  accumulates it and puts it in the terminal `done` frame. A page that streamed
  into a DOM node does not have to have kept the string itself.
- `model` is the id that actually ran.

**`usage` IS NOT THE ANTHROPIC SHAPE ON THIS PATH.** The local relay returns:

```js
usage = { output_tokens: 412, seconds: 9.31 }
```

There is **no `input_tokens`** — reading it gives `undefined`, and a UI that
prints it shows a blank where a number belongs. `seconds` is the worker's own
generation clock and is **absent on a cancelled run** (§3). Every surface reads
`output_tokens` and treats `seconds` as optional (`app-chat.md §5`).

**Rule: echo the reply, never the request.** `res.model` is what answered; the
dropdown is only what was asked for. They differ whenever another page or the AI
Models tab swapped the resident model under us.

## 2. Cold start: text FAILS FAST, images do not

This is the sharpest difference from the sibling app, and getting it wrong is
the single most likely way to ship a broken first-run experience.

A `fused.ai()` call naming a model that is not resident **rejects immediately**
with `.type === "model_loading"` — *having already started the load* — and hands
back `err.jobId`. It does not wait. The reasoning upstream is explicit: a chat
box must not hang for the minutes a cold multi-GB load takes, so the first call
fails fast having kicked off exactly the work the caller needed.

**So this app owns a cold-start retry dance, and every generating surface uses
the same one** (`app.md §5`):

```js
try {
  return await fused.ai(prompt, opts);
} catch (err) {
  if (err.type !== "model_loading") throw err;
  await fused.watchJob(err.jobId).watch(onTick);   // bytes; resolves on terminal state
  return await fused.ai(prompt, opts);             // resident now — retried ONCE
}
```

- `err.jobId` is the load's row. `fused.watchJob(id).watch(cb, ms)` polls
  `/api/jobs` (~700 ms), calls `cb(record)` per tick, and resolves with the
  terminal record — **or `null`** if the row vanished (retention window). A `null`
  is not a failure; retry the call and let the second attempt say what is true.
- The tick record's `done`/`total` are **BYTES** (a model download), not tokens.
- **Retry exactly once.** A second `model_loading` after a completed load means
  something evicted the model between the two calls, and looping would hide it.
- A model that is *already loading* (started by another page) rejects the same
  way with the in-flight job's id, so the dance covers that case for free.

## 3. Cancel: Stop RESOLVES, it does not reject

`fused.ai.cancel(capability?)` → `Promise<boolean>`. The argless form defaults to
`"text-generation"`, which is exactly this app's capability — so **the bare call
is correct here** (unlike the image sibling, where it had to be named).

What happens to the in-flight generation is the part that surprises people. The
worker sees the cancel flag between tokens and closes its stream with
`{"type":"done","ok":true,"cancelled":true,"tokens":<n>}`. `ok` is **true**, so:

- The `fused.ai()` promise **resolves normally** with the tokens produced so far.
- `usage.output_tokens` is the partial count; **`usage.seconds` is absent.**
- There is **no `cancelled` rejection type on the text path at all.**

Consequences the surfaces must honour: a stopped answer is **kept**, not
discarded (`app-chat.md §4`); tokens/second must be computed from a clock the
page owns, because the server's is missing on precisely the runs a user is most
likely to stop (`app-chat.md §5`); and `cancel()` resolving `false` (nothing was
generating) is not an error.

`fused.ai.cancel()` stops **generation only** — the weights stay resident, which
is what makes Stop cheap and re-asking instant.

## 4. Rejections

Every rejection is an `Error` with `.type` (and `.jobId` where a row exists):

| `.type` | Meaning here | Required UI response |
|---|---|---|
| `model_loading` | **Not a failure.** The model was not resident; this call started the load. `err.jobId` is it. | Show the download, then retry once (§2). |
| `ai_unavailable` | The worker will not start, the model was unloaded mid-wait, or the runner is missing. | Banner with `err.message`; disable Send. |
| `bad_request` | Our bug — empty prompt, out-of-range sampling, malformed `history`, `raw` + `history`. | Show it; it means the page shipped broken. |
| `ai_error` | Ran and failed (no model loaded, worker crash, generation error). | Banner with `err.message`. |
| `timeout` | 600 s with no answer. | Offer retry. |

`cancelled` and `unavailable` are **image/transcription types and cannot reach
this app** — no surface may branch on them (§3 explains why `cancelled` in
particular is a trap: it looks like the obvious Stop path and never fires).

## 5. Model discovery — always ask, never hard-code

- `fused.ai.models.catalog()` → `{capabilities: [{capability, runner, runnerLabel,
  available, reason, default, models: [{id, label, size_gb, note}]}]}`.
- `fused.ai.models.list()` → `{runners: [{code, capability, label, note, available,
  reason}], loaded: [...], downloading: [...], totalResidentBytes}`.

**A repo id belongs to a backend, not to a capability.** `text-generation` has
**two** runners — MLX (Apple Silicon) and Transformers (PyTorch) — and the user
can force either from Preferences → Inference engines. MLX wants 4-bit MLX repos
(`mlx-community/Qwen3-8B-4bit`); Transformers wants unquantized safetensors
(`Qwen/Qwen3-8B`). A hard-coded id becomes an unloadable download the moment the
other engine serves. Every model list in this app is read from `catalog()` at
load time, and the `default` it reports is the fallback for the `model` param.

- `fused.ai.models.load(id, {capability: "text-generation"})` → `{jobId}` — a job,
  **not a loaded model**. Watch it with `fused.watchJob(jobId)`. Pass the
  capability even though text generation is the inferred fallback: naming it
  costs nothing and keeps the call correct if the catalog is ever wrong.
- `fused.ai.models.unload({capability: "text-generation"})` — **by capability,
  never by id.** The resident model may not be the one our dropdown shows.
- **An engine switch EVICTS** whatever was resident. The strip and the model list
  are therefore re-read after every load, unload, and generation failure
  (`app.md §4`), never cached across one.

**Observed on this machine (2026-08-16), recorded as a reading, not a constant:**
`text-generation` served by `mlx-text` ("MLX (Apple Silicon)"), both runners
`available: true`, nothing resident, catalog default `mlx-community/Qwen3-8B-4bit`
(4.6 GB) alongside Gemma 3 12B (8.1 GB), Gemma 3 4B (3.4 GB), and Ternary Bonsai
27B (6.1 GB). The sibling app recorded two probes minutes apart that disagreed
after a Preferences switch — the concrete reason this section says *always ask*.

## 6. Export: this app is local-only, deliberately

- The exporter **rejects any page containing the literal string `fused.ai(`**
  (SPEC RH-11) — a *textual* match, so an `if (fused.env === "local")` guard does
  not make it exportable. Every surface here generates text, so **the entry page
  is non-exportable by construction**. Accepted, and stated in the UI rather than
  worked around.
- The **dotted** calls (`fused.ai.models.*`, `fused.ai.cancel(`) slip past that
  match and would export cleanly, then fail at the reader. Nothing stops us at
  export time, so **every dotted call is gated on `fused.env === "local"` by
  hand** (`app.md §6`).
- `fused.trackJob` and `fused.watchJob` export fine. We only ever *watch* rows the
  server owns; we never create one.

## 7. Params are strings; Python coerces

`fused.params.get()` always returns a string or `undefined`, and `set()` **throws
on a non-string**. Numbers are `String(n)` out and parsed in. Python-side
coercion is driven by annotations on `main()` — `limit: int` receives `int("50")`,
an unannotated parameter receives `"50"` (`app-library.md §2`).

## 8. File IO: only `readFile` is page-relative

Measured against the running server on 2026-08-16, not read off the docs, and it
cost an afternoon:

| Call | Relative path | Why |
|---|---|---|
| `fused.readFile(p)` | **works** | It is built on `rawUrl`, which appends the page's own absolute path as `base` and lets the server join them. |
| `fused.rawUrl(p)` | **works** | Same mechanism, and the only one that documents it. |
| `fused.stat(p)` | **404** | Sends `?path=` bare — no `base` — so the server resolves against *its own* working directory. |
| `fused.writeFile(p, …)` | **404 / wrong file** | Same: the POST body is `{path, content}` with no `base`. |

So a page cannot mix the two styles: `readFile("chats/x.json")` succeeding next to
`stat("chats/x.json")` failing reads as a missing file rather than as a resolution
difference, which is exactly how it presented.

**This app therefore makes every path absolute itself**, from the page's own URL,
the same way the runtime builds `base`:

```js
const PAGE_PATH = new URLSearchParams(window.location.search).get("path") || "";
const PAGE_DIR  = PAGE_PATH.slice(0, PAGE_PATH.lastIndexOf("/"));
const here = (rel) => (PAGE_DIR ? PAGE_DIR + "/" + rel : "./" + rel);
```

`runPython("./chats.py")` is unaffected — it resolves page-relative by its own
documented contract, and `chats.py`'s own working directory is set to the folder
holding it, so `./chats/` inside Python needs no such helper.

## See also

- `app.md` — the shell that puts these contracts to work.
- `../../text-to-image/specs/assumptions.md` — the same file for the image path;
  read it beside §2 and §3 to see which differences are real.
