"""Shared helpers for DocChat: folder walking, chunking, DuckDB/VSS.

Used by ragserver.py (the warm embedding server) and browse.py (the picker).
No embedding lives here — the server owns the model. Nothing talks to a network
service; embeddings are produced in-process by sentence-transformers.
"""

import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DOCS = os.path.join(HERE, "docs")

# Where embeddings are cached. Default to the persistent fused-render app cache
# (survives worktree churn, so re-running the same model reuses the index instead
# of rebuilding). Namespaced under docchat/ so it doesn't mingle with the app's
# own cache files. Override with $RAG_CACHE_DIR or a per-call cache_dir.
DEFAULT_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "docchat")
CACHE_DIR = os.environ.get("RAG_CACHE_DIR") or DEFAULT_CACHE_DIR
INDEX_DIR = CACHE_DIR   # one DuckDB file per (folder, model) under here

# Text-readable extensions we index. PDFs and binaries are intentionally absent.
TEXT_EXTS = {
    ".md", ".mdx", ".markdown", ".txt", ".rst", ".text",
    ".html", ".htm", ".xml",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".sql", ".sh", ".bash", ".ps1",
    ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".log",
}
# Directories never worth indexing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", "dist", "build", ".next", "target", ".mypy_cache",
    ".pytest_cache", ".cache", ".indexes", ".fused-render",
}
MAX_FILE_BYTES = 1_000_000  # skip files larger than ~1MB
MAX_FILES = 5000            # cap a single folder's file count for a demo build


# --------------------------------------------------------------------------- #
# Chunking + reading a folder of text files
# --------------------------------------------------------------------------- #

def chunk_text(text, size=700, overlap=100):
    """Split text into ~`size`-char chunks with `overlap`, breaking on
    paragraph/sentence boundaries where possible."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", ". ", "\n"):
                pos = window.rfind(sep)
                if pos > size * 0.5:
                    end = start + pos + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def html_to_text(html):
    """Strip tags/scripts from HTML so we embed prose, not markup."""
    from html.parser import HTMLParser

    class _Extract(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip and data.strip():
                self.parts.append(data.strip())

    p = _Extract()
    p.feed(html)
    return " ".join(p.parts)


def normalize_path(p):
    """Clean a user-entered path.

    Strips surrounding quotes (Windows "Copy as path" wraps backslash paths in
    double quotes), expands ~, and makes it absolute. Handles both / and \\ .
    """
    if not p:
        return ""
    p = p.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in ("\"", "'"):
        p = p[1:-1].strip()
    if not p:
        return ""
    return os.path.abspath(os.path.expanduser(p))


def _read_text(full, force=False):
    """Read a file as UTF-8 text, or None if binary / too large / unreadable.

    force=True (an explicitly chosen single file) relaxes the extension filter
    and the size cap, but the file must still decode as text.
    """
    ext = os.path.splitext(full)[1].lower()
    try:
        size = os.path.getsize(full)
    except OSError:
        return None
    if size == 0 or (size > MAX_FILE_BYTES and not force) or size > 8 * MAX_FILE_BYTES:
        return None
    try:
        with open(full, "r", encoding="utf-8") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError):
        return None
    if "\x00" in text:
        return None
    if ext in (".html", ".htm"):
        text = html_to_text(text)
    return text


def read_docs(folder):
    """Walk `folder` recursively for text-readable files.

    Returns (items, truncated) where items is a sorted list of
    (relpath, mtime, text) and truncated is True if MAX_FILES was hit.
    """
    items = []
    truncated = False
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(files):
            if name.startswith("."):                 # skip hidden/dotfiles (.env, .DS_Store, …)
                continue
            if os.path.splitext(name)[1].lower() not in TEXT_EXTS:
                continue
            full = os.path.join(root, name)
            text = _read_text(full)
            if text is None:
                continue
            items.append((os.path.relpath(full, folder), os.path.getmtime(full), text))
            if len(items) >= MAX_FILES:
                truncated = True
                break
        if truncated:
            break
    items.sort(key=lambda e: e[0])
    return items, truncated


def collect_docs(path):
    """Gather documents for a FILE or a FOLDER path.

    Returns (items, truncated, is_file). A single file is read permissively
    (any extension, as long as it decodes as text).
    """
    if os.path.isfile(path):
        text = _read_text(path, force=True)
        if text is None:
            return [], False, True
        return [(os.path.basename(path), os.path.getmtime(path), text)], False, True
    items, truncated = read_docs(path)
    return items, truncated, False


def list_dir(path):
    """Subfolders and indexable files directly under `path`, for the picker."""
    dirs, files = [], []
    for name in sorted(os.listdir(path), key=str.lower):
        full = os.path.join(path, name)
        if name in SKIP_DIRS or name.startswith("."):
            continue
        if os.path.isdir(full):
            dirs.append({"name": name, "path": full})
        elif len(files) < 300 and os.path.splitext(name)[1].lower() in TEXT_EXTS:
            files.append({"name": name, "path": full})
    return dirs, files


_ignored_cache = {}   # path -> (dir_mtime, [names]); keeps the /status poll off a full listdir


def top_level_ignored(path):
    """Folder names directly under `path` that the indexer skips (VCS / build /
    hidden dirs), so the UI can show what was left out and why. Memoized on the
    directory's mtime — the hot /status poll then does one stat, not a walk."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _ignored_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    out = []
    try:
        entries = sorted(os.listdir(path), key=str.lower)
    except OSError:
        return out
    for name in entries:
        if os.path.isdir(os.path.join(path, name)) and (name in SKIP_DIRS or name.startswith(".")):
            out.append(name)
            if len(out) >= 20:
                break
    _ignored_cache[path] = (mtime, out)
    return out


def count_indexable(folder, cap=2000):
    """Approximate count of indexable files under `folder` (walk capped)."""
    n = 0
    scanned = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            scanned += 1
            if not name.startswith(".") and os.path.splitext(name)[1].lower() in TEXT_EXTS:
                n += 1
            if scanned >= cap:
                return n, True
    return n, False


def content_hash(text):
    """Short content signature, cheap to compute since callers already hold `text` in memory."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def docs_fingerprint(items):
    """A signature that changes when any file is added, removed, or edited.

    Keyed on mtime AND content hash (not mtime alone) so a tool that preserves
    the original mtime across a content change (zip/tar -p, robocopy /COPY:DAT,
    rsync -a, a cloud-sync client restoring the remote edit time) still trips
    the fingerprint.
    """
    parts = [name + ":" + repr(round(mtime, 3)) + ":" + content_hash(text) for name, mtime, text in items]
    return "|".join(parts)


# --------------------------------------------------------------------------- #
# DuckDB + VSS
# --------------------------------------------------------------------------- #

def db_path_for(folder, provider, cache_dir=None):
    """One DuckDB index file per (absolute folder, embedding provider/model),
    under `cache_dir` (defaults to the persistent Fused cache)."""
    base = normalize_path(cache_dir) or INDEX_DIR
    key = os.path.abspath(folder) + "|" + provider
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, h + ".duckdb")


def connect(path):
    import duckdb
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = duckdb.connect(path)
    con.execute("INSTALL vss;")
    con.execute("LOAD vss;")
    con.execute("SET hnsw_enable_experimental_persistence = true;")
    return con


def read_meta(con):
    try:
        rows = con.execute("SELECT key, value FROM docmeta").fetchall()
        return {k: v for k, v in rows}
    except Exception:
        return None
