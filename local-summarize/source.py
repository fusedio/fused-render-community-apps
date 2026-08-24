"""The text being summarized — loading it, measuring it, and cutting it up.

The page owns the textarea; this file owns everything about the text that is
cheaper or safer to do in Python than in the browser:

- `load`  — pull a file off disk, with a size guard and a decoding fallback.
- `stats` — one honest measurement of a string.
- `chunk` — split it on paragraph boundaries so a document too long for the
            model's context can be summarized section by section.

Writing is deliberately NOT here: `fused.writeFile` is atomic and the page
already holds the summary, so routing a save through a subprocess would only
add a second serializer that could disagree with `saved.py`'s reader.
"""

import os
import re

# Both sides of this app estimate tokens the same way — four characters to a
# token — so the header the reader sees and the split the model gets can never
# disagree. It is a heuristic, not a tokenizer: no runner exposes its tokenizer
# to a page, and being wrong by 15% only changes how many sections a document
# is cut into.
CHARS_PER_TOKEN = 4


def main(action: str = "stats", path: str = "", text: str = "",
         budget: int = 3000, max_chunks: int = 60, max_bytes: int = 4000000):
    if action == "load":
        return _load(path, max_bytes)
    if action == "stats":
        return _stats(text)
    if action == "chunk":
        return _chunk(text, budget, max_chunks)
    raise ValueError(
        f"unknown action {action!r} — expected 'load', 'stats' or 'chunk'")


# ---------------------------------------------------------------- load

def _load(path: str, max_bytes: int):
    """Read a text file. A relative path resolves beside this script, i.e. the
    app folder; saved summaries do NOT live there — see `saved.py._root`."""
    if not path.strip():
        raise ValueError("load needs a 'path'")

    full = os.path.abspath(os.path.expanduser(path.strip()))
    if os.path.isdir(full):
        raise IsADirectoryError(f"{full} is a directory, not a text file")
    if not os.path.isfile(full):
        raise FileNotFoundError(full)

    size = os.path.getsize(full)
    with open(full, "rb") as handle:
        blob = handle.read(max_bytes)
    # Truncation is reported, never silent: a summary of the first 4 MB of a
    # bigger file is a different claim from a summary of the file.
    truncated = size > len(blob)

    try:
        body = blob.decode("utf-8")
    except UnicodeDecodeError:
        # Not a hard error. A log or an export in some 8-bit encoding is still
        # worth summarizing, and latin-1 decodes every byte sequence.
        body = blob.decode("latin-1", errors="replace")

    if "\x00" in body[:4096]:
        raise ValueError(f"{os.path.basename(full)} looks binary, not text")

    out = _stats(body)
    out.update({
        "text": body,
        "path": full,
        "name": os.path.basename(full),
        "bytes": size,
        "read_bytes": len(blob),
        "truncated": truncated,
    })
    return out


# ---------------------------------------------------------------- stats

def _stats(text: str):
    body = text or ""
    return {
        "chars": len(body),
        "words": len(body.split()),
        "lines": body.count("\n") + (1 if body else 0),
        "paragraphs": len([p for p in re.split(r"\n\s*\n", body) if p.strip()]),
        "tokens": _tokens(body),
    }


def _tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ---------------------------------------------------------------- chunk

def _chunk(text: str, budget: int, max_chunks: int):
    """Pack the text into sections of at most `budget` estimated tokens.

    Paragraph boundaries first, then sentences, then words — a section never
    starts mid-sentence unless a single sentence is itself over budget. There is
    no overlap between sections on purpose: the boundaries are the author's own,
    so nothing is cut in half, and repeated context would only show up as a
    repeated bullet in the digest.
    """
    body = (text or "").strip()
    stats = _stats(body)
    if budget < 200:
        raise ValueError(f"budget {budget} is too small to summarize with")

    if not body:
        return dict(stats, chunks=[], total=0, dropped=0, budget=budget)

    limit = budget * CHARS_PER_TOKEN

    chunks, current = [], ""
    for piece in _pieces(body, limit):
        candidate = (current + "\n\n" + piece) if current else piece
        if current and len(candidate) > limit:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)

    dropped = max(0, len(chunks) - max_chunks)
    kept = chunks[:max_chunks]
    return dict(
        stats,
        chunks=[
            {"index": i + 1, "text": c, "words": len(c.split()), "tokens": _tokens(c)}
            for i, c in enumerate(kept)
        ],
        total=len(kept),
        # The page prints this. A cap the reader cannot see reads as "the whole
        # document was summarized" when it was not.
        dropped=dropped,
        budget=budget,
    )


def _pieces(body: str, limit: int):
    """Paragraphs, each already small enough to pack."""
    out = []
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            out.append(para)
        else:
            out.extend(_split_sentences(para, limit))
    return out


def _split_sentences(para: str, limit: int):
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", para):
        if len(sentence) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_split_words(sentence, limit))
            continue
        candidate = (current + " " + sentence) if current else sentence
        if current and len(candidate) > limit:
            out.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def _split_words(sentence: str, limit: int):
    """Last resort — minified JSON, a wall of CSV, a sentence with no full stop."""
    out, current = [], ""
    for word in sentence.split():
        candidate = (current + " " + word) if current else word
        if current and len(candidate) > limit:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)

    # A single "word" longer than the whole budget (a base64 blob, a minified
    # bundle) still has to go somewhere; cut it on characters rather than drop it.
    final = []
    for piece in out:
        while len(piece) > limit:
            final.append(piece[:limit])
            piece = piece[limit:]
        if piece:
            final.append(piece)
    return final
