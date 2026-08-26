import json
import os
from pathlib import Path

from lens import memguard

# `apple_photos` is off until asked for: reading the Photos library needs a macOS
# permission the user has to grant by hand, and the first sync of a real library
# is long. Both are things to opt into, never to discover having happened.
#
# `max_index_memory_gb` has one source of truth (memguard.DEFAULT_LIMIT_GB)
# rather than a second literal here, so the number in a fresh config and the
# number the guard falls back to when a key is simply absent can never drift
# apart.
DEFAULTS = {"roots": [], "port": 8877, "model": "siglip2",
            "apple_photos": False,
            "max_index_memory_gb": memguard.DEFAULT_LIMIT_GB}


def cache_dir() -> Path:
    d = Path(os.environ.get("LENS_CACHE", Path.home() / ".fused-render/cache/lens"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path(cache: Path = None) -> Path:
    """`cache` names the directory to read/write in; it defaults to the
    process-wide cache. The daemon passes its own so that a server and its
    config can never disagree about where they live."""
    return (Path(cache) if cache is not None else cache_dir()) / "config.json"


def load_config(cache: Path = None) -> dict:
    """Never raises on a damaged file. This config is the daemon's only source
    of roots, and it is now rewritten while the daemon runs — a file that
    cannot be parsed must degrade to the defaults with a warning, not take the
    whole daemon (and the CLI, and the view) down with it."""
    cfg = dict(DEFAULTS)
    p = _config_path(cache)
    if p.exists():
        stored = None
        try:
            stored = json.loads(p.read_text())
        except (ValueError, OSError) as exc:
            print(f"lens: ignoring unreadable config {p} ({exc}); using defaults")
        if isinstance(stored, dict):
            cfg.update(stored)
        elif stored is not None:
            print(f"lens: ignoring config {p} (not a JSON object); using defaults")
    if not isinstance(cfg.get("roots"), list):
        cfg["roots"] = list(DEFAULTS["roots"])
    else:
        cfg["roots"] = list(cfg["roots"])
    return cfg


def save_config(cfg: dict, cache: Path = None) -> None:
    """Written to a sibling and moved into place: os.replace is atomic, so a
    crash — or one request saving while another reads — can never leave a
    half-written config.json behind."""
    p = _config_path(cache)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)         # nothing left behind on failure


def normalize_root(path: str) -> str:
    """The one spelling a root is stored under: absolute, ~-expanded, symlinks
    resolved. Add and remove have to agree on it — otherwise `~/Pictures` and
    `/Users/me/Pictures` are two entries for one folder, and removing the
    folder you just added silently does nothing."""
    return str(Path(path).expanduser().resolve())
