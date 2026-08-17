# Gallery

> **Status — target.** Defers to `app.md` for the shell. This file owns
> **`./gallery/` and everything that touches it**: the on-disk layout, the save /
> list / delete operations, and the browse surface. It is the only surface backed
> by Python. Implementing modules: `gallery.py` (`main`, actions `save` | `list` |
> `delete`), `index.html` (`saveToGallery`, `loadGallery`, `drawGallery`).

## 1. Why Python at all

`fused.ai.image` writes its PNG to `<home>/ai/images/`, outside this app
(`assumptions.md §1`). Keeping a per-app record means **copying bytes**, and
`fused.writeFile` is UTF-8 text only — so the copy is a `runPython` call. That is
the honest reason this file exists, and it is also what makes the demo show the
`runPython` bridge alongside the AI bridge.

## 2. On-disk layout and `gallery.py`

`./gallery/` sits beside `index.html` and is **gitignored** — it is user output,
not source. Each render is two files sharing a stem:

```
gallery/20260816-142233-a1b2c3.png     the copied bytes
gallery/20260816-142233-a1b2c3.json    the sidecar
```

The stem is the source PNG's own basename, so a saved image is traceable to the
render that produced it and two saves of the same render collide harmlessly rather
than duplicating.

**Sidecar schema** — the settled reply, verbatim, plus when we saved it:

| Field | Source |
|---|---|
| `prompt`, `model`, `width`, `height`, `steps`, `guidance`, `seed` | the resolved `fused.ai.image` object (`assumptions.md §1`) |
| `source_path` | `res.path`, the original under `<home>/ai/images/` |
| `saved_at` | ISO-8601 local timestamp, written by Python |
| `surface` | `studio` \| `sweep` \| `lab` — which tab produced it |

Storing the **reply** rather than the request is the same rule as the Studio meta
row: a sidecar claiming `steps: 500` for a render clamped to 100 is a lie that
outlives the session.

`main(action: str = "list", ...)` — one entry point, three actions, all
JSON-native returns:

- **`save`** — takes the sidecar fields plus `src`. Creates `./gallery/` if absent,
  copies `src` (`shutil.copyfile`), writes the sidecar, returns the record. A
  missing or unreadable `src` returns `{"error": ...}` rather than raising, so the
  page can show a banner without the traceback overlay — the image still exists at
  `source_path` and losing the copy must not look like losing the render.
- **`list`** — globs `./gallery/*.json`, parses each, attaches `name`, `png`
  (basename) and file `size`, **skips** any sidecar that fails to parse or whose PNG
  is missing (reporting the count as `skipped`), and returns newest-first by
  `saved_at`. A corrupt sidecar must not blank the gallery.
- **`delete`** — takes `name` (the stem). **Rejects any name containing a path
  separator or `..`** and resolves the target inside `./gallery/` before unlinking,
  so a param cannot reach outside the folder. Removes both files; a missing file is
  not an error.

`main` is annotated on every parameter (`width: int`, `guidance: float`, …) —
params arrive as strings and annotations drive the coercion (`assumptions.md §6`).
The module imports only stdlib (`os`, `json`, `glob`, `shutil`, `datetime`), so it
runs on the bundled interpreter with no `pyproject.toml` and no first-run install.

## 3. The browse surface

- **Search** (`q`) filters client-side on the sidecar `prompt`, case-insensitively,
  with matches highlighted. Filtering in JS keeps typing instant; the list is at
  most a few hundred records.
- **Grid** of thumbnails via `fused.rawUrl(path)` — never `readFile`; these are
  bytes for the browser to fetch, not text to process. Each tile shows a truncated
  prompt and the seed. `loading="lazy"` on every `<img>`.
- **Detail** — clicking a tile expands it: full image, the complete sidecar as a
  labelled table, and three actions: **Open in Studio** (writes `prompt`, `w`, `h`,
  `steps`, `guidance`, `seed`, `model` into params and sets `tab=studio` — the same
  param handoff `app-sweep.md §4` uses), **Copy prompt**, and **Delete**.
- **Delete confirms first** and, on success, re-lists rather than mutating the local
  array — the disk is the source of truth, and a page whose list disagrees with the
  folder is worse than one extra call.
- **Empty state** distinguishes *no gallery yet* ("renders you save will appear
  here") from *no matches for this search*. They are different facts and one message
  for both sends people looking for a bug.

`loadGallery()` runs on entering the tab and after any save or delete. Its
rejection is caught and shown as a banner (`app.md §6`).

## Non-goals

- Generating anything — Gallery only reads and deletes what other surfaces made.
- Editing a sidecar. It records what happened; a mutable record of a past render is
  a different feature with no demo value.
- Managing `<home>/ai/images/`. That folder is the runtime's, and reaching into it
  to delete would be this app deleting another app's files.

## See also

- `app-studio.md §5` — the auto-save that feeds this folder.
- `assumptions.md §1` — why the PNG needs copying at all.
