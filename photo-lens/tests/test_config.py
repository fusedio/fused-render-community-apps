from pathlib import Path

from lens import config, memguard


def test_cache_dir_honors_env(cache_dir):
    assert config.cache_dir() == cache_dir
    assert cache_dir.exists()


def test_config_roundtrip_and_defaults(cache_dir):
    cfg = config.load_config()
    assert cfg == {"roots": [], "port": 8877, "model": "siglip2",
                   "apple_photos": False,
                   "max_index_memory_gb": memguard.DEFAULT_LIMIT_GB}
    cfg["roots"].append("/photos")
    config.save_config(cfg)
    assert config.load_config()["roots"] == ["/photos"]


def test_config_can_be_read_and_written_in_a_named_cache(tmp_path):
    """The daemon passes its own cache directory, so a server and its config
    can never disagree about where they live."""
    other = tmp_path / "elsewhere"
    config.save_config({**config.DEFAULTS, "roots": ["/a"]}, other)
    assert config.load_config(other)["roots"] == ["/a"]
    assert (other / "config.json").exists()          # created on demand


def test_save_config_is_atomic(cache_dir, monkeypatch):
    """The config is rewritten while the daemon is running, so a reader must
    never see a partial file — and a failed write must not leave one."""
    import os

    config.save_config({**config.DEFAULTS, "roots": ["/a"]})
    seen = {}
    real_replace = os.replace

    def watching_replace(src, dst):
        # whatever a concurrent reader would find at `dst` mid-write
        seen["before"] = config.load_config()["roots"]
        return real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", watching_replace)
    config.save_config({**config.DEFAULTS, "roots": ["/a", "/b"]})

    assert seen["before"] == ["/a"], "the old config was already clobbered"
    assert config.load_config()["roots"] == ["/a", "/b"]
    assert [p.name for p in cache_dir.iterdir() if ".tmp" in p.name] == []


def test_save_config_leaves_no_temp_file_when_the_write_fails(cache_dir,
                                                              monkeypatch):
    config.save_config({**config.DEFAULTS, "roots": ["/a"]})

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", boom)
    try:
        config.save_config({**config.DEFAULTS, "roots": ["/b"]})
    except OSError:
        pass
    assert [p.name for p in cache_dir.iterdir() if ".tmp" in p.name] == []
    assert config.load_config()["roots"] == ["/a"]      # untouched


def test_load_config_falls_back_on_a_damaged_file(cache_dir, capsys):
    """A config that can't be parsed must not take down the daemon, the CLI and
    the view with it — the defaults are a working library, just an empty one."""
    p = config.cache_dir() / "config.json"
    for junk in ('{"roots": ["/a"', "", "not json at all"):
        p.write_text(junk)
        assert config.load_config() == config.DEFAULTS
        assert "ignoring unreadable config" in capsys.readouterr().out

    p.write_text('["/a", "/b"]')                    # valid JSON, wrong shape
    assert config.load_config() == config.DEFAULTS
    assert "not a JSON object" in capsys.readouterr().out

    p.write_text('{"roots": "/just/one"}')          # roots isn't a list
    assert config.load_config()["roots"] == []

    # and the damaged file is simply overwritten by the next save
    config.save_config({**config.DEFAULTS, "roots": ["/a"]})
    assert config.load_config()["roots"] == ["/a"]


def test_normalize_root_is_one_spelling_per_folder(tmp_path, monkeypatch):
    """`~/Pictures` and `/Users/me/Pictures` have to become the same string, or
    removing the folder you just added does nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Pictures").mkdir()
    want = str(Path(tmp_path / "Pictures").resolve())
    for spelling in ("~/Pictures", str(tmp_path / "Pictures") + "/",
                     str(tmp_path / "." / "Pictures"),
                     str(tmp_path / "Pictures" / "..") + "/Pictures"):
        assert config.normalize_root(spelling) == want, spelling
