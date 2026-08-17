# Spec registry — local-text-chatbot

The capability index for this app. Every spec earns exactly one bullet here,
ending in its owning filename. To find the owner of a concept, read this list
first — never grep the code.

## Capabilities

- **Assumptions** — the `fused` bridge facts and machine facts every spec in this
  folder takes as given: the `fused.ai` text contract on the **local** path, the
  cold-start `model_loading` dance, cancel semantics, catalog/runtime shape,
  export rules, param typing (`assumptions.md`).
- **App shell** — the hub: the single entry page, the params that are the app's
  settings state, the shared runtime/model strip, the generation lock, the notice
  surface, and theming (`app.md`).
- **Chat** — the app's one surface, streamed token by token: the transcript,
  multi-turn `history`, the system prompt, sampling controls, Stop, thinking
  blocks, the markdown pass, and the tokens-per-second readout that is the whole
  point of a local demo (`app-chat.md`).
- **Library** — the on-disk record: `./chats/` JSON transcripts, listed and
  searched in the aside, reopened in place, deleted. The only part of the app
  backed by Python (`app-library.md`).

## Reading order

`assumptions.md` → `app.md` → whichever surface spec you are changing.
A surface spec never restates the hub; it opens by deferring to it.

## Removed surfaces

**Sweep** (one prompt, one sampling axis, N answers side by side) and **Raw**
(the same text sent chat-templated and with `raw: true`) were built, then cut
when the app became a single chat page — with them went the tab bar, the
`q` / `axis` / `n` / `from` / `to` / `text` params, and `generate()`'s `raw` and
`override` arguments. Their specs are in git history (`app-sweep.md`,
`app-raw.md`), and `assumptions.md §1`'s note that `raw` and `history` are
mutually exclusive is the fact to re-read before bringing either back.

## Sibling app

`../text-to-image` is the image-generation demo this app is deliberately shaped
after — same spec layout, same shell skeleton, same "always ask the catalog"
discipline. Where a rule is identical it is written the same way on purpose, so
someone who has read one folder can read the other. Where the **text** path
genuinely differs from the image path, the difference is called out inline
rather than smoothed over — `assumptions.md §2` (cold start), `§3` (cancel), and
`§1` (the `usage` shape) are all such places, and each is a real trap.
