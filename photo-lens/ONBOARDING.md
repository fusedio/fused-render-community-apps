# Lens — local photo search

Search your photo library by describing what's in the pictures ("beach",
"sunset", "a person smiling"), plus trips, people (face clusters), and a
metadata browser. Everything runs on your own machine; nothing leaves it.

## Setup

There isn't any. Lens runs inside **fused-render** — no daemon to start, no
venv to build, no model to download by hand:

1. Run fused-render **0.4.45 or newer** — the release that added the
   `embeddings` AI capability. An older build reports "no engine that can run
   one" and `fused.ai.embed is not a function`.
2. Open `index.html` from this folder in the explorer (it carries the
   `<meta name="fused-app">` marker, so it also appears on the /apps hub).

That's it. Two one-time waits happen on their own, with progress shown:

- **First render** builds this folder's Python environment (~40s warm).
- **First search** downloads the SigLIP 2 search model (~4.6GB) into
  fused-render's model runtime and keeps it there. Every later search embeds
  your words in milliseconds.

## Pointing it at your photos

Use the ⚙ menu in the app to add photo folders, then press ↻ to index.
Indexing runs as a background job — it shows up in fused-render's download
manager (bottom right) with live progress and a working cancel, and it keeps
running if you navigate away. Cancelling keeps partial progress; the next run
resumes where it stopped.

## The other two pages

- `views/explain.html` — how the search actually works, with a live demo.
- `views/validate.html` — an audit that scores the index's health (~20s).

## How it works now (one paragraph)

Lens used to ship its own long-lived daemon holding the model and every photo
vector in RAM. That's gone. The model lives in fused-render's AI runtime
(`fused.ai.embed`, one warm worker per machine, shared by every app); the
vectors and catalog live on disk (`~/.fused-render/cache/lens/`) and are read
by short per-request Python calls. Same vectors, same model
(`google/siglip2-so400m-patch14-384`, 1152-dim) — an index built under the
daemon keeps working unchanged, verified at cosine 1.000000 on re-embedding.

## macOS permissions

Indexing reads your photo folders, so the app that hosts fused-render
(Terminal, iTerm, or FusedRender.app) needs **Full Disk Access** or at least
Photos-folder access under System Settings → Privacy & Security if your
library lives somewhere protected.
