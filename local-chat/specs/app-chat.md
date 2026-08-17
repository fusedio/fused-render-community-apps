# Chat

> **Status — target.** Defers to `app.md` for the shell, params, the runtime
> strip, `generate()`, the lock, notices and theming. This file owns the **chat
> surface**: the transcript, multi-turn `history`, the composer, streaming into
> the DOM, Stop, thinking blocks, and the tokens-per-second meter. Implementing
> modules: `index.html` (`convo`, `send`, `stop`, `paintConvo`, `bubble`,
> `splitThinking`, `meta`). Assumes `assumptions.md`.

## 1. What this surface is for

It is the answer to "can a model running entirely on this laptop hold a
conversation, and how fast?" Everything on screen serves one of those two
questions: the transcript answers the first, the meta line under each answer
answers the second, and nothing else is on screen.

## 2. Controls (the shared aside)

Chat adds no controls of its own — it uses the shell's model group plus the
sampling group:

| Control | Param | Range | Note |
|---|---|---|---|
| System prompt | `system` | free text | Empty means **omit `systemPrompt` entirely** so the worker's own default applies. Sending the default string back would be a no-op the server has to detect (`assumptions.md §1`); omitting it is honest. |
| Temperature | `temp` | 0–2, step 0.05 | |
| Top-p | `topp` | 0–1, step 0.05 | |
| Max tokens | `maxtok` | 1–32768 | The length cap **and** the worst-case wait. |
| Collapse thinking | `think` | `0`/`1` | §6. |
| Mode | `mode` | `chat`/`raw` | §9. In the top bar beside the model, not in the drawer — it changes what Send does. |

A change to any of them applies to the **next** turn and does not touch the
turns already on screen — a transcript is a record of what was said, not a live
re-render of it. The composer says so once, quietly, when a setting changes
mid-conversation.

## 3. The conversation, and how it reaches the model

`convo` is an array of `{role, content, meta?}` — `role` ∈ `user` | `assistant`,
matching the wire roles exactly (`assumptions.md §1`), so no mapping is needed
on the way out and a saved transcript reloads without one either.

Sending turn N:

1. Push `{role: "user", content: text}` and paint it.
2. Call `generate({ prompt: text, history: convo.slice(0, -1), onChunk })` —
   **`prompt` is the turn being asked now and `history` is everything before it.**
   Putting the new turn in both is the classic double-send bug; the slice is
   what prevents it.
3. Push `{role: "assistant", content: ""}` and stream into it.

`history` carries only `role` and `content`; `meta` is ours and the server
rejects nothing for it being absent. A `system` turn is **never** put in
`history` — the API takes it as `systemPrompt` and validates `history` roles
against `user`/`assistant` only, so a system turn there is a `bad_request`
naming the index.

## 4. Streaming, and Stop

The composer's Send is disabled the moment a turn starts (the shell lock,
`app.md §8`) and Stop takes its place.

- `onChunk(text)` appends to the open assistant bubble and scrolls the
  transcript **only if the user was already at the bottom** — yanking someone
  back down while they are re-reading an earlier answer is the one interaction
  bug a streaming UI reliably ships.
- The promise resolves with the **complete** text regardless of streaming, so
  the bubble is set from `res.text` at the end rather than trusted to the
  accumulated chunks (`assumptions.md §1`).

**Stop calls `fused.ai.cancel()` — argless, because the default capability is
`text-generation`** (`assumptions.md §3`) — and then does **nothing else**. It
does not clear the bubble, does not remove the turn, and does not show an error,
because the call it interrupted *resolves normally with the partial text*. The
partial answer stays in `convo` and is a legitimate turn: the model can be asked
to continue from it.

A stopped turn is marked in its meta line (`stopped after 212 tokens`), which is
the only place cancellation is visible. There is no `cancelled` catch branch
anywhere in this surface, and `assumptions.md §3` is why.

**A turn that streamed and then FAILED is kept too**, for the same reason a
stopped one is: the words arrived, the reader watched them arrive, and the only
copy is the bubble they are in. The turn stays with a meta line reading
`incomplete — the generation failed`, and the error goes to a notice as usual.
Its meta carries no tokens and no seconds — the call rejected, so the server's
counts never came back and inventing them would put a made-up tok/s figure under
a broken answer. Only a turn where **nothing** streamed is removed, because
there an empty bubble under an error message is noise, and dropping it leaves
the question in place for Send to retry without retyping.

## 5. The meta line — the point of the demo

Under every assistant turn, one small line:

```
Qwen3 8B (4-bit) · 412 tokens · 9.3 s · 44 tok/s
```

- **Model** — from `res.model`, the id that actually answered, mapped to its
  catalog label. Never the dropdown's value (`assumptions.md §1`).
- **Tokens** — `res.usage.output_tokens`. **Not `input_tokens`**, which does not
  exist on this path and would render as `undefined tokens`.
- **Seconds** — the shell's own clock (`app.md §5`), not `usage.seconds`, so the
  number is present on stopped runs too. When the server did report `seconds`
  the two agree to within the round trip; when it did not, ours is the only one.
- **tok/s** — `output_tokens / seconds`, one decimal. Omitted rather than shown
  as `∞` or `NaN` when tokens is 0 or seconds is under 0.05.

The first turn after a cold start also carries `after a 4.6 GB load` — the load
time is deliberately **excluded** from the tok/s figure, because a download is
not generation and averaging it in makes a fast model look slow forever.

## 6. Thinking blocks

Reasoning models in the catalog (Qwen3 especially) emit `<think>…</think>`
before the answer, and on a chat surface that renders as a wall of monologue
where an answer should be.

`splitThinking(text)` splits the raw completion into `{thinking, answer}`. Four
cases, and the third is the one measured against a real run rather than assumed:

| Input | thinking | answer |
|---|---|---|
| no tags | `""` | the whole text |
| `<think>x</think>y` | `x` | `y` (plus anything before the open tag) |
| **`x</think>y` — close only** | `x` | `y` |
| `<think>x` — unclosed | `x` | `""` so far |

**The close-only case is real, not defensive.** Some runners never emit the
opening tag: the reasoning *is* the start of the stream and only its end is
marked. Until `</think>` arrives there is no way to tell that apart from a model
that does no thinking at all, so it renders as a plain answer and then
re-splits when the close lands. `chats.py`'s `_without_thinking` mirrors all
four cases — a library preview that handled only `<think>` still led with the
monologue on exactly the models this demo ships with.

- With `think=1` (default) the thinking is a collapsed `<details>` labelled with
  its own token-ish length; with `think=0` it renders inline, dimmed. While the
  thinking is still streaming the block carries `.live` and pulses, so a model
  that spends thirty seconds reasoning before its first answer token looks busy
  rather than stuck.
- **The full text — thinking included — is what is saved to disk**, so a
  reopened transcript still folds open to show how the answer was reached.
- **An assistant turn goes back in `history` WITHOUT its thinking.** Qwen3's own
  template guidance is to drop prior reasoning from a multi-turn history, and
  the arithmetic agrees: a reasoning model routinely spends more tokens thinking
  than answering, so a kept monologue is most of the prompt by turn five and the
  context window fills with workings the model has already finished with.
  A turn that is *all* unclosed thinking has no answer to send, and sends its
  raw content instead — an empty `content` is a `bad_request`, not a saving.
- A model that emits no `<think>` is the ordinary case and produces no
  `<details>` at all.

## 7. Markdown

Chat models emit markdown whether or not anyone asked them to, and a demo that
shows the asterisks is a demo of the page, not of the model. Answers and
thinking both render through a small in-page markdown pass (`mdHtml`, `esc`, and the
`.md` stylesheet in `index.html`) covering what a chat model actually produces:
headings, lists, blockquotes, rules, links, inline code, fenced code with a
language label, and tables.

Three constraints on it, each load-bearing:

- **No library.** There is no network at runtime, and a bundled parser is a
  dependency this page does not otherwise have.
- **It runs on every streamed chunk**, so it stays one pass over the lines. A
  quadratic re-parse would show up as stutter at exactly the point the demo is
  meant to look fast.
- **Everything is escaped first** (`esc`). The text is model output going into
  `innerHTML`; a model that emits a `<script>` tag — or a saved transcript that
  contains one — must render as characters, never as markup.

The **stored turn keeps the raw markdown** (§6's rule, same reason): rendering
is a display decision, and `history` must carry back exactly what the model
produced.

## 8. Clear and Save

Two buttons above the composer:

- **Clear** empties `convo`, clears the `chat` param, and repaints. It does not
  touch the model, the settings, or anything on disk.
- **Save** writes the transcript to `./chats/` and is specified by
  `app-library.md §4` — including what happens to the `chat` param, which is how
  a saved conversation survives the refresh that `app.md §2` says loses an
  unsaved one.

An empty `convo` renders an empty state naming the resident-or-first-load model
and one example question, not a blank panel.

## 9. Completion mode

A chat template is a wrapper, and the wrapper is normally invisible: you type a
question, the model answers as an assistant, and nothing on screen admits that a
`<|im_start|>` was ever involved. `mode=raw` takes the wrapper off. The text is
handed to the model as-is and what comes back is a **continuation** of it — the
next paragraph of a story, the rest of a function, the completion of a sentence.
It is the closest a page can get to showing what the weights actually do, which
is why a local-model demo is the right place for it.

- `generate()` sets `raw: true` and sends **neither `history` nor
  `systemPrompt`**. Not as an optimisation: `raw` and `history` are mutually
  exclusive at the bridge (a 400, not a silent preference), and a system prompt
  has no template slot to occupy. Sampling (`temp`, `topp`, `maxtok`) still
  applies and is still the only thing that does.
- **Both controls that stop applying say so on screen** — the system prompt is
  disabled and reads *not sent in completion mode*, and the composer asks for
  *text for the model to continue* rather than a message. A setting you can
  watch have no effect is the failure this page is built to avoid, and the
  bridge takes the same line by refusing rather than dropping options.
- **The transcript still renders as turns**, because there is one rendering path
  and a continuation is still a pair of *what went in* and *what came out*. The
  assistant's `who` line reads `Qwen3 8B (4-bit) · continuing your text` and the
  meta line carries `raw completion`, so a mixed transcript — a conversation
  that was switched to raw halfway — says per turn how each one was produced.
  **Earlier turns stay on screen and are not sent**, which those two marks are
  what make legible.
- `mode` is stored in a saved record's `settings` and restored on Open, like the
  sampling params — a transcript is only reproducible with the settings that
  produced it. A record written before this existed has no `mode` and is read as
  `chat`, which is what it was.

## Non-goals

- Sampling semantics and ranges — `assumptions.md §1`.
- The cold-start progress UI — `app.md §5` owns it; this surface only supplies
  the `onLoading` callback that draws it into the composer area.
- Saving, listing and reopening transcripts — `app-library.md`.

## See also

- `app.md` — the shell, `generate()`, the lock.
- `app-library.md` — where a conversation goes when it is worth keeping.
