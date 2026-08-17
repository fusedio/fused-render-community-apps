"""Saved conversations on disk — the Library surface's data file.

Owns listing, searching and deleting `./chats/*.json`; see
`specs/app-library.md`. Writing is deliberately NOT here: the page holds the
transcript and `fused.writeFile` is atomic, so routing a save through a
subprocess would only add a second serializer that could disagree with this
reader.
"""

import json
import os

CHATS_DIR = "chats"
FORMAT_VERSION = 1


def main(action: str = "list", name: str = "", find: str = "", limit: int = 200):
    if action == "list":
        return _list(find, limit)
    if action == "delete":
        return _delete(name)
    raise ValueError(f"unknown action {action!r} — expected 'list' or 'delete'")


def _list(find: str, limit: int):
    """Newest first. A file that will not parse is LISTED with its error, never
    skipped — a search that silently drops a corrupt file is how you lose a
    conversation without noticing."""
    if not os.path.isdir(CHATS_DIR):
        # The true answer. Creating the directory as a side effect of reading it
        # would be a write nobody asked for.
        return {"chats": [], "total": 0, "bytes": 0, "dir": os.path.abspath(CHATS_DIR)}

    needle = find.strip().lower()
    rows = []
    for filename in os.listdir(CHATS_DIR):
        if not filename.endswith(".json"):
            continue
        # A dotfile in here is the page's own bookkeeping — `.session.json`, the
        # autosave of the transcript currently open (`specs/app-library.md` §5).
        # It is not a saved conversation and listing it would offer the reader a
        # duplicate row of the chat they are already looking at.
        if filename.startswith("."):
            continue
        row = _read(filename, needle)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: r["saved"] or r["name"], reverse=True)
    return {
        "chats": rows[:limit],
        "total": len(rows),
        "bytes": sum(r["bytes"] for r in rows),
        "dir": os.path.abspath(CHATS_DIR),
    }


def _read(filename: str, needle: str):
    """One row, or None when `needle` does not match this file."""
    path = os.path.join(CHATS_DIR, filename)
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict):
            raise ValueError("the file is not a conversation object")
    except (OSError, ValueError) as err:
        # Still searchable by name, so a corrupt file cannot hide behind a query.
        if needle and needle not in filename.lower():
            return None
        return _row(filename, 0, error=str(err))

    version = record.get("version")
    if version != FORMAT_VERSION:
        if needle and needle not in filename.lower():
            return None
        return _row(filename, size, error=f"unknown format version {version!r}")

    turns = record.get("turns") or []
    if needle:
        haystack = filename.lower() + "\n" + "\n".join(
            str(turn.get("content", "")) for turn in turns if isinstance(turn, dict)
        ).lower()
        if needle not in haystack:
            return None

    return _row(
        filename,
        size,
        saved=str(record.get("saved") or ""),
        model=str(record.get("model") or ""),
        turns=len(turns),
        first=_line(_role_text(turns, "user", first=True)),
        preview=_line(_role_text(turns, "assistant", first=False)),
    )


def _row(name: str, size: int, saved: str = "", model: str = "", turns: int = 0,
         first: str = "", preview: str = "", error: str = ""):
    return {
        "name": name,
        "saved": saved,
        "model": model,
        "turns": turns,
        "first": first,
        "preview": preview,
        "bytes": size,
        "ok": not error,
        "error": error,
    }


def _role_text(turns, role: str, first: bool) -> str:
    matches = [
        str(turn.get("content", ""))
        for turn in turns
        if isinstance(turn, dict) and turn.get("role") == role
    ]
    if not matches:
        return ""
    return matches[0] if first else matches[-1]


def _line(text: str, width: int = 140) -> str:
    """One line, collapsed and truncated — the list shows a hint, not a turn.

    Thinking is dropped from the HINT only; the stored turn keeps it (see
    `specs/app-chat.md` §6 — splitting is a rendering decision). Without this
    every preview from a reasoning model is the first sentence of its
    monologue, which is the same sentence for every conversation.
    """
    flat = " ".join(_without_thinking(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _without_thinking(text: str) -> str:
    """Mirrors `splitThinking` in index.html — including the close-only case.

    Some runners never emit the opening tag: the reasoning simply IS the start
    of the stream and only its end is marked. A preview that handled `<think>`
    but not a bare `</think>` still led with the monologue on exactly the models
    this demo ships with.
    """
    while True:
        open_at = text.find("<think>")
        if open_at == -1:
            close_only = text.find("</think>")
            return text if close_only == -1 else text[close_only + 8:].lstrip()
        close_at = text.find("</think>", open_at)
        if close_at == -1:
            # Unclosed: the turn ran out mid-thought, so there is no answer left.
            return text[:open_at]
        text = text[:open_at] + text[close_at + 8:]


def _delete(name: str):
    """Refuses anything that is not a plain .json filename in ./chats/, before
    touching the disk — the name arrives from a URL param."""
    if not name:
        raise ValueError("delete needs a 'name'")
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        raise ValueError(f"{name!r} is not a plain filename")
    if name in (".", "..") or name.startswith(".") or not name.endswith(".json"):
        raise ValueError(f"{name!r} is not a saved conversation")

    path = os.path.join(CHATS_DIR, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{name} is not in {os.path.abspath(CHATS_DIR)}")
    os.remove(path)
    return {"deleted": name}
