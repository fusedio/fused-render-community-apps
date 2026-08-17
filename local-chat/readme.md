# Local Chat

![Local Chat — a dark-themed chat UI with a model picker, settings panel, and a saved-chat sidebar](preview.png)

**Chat with a language model that never leaves your machine.** Pick a model
from the catalog (or anything you've already loaded from the AI Models page),
load it, and talk — no network round-trip after the weights are on disk.

- **Streamed replies**, token by token, with a tokens-per-second readout — the
  whole point of running a model locally is seeing how fast it actually is.
- **Multi-turn conversation** with an editable system prompt and sampling
  controls (temperature, max tokens), all persisted in the URL so a link
  restores the exact setup.
- **Reasoning models** get their `<think>…</think>` narration folded into a
  collapsed block above the answer, so the reply itself stays readable.
- **Stop** cancels a generation mid-stream without unloading the model.
- Replies are rendered as markdown — code blocks, lists, and tables land the
  way the model meant them.

## Chat library

Every conversation is written to `./chats/` as JSON by `chats.py` (stdlib
only, no dependencies). The sidebar lists them, searches across them, reopens
one in place, and deletes the ones you're done with — so closing the tab
doesn't lose yesterday's thread.

## How the model is managed

The model is held resident by fused-render, not by this app: loading,
download progress, and unloading all go through the same `fused.ai` runtime
the AI Models page uses. Ask the catalog, don't hardcode — if you've already
loaded a model elsewhere, this app picks it up.
