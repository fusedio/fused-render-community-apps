"""fused-render data file: the conversation on disk, and nothing else.

This file used to run the model. It managed a long-lived MLX server process, a
per-message streaming worker, pid files, process groups, a bootstrap timeout, a
curated model list and an environment scrub for the packaged app's interpreter —
about 800 lines across three files, none of which was about chatting.

All of it now belongs to fused-render (SPEC §40): the app holds a model resident
and hands it to any page through `fused.ai(prompt, {model})`, with the download,
the progress row and the ✕ owned by the same server that owns the process. What
is left here is the one thing that IS this app's: the transcript, so a reload
shows what you said yesterday.

Deliberately no `pyproject.toml` beside this file any more. It needs nothing but
the standard library, which is the point — a chat app should not be a reason to
build a multi-gigabyte environment.
"""
import json
import os
import time

# Kept out of the app folder, and configurable, because the executor sometimes
# runs a call from an ephemeral per-call sandbox copy of the repo — state
# written next to the code would not survive it.
STATE = os.environ.get(
    "LOCAL_CHAT_STATE_DIR", os.path.expanduser("~/.fused-render/local-chat")
)
CONVERSATION = os.path.join(STATE, "conversation.json")

#: Roles the transcript will store. The same two `fused.ai`'s `history` accepts
#: — this file is where they are persisted and that is where they are sent, so a
#: third role here would be one the model never sees.
ROLES = ("user", "assistant")


def _read():
    try:
        with open(CONVERSATION) as handle:
            stored = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    messages = stored.get("messages") if isinstance(stored, dict) else None
    return messages if isinstance(messages, list) else []


def _write(messages):
    os.makedirs(STATE, exist_ok=True)
    tmp = CONVERSATION + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({"messages": messages}, handle)
    # Atomic, because the page appends a turn while it is also reading the
    # transcript back: a half-written file is a conversation that vanishes.
    os.replace(tmp, CONVERSATION)


def main(action: str = "load", role: str = "", content: str = ""):
    """load | append | drop | clear.

    `append` takes ONE turn. The page owns when a turn is real — a reply that
    was cancelled halfway is still worth keeping, an empty one is not — and that
    judgement does not belong in the store.

    `drop` is append's undo, and it exists for one case: the model was not
    resident, so the question was never actually asked. A turn committed before
    a request that never happened is worse than no turn — it becomes history for
    the NEXT message, so the model is handed a question it never answered and
    the user retypes it into a duplicate.
    """
    if action == "load":
        return {"messages": _read()}

    if action == "drop":
        # Matched, not blind: the store is shared with the page's own view and
        # a blind pop would take whatever landed last if anything raced. Nothing
        # matching is a no-op, which is the right answer for an undo that has
        # already been applied.
        messages = _read()
        if messages and messages[-1].get("role") == role and \
                messages[-1].get("content") == content:
            messages.pop()
            _write(messages)
        return {"messages": messages}

    if action == "append":
        if role not in ROLES:
            return {"error": "role must be one of: " + ", ".join(ROLES)}
        if not content:
            return {"error": "content is empty"}
        messages = _read()
        messages.append({"role": role, "content": content, "at": time.time()})
        _write(messages)
        return {"messages": messages}

    if action == "clear":
        _write([])
        return {"messages": []}

    return {"error": "unknown action: {}".format(action)}
