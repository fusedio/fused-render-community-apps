"""Shared cache catalog + directory sizing helpers.

Imported by caches.py, clean.py and scan.py. Every path a cleanup action is
allowed to touch has to come from CATALOG below — clean.py refuses anything
else, so this file is the whole trust boundary.

Entries are cross-platform where the underlying tool's cache actually lives
in the same place on both OSes (npm, cargo, gradle, …). Where it doesn't, an
entry's "path" is a dict keyed by `platform.system()` value ("Darwin" /
"Linux") instead of a single string, and an OS missing from that dict means
the entry doesn't apply there at all (e.g. Xcode). `catalog_for_os()` is what
every caller should iterate — it resolves each entry to this OS's path and
drops the ones that don't apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import time

HOME = os.path.expanduser("~")
OS = platform.system()  # "Darwin", "Linux", or something else entirely

# Where the scripts memoise scan results. The executor runs each .py with the
# working directory set to its own folder and does not define __file__, so this
# stays a relative path.
CACHE_DIR = ".cache"

# risk: "safe"      — regenerated automatically, no user data
#       "moderate"  — regenerated, but costs a slow rebuild / re-download
#       "review"    — may contain things you actually want; look before deleting
CATALOG = [
    # --- system / app caches -------------------------------------------------
    {
        "id": "user-caches",
        "name": "Application caches",
        "path": {"Darwin": "~/Library/Caches", "Linux": "~/.cache"},
        "group": "System",
        "risk": "safe",
        "desc": "Per-app scratch data. Apps rebuild it on next launch.",
    },
    {
        "id": "logs",
        "name": "Application logs",
        "path": {"Darwin": "~/Library/Logs"},
        "group": "System",
        "risk": "safe",
        "desc": "Diagnostic logs written by apps and crash reporters.",
    },
    {
        "id": "trash",
        "name": "Trash",
        "path": {"Darwin": "~/.Trash", "Linux": "~/.local/share/Trash/files"},
        "group": "System",
        "risk": "review",
        "desc": "Deleted files still occupying disk. Emptying is permanent.",
    },
    {
        "id": "saved-state",
        "name": "Saved application state",
        "path": {"Darwin": "~/Library/Saved Application State"},
        "group": "System",
        "risk": "safe",
        "desc": "Window/tab restore snapshots. Apps just reopen fresh.",
    },
    # --- developer tooling ---------------------------------------------------
    {
        "id": "xcode-derived",
        "name": "Xcode DerivedData",
        "path": {"Darwin": "~/Library/Developer/Xcode/DerivedData"},
        "group": "Developer",
        "risk": "safe",
        "desc": "Build intermediates and indexes. Next build recreates them.",
    },
    {
        "id": "xcode-archives",
        "name": "Xcode archives",
        "path": {"Darwin": "~/Library/Developer/Xcode/Archives"},
        "group": "Developer",
        "risk": "review",
        "desc": "Shipped build archives — needed to re-symbolicate crashes.",
    },
    {
        "id": "ios-device-support",
        "name": "iOS DeviceSupport",
        "path": {"Darwin": "~/Library/Developer/Xcode/iOS DeviceSupport"},
        "group": "Developer",
        "risk": "moderate",
        "desc": "Symbols per iOS version. Re-downloaded when you attach a device.",
    },
    {
        "id": "coresim-caches",
        "name": "Simulator caches",
        "path": {"Darwin": "~/Library/Developer/CoreSimulator/Caches"},
        "group": "Developer",
        "risk": "safe",
        "desc": "Downloaded simulator runtime caches.",
    },
    {
        "id": "npm",
        "name": "npm cache",
        "path": "~/.npm/_cacache",
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded packages. Re-fetched from the registry.",
    },
    {
        "id": "bun",
        "name": "bun install cache",
        "path": "~/.bun/install/cache",
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded packages. Re-fetched on next install.",
    },
    {
        "id": "yarn",
        "name": "Yarn cache",
        "path": {"Darwin": "~/Library/Caches/Yarn", "Linux": "~/.cache/yarn"},
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded packages. Re-fetched on next install.",
    },
    {
        "id": "pnpm",
        "name": "pnpm store",
        "path": {"Darwin": "~/Library/pnpm/store", "Linux": "~/.local/share/pnpm/store"},
        "group": "Developer",
        "risk": "moderate",
        "desc": "Content-addressed package store shared by pnpm projects.",
    },
    {
        "id": "pip",
        "name": "pip cache",
        "path": {"Darwin": "~/Library/Caches/pip", "Linux": "~/.cache/pip"},
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded wheels. Re-fetched from PyPI.",
    },
    {
        "id": "uv",
        "name": "uv cache",
        "path": "~/.cache/uv",
        "group": "Developer",
        "risk": "moderate",
        "desc": "uv's wheel and source cache.",
    },
    {
        "id": "homebrew",
        "name": "Homebrew downloads",
        "path": {"Darwin": "~/Library/Caches/Homebrew", "Linux": "~/.cache/Homebrew"},
        "group": "Developer",
        "risk": "safe",
        "desc": "Downloaded bottles and casks. Same as `brew cleanup`.",
    },
    {
        "id": "go-build",
        "name": "Go build cache",
        "path": {"Darwin": "~/Library/Caches/go-build", "Linux": "~/.cache/go-build"},
        "group": "Developer",
        "risk": "safe",
        "desc": "Compiled package objects. Rebuilt on demand.",
    },
    {
        "id": "cargo-cache",
        "name": "Cargo registry cache",
        "path": "~/.cargo/registry/cache",
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded crate archives. Re-fetched from crates.io.",
    },
    {
        "id": "gradle",
        "name": "Gradle caches",
        "path": "~/.gradle/caches",
        "group": "Developer",
        "risk": "moderate",
        "desc": "Dependencies and build caches. Rebuilt on next build.",
    },
    {
        "id": "playwright",
        "name": "Playwright browsers",
        "path": {"Darwin": "~/Library/Caches/ms-playwright", "Linux": "~/.cache/ms-playwright"},
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded browser builds. Re-installed on demand.",
    },
    {
        "id": "puppeteer",
        "name": "Puppeteer browsers",
        "path": "~/.cache/puppeteer",
        "group": "Developer",
        "risk": "moderate",
        "desc": "Downloaded Chromium builds. Re-installed on demand.",
    },
    {
        "id": "docker-logs",
        "name": "Docker Desktop logs",
        "path": {"Darwin": "~/Library/Containers/com.docker.docker/Data/log"},
        "group": "Developer",
        "risk": "safe",
        "desc": "Docker Desktop diagnostic logs (not images or volumes).",
    },
    # --- browsers & apps -----------------------------------------------------
    {
        "id": "chrome-cache",
        "name": "Chrome cache",
        "path": {"Darwin": "~/Library/Caches/Google/Chrome", "Linux": "~/.cache/google-chrome"},
        "group": "Apps",
        "risk": "safe",
        "desc": "Cached web content. Does not touch history or passwords.",
    },
    {
        "id": "firefox-cache",
        "name": "Firefox cache",
        "path": {"Darwin": "~/Library/Caches/Firefox", "Linux": "~/.cache/mozilla/firefox"},
        "group": "Apps",
        "risk": "safe",
        "desc": "Cached web content. Profiles are left alone.",
    },
    {
        "id": "safari-cache",
        "name": "Safari cache",
        "path": {"Darwin": "~/Library/Caches/com.apple.Safari"},
        "group": "Apps",
        "risk": "safe",
        "desc": "Cached web content.",
    },
    {
        "id": "slack-cache",
        "name": "Slack cache",
        "path": {
            "Darwin": "~/Library/Application Support/Slack/Cache",
            "Linux": "~/.config/Slack/Cache",
        },
        "group": "Apps",
        "risk": "safe",
        "desc": "Slack's web content cache.",
    },
    {
        "id": "spotify-cache",
        "name": "Spotify cache",
        "path": {"Darwin": "~/Library/Caches/com.spotify.client", "Linux": "~/.cache/spotify"},
        "group": "Apps",
        "risk": "safe",
        "desc": "Cached audio. Re-streamed as needed.",
    },
    {
        "id": "quicklook",
        "name": "QuickLook thumbnails",
        "path": {"Darwin": "~/Library/Caches/com.apple.QuickLook.thumbnailcache"},
        "group": "Apps",
        "risk": "safe",
        "desc": "Finder preview thumbnails. Regenerated on demand.",
    },
    {
        "id": "thumbnail-cache",
        "name": "Thumbnail cache",
        "path": {"Linux": "~/.cache/thumbnails"},
        "group": "Apps",
        "risk": "safe",
        "desc": "GNOME/KDE file-manager preview thumbnails. Regenerated on demand.",
    },
]

CATALOG_BY_ID = {e["id"]: e for e in CATALOG}


def entry_path(entry: dict) -> str | None:
    """This OS's path string for `entry`, or None if it doesn't apply here."""
    p = entry["path"]
    return p if isinstance(p, str) else p.get(OS)


def catalog_for_os() -> list[dict]:
    """CATALOG filtered to entries that have a path on this OS."""
    return [e for e in CATALOG if entry_path(e) is not None]


def resolve(path: str) -> str:
    """Expand ~ and normalise, without following the final symlink."""
    return os.path.normpath(os.path.expanduser(path))


def size_cache_path(abs_path: str) -> str:
    key = hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, "size-%s.json" % key)


def read_size_cache(abs_path: str, max_age: float) -> dict | None:
    """A directory's cached recursive size, if one exists and isn't stale.

    There's no cheap, reliable way to know a whole subtree is unchanged — a
    directory's own mtime only reflects direct children (add/remove/rename),
    not a file deep inside changing size, and it's inconsistent across
    filesystems even for that. So this is a plain time-based TTL rather than
    a content-hash check: good enough for "did the user just install a
    node_modules tree", not meant to catch every edit within max_age.
    """
    try:
        with open(size_cache_path(abs_path)) as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    age = time.time() - payload.get("scanned_at", 0)
    if age > max_age:
        return None
    payload = dict(payload)
    payload["age"] = age
    payload["cached"] = True
    return payload


def write_size_cache(abs_path: str, size: int, files: int, truncated: bool) -> dict:
    payload = {
        "path": abs_path,
        "size": size,
        "files": files,
        "truncated": truncated,
        "scanned_at": time.time(),
        "cached": False,
        "age": 0.0,
    }
    cache_file = size_cache_path(abs_path)
    os.makedirs(CACHE_DIR, exist_ok=True)
    # A per-process tmp name avoids a rare collision if the same directory
    # gets sized by two concurrent calls (e.g. Overview and a Rescan at once).
    tmp = "%s.%d.tmp" % (cache_file, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, cache_file)
    return payload


def resolve_entry(entry: dict) -> str | None:
    """Absolute, OS-resolved path for `entry`, or None if inapplicable here."""
    p = entry_path(entry)
    return resolve(p) if p is not None else None


def fmt_entry(entry: dict) -> dict:
    out = dict(entry)
    out["path"] = entry_path(entry)
    out["abs"] = resolve_entry(entry)
    return out


def mark_nested(entries: list[dict]) -> None:
    """Flag entries whose path is a subdirectory of another entry's path.

    Several catalog entries live inside a broader one on the same OS (Chrome's
    cache sits inside "Application caches", for instance) — real on both
    macOS and, more heavily, Linux, where almost everything funnels through
    ~/.cache. Summing every entry's size double-counts those bytes, so each
    nested entry is tagged with the id of its closest ancestor; callers that
    total sizes should skip entries where `nested_under` is set.
    """
    by_depth = sorted((e for e in entries if e.get("abs")), key=lambda e: len(e["abs"]))
    for e in entries:
        e["nested_under"] = None
    for i, child in enumerate(by_depth):
        best = None
        for ancestor in by_depth[:i]:
            if child["abs"].startswith(ancestor["abs"] + os.sep):
                if best is None or len(ancestor["abs"]) > len(best["abs"]):
                    best = ancestor
        if best is not None:
            child["nested_under"] = best["id"]


def dir_size(path: str, deadline: float | None = None) -> tuple[int, int, bool]:
    """Recursive apparent size of `path`.

    Returns (bytes, file_count, truncated). Two things are deliberately not
    followed, both for correctness and for speed:

    * symlinks — so nothing is counted twice and no loop can hang the walk;
    * mount points (any child whose st_dev differs from `path`'s) — the same
      rule as `du -x`. Without it a network or remote mount under the home
      directory gets walked over the wire, which is both wrong for a *disk*
      usage number and slow enough to blow the whole time budget.

    `truncated` is True when the time budget ran out before the walk finished.
    """
    try:
        root_dev = os.stat(path).st_dev
    except OSError:
        return 0, 0, False

    total = 0
    files = 0
    stack = [path]
    truncated = False
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for de in it:
                    try:
                        if de.is_symlink():
                            continue
                        st = de.stat(follow_symlinks=False)
                        if st.st_dev != root_dev:
                            continue
                        if de.is_dir(follow_symlinks=False):
                            stack.append(de.path)
                        else:
                            total += st.st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files, truncated


def entry_size(entry: dict, deadline: float | None = None) -> dict:
    """Size one catalog entry, tolerating a missing path."""
    out = fmt_entry(entry)
    abs_path = out["abs"]
    if not abs_path or not os.path.isdir(abs_path):
        out.update(exists=False, size=0, files=0, truncated=False)
        return out
    size, files, truncated = dir_size(abs_path, deadline)
    out.update(exists=True, size=size, files=files, truncated=truncated)
    return out
