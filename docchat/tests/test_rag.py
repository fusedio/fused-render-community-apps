"""Tests for the DocChat RAG pipeline.

Run against a small fast model so they're quick, but they exercise the real code
path: chunk -> embed (sentence-transformers) -> DuckDB + HNSW -> cosine search.
The key regression they guard is the one that made the old app slow: an unchanged
folder must NOT re-embed (build_index returns cached=True), and a question is a
pure search — no re-chunk.

    uv run --no-project --with sentence-transformers --with duckdb --with numpy \
        --with pytest pytest tests/ -q
"""

import os
import shutil
import sys
import threading
import time

import pytest

os.environ.setdefault("RAG_MODEL", "sentence-transformers/all-MiniLM-L6-v2")  # small + no license gate
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # app root (parent of tests/)

import rag_common as rc
import ragserver
import serve


def test_chunk_text_boundaries():
    assert rc.chunk_text("") == []
    assert rc.chunk_text("short text") == ["short text"]
    big = " ".join("word%d" % i for i in range(400))
    chunks = rc.chunk_text(big, size=200, overlap=40)
    assert len(chunks) > 1
    assert all(len(c) <= 260 for c in chunks)          # size + boundary slack
    assert "".join(chunks).replace(" ", "").count("word0") == 1


def test_fingerprint_reacts_to_edits(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("hello", encoding="utf-8")
    fp1 = rc.docs_fingerprint(rc.collect_docs(str(tmp_path))[0])
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    fp2 = rc.docs_fingerprint(rc.collect_docs(str(tmp_path))[0])
    assert fp1 != fp2


def test_fingerprint_reacts_to_content_change_with_preserved_mtime(tmp_path):
    # A tool that restores the original mtime after editing (zip/tar with -p,
    # robocopy /COPY:DAT, rsync -a, a cloud-sync client) must not fool the
    # fingerprint into thinking nothing changed.
    f = tmp_path / "a.md"
    f.write_text("hello", encoding="utf-8")
    original_mtime = f.stat().st_mtime
    fp1 = rc.docs_fingerprint(rc.collect_docs(str(tmp_path))[0])

    f.write_text("goodbye", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, original_mtime))
    fp2 = rc.docs_fingerprint(rc.collect_docs(str(tmp_path))[0])

    assert fp1 != fp2


def test_incremental_reembeds_content_change_with_preserved_mtime(tmp_path, monkeypatch):
    f = tmp_path / "a.md"
    f.write_text("Alpha document about grinders and burrs.", encoding="utf-8")
    ragserver.build_index(str(tmp_path))
    original_mtime = f.stat().st_mtime

    calls = []
    real = ragserver.embed_docs
    monkeypatch.setattr(ragserver, "embed_docs",
                        lambda texts, batch_size=64: (calls.extend(list(texts)), real(texts, batch_size=batch_size))[1])

    f.write_text("Alpha document now about milk steaming and microfoam.", encoding="utf-8")
    os.utime(f, (f.stat().st_atime, original_mtime))   # a tool that restores the original mtime

    r = ragserver.build_index(str(tmp_path))
    assert r["ok"] and r["cached"] is False
    assert any("microfoam" in c for c in calls)          # re-embedded despite the unchanged mtime

    s = ragserver.search_index(str(tmp_path), "microfoam steaming", k=1)
    assert s["ok"] and "microfoam" in s["results"][0]["chunk"]


def test_skips_hidden_files(tmp_path):
    (tmp_path / "visible.md").write_text("The grinder burrs need weekly cleaning.", encoding="utf-8")
    (tmp_path / ".secret.md").write_text("hidden note that must not be indexed", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=super-secret", encoding="utf-8")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "inside.md").write_text("also hidden", encoding="utf-8")
    items, _ = rc.read_docs(str(tmp_path))
    assert [n for n, _m, _t in items] == ["visible.md"]   # dotfiles and dot-dirs excluded
    n, _ = rc.count_indexable(str(tmp_path))
    assert n == 1


def test_build_search_and_cache(tmp_path):
    (tmp_path / "espresso.md").write_text(
        "Espresso recipe: dose 18 grams in, 36 grams out, about 27 seconds, medium-fine grind.",
        encoding="utf-8")
    (tmp_path / "beans.md").write_text(
        "Store roasted coffee beans in an airtight, opaque container away from heat, light and "
        "moisture. They stay freshest for three to four weeks after the roast date.",
        encoding="utf-8")

    r = ragserver.build_index(str(tmp_path))
    assert r["ok"] is True
    assert r["cached"] is False
    assert r["chunks"] >= 2
    assert r["dim"] == 384                              # all-MiniLM-L6-v2

    # retrieval lands on the right file for each intent
    s = ragserver.search_index(str(tmp_path), "how do I keep my beans fresh?", k=2)
    assert s["ok"] and s["results"]
    assert s["results"][0]["source"] == "beans.md"
    assert 0.0 <= s["results"][0]["score"] <= 1.0

    s2 = ragserver.search_index(str(tmp_path), "what is the shot dose and yield?", k=2)
    assert s2["results"][0]["source"] == "espresso.md"

    # THE regression guard: an unchanged folder is served from cache, not re-embedded
    r2 = ragserver.build_index(str(tmp_path))
    assert r2["cached"] is True
    assert r2["chunks"] == r["chunks"]


def test_incremental_only_reembeds_changed(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("Alpha document about grinders and burrs.", encoding="utf-8")
    (tmp_path / "b.md").write_text("Beta document about milk.", encoding="utf-8")
    ragserver.build_index(str(tmp_path))

    calls = []                                             # spy on what actually gets embedded
    real = ragserver.embed_docs
    monkeypatch.setattr(ragserver, "embed_docs",
                        lambda texts, batch_size=64: (calls.extend(list(texts)), real(texts, batch_size=batch_size))[1])

    (tmp_path / "b.md").write_text("Beta now about microfoam steaming.", encoding="utf-8")
    os.utime(tmp_path / "b.md", (time.time() + 10, time.time() + 10))
    (tmp_path / "c.md").write_text("Gamma document about tamping pressure.", encoding="utf-8")

    r = ragserver.build_index(str(tmp_path))
    assert r["ok"] and r["cached"] is False
    joined = " ".join(calls)
    assert "microfoam" in joined and "tamping" in joined   # b (changed) + c (new) were embedded
    assert "grinders" not in joined                        # a (unchanged) was NOT re-embedded

    assert ragserver.search_index(str(tmp_path), "grinders", k=1)["results"][0]["source"] == "a.md"

    (tmp_path / "a.md").unlink()                            # removal is reconciled too
    ragserver.build_index(str(tmp_path))
    names = [f["source"] for f in ragserver.index_files(str(tmp_path))["files"]]
    assert names == ["b.md", "c.md"]


def test_top_level_ignored(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "readme.md").write_text("hi", encoding="utf-8")
    ig = rc.top_level_ignored(str(tmp_path))
    assert ".git" in ig and "node_modules" in ig and "src" not in ig
    assert set(ragserver.index_status(str(tmp_path))["ignored"]) >= {".git", "node_modules"}


def test_status_lifecycle(tmp_path):
    (tmp_path / "notes.txt").write_text("DuckDB stores the vectors and an HNSW index serves search.",
                                        encoding="utf-8")
    assert ragserver.index_status(str(tmp_path))["state"] == "none"
    ragserver.build_index(str(tmp_path))
    st = ragserver.index_status(str(tmp_path))
    assert st["state"] == "ready"
    assert st["chunks"] >= 1
    assert st["is_file"] is False


def test_browse_lists_dirs_and_files(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n")     # non-text -> excluded
    r = ragserver.browse(str(tmp_path))
    assert r["ok"] is True
    assert any(d["name"] == "sub" for d in r["dirs"])
    assert [f["name"] for f in r["files"]] == ["a.md"]     # png filtered out
    assert r["home"]
    # pointing at a file returns its parent listing + marks the file selected
    r2 = ragserver.browse(str(tmp_path / "a.md"))
    assert r2["selected"].endswith("/a.md")
    assert r2["path"] == str(tmp_path).replace("\\", "/")


def test_list_dir_keeps_dirs_past_file_cap(tmp_path):
    for i in range(305):
        (tmp_path / ("a%04d.txt" % i)).write_text("x", encoding="utf-8")
    (tmp_path / "zzz_subdir").mkdir()                      # sorts after every file
    dirs, files = rc.list_dir(str(tmp_path))
    assert len(files) == 300                               # file cap still applies
    assert [d["name"] for d in dirs] == ["zzz_subdir"]     # dirs past the cap still listed


def test_hnsw_restored_when_meta_still_says_ready(tmp_path):
    (tmp_path / "a.md").write_text("Alpha document about grinders and burrs.", encoding="utf-8")
    ragserver.build_index(str(tmp_path))
    con = ragserver._con(rc.db_path_for(str(tmp_path), ragserver.PROVIDER))
    assert ragserver._has_hnsw(con)

    # A kill (e.g. serve.py's model switch) can land between the reconcile's DROP
    # INDEX auto-committing and the meta rewrite: the HNSW index is gone while
    # docmeta still says status=ready with the old fingerprint.
    con.execute("DROP INDEX chunks_hnsw;")
    assert not ragserver._has_hnsw(con)

    r = ragserver.build_index(str(tmp_path))               # unchanged folder, rebuild=False
    assert r["ok"] is True
    assert ragserver._has_hnsw(con)                        # fast path must not trust stale meta


def _make_dir_link(link, target):
    """Create a directory symlink/junction without needing admin/Developer Mode
    (a Windows junction needs neither; a POSIX symlink needs no privilege at
    all). Returns True on success, False if this machine can't make one."""
    if os.name == "nt":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
        return r.returncode == 0
    try:
        os.symlink(str(target), str(link))
        return True
    except OSError:
        return False


def test_file_preview_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "secret.txt").write_text("SECRET outside content", encoding="utf-8")

    docs = tmp_path / "docs"; docs.mkdir()
    link = docs / "linked"
    if not _make_dir_link(link, outside):
        pytest.skip("could not create a directory symlink/junction on this machine")

    r = ragserver.file_preview(str(docs), "linked/secret.txt")
    assert r["ok"] is False


def test_index_files_and_preview(tmp_path):
    (tmp_path / "a.md").write_text("Alpha document about grinders and burrs.", encoding="utf-8")
    (tmp_path / "b.md").write_text("Beta document about milk steaming and microfoam.", encoding="utf-8")
    ragserver.build_index(str(tmp_path))

    lst = ragserver.index_files(str(tmp_path))
    assert lst["ok"] and lst["total"] == 2 and lst["capped"] is False
    names = [f["source"] for f in lst["files"]]
    assert names == ["a.md", "b.md"]                       # sorted
    assert all(f["chunks"] >= 1 for f in lst["files"])

    pv = ragserver.file_preview(str(tmp_path), "b.md")
    assert pv["ok"] and "microfoam" in pv["text"] and pv["chunks"] >= 1
    assert pv["path"].endswith("/b.md")

    assert ragserver.index_files(str(tmp_path / "nope"))["error"] == "not_indexed"


class _FakeThread:
    """Stand-in for threading.Thread that never actually runs the build, so a
    test can inspect _BUILDS mid-"build" deterministically without waiting on
    or mocking real embedding work."""
    def __init__(self, target=None, daemon=None):
        pass

    def start(self):
        pass


def test_start_build_keys_by_folder_and_cache_dir(tmp_path, monkeypatch):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.md").write_text("hi", encoding="utf-8")
    a = str(tmp_path / "cacheA")
    b = str(tmp_path / "cacheB")

    monkeypatch.setattr(ragserver.threading, "Thread", _FakeThread)

    key_a = ragserver._build_key(str(docs), a)
    key_b = ragserver._build_key(str(docs), b)
    try:
        r1 = ragserver.start_build(str(docs), cache_dir=a)
        assert r1["ok"] is True and r1.get("started") is True

        # A build "in progress" under cache_dir A must not block one under cache_dir
        # B for the SAME folder -- they're different index identities.
        r2 = ragserver.start_build(str(docs), cache_dir=b)
        assert r2["ok"] is True
        assert r2.get("already") is not True

        assert ragserver.index_status(str(docs), cache_dir=a)["state"] == "indexing"
        assert ragserver.index_status(str(docs), cache_dir=b)["state"] == "indexing"
    finally:
        # The fake thread never runs _run(), so these entries would otherwise
        # sit "building" forever in the shared, process-wide _BUILDS dict and
        # trip move_cache's "is anything building?" guard in later tests.
        ragserver._BUILDS.pop(key_a, None)
        ragserver._BUILDS.pop(key_b, None)


def test_move_cache_reports_failed_files_instead_of_silent_success(tmp_path, monkeypatch):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.md").write_text("hi", encoding="utf-8")
    a = tmp_path / "cacheA"; b = tmp_path / "cacheB"
    ragserver.build_index(str(docs), cache_dir=str(a))

    def flaky_move(src, dst):
        raise OSError("simulated: file locked")
    monkeypatch.setattr(shutil, "move", flaky_move)

    r = ragserver.move_cache(str(a), str(b))
    assert r["ok"] is False
    assert r["moved"] == 0
    assert os.listdir(a) != []   # the file that failed to move is still where it was


def test_move_cache_holds_build_lock_for_the_whole_operation(tmp_path, monkeypatch):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "a.md").write_text("hi", encoding="utf-8")
    a = tmp_path / "cacheA"; b = tmp_path / "cacheB"
    ragserver.build_index(str(docs), cache_dir=str(a))

    entered = threading.Event()
    release = threading.Event()

    def slow_move(src, dst):
        entered.set()
        assert release.wait(5), "test setup itself timed out"
    monkeypatch.setattr(shutil, "move", slow_move)

    t = threading.Thread(target=ragserver.move_cache, args=(str(a), str(b)), daemon=True)
    t.start()
    assert entered.wait(5), "move_cache never reached the move step"

    # _BUILD_LOCK must still be held by move_cache at this exact point -- a
    # concurrent start_build() for the same folder must not be able to touch
    # the .duckdb files mid-move.
    got_lock = ragserver._BUILD_LOCK.acquire(blocking=False)
    if got_lock:
        ragserver._BUILD_LOCK.release()

    release.set()
    t.join(5)
    assert got_lock is False


def test_move_cache_preserves_index(tmp_path):
    docs = tmp_path / "docs"; docs.mkdir()
    (docs / "grind.md").write_text("Dial the grinder finer to slow the shot down.", encoding="utf-8")
    a = tmp_path / "cacheA"; b = tmp_path / "cacheB"

    r = ragserver.build_index(str(docs), cache_dir=str(a))
    assert r["ok"] and r["cached"] is False
    dbs = [p for p in os.listdir(a) if p.endswith(".duckdb")]
    assert dbs                                             # index landed in A

    mv = ragserver.move_cache(str(a), str(b))
    assert mv["ok"] and mv["moved"] == 1
    assert os.listdir(a) == []                             # A emptied of indexes
    assert os.path.exists(os.path.join(str(b), dbs[0]))    # same file now in B

    # served from B without re-embedding, and searchable there
    r2 = ragserver.build_index(str(docs), cache_dir=str(b))
    assert r2["cached"] is True
    s = ragserver.search_index(str(docs), "how do I slow the shot?", k=1, cache_dir=str(b))
    assert s["ok"] and s["results"][0]["source"] == "grind.md"

    # moving onto the same directory is a no-op, not an error
    same = ragserver.move_cache(str(b), str(b))
    assert same["ok"] and same["same"] is True and same["moved"] == 0


def test_parse_int_helper():
    # Present-and-absent are the ONLY two cases callers should ever hit; a
    # present-but-invalid value must yield None so the caller can send a clean
    # error instead of letting int(...) raise mid-request.
    assert ragserver._parse_int(5) == 5              # k omitted -> default of 5 passed straight through
    assert ragserver._parse_int("5") == 5            # numeric string, still fine
    assert ragserver._parse_int("all") is None       # malformed body -> caller sends 400
    assert ragserver._parse_int([1, 2]) is None
    assert ragserver._parse_int(None) is None


def test_single_file_source(tmp_path):
    f = tmp_path / "solo.md"
    f.write_text("The closing checklist: wipe the group heads, empty the drip tray, lock the safe.",
                 encoding="utf-8")
    r = ragserver.build_index(str(f))
    assert r["ok"] and r["is_file"] is True and r["chunks"] >= 1
    s = ragserver.search_index(str(f), "what do I do when closing?", k=1)
    assert s["results"][0]["source"] == "solo.md"
