"""Tests for serve.py's spawn-lock: staleness must be PID-liveness based, not
just wall-clock age, so a slow-but-alive spawn (Windows AV scanning a fresh
python.exe, importing torch/sentence-transformers) is never mistaken for a
crashed one and reclaimed out from under it.

    uv run --no-project --with sentence-transformers --with duckdb --with numpy \
        --with pytest pytest tests/ -q
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # app root (parent of tests/)

import serve

DEAD_PID = 999999999   # astronomically unlikely to be a live PID on any machine


def test_pid_alive_true_for_self():
    assert serve._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_dead_pid():
    assert serve._pid_alive(DEAD_PID) is False


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path, monkeypatch):
    lock = tmp_path / ".spawn"
    lock.write_text(str(DEAD_PID), encoding="utf-8")
    os.utime(lock, (time.time() - 120, time.time() - 120))
    monkeypatch.setattr(serve, "SPAWN_LOCK", str(lock))

    fd = serve._acquire_spawn_lock()
    assert fd is not None
    serve._release_spawn_lock(fd)


def test_slow_but_alive_holder_is_not_reclaimed(tmp_path, monkeypatch):
    lock = tmp_path / ".spawn"
    lock.write_text(str(os.getpid()), encoding="utf-8")   # the holder (us) is alive
    os.utime(lock, (time.time() - 120, time.time() - 120))   # but the lock looks old
    monkeypatch.setattr(serve, "SPAWN_LOCK", str(lock))

    fd = serve._acquire_spawn_lock()
    assert fd is None   # must NOT reclaim a live holder's lock just because it's old


def test_release_does_not_remove_a_lock_someone_else_now_owns(tmp_path, monkeypatch):
    lock = tmp_path / ".spawn"
    monkeypatch.setattr(serve, "SPAWN_LOCK", str(lock))

    fd = serve._acquire_spawn_lock()
    assert fd is not None
    lock.write_text("999999998", encoding="utf-8")   # another process reclaimed this path

    serve._release_spawn_lock(fd)
    assert lock.read_text(encoding="utf-8").strip() == "999999998"   # not deleted out from under them
