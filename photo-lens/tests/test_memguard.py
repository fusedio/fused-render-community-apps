import platform

import pytest

from lens import memguard


def test_peak_rss_gb_corrects_the_linux_kb_unit(monkeypatch):
    """ru_maxrss is bytes on macOS/BSD, kilobytes on Linux. The same raw
    number must read as 1000x more GB on the platform that reports KB."""
    class FakeUsage:
        ru_maxrss = 2 * 1024 * 1024      # 2 GiB in KB, or ~2MiB in bytes

    monkeypatch.setattr("resource.getrusage", lambda who: FakeUsage())

    monkeypatch.setattr(memguard, "_RUSAGE_IS_KB", True)
    assert memguard.peak_rss_gb() == pytest.approx(2.0, rel=1e-6)

    monkeypatch.setattr(memguard, "_RUSAGE_IS_KB", False)
    assert memguard.peak_rss_gb() == pytest.approx(2.0 / 1024, rel=1e-6)


def test_mps_allocated_gb_is_zero_without_torch(monkeypatch):
    """No torch, or an old build with no `torch.mps`, or a call that itself
    raises: all three report 0.0 rather than propagating."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "torch":
            raise ImportError("no torch here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert memguard.mps_allocated_gb() == 0.0


def test_footprint_gb_sums_rss_and_mps(monkeypatch):
    monkeypatch.setattr(memguard, "current_rss_gb", lambda: 3.0)
    monkeypatch.setattr(memguard, "mps_allocated_gb", lambda: 1.5)
    assert memguard.footprint_gb() == pytest.approx(4.5)


def test_release_never_raises_without_torch(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "torch":
            raise ImportError("no torch here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    memguard.release()          # must not raise


# ── MemGuard's state machine ────────────────────────────────────────────────

def _guard(limit_gb, *readings):
    """A MemGuard whose footprint_fn returns each of `readings` in turn, then
    keeps returning the last one — so a test can describe "over, over, under"
    without worrying about running out of values if it checks once more."""
    seq = list(readings)

    def fn():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return memguard.MemGuard(limit_gb, footprint_fn=fn)


def test_first_breach_is_soft():
    g = _guard(8, 9.0)
    status, gb = g.check()
    assert status == "soft" and gb == 9.0


def test_second_consecutive_breach_is_hard():
    g = _guard(8, 9.0, 9.5)
    assert g.check()[0] == "soft"
    status, gb = g.check()
    assert status == "hard" and gb == 9.5


def test_dropping_back_under_the_limit_resets_to_ok():
    """A soft breach that the cleanup actually fixed must not count against
    the *next* unrelated spike — "still above" means still above, not "was
    ever above this run"."""
    g = _guard(8, 9.0, 6.0, 9.0)
    assert g.check()[0] == "soft"
    assert g.check()[0] == "ok"
    # A fresh breach after recovering is a new soft breach, not a hard one.
    assert g.check()[0] == "soft"


def test_never_breaching_is_always_ok():
    g = _guard(8, 1.0, 2.0, 3.0)
    for _ in range(3):
        assert g.check()[0] == "ok"


def test_zero_or_negative_limit_means_no_limit():
    g = _guard(0, 999.0)
    assert g.check()[0] == "ok"
    g = _guard(-1, 999.0)
    assert g.check()[0] == "ok"


def test_peak_gb_tracks_the_highest_reading_seen():
    g = _guard(8, 3.0, 12.0, 5.0)
    g.check(); g.check(); g.check()
    assert g.peak_gb == 12.0


def test_platform_unit_flag_matches_darwin():
    """Not a fake — the actual flag this process would use, checked once
    against `platform.system()` so the two never quietly disagree."""
    assert memguard._RUSAGE_IS_KB == (platform.system() != "Darwin")
