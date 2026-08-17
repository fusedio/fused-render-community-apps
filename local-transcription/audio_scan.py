"""Find audio/video files worth transcribing, so the page can offer a pick list
instead of asking the user to type an absolute path."""

AUDIO_EXT = {
    ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aac", ".aiff",
    ".aif", ".wma", ".caf", ".amr",
}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg"}


def main(roots: str = "", depth: int = 3, limit: int = 200):
    """List media files under `roots` (comma-separated dirs; blank = sensible defaults).

    Returns {"roots": [...], "files": [...], "truncated": bool}. Each file carries
    path/name/dir/ext/size/mtime/kind — enough for the page to render and sort
    without a second call.
    """
    import os
    import time

    home = os.path.expanduser("~")
    if roots.strip():
        candidates = [r.strip() for r in roots.split(",") if r.strip()]
    else:
        candidates = [
            os.path.dirname(os.path.abspath(__file__)),
            os.path.join(home, "Downloads"),
            os.path.join(home, "Music"),
            os.path.join(home, "Movies"),
            os.path.join(home, "Desktop"),
            os.path.join(home, "Documents"),
        ]

    seen_roots, scanned = [], []
    for root in candidates:
        root = os.path.abspath(os.path.expanduser(root))
        if os.path.isdir(root) and root not in seen_roots:
            seen_roots.append(root)
            scanned.append(root)

    files, seen_paths, truncated = [], set(), False
    for root in scanned:
        root_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune noise before descending: dotfiles, caches, bundles, and app
            # payloads produce thousands of hits and nothing transcribable.
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and d not in {"node_modules", "__pycache__", "Library", "venv", ".venv"}
                and not d.endswith((".app", ".framework", ".photoslibrary", ".musiclibrary"))
            ]
            if dirpath.rstrip(os.sep).count(os.sep) - root_depth >= depth:
                dirnames[:] = []
            for name in filenames:
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext not in AUDIO_EXT and ext not in VIDEO_EXT:
                    continue
                full = os.path.join(dirpath, name)
                if full in seen_paths:
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                seen_paths.add(full)
                files.append({
                    "path": full,
                    "name": name,
                    "dir": dirpath,
                    "ext": ext,
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                    "kind": "video" if ext in VIDEO_EXT else "audio",
                })

    files.sort(key=lambda f: -f["mtime"])
    if len(files) > limit:
        files, truncated = files[:limit], True

    return {
        "roots": scanned,
        "files": files,
        "truncated": truncated,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
