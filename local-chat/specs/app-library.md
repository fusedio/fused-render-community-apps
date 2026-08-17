# Library

> **Status — target.** Defers to `app.md` for the shell, params, notices and
> theming. This file owns the **on-disk record**: the `./chats/` directory, the
> transcript file format, `chats.py`, and the list / search / open / delete UI.
> The only surface backed by Python. Implementing modules: `chats.py` (`main`),
> `index.html` (`libraryLoad`, `saveChat`, `openChat`, `deleteChat`). Assumes
> `assumptions.md`.

## 1. Why Python at all

Chat itself needs no filesystem. This part needs to **list** a
directory, and the `fused` bridge has `readFile`/`writeFile`/`stat` but no
`listDir` — so the listing is the one thing that must be Python. Having a `.py`,
it also does the searching and the deleting, because a search implemented in JS
over N files means N `readFile` round trips for something `main()` does in one
pass.

Writes go the other way: **saving is `fused.writeFile`, not Python.** The page
already holds the transcript, `writeFile` is atomic and returns a fresh stat, and
routing it through a subprocess would add a second serializer that could disagree
with the reader (`assumptions.md §7` — a `.py` receives strings, and a transcript
is not a param).

## 2. The file format

One file per conversation: `./chats/<YYYYmmdd-HHMMSS>-<slug>.json`, where `slug`
is the first user turn lowercased, non-alphanumerics collapsed to `-`, truncated
to 40 chars. Timestamp first so the directory sorts chronologically in any
listing that has never heard of this app.

```json
{
  "version": 1,
  "saved": "2026-08-16T20:14:03",
  "model": "mlx-community/Qwen3-8B-4bit",
  "system": "",
  "settings": { "temperature": 0.7, "top_p": 0.95, "max_tokens": 1024 },
  "turns": [
    { "role": "user", "content": "…" },
    { "role": "assistant", "content": "…",
      "meta": { "model": "mlx-community/Qwen3-8B-4bit", "tokens": 412,
                "seconds": 9.31, "stopped": false } }
  ]
}
```

- **`turns` is `convo` unchanged** (`app-chat.md §3`) — same `role` vocabulary as
  the wire, so reopening a transcript needs no mapping and `history` can be
  sliced straight out of it.
- **`content` is the full text, thinking blocks included** (`app-chat.md §6`).
  The renderer splits; the record does not.
- `model` at the top is the id that answered the **last** turn; per-turn `meta.model`
  is authoritative when they differ (the resident model can change mid-conversation).
- `version` exists so a future format change can be detected rather than guessed
  at. A file whose `version` is unknown is listed, marked, and not opened.

`chats.py` `main()` — annotate every parameter, return JSON-native values only:

```python
def main(action: str = "list", name: str = "", find: str = "", limit: int = 200):
```

| `action` | Returns |
|---|---|
| `list` | `{chats: [{name, saved, model, turns, first, preview, bytes, ok}], total, dir}` — newest first, `limit` applied after filtering. `first` is the first user turn (one line, truncated); `preview` is the last assistant turn similarly. `ok` is false for a file that will not parse or carries an unknown `version`, and such a file is **listed with its name and an error, never skipped** — a search that silently drops a corrupt file is how you lose a conversation without noticing. |
| `delete` | `{deleted: name}`. Refuses any `name` containing a path separator or `..`, and any name not ending `.json`, before touching the disk. |

`find` matches case-insensitively against the whole of every turn's content plus
the filename, so searching for a phrase you remember saying works. Matching
happens in Python over the parsed files; the page never downloads the corpus.

The directory is created on demand by the **saver**, not by `main()` — a `list`
against a missing directory returns `{chats: [], total: 0}`, which is the true
answer, rather than creating a directory as a side effect of reading.

## 3. The list UI

It lives in the **aside**, under Model and Sampling, beside the conversation it
opens rather than behind a tab that would hide the chat to show it.

- A search box bound to `find` (`app.md §2`), debounced 200 ms, and a count line
  (`12 conversations · 340 KB`).
- One row per transcript: first user turn as the title (one line, ellipsised —
  the column is 268 px), turn count and save date as the subtitle. No preview
  line: at this width it would be three words of a sentence.
- **The row itself opens it** (§4); the open one is marked `aria-current` and
  keeps its accent border. **Delete** is a button inside the row, revealed on
  hover or when the row is the open one, confirming inline (never a
  `window.confirm`) and stopping the click from also opening the row.
- Empty states are distinguished, because they mean different things: *nothing
  saved yet* (naming Save as the way in) vs *no match for "…"* (with a
  clear-search action).
- A `runPython` rejection renders as a banner (`app.md §7`), not the red overlay.
- The list is **not** rebuilt on every `draw()` — see `app.md §2` for the
  `shownFind` guard and who rebuilds it instead.

## 4. Save and Open — the round trip

**Save** (the button lives in Chat, `app-chat.md §8`):

1. Build the record (§2) from `convo` and the current params.
2. `fused.writeFile("./chats/<name>.json", json, {create: true})`. `create`
   rather than a stat-then-write: an existing name rejects `.type === "exists"`
   and nothing is clobbered, with no race in between.
3. On success, set the `chat` param to the filename and confirm inline. On
   `exists`, append `-2`, `-3`… and retry — two conversations started in the same
   second with the same opening line is a real case, and failing the save over it
   would be absurd.
4. Re-saving an **already-saved** conversation (the `chat` param is set and the
   file exists) writes the same name with `expectedMtime` from the last write, so
   a conflict is reported rather than a second copy created.

**Open** writes `chat=<name>` and nothing else, and `draw()` does the rest: when
`chat` names a file that is not already loaded, it reads it, replaces `convo`,
and restores `model`, `system` and the sampling params from the record — a
transcript is only reproducible with the settings that produced it. A `chat`
param naming a missing or unparseable file clears itself and shows a banner; it
must not leave the URL pointing at a conversation that is not on screen.

## 5. The autosave — `chats/.session.json`

Save is deliberate, and it always was; the cost was that a reload of an unsaved
conversation lost it, which made "will I want this later?" a question to answer
*before* the conversation rather than after it. So the open transcript is
mirrored to `chats/.session.json` — the **same §2 record**, written by the same
`record()`, so there is exactly one serializer and a session file is a saved
chat that happens to have no name.

- **Written after every finished turn** — answered, stopped, or failed alike —
  and when New chat empties the pane, so a cleared conversation stays cleared
  across a reload.
- **Not written while a conversation from the library is open.** That one is
  restored by the `chat` param already, and a second file claiming to be the
  same conversation is how the two come to disagree.
- **Read on boot only when the URL names no `chat`**, so an explicit link to a
  saved transcript always wins over the last thing that happened to be on
  screen. A missing file is the ordinary first run and says nothing; a file that
  exists and will not parse gets a notice, because that is a conversation that
  was there a moment ago.
- **The library does not list it.** `chats.py` skips dotfiles (`_list`), and its
  delete guard already refuses them — so the autosave cannot be opened as a row,
  deleted as a row, or counted in the "N conversations" line. Save is still the
  only thing that puts a conversation in the library, under a name, with the
  turn count and preview that make it findable later.

## Non-goals

- The transcript's rendering — `app-chat.md §6` owns thinking blocks and bubbles;
  this file owns only what is on disk.

## Open questions

- **Anything that is not a conversation.** §2's shape is a `turns` array, and
  only a conversation fits it. If some future comparison view earns persistence,
  it earns its own `version` and its own list section — not a `turns` array
  pretending to be one.

## See also

- `app.md` — the shell, params, notices.
- `app-chat.md` — where a transcript comes from and where it goes back to.
- `assumptions.md §7` — param typing and the `main()` coercion rules.
