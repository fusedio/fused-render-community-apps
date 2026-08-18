"""DocChat's warm embedding + retrieval server (no Ollama, no external daemon).

fused-render spins this up on demand (see serve.py). It loads a sentence-
transformers model ONCE (auto-using CUDA/MPS if present, else CPU) and keeps the
per-folder DuckDB index warm in memory, so answering a question is just
embed-query + vector-search — never a re-walk / re-chunk / re-embed. Because it
lives outside the 60s runPython budget, indexing large folders runs to
completion here while the page polls /status.

Core functions (embed_docs / embed_query / build_index / search_index /
index_status) are importable and unit-tested directly; the HTTP layer at the
bottom is a thin stdlib wrapper. Model id comes from $RAG_MODEL.
"""

import json
import os
import re
import secrets
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rag_common as rc

# Download model weights INTO the fused-render cache (not the user's global HF
# cache), so everything DocChat pulls lives under ~/.fused-render/cache. Must be
# set before sentence-transformers / huggingface_hub import — they read it once.
MODELS_DIR = os.path.join(os.path.expanduser("~"), ".fused-render", "cache", "models")
os.environ.setdefault("HF_HOME", MODELS_DIR)

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
MODEL_NAME = os.environ.get("RAG_MODEL", DEFAULT_MODEL)
# Fixed port so the server is a singleton: binding is atomic, so a second launch
# (e.g. from a page reload racing serve.py) fails to bind and simply exits — only
# one process ever owns the DuckDB files. Override with $RAG_PORT if 8271 is taken.
PORT = int(os.environ.get("RAG_PORT", "8271"))
# Per-process secret gating every data route. Because the server binds a fixed
# localhost port with CORS *, any web page could otherwise call /browse and /file
# and read local files; the token travels only over the on-disk sidecar (read by
# serve.py and handed to the page via runPython), never over the HTTP surface.
TOKEN = secrets.token_urlsafe(32)
SIDECAR = os.path.join(rc.HERE, ".ragserver.json")

_MODEL = None
_MODEL_LOCK = threading.Lock()
_DB_LOCK = threading.Lock()
_CONS = {}                 # db_path -> duckdb connection (warm)
_BUILDS = {}              # (folder, cache_dir) -> {"state","done","total","docs","error"}
_BUILD_LOCK = threading.Lock()

READY = False
DEVICE = "cpu"
DIM = 0
STAGE = "starting"     # starting -> downloading | loading -> ready (what the model is doing)


def provider_slug(model=None):
    """Filesystem-safe id for a model, so each model gets its own index files."""
    return re.sub(r"[^a-z0-9]+", "-", (model or MODEL_NAME).lower()).strip("-")


def _dim(m):
    # sentence-transformers 5.x renamed get_sentence_embedding_dimension -> get_embedding_dimension.
    fn = getattr(m, "get_embedding_dimension", None) or m.get_sentence_embedding_dimension
    return int(fn())


PROVIDER = provider_slug()


# --------------------------------------------------------------------------- #
# Model + embedding
# --------------------------------------------------------------------------- #

def _model_cached(name):
    """True if this model's weights are already on disk (so we're loading, not
    downloading). HF stores a repo as hub/models--org--name under MODELS_DIR."""
    return os.path.isdir(os.path.join(MODELS_DIR, "hub", "models--" + name.replace("/", "--")))


def get_model():
    global _MODEL, DEVICE, DIM, READY, STAGE
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                STAGE = "loading" if _model_cached(MODEL_NAME) else "downloading"
                from sentence_transformers import SentenceTransformer
                m = SentenceTransformer(MODEL_NAME)
                DEVICE = str(getattr(m, "device", "cpu"))
                DIM = _dim(m)
                _MODEL = m
                READY = True
                STAGE = "ready"
    return _MODEL


def _prompt_kw(m, name):
    # Asymmetric models (e.g. Qwen3-Embedding) ship distinct "query"/"document"
    # prompts; use them when present, otherwise encode plain (e.g. MiniLM/bge).
    prompts = getattr(m, "prompts", None)
    return {"prompt_name": name} if (prompts and name in prompts) else {}


def embed_docs(texts, batch_size=64):
    m = get_model()
    vecs = m.encode(list(texts), batch_size=batch_size, normalize_embeddings=True,
                    convert_to_numpy=True, **_prompt_kw(m, "document"))
    return vecs.astype("float32")


def embed_query(q):
    m = get_model()
    v = m.encode([q], normalize_embeddings=True, convert_to_numpy=True, **_prompt_kw(m, "query"))[0]
    return v.astype("float32").tolist()


# --------------------------------------------------------------------------- #
# DuckDB access (warm connections, serialized by one lock — single-user server)
# --------------------------------------------------------------------------- #

def _con(db_path):
    with _DB_LOCK:
        c = _CONS.get(db_path)
        if c is None:
            c = rc.connect(db_path)
            _CONS[db_path] = c
        return c


def _meta(con):
    """read_meta under _DB_LOCK. The warm connection is shared across
    ThreadingHTTPServer workers and DuckDB connections aren't thread-safe, so
    every read has to serialize with builds/searches on the same handle."""
    with _DB_LOCK:
        return rc.read_meta(con)


def _write_meta(con, meta):
    con.execute("CREATE TABLE IF NOT EXISTS docmeta (key VARCHAR, value VARCHAR);")
    con.execute("DELETE FROM docmeta;")
    con.executemany("INSERT INTO docmeta VALUES (?, ?);", list(meta.items()))


def _has_incremental_tables(con):
    """Both the chunks table and a docfiles table with a content_hash column
    present (the latter is what enables incremental reuse; an older index
    without it — or with the pre-content-hash schema — forces one full
    rebuild to add it)."""
    try:
        con.execute("SELECT 1 FROM chunks LIMIT 0")
        con.execute("SELECT content_hash FROM docfiles LIMIT 0")
        return True
    except Exception:
        return False


def _has_hnsw(con):
    try:
        return con.execute("SELECT count(*) FROM duckdb_indexes() WHERE index_name = 'chunks_hnsw'").fetchone()[0] > 0
    except Exception:
        return False


def _source_name(folder):
    return os.path.basename(folder.rstrip("/\\")) or folder


def _build_key(folder, cache_dir=None):
    """The _BUILDS tracking key, matching the actual index identity used by
    rc.db_path_for (folder, provider, cache_dir) rather than folder alone —
    a build under one cache_dir must not be mistaken for one under another."""
    return (os.path.abspath(folder), rc.normalize_path(cache_dir) or rc.INDEX_DIR)


# --------------------------------------------------------------------------- #
# Build / search / status (the API surface, all importable + testable)
# --------------------------------------------------------------------------- #

def build_index(folder, rebuild=False, progress=None, cache_dir=None):
    """Chunk + embed a folder (or single file) into a warm DuckDB + HNSW index.

    INCREMENTAL: reuses embeddings for files whose mtime is unchanged and only
    (re)embeds added / modified files, dropping removed ones — so editing one file
    in a big folder re-embeds just that file, not the whole tree. An unchanged
    folder returns instantly; `rebuild=True` forces a full re-embed. `progress(done,
    total)` is called after each batch; runs to completion (no 60s cap).
    """
    folder = rc.normalize_path(folder) or rc.DEFAULT_DOCS
    if not os.path.exists(folder):
        return {"ok": False, "error": "Path not found: " + folder}
    dim = _dim(get_model())
    db_path = rc.db_path_for(folder, PROVIDER, cache_dir)

    docs, truncated, is_file = rc.collect_docs(folder)
    if not docs:
        return {"ok": False, "error": ("File is empty or not text-readable"
                if is_file else "No text-readable files found under " + folder)}
    fingerprint = rc.docs_fingerprint(docs)

    con = _con(db_path)
    meta = _meta(con)
    dim_ok = bool(meta and meta.get("dim") == str(dim))
    fresh = bool(dim_ok and meta.get("fingerprint") == fingerprint)
    if fresh and meta.get("status") == "ready" and not rebuild:
        with _DB_LOCK:
            n = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
            # Verify the HNSW index actually exists before trusting "ready": a kill
            # mid-reconcile (e.g. serve.py's model switch) can land after DROP INDEX
            # auto-commits but before meta is rewritten. Falling through reconciles
            # (a no-op diff) and rebuilds the index at the end. Huge sets have no
            # HNSW by design, so they still shortcut.
            intact = n > 50000 or _has_hnsw(con)
        if intact:
            return _summary(True, folder, is_file, meta.get("truncated") == "1",
                            docs=int(meta.get("docs", len(docs))), chunks=n, dim=dim)

    # Reconcile the index against the current files. A full (re)build wipes and
    # re-chunks everything; otherwise we diff on per-file mtime AND content hash
    # and touch only what changed — mtime alone would miss a file whose content
    # changed but whose mtime a copy tool (zip -p, robocopy /COPY:DAT, rsync -a)
    # preserved. New rows go in with NULL embeddings; the fill loop below embeds
    # them (and any left over from an interrupted run — so builds resume, not
    # restart).
    with _DB_LOCK:
        full = rebuild or not dim_ok or not _has_incremental_tables(con)
        if full:
            con.execute("DROP INDEX IF EXISTS chunks_hnsw;")
            con.execute("DROP TABLE IF EXISTS chunks;")
            con.execute("DROP TABLE IF EXISTS docfiles;")
            con.execute("CREATE TABLE chunks (id INTEGER, source VARCHAR, chunk_index INTEGER, "
                        "content VARCHAR, embedding FLOAT[" + str(dim) + "]);")
            con.execute("CREATE TABLE docfiles (source VARCHAR, mtime DOUBLE, content_hash VARCHAR);")
            changed = list(docs)
            structural = True
        else:
            stored = {s: (m, h) for s, m, h in
                      con.execute("SELECT source, mtime, content_hash FROM docfiles").fetchall()}
            current = {name: mtime for name, mtime, _ in docs}
            changed = [d for d in docs if d[0] not in stored
                       or round(stored[d[0]][0], 3) != round(d[1], 3)
                       or stored[d[0]][1] != rc.content_hash(d[2])]
            gone = [s for s in stored if s not in current]
            stale = [d[0] for d in changed] + gone
            structural = bool(stale)
            if stale:
                con.execute("DROP INDEX IF EXISTS chunks_hnsw;")   # rebuilt after the fill
                ph = ",".join(["?"] * len(stale))
                con.execute("DELETE FROM chunks WHERE source IN (" + ph + ")", stale)
                con.execute("DELETE FROM docfiles WHERE source IN (" + ph + ")", stale)
        if changed:
            next_id = con.execute("SELECT coalesce(max(id), -1) + 1 FROM chunks").fetchone()[0]
            records, dfrows = [], []
            for name, mtime, text in changed:
                for i, chunk in enumerate(rc.chunk_text(text)):
                    records.append((next_id, name, i, chunk)); next_id += 1
                dfrows.append((name, mtime, rc.content_hash(text)))
            con.executemany("INSERT INTO chunks (id, source, chunk_index, content, embedding) "
                            "VALUES (?, ?, ?, ?, NULL);", records)
            con.executemany("INSERT INTO docfiles (source, mtime, content_hash) VALUES (?, ?, ?);", dfrows)
        total_rows = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if total_rows == 0:
            return {"ok": False, "error": "Files produced no text chunks."}
        _write_meta(con, {"fingerprint": fingerprint, "folder": folder, "provider": PROVIDER,
                          "model": MODEL_NAME, "dim": str(dim), "docs": str(len(docs)),
                          "total": str(total_rows), "truncated": "1" if truncated else "0",
                          "is_file": "1" if is_file else "0", "status": "building",
                          "built_at": str(int(time.time()))})

    # Fill un-embedded chunks in small batches (device-sized). Progress is read
    # from the DB each pass, so /status reflects real progress even after a restart.
    with _DB_LOCK:
        total = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    batch_n = 4 if DEVICE == "cpu" else 32
    cast = "?::FLOAT[" + str(dim) + "]"
    while True:
        with _DB_LOCK:
            done = con.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
            pending = con.execute("SELECT id, content FROM chunks WHERE embedding IS NULL "
                                  "ORDER BY id LIMIT ?", [batch_n]).fetchall()
        if progress:
            progress(done, total)
        if not pending:
            break
        vecs = embed_docs([c for _id, c in pending], batch_size=batch_n)
        with _DB_LOCK:
            for (cid, _content), vec in zip(pending, vecs):
                con.execute("UPDATE chunks SET embedding = " + cast + " WHERE id = ?", [vec.tolist(), cid])

    with _DB_LOCK:
        m = rc.read_meta(con) or {}
        if m.get("status") != "ready":
            # DuckDB VSS can't update HNSW in place, so a chunk-set change needs a full
            # rebuild — but only then. An unchanged/resumed build reuses the index (and
            # builds it only if it's somehow missing). Huge sets stay brute-force.
            if total <= 50000 and (structural or not _has_hnsw(con)):
                con.execute("DROP INDEX IF EXISTS chunks_hnsw;")
                con.execute("CREATE INDEX chunks_hnsw ON chunks USING HNSW (embedding) WITH (metric = 'cosine');")
            con.execute("UPDATE docmeta SET value = 'ready' WHERE key = 'status';")
    return _summary(False, folder, is_file, m.get("truncated") == "1",
                    docs=int(m.get("docs", len(docs))), chunks=total, dim=dim)


def _summary(cached, folder, is_file, truncated, docs, chunks, dim):
    return {"ok": True, "cached": cached, "folder": folder.replace(os.sep, "/"),
            "source_name": _source_name(folder), "is_file": is_file, "truncated": truncated,
            "docs": docs, "chunks": chunks, "dim": dim, "provider": PROVIDER, "model": MODEL_NAME}


def search_index(folder, q, k=5, cache_dir=None):
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "empty query"}
    folder = rc.normalize_path(folder) or rc.DEFAULT_DOCS
    db_path = rc.db_path_for(folder, PROVIDER, cache_dir)
    if not os.path.exists(db_path):
        return {"ok": False, "error": "not_indexed"}
    con = _con(db_path)
    meta = _meta(con)
    if not meta:
        return {"ok": False, "error": "not_indexed"}
    dim = int(meta.get("dim", DIM))
    qvec = embed_query(q)
    sql = ("SELECT source, chunk_index, content, "
           "array_cosine_similarity(embedding, ?::FLOAT[" + str(dim) + "]) AS score "
           "FROM chunks WHERE embedding IS NOT NULL "
           "ORDER BY array_cosine_distance(embedding, ?::FLOAT[" + str(dim) + "]) LIMIT ?;")
    with _DB_LOCK:
        rows = con.execute(sql, [qvec, qvec, int(k)]).fetchall()
    results = [{"source": r[0], "chunk_index": int(r[1]), "chunk": r[2],
                "score": round(float(r[3]), 4)} for r in rows]
    return {"ok": True, "q": q, "results": results}


def index_status(folder, cache_dir=None):
    folder = rc.normalize_path(folder) or rc.DEFAULT_DOCS
    out = {"ok": True, "folder": folder.replace(os.sep, "/"),
           "home": os.path.expanduser("~").replace(os.sep, "/"),
           "cache_dir": (cache_dir or rc.INDEX_DIR).replace(os.sep, "/"),
           "source_name": _source_name(folder), "is_file": os.path.isfile(folder),
           "state": "none", "docs": 0, "chunks": 0, "done": 0, "total": 0, "truncated": False,
           "ignored": []}
    if not os.path.exists(folder):
        out["state"] = "missing"
        return out
    if os.path.isdir(folder):
        out["ignored"] = rc.top_level_ignored(folder)
    with _BUILD_LOCK:
        b = _BUILDS.get(_build_key(folder, cache_dir))
    if b and b.get("state") == "building":
        out.update(state="indexing", done=b["done"], total=b["total"])
        return out
    if b and b.get("state") == "error":
        out.update(state="error", error=b.get("error", "build failed"))
        return out
    db_path = rc.db_path_for(folder, PROVIDER, cache_dir)
    if not os.path.exists(db_path):
        return out
    try:
        con = _con(db_path)
        meta = _meta(con)
        if meta:
            with _DB_LOCK:
                total = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
                done = con.execute("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
            out.update(state=meta.get("status", "building"), docs=int(meta.get("docs", 0)),
                       chunks=total, done=done, total=total,
                       truncated=meta.get("truncated") == "1", is_file=meta.get("is_file") == "1")
    except Exception:
        pass
    return out


def _fwd(p):
    return p.replace(os.sep, "/") if p else p


def browse(path):
    """Directory listing for the folder picker (filesystem only; served over
    fetch so the picker needs no per-navigation runPython subprocess)."""
    path = rc.normalize_path(path) or rc.DEFAULT_DOCS
    if not os.path.exists(path):
        return {"ok": False, "error": "Not found: " + _fwd(path)}
    selected = None
    if os.path.isfile(path):
        selected = _fwd(path)
        path = os.path.dirname(path)
    try:
        dirs, files = rc.list_dir(path)
    except (PermissionError, OSError) as e:
        return {"ok": False, "error": "Cannot open " + _fwd(path) + " (" + type(e).__name__ + ")"}
    parent = os.path.dirname(path)
    n_files, capped = rc.count_indexable(path)
    return {"ok": True, "path": _fwd(path),
            "parent": _fwd(parent) if parent and parent != path else None,
            "home": _fwd(os.path.expanduser("~")),
            "dirs": [{"name": d["name"], "path": _fwd(d["path"])} for d in dirs],
            "files": [{"name": f["name"], "path": _fwd(f["path"])} for f in files],
            "selected": selected, "indexable": n_files, "indexable_capped": capped}


def index_files(folder, cache_dir=None, limit=4000):
    """The files actually stored in the index (for the 'what got indexed' list),
    each with its chunk count. Read straight from the DB, so it reflects the
    index, not a fresh disk walk."""
    folder = rc.normalize_path(folder) or rc.DEFAULT_DOCS
    db_path = rc.db_path_for(folder, PROVIDER, cache_dir)
    if not os.path.exists(db_path):
        return {"ok": False, "error": "not_indexed"}
    con = _con(db_path)
    if not _meta(con):
        return {"ok": False, "error": "not_indexed"}
    with _DB_LOCK:
        total = con.execute("SELECT count(DISTINCT source) FROM chunks").fetchone()[0]
        rows = con.execute("SELECT source, count(*) FROM chunks GROUP BY source "
                           "ORDER BY source LIMIT ?", [int(limit)]).fetchall()
    return {"ok": True, "folder": folder.replace(os.sep, "/"),
            "files": [{"source": r[0], "chunks": int(r[1])} for r in rows],
            "total": int(total), "capped": int(total) > len(rows)}


def file_preview(folder, source, cache_dir=None, max_chars=200000):
    """One indexed file's text for the preview pane. Reads it off disk (folder +
    source); if the file is gone, reassembles the stored chunks so the pane still
    shows what was embedded."""
    folder = rc.normalize_path(folder) or rc.DEFAULT_DOCS
    if os.path.isfile(folder):
        path = folder
    else:
        # `source` is a folder-relative path from the index; contain it so an
        # absolute or ../ source can't be joined into an arbitrary file read.
        # Checked on the REALPATH (symlinks/junctions resolved), not just the
        # lexical join — a symlink inside `folder` whose target resolves
        # elsewhere would otherwise pass a purely lexical containment check.
        root = os.path.normpath(folder)
        path = os.path.normpath(os.path.join(root, source))
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
        try:
            contained = os.path.commonpath([real_root, real_path]) == real_root
        except ValueError:
            contained = False              # different drive (Windows) -> outside
        if not contained:
            return {"ok": False, "error": "invalid source"}
    text = rc._read_text(path, force=True) if os.path.isfile(path) else None
    chunks = None
    db_path = rc.db_path_for(folder, PROVIDER, cache_dir)
    if os.path.exists(db_path):
        con = _con(db_path)
        with _DB_LOCK:
            rows = con.execute("SELECT content FROM chunks WHERE source = ? ORDER BY chunk_index",
                               [source]).fetchall()
        chunks = len(rows)
        if text is None and rows:
            text = "\n\n".join(r[0] for r in rows)
    if text is None:
        return {"ok": False, "error": "not_readable"}
    return {"ok": True, "source": source, "path": _fwd(path), "chunks": chunks,
            "bytes": len(text.encode("utf-8")), "truncated": len(text) > max_chars, "text": text[:max_chars]}


def move_cache(old_dir, new_dir):
    """Relocate the embedding cache: move every *.duckdb index from `old_dir` to
    `new_dir` so changing the save location keeps the existing indexes (no
    re-embedding). Warm DuckDB handles rooted in old_dir are closed first —
    Windows won't move a file that's still open."""
    import shutil
    old = rc.normalize_path(old_dir) if old_dir else rc.INDEX_DIR
    new = rc.normalize_path(new_dir) if new_dir else rc.INDEX_DIR
    if not new:
        return {"ok": False, "error": "empty destination path"}
    if os.path.normcase(old) == os.path.normcase(new):
        return {"ok": True, "moved": 0, "same": True, "from": _fwd(old), "to": _fwd(new)}
    with _BUILD_LOCK:                       # never yank a file out from under a running build
        if any(b.get("state") == "building" for b in _BUILDS.values()):
            return {"ok": False, "error": "An index is still building — wait for it to finish, then move the cache."}
    try:
        os.makedirs(new, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": "cannot create " + _fwd(new) + " (" + type(e).__name__ + ")"}
    with _DB_LOCK:
        for p in list(_CONS):
            if os.path.normcase(os.path.dirname(p)) == os.path.normcase(old):
                try:
                    _CONS.pop(p).close()
                except Exception:
                    pass
    moved = 0
    if os.path.isdir(old):
        for name in sorted(os.listdir(old)):
            if not (name.endswith(".duckdb") or name.endswith(".duckdb.wal")):
                continue
            try:
                dst = os.path.join(new, name)
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(os.path.join(old, name), dst)
                if name.endswith(".duckdb"):
                    moved += 1
            except OSError:
                pass
    return {"ok": True, "moved": moved, "from": _fwd(old), "to": _fwd(new)}


def start_build(folder, rebuild=False, cache_dir=None):
    """Kick a build off in a background thread; /status polls the progress."""
    folder = rc.normalize_path(folder) or rc.DEFAULT_DOCS
    key = _build_key(folder, cache_dir)
    with _BUILD_LOCK:
        b = _BUILDS.get(key)
        if b and b.get("state") == "building":
            return {"ok": True, "already": True}
        _BUILDS[key] = {"state": "building", "done": 0, "total": 0, "docs": 0}

    def _run():
        def prog(done, total):
            with _BUILD_LOCK:
                _BUILDS[key].update(done=done, total=total)
        try:
            res = build_index(folder, rebuild=rebuild, progress=prog, cache_dir=cache_dir)
            with _BUILD_LOCK:
                _BUILDS[key] = {"state": "done" if res.get("ok") else "error",
                                   "done": res.get("chunks", 0), "total": res.get("chunks", 0),
                                   "docs": res.get("docs", 0), "error": res.get("error", "")}
        except Exception as e:
            with _BUILD_LOCK:
                _BUILDS[key] = {"state": "error", "done": 0, "total": 0, "error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True}


# --------------------------------------------------------------------------- #
# HTTP layer (stdlib, CORS-enabled, single free port written to the sidecar)
# --------------------------------------------------------------------------- #

def _health():
    return {"ok": True, "ready": READY, "stage": STAGE, "model": MODEL_NAME, "provider": PROVIDER,
            "device": DEVICE, "dim": DIM, "cache_dir": rc.INDEX_DIR.replace(os.sep, "/"),
            "models_dir": MODELS_DIR.replace(os.sep, "/")}


def _port_taken(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def run_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs

    # Singleton guard: if a server already holds the port, vanish immediately
    # (os._exit, not return — a plain return was leaving a zombie that fought
    # over the DuckDB files). The exclusive bind below is the race-safe backstop.
    if _port_taken(PORT):
        os._exit(0)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send({"ok": True})

        def _authed(self, qs):
            return qs.get("t", [""])[0] == TOKEN

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/health":            # bootstrap only — carries no token itself
                self._send(_health())
                return
            if not self._authed(qs):
                self._send({"ok": False, "error": "forbidden"}, 403)
                return
            if u.path == "/status":
                self._send(index_status(qs.get("folder", [""])[0], qs.get("cache_dir", [""])[0] or None))
            elif u.path == "/browse":
                self._send(browse(qs.get("path", [""])[0]))
            elif u.path == "/cached":
                m = qs.get("model", [""])[0]
                self._send({"ok": True, "model": m, "cached": bool(m) and _model_cached(m)})
            elif u.path == "/files":
                self._send(index_files(qs.get("folder", [""])[0], qs.get("cache_dir", [""])[0] or None))
            elif u.path == "/file":
                self._send(file_preview(qs.get("folder", [""])[0], qs.get("source", [""])[0],
                                        qs.get("cache_dir", [""])[0] or None))
            else:
                self._send({"ok": False, "error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authed(parse_qs(u.query)):
                self._send({"ok": False, "error": "forbidden"}, 403)
                return
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            if u.path == "/movecache":     # filesystem-only; works even while the model loads
                self._send(move_cache(body.get("old", ""), body.get("new", "")))
                return
            if not READY:
                self._send({"ok": False, "error": "loading", "ready": False}, 503)
                return
            if u.path == "/index":
                self._send(start_build(body.get("folder", ""), bool(body.get("rebuild")), body.get("cache_dir") or None))
            elif u.path == "/search":
                self._send(search_index(body.get("folder", ""), body.get("q", ""), int(body.get("k", 5)), body.get("cache_dir") or None))
            else:
                self._send({"ok": False, "error": "not found"}, 404)

    class Server(ThreadingHTTPServer):
        # Truly exclusive bind. On Windows, plain bind (even without SO_REUSEADDR)
        # lets a 2nd process share the port — so duplicate servers would both come
        # up and fight over the DuckDB files. SO_EXCLUSIVEADDRUSE makes the 2nd
        # bind fail (-> os._exit below), so exactly one server survives a race.
        allow_reuse_address = False

        def server_bind(self):
            import socket as _sock
            if hasattr(_sock, "SO_EXCLUSIVEADDRUSE"):
                try:
                    self.socket.setsockopt(_sock.SOL_SOCKET, _sock.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            super().server_bind()

    try:
        httpd = Server(("127.0.0.1", PORT), H)
    except OSError:
        os._exit(0)                    # lost the bind race — another instance owns the port
    _write_sidecar(PORT, ready=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        get_model()                    # blocking: downloads on first run, then warm
        _write_sidecar(PORT, ready=True)
    except Exception as e:
        _write_sidecar(PORT, ready=False, error=str(e))
        raise
    while True:
        time.sleep(3600)


def _write_sidecar(port, ready, error=""):
    data = {"port": port, "pid": os.getpid(), "token": TOKEN, "model": MODEL_NAME, "provider": PROVIDER,
            "device": DEVICE, "dim": DIM, "ready": ready, "error": error}
    try:
        with open(SIDECAR, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


if __name__ == "__main__":
    run_server()
