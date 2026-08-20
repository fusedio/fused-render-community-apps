"""Land an exported .srt in the user's Downloads folder.

`fused.writeFile` only reaches paths inside the app's own tree, and a
transcript is something the user takes AWAY from this page — into a video
editor, a captioning tool, wherever — so it belongs beside every other browser
download, not next to the source recording buried in ./recordings/.
"""

import os
import re


def main(name: str = "transcript", body: str = ""):
    """Write `body` to ~/Downloads/<name>.srt, numbering on a name clash.

    Returns {"path"} — the absolute path written.
    """
    name = (name or "transcript").strip()
    if not re.fullmatch(r"[A-Za-z0-9._ -]{1,120}", name) or name.startswith("."):
        raise ValueError(f"unsafe file name: {name!r}")

    out_dir = os.path.expanduser("~/Downloads")
    os.makedirs(out_dir, exist_ok=True)

    path = os.path.join(out_dir, f"{name}.srt")
    n = 1
    # Numbered like a browser download, not overwritten and not blocked on a
    # confirm() the user never sees a second time — every export lands.
    while os.path.exists(path):
        n += 1
        path = os.path.join(out_dir, f"{name} ({n}).srt")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)

    return {"path": path}
