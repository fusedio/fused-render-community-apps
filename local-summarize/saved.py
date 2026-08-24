"""Saved summaries on disk — the sidebar's data file.

Owns resolving, listing, searching and deleting the app's summary folder,
`<fused-render home>/cache/<app slug>/*.md` (see `_root`). Each file is plain
markdown with a small `---` header the page writes, so it opens and reads
correctly in any markdown viewer (fused-render's included) with this app
nowhere in the picture. That is the point of the format: the summary is the
artefact, and it must outlive the app that produced it.

Writing is the page's job (`fused.writeFile` is atomic); a second serializer
here could only disagree with this reader.
"""

import os


def _root(create: bool = False) -> str:
    """`<fused-render home>/cache/<app slug>/` — where this app's summaries live.

    NOT a folder inside the app: an installed app folder can be read-only, and
    `fused.writeFile` does not create missing parents, so the first Save into a
    `./summaries/` that had never been created failed with "parent directory does
    not exist". The cache directory is the machine's own place for this, already
    laid out one-subdirectory-per-app.

    `FUSED_RENDER_HOME_DIR` is exported into every `runPython` subprocess, so the
    location is read from the app rather than guessed; the expanduser fallback is
    for running this file by hand. The slug is the name of the folder holding this
    script, which CONTRIBUTING.md makes the app's permanent identity — so a copy
    of the app under a new name gets its own drawer without an edit here.
    """
    home = os.environ.get("FUSED_RENDER_HOME_DIR") or os.path.expanduser("~/.fused-render")
    slug = os.path.basename(os.path.dirname(os.path.abspath(__file__))) or "local-summarize"
    path = os.path.join(home, "cache", slug)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def main(action: str = "list", name: str = "", find: str = "", limit: int = 200):
    if action == "dir":
        # The one action that CREATES. The page calls it before its first write,
        # because `fused.writeFile` will not make a missing parent itself.
        return {"dir": _root(create=True)}
    if action == "list":
        return _list(find, limit)
    if action == "read":
        return _read_one(name)
    if action == "delete":
        return _delete(name)
    raise ValueError(
        f"unknown action {action!r} — expected 'dir', 'list', 'read' or 'delete'")


def _list(find: str, limit: int):
    """Newest first. A file that will not parse is LISTED with its error, never
    skipped — a search that silently drops an unreadable file is how you lose a
    summary without noticing."""
    root = _root()
    if not os.path.isdir(root):
        # The true answer. Creating the directory as a side effect of READING it
        # would be a write nobody asked for — `dir` is the action that creates.
        return {"summaries": [], "total": 0, "bytes": 0, "dir": root}

    needle = find.strip().lower()
    rows = []
    for filename in sorted(os.listdir(root)):
        if not filename.endswith(".md"):
            continue
        # A dotfile in here is the page's own bookkeeping — `.session.json`, the
        # autosave of whatever is open right now. Listing it would offer the
        # reader a duplicate row of the summary they are already looking at.
        if filename.startswith("."):
            continue
        row = _row_for(filename, needle)
        if row is not None:
            rows.append(row)

    rows.sort(key=lambda r: (r["saved"] or "", r["name"]), reverse=True)
    return {
        "summaries": rows[:limit],
        "total": len(rows),
        "bytes": sum(r["bytes"] for r in rows),
        "dir": root,
    }


def _row_for(filename: str, needle: str):
    path = os.path.join(_root(), filename)
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as err:
        # Still searchable by name, so an unreadable file cannot hide behind a
        # query the reader typed.
        if needle and needle not in filename.lower():
            return None
        return _row(filename, 0, error=str(err))

    head, body = _split_header(text)
    if needle and needle not in (filename.lower() + "\n" + text.lower()):
        return None

    return _row(
        filename, size,
        saved=head.get("saved", ""),
        model=head.get("model", ""),
        length=head.get("length", ""),
        fmt=head.get("format", ""),
        source=head.get("source", ""),
        sections=head.get("sections", ""),
        words=head.get("summary_words", ""),
        preview=_line(body),
    )


def _read_one(name: str):
    path = _safe_path(name)
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    head, body = _split_header(text)
    return {"name": name, "head": head, "body": body,
            "bytes": os.path.getsize(path)}


def _split_header(text: str):
    """`---` … `---` at the very top, `key: value` a line. Deliberately not YAML:
    the header is written by this app for this app, a dependency to parse three
    dozen bytes is not worth an install, and a file WITHOUT a header is a valid
    markdown summary rather than an error."""
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.split("\n")
    head = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return head, "\n".join(lines[i + 1:]).strip()
        key, sep, value = line.partition(":")
        if sep:
            head[key.strip()] = value.strip()
    # No closing fence — treat the whole thing as body rather than swallow it.
    return {}, text.strip()


def _row(name: str, size: int, saved: str = "", model: str = "", length: str = "",
         fmt: str = "", source: str = "", sections: str = "", words: str = "",
         preview: str = "", error: str = ""):
    return {
        "name": name, "saved": saved, "model": model, "length": length,
        "format": fmt, "source": source, "sections": sections, "words": words,
        "preview": preview, "bytes": size, "ok": not error, "error": error,
    }


def _line(body: str, width: int = 150) -> str:
    """One line of hint, with markdown scaffolding stripped — a leading `#` or
    `- ` is the same first character on every row and tells the reader nothing."""
    for raw in body.split("\n"):
        line = raw.strip().lstrip("#*->+ ").strip()
        if line and not set(line) <= set("-=*_ |"):
            flat = " ".join(line.split())
            return flat if len(flat) <= width else flat[:width - 1] + "…"
    return ""


def _safe_path(name: str) -> str:
    """Refuses anything that is not a plain .md filename in ./summaries/, before
    touching the disk — the name arrives from a URL param."""
    if not name:
        raise ValueError("a 'name' is required")
    if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        raise ValueError(f"{name!r} is not a plain filename")
    if name in (".", "..") or name.startswith(".") or not name.endswith(".md"):
        raise ValueError(f"{name!r} is not a saved summary")
    path = os.path.join(_root(), name)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{name} is not in {_root()}")
    return path


def _delete(name: str):
    path = _safe_path(name)
    os.remove(path)
    return {"deleted": name}
