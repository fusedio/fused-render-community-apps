# Contributing an app

1. Build your app locally in fused-render (any folder under
   `~/Documents/Fused/<tag>/<slug>/` works).
2. Make sure it is self-contained: no absolute local paths, no secrets or API
   keys, no dependence on files that only exist on your machine. Data your app
   needs should either ship in the folder (small) or be downloaded on first
   run by a `.py` helper (large).
3. Add the three required files next to your `index.html`:
   - `readme.md` — what the app does, how to use it, screenshots welcome
     (relative image links work).
   - `preview.png` — a preview image of the app.
   - `metadata.json` — see the schema below.
4. Fork this repo, copy your folder in at the repo root, open a PR.

CI validates every PR; a maintainer reviews and merges. On merge, the catalog
(`index.json`) regenerates and your app appears in everyone's marketplace on
their next refresh.

## Slug rules

The folder name is the slug: `^[a-z0-9][a-z0-9-]{1,63}$`. It is globally
unique (it's a directory name) and permanent — renaming a folder is a new app.

## metadata.json

```json
{
  "schema": 1,
  "name": "Trip Explorer",
  "description": "Browse and filter GPS trip parquet files on a map.",
  "author": { "name": "Ada L.", "github": "adal" },
  "tags": ["maps", "parquet"],
  "category": "geospatial",
  "version": "1.0.0",
  "min_fused_render": "0.4.0",
  "requires_python": true
}
```

| Field | Required | Notes |
|---|---|---|
| `schema` | yes | manifest format version; currently `1` |
| `name` | yes | display name, ≤ 60 chars |
| `description` | yes | one-liner for the card, ≤ 200 chars |
| `author.name` | yes | display name |
| `author.github` | no | GitHub username; should match the PR author |
| `tags` | no | ≤ 5 lowercase strings; drive the marketplace filter chips |
| `category` | yes | exactly one of `geospatial`, `productivity`, `starters`, `local-ai` — unlike tags, each app has a single category |
| `version` | yes | semver, bump on changes |
| `min_fused_render` | no | oldest fused-render version the app works on |
| `requires_python` | yes | `true` if the app calls `fused.runPython` |
| `ai_capabilities` | no | AI capability ids the app can drive with *any* model of that capability — see below |
| `ai_models` | no | exact model ids when the app only works with specific models — see below |
| `ai_model_param` | no | the URL query param the app reads its model from; required if either AI list is set |

`local-ai` is for apps built around fused-render's on-device model runtime —
they call `fused.ai(...)` / `fused.ai.image(...)` (text-generation or
text-to-image against a locally resident model) rather than a remote API.

Unknown keys are ignored (forward-lenient).

### AI metadata (`ai_capabilities`, `ai_models`, `ai_model_param`)

Declare these when your app accepts a model handoff — fused-render's AI
Playground lists matching apps under its stage and opens yours with the
selected model in your declared param.

- `ai_capabilities` — a list of catalog capability ids, exactly one of:
  `text-generation`, `text-to-image`, `automatic-speech-recognition`,
  `embeddings`. It claims your app works with *any* model of that capability.
- `ai_models` — a list of exact wire model ids (a Hugging Face repo id like
  `mlx-community/Llama-3.2-3B-Instruct-4bit`, or a bare GGUF filename — copy
  the id from the AI Models page). Narrower than `ai_capabilities` and it
  wins: a matching model shows your app as **Recommended**; an
  `ai_models`-only app is not listed for other models at all.
- `ai_model_param` — the query param your app reads the model id from, e.g.
  `"chatModel"`. It must **not** be literally `model` (the fused-render shell
  owns that query key and the two would stomp each other). Without a usable
  param your app is never listed — a handoff needs somewhere to land.

Example: [`local-chat/metadata.json`](local-chat/metadata.json) declares
`"ai_capabilities": ["text-generation"], "ai_model_param": "chatModel"`.

## What CI checks

- exactly one top-level `.html` in the folder, named `index.html`
- `readme.md`, `preview.png`, `metadata.json` present and schema-valid
- slug matches the pattern; no symlinks in the folder
- folder ≤ 20 MB, no single file > 10 MB
- no obvious absolute local paths in `.html`/`.py` (lint, not a security
  boundary — human review is the gate)

## Trust expectations

Installed apps run with the same trust as the user's own files, including
Python on their machine when `requires_python` is true. Don't submit apps that
phone home, collect data, or fetch and execute remote code without saying so
prominently in the readme. Maintainers will reject or remove apps that
surprise users.
