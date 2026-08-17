# App shell

> **Status — target.** This file owns the **shell**: the single entry page, the
> params that hold the app's settings, the shared runtime/model strip, the
> app-wide generation lock, the cold-start helper, the notice surface, and
> theming. It owns nothing about what the chat surface renders. Implementing
> modules: `index.html` (`draw`, `refreshRuntime`, `fillModels`, `generate`,
> `notice`, `lock`, `P`), `chats.py` (`main`). Assumes `assumptions.md` throughout.

## 1. Family index

| Surface | Where | Spec |
|---|---|---|
| Chat | `<main>` — the page | `app-chat.md` |
| Library | the `<aside>`'s Saved chats group | `app-library.md` |

**One folder, one entry page, one surface.** `index.html` carries
`<meta name="fused-app" />` near the top of `<head>` — the only thing that makes
this folder an app. There is **no tab bar**: the app is a chat page, and the
saved-transcript list lives in the same aside as the model and sampling controls
rather than behind a tab. Two earlier surfaces (Sweep, Raw) were cut with the tab
bar; `overview.md` records what went with them.

## 2. Params are the settings — and the one deliberate exception

Every piece of **settings** state lives in `fused.params`, so any view is
refresh-proof and bookmarkable, and so the Library can hand settings to Chat by
writing params rather than reaching into its DOM.

| Param | Default | Owner | Meaning |
|---|---|---|---|
| `model` | catalog default | §4 | HF repo id for `text-generation`. |
| `mode` | `chat` | `app-chat.md` §9 | `chat` (template + history) or `raw` (a bare continuation). |
| `system` | `""` (= the worker's own default) | `app-chat.md` §2 | System prompt. |
| `temp` | `0.7` | `app-chat.md` §2 | Temperature, 0–2. |
| `topp` | `0.95` | `app-chat.md` §2 | Top-p, 0–1. |
| `maxtok` | `1024` | `app-chat.md` §2 | Max new tokens, 1–32768. |
| `think` | `1` | `app-chat.md` §6 | Collapse `<think>` blocks (`1`) or show them inline (`0`). |
| `find` | `""` | `app-library.md` §3 | Saved-chat search text. |
| `chat` | `""` | `app-library.md` §4 | Filename of the open transcript. |

**The exception, and its reason: the conversation is NOT a param.** A transcript
is unbounded and grows with every turn; a URL is not, and `replaceState` is rate
limited. Turns therefore live in one JS array, `convo`, and reach the URL only as
a *filename* (`chat`) once the user saves them (`app-library.md §4`) — or, for an
unnamed one, as the autosave `app-library.md §5` writes beside them. What this
costs is stated plainly in the UI rather than hidden: **a refresh keeps every
setting, and restores the open conversation from the autosave without ever
putting it in the library.** Save is still the durability story
(`app-chat.md §8`) — it is what gives a conversation a name, a row, and a life
longer than the next New chat — and it is one click.

**The wiring rule, without exception:** a control writes its param and does
nothing else; `fused.params.onChange(draw)` is the single re-render path; `draw()`
reads params, never `input.value`. A control that reads its own DOM to decide
what to render is a state fork, and refresh loses it.

**`draw()` must be idempotent and cheap.** It reflects params into controls and
repaints the transcript from `convo`. It never generates, never calls `fused.ai`,
and never writes a param — a `set()` inside `draw` is a render loop, and the call
log shows it as calls with no interactions (`fused-render calls --page <page>`).

Cheap has one live consequence: `draw()` runs on **every** param change,
including each drag tick of a slider, and the saved-chat list is a `runPython`
round trip. So `draw()` rebuilds that list only when `find` differs from the
value it was last built for (`shownFind`), and otherwise just re-marks which row
is open. Save and Delete rebuild it directly, because they change the directory
without changing `find`.

## 3. Layout

Deliberately the same skeleton as the image sibling, so someone who has read one
can read the other:

- `<header>` — title, a one-line subtitle naming the bridge call, and a **runtime
  strip** on the right (§4): a status dot plus the active runner's label.
- `.layout` grid: a fixed-width `<aside>` control panel on the left, `<main>` on
  the right. The aside holds three always-present groups — Model, Sampling, and
  Saved chats (`app-library.md`).
- `<main>` holds the notice surface (§7), the transcript, and the composer.

## 4. Runtime and model strip (shared)

On load, and after every load / unload / generation failure, in this order:

1. `fused.ai.models.list()` → find the entry in `runners` whose `capability` is
   `text-generation`. Record it (`available`, `reason`, `label`) and read
   `loaded` for an entry on that capability — that, and not the dropdown, is what
   is resident.
2. `fused.ai.models.catalog()` → find the `text-generation` capability block; fill
   the model `<select>` from its `models` (label + state + `note`), and take
   its `default` as the fallback for the `model` param.

**Every entry `models[]` carries is offered, and each says what picking it would
cost** — `resident` (a worker is holding it now), `on disk` (`downloaded`, so the
first message is a load and not a fetch), or `4.6 GB download`. The catalog
already includes repos found on this disk that the shortlist never heard of
(`source: "cached"`), and filtering to the curated ones would hide exactly the
model a user went and downloaded on purpose. `resident` is decided from step 1's
`loaded`, not from the catalog's own flag, because `list()` is the authoritative
answer to what is held right now. A resident model the catalog does not list at
all — loaded from the AI Models page, say — is appended, so the strip can never
name a model the dropdown cannot select.

Both are read **every time**, never cached across a load/unload, because the
resident model can change under us (the AI Models page, another app, an engine
switch in Preferences, which evicts — `assumptions.md §5`).

**Three honest states, and the strip says which:**

| State | Dot | Text | Effect on Send |
|---|---|---|---|
| Runner available, model resident | on (accent) | `MLX (Apple Silicon) · Qwen3 8B (4-bit) resident` | enabled |
| Runner available, nothing resident | on | `MLX (Apple Silicon) · first message downloads 4.6 GB`, or `· first message loads Qwen3 8B (4-bit) from disk` when it is already downloaded | **enabled** — the wait is the cold-start dance (`assumptions.md §2`), shown as progress, not refused |
| No runner | off (err) | the runner's `reason`, verbatim | **disabled**, with the reason in a banner |

**Two runners are listed for `text-generation` and only one is serving.** This
build's `/api/ai/runtime` reports both with `available: true` and no `active`
flag, so the strip names the **first available** entry and says "or Transformers"
nowhere — guessing which one serves would be a lie the payload cannot support.
The truthful label is the runner set; the truthful *proof* is `res.model` on the
first answer, which the Chat meta line shows (`app-chat.md §5`).

If `catalog()` fails or returns no `text-generation` block, the select falls back
to a single entry built from `list().loaded` (or, failing that, is left empty
with Send disabled) and the strip says the catalog could not be read. A fallback
is a last resort, not the default path (`assumptions.md §5`).

**Preload / Unload** live under the model select and say what they *would* do
before you press them:

- Preload → `fused.ai.models.load(id, {capability: "text-generation"})` →
  `{jobId}` → `fused.watchJob(jobId).watch(cb)`, drawing bytes, then re-runs
  step 1. Its caption names the size from the catalog, so a 8.1 GB press is a
  choice and not a surprise.
- Unload → `fused.ai.models.unload({capability: "text-generation"})`, **by
  capability** (`assumptions.md §5`), then re-runs step 1. The caption names what
  is actually resident, not what the dropdown shows, and is disabled when nothing
  is.

## 5. `generate()` — the one call site (shared)

Generation goes through **one** shell helper, so the cold-start dance, the lock,
and error routing exist once:

```js
generate({ prompt, history, onChunk, onLoading }) -> {text, model, usage, seconds}
```

1. Takes the lock (§8) or throws `busy` before doing anything else.
2. Builds `opts` from params: `model`, `systemPrompt` (omitted when `system` is
   empty), `temperature`, `topP`, `maxTokens`, plus the caller's `history` /
   `onChunk`. There is no `raw` argument — the surface that used it is gone, and
   an unused option that `assumptions.md §1` says conflicts with `history` is a
   trap, not a feature.
3. Calls `fused.ai`. On `model_loading`, calls `onLoading(record)` per watch tick
   and **retries exactly once** (`assumptions.md §2`).
4. Starts a clock before the call and stops it at the resolve, returning
   `seconds` alongside `usage` — because `usage.seconds` is missing on every
   stopped run (`assumptions.md §3`) and a tokens/second readout that blanks out
   exactly when a user interrupts is worse than none.
5. Releases the lock on **every** exit path.

Callers never touch `fused.ai` directly. A surface that needs different
behaviour changes this helper and says so in its spec.

## 6. The local-only gate

```js
const IS_LOCAL = fused.env === "local";
```

Every `fused.ai.*` call site checks it. The dotted calls export cleanly and then
fail at the reader, so nothing but this check stops them (`assumptions.md §6`).
When `IS_LOCAL` is false the page renders its chrome, disables every generate
control, and shows one banner explaining that local AI is not available on a
hosted copy. In practice this page cannot be exported at all (`fused.ai(` is a
textual match and every surface has one), so the gate is belt-and-braces — kept
because it is the correct habit and cheap, not because we expect to hit it.

## 7. Notices and errors

One `#notices` container above the surface cards. `notice(html, kind)` renders a
banner (`warn` default, `error` for failures) and replaces the previous one for
the same cause rather than stacking.

Failures are **typed and routed** (`assumptions.md §4`), never rethrown into the
red traceback overlay:

- `ai_unavailable` → error banner with `err.message`, Send disabled, strip re-read.
- `ai_error` / `bad_request` / `timeout` → error banner with `err.message`.
- `model_loading` → **never reaches here**; §5 handles it as progress.

No surface branches on `cancelled` or `unavailable` — neither can occur on the
text path, and a dead branch reads as a handled case that is silently not
(`assumptions.md §3`).

A `runPython` rejection (Library only) is caught and shown as a banner with
`err.type + ": " + err.message`; the traceback stays in the console.

## 8. One generation at a time

The shell serialises generation app-wide: `lock` is a single boolean, Send is
disabled while it is held, and Stop is the only live control. Two reasons, both
concrete: **one model is resident per capability**, so a second request queues
inside the worker rather than running beside the first; and `fused.ai` has **no
stale-request cancellation** (unlike `runPython`), so a double-click really does
send two generations that both complete.

With one surface there is one generation in flight and the lock is a single
boolean. Anything that fans out to several answers must hold it **across** the
whole run rather than per call — not because the API refuses concurrency, but
because a queue the user cannot see reads as a hang.

## 9. Theming

`<html data-fused-theme="shell">` — the runtime resolves the app's Light/Dark
setting and writes `data-theme` before our stylesheet parses. Two `:root` blocks
define **the same token set** and no colour literal appears anywhere else in the
stylesheet. There is no in-page theme switcher (it would silently lose to the app
setting). No colour is handed to JS, so there is nothing to redraw on a flip.

## Non-goals

- What the chat surface renders — `app-chat.md`.
- The `fused.ai` contract, cold start, cancel, model discovery, export rules —
  `assumptions.md`.
- Persisting transcripts to disk — `app-library.md` owns `./chats/` and `chats.py`.

## Open questions

- **Context window.** Nothing trims `history`, so a long conversation eventually
  exceeds the model's context and the worker's answer degrades before it errors.
  A turn-count or token-budget trim belongs in `app-chat.md §3` once we have
  measured where it actually bites on a 4-bit 8B; guessing a limit now would cut
  conversations that work fine today.

## See also

- `assumptions.md` — the bridge contracts this shell wires up.
- `app-chat.md`, `app-library.md` — the surfaces.
