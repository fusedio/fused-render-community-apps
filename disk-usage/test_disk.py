"""Tests for disk.py's Windows backend: index-backed scan, preview, delete.

Run:  uv run --with duckdb --with pytest pytest test_disk.py -v -s

The scan tests read the real fused-render file index and are skipped when no
index has been built on this machine. `-s` shows the timing print-outs.
"""
import os
import shutil
import time

import pytest

import disk

WIN = os.name == "nt"
win_only = pytest.mark.skipif(not WIN, reason="Windows backend")


def test_win_path_normalises_slash_and_quotes():
    # breadcrumb hrefs ("/C:/Users/…") resolve drive-relative unless the leading
    # slash is dropped; the path box often holds a quoted Explorer path.
    assert disk._win_path("/C:/Users/Admin/Desktop") == "C:/Users/Admin/Desktop"
    assert disk._win_path("C:/Users/Admin") == "C:/Users/Admin"   # already clean
    assert disk._win_path("//D:/data") == "D:/data"
    assert disk._win_path("/") == "/"                             # root, no drive
    assert disk._win_path('"C:\\work\\fused"') == "C:\\work\\fused"      # double-quoted (Explorer)
    assert disk._win_path('  "C:\\work"  ') == "C:\\work"               # padded + double-quoted
    # apostrophes are legal in Windows names, so they must survive untouched
    assert disk._win_path("C:/Users/Admin/O'Brien") == "C:/Users/Admin/O'Brien"
    assert disk._win_path("'C:/x'") == "'C:/x'"


@pytest.fixture(scope="module")
def home():
    if not WIN:
        pytest.skip("Windows backend")
    if disk._index_connect() is None:
        pytest.skip("no fused-render file index on this machine")
    return os.path.expanduser("~")


@win_only
def test_scan_is_fast_on_a_huge_tree(home):
    # The whole reason scan moved to the index: walking a tree this big with a
    # du-style scandir blew past the 60s runPython timeout. It must not anymore.
    t = time.perf_counter()
    r = disk.main(action="scan", path=home)
    dt = time.perf_counter() - t
    print(f"\nscan {home}: {len(r.get('children', []))} children in {dt * 1000:.0f} ms")
    assert "error" not in r, r
    assert dt < 15, f"scan took {dt:.1f}s — should be far under the 60s timeout"


@win_only
def test_scan_result_shape(home):
    r = disk.main(action="scan", path=home)
    assert "error" not in r, r
    assert r["path"].startswith("C:/") and "\\" not in r["path"]        # POSIX-style
    assert set(r) >= {"path", "total", "children", "truncated", "disk"}
    assert r["children"] == sorted(r["children"], key=lambda c: -c["size"])
    assert len(r["children"]) <= 400
    for c in r["children"]:
        assert set(c) == {"name", "path", "size", "dir"}
        assert "\\" not in c["path"] and c["size"] >= 0


@win_only
def test_breadcrumb_path_resolves_same_as_clean(home):
    # The leading-slash bug: "/C:/Users" must give the same result as "C:/Users".
    clean = disk.main(action="scan", path="C:/Users")
    crumb = disk.main(action="scan", path="/C:/Users")
    assert "error" not in clean and "error" not in crumb
    assert clean["path"] == crumb["path"] == "C:/Users"


@win_only
def test_quoted_explorer_path_resolves(home):
    # A path pasted from Explorer arrives quoted with backslashes, e.g.
    # "C:\Users\Admin" — it must scan the same folder as the bare path.
    bare = disk.main(action="scan", path=home)
    quoted = disk.main(action="scan", path=f'"{home}"')
    assert "error" not in quoted
    assert quoted["path"] == bare["path"]


@win_only
def test_uncovered_path_reports_clearly(home):
    # C:/ root is not a scanned index root — say so, don't hang or return empty.
    r = disk.main(action="scan", path="C:/")
    assert "error" in r and "index" in r["error"].lower()


@win_only
def test_preview_dir_and_file(tmp_path):
    d = disk.main(action="preview", path=str(tmp_path))
    assert d["dir"] is True and "entry_count" in d and "\\" not in d["path"]

    f = tmp_path / "note.txt"
    f.write_text("hello " * 20, encoding="utf-8")
    pv = disk.main(action="preview", path=str(f))
    assert pv["dir"] is False
    assert pv["size"] == f.stat().st_size
    assert pv["head"].startswith("hello")


@win_only
def test_delete_refuses_protected_and_shallow():
    assert "error" in disk.main(action="delete", path="~")
    assert "error" in disk.main(action="delete", path="C:/foo")
    assert "error" in disk.main(action="delete", path=os.path.expanduser("~/Desktop"))


@win_only
def test_delete_moves_to_trash():
    root = os.path.join(os.path.expanduser("~"), "disktest_victim")
    victim = os.path.join(root, "a", "b", "gone.txt")
    os.makedirs(os.path.dirname(victim), exist_ok=True)
    with open(victim, "w", encoding="utf-8") as fh:
        fh.write("x" * 500)
    res = disk.main(action="delete", path=victim)
    try:
        assert res.get("ok") and res["freed"] == 500
        assert not os.path.exists(victim)                       # gone from source
        assert os.path.exists(res["trashed_to"].replace("/", os.sep))  # in trash
    finally:
        shutil.rmtree(root, ignore_errors=True)
        try:
            os.remove(res["trashed_to"].replace("/", os.sep))
        except OSError:
            pass
