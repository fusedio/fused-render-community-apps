#!/usr/bin/env python
"""Lens CLI: add-root | index | daemon | reindex | status."""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lens import config


def cmd_add_root(args):
    root = config.normalize_root(args.path)
    if not Path(root).is_dir():
        sys.exit(f"not a directory: {root}")
    cfg = config.load_config()
    if root not in cfg["roots"]:
        cfg["roots"].append(root)
        config.save_config(cfg)
    print(f"roots: {cfg['roots']}")


def _embedder(cfg):
    from lens.embed import Embedder
    return Embedder(cfg["model"])


def cmd_index(args):
    from lens.indexer import index_once
    from lens.store import Store
    cfg = config.load_config()
    if not cfg["roots"]:
        sys.exit("no roots configured — run: lens.py add-root <path>")
    store = Store(config.cache_dir())
    stats = index_once(
        store, cfg["roots"], _embedder(cfg), config.cache_dir(),
        progress=lambda d, t: print(f"\r{d}/{t}", end="", flush=True))
    print(f"\n{json.dumps(stats)}")


def cmd_daemon(args):
    """The daemon takes no roots: it reads them from the config on every index
    run, so folders added from the view take effect without a restart."""
    from lens.daemon import LensServer
    cfg = config.load_config()
    srv = LensServer(config.cache_dir(), embedder=_embedder(cfg),
                     port=cfg["port"])
    srv.start_reindex()
    print(f"lensd on http://127.0.0.1:{srv.port} "
          + (f"(roots: {cfg['roots']})" if cfg["roots"]
             else "(no folders yet — add them from the view's ⚙ menu)"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


def _call(port: int, path: str, method: str = "GET"):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:            # answered, but refused
        sys.exit(f"daemon on port {port} rejected {method} {path}: "
                 f"{exc.code} {exc.reason}")
    except urllib.error.URLError as exc:             # nothing listening
        sys.exit(f"no lens daemon on port {port} ({exc.reason}) — "
                 f"start it with: lens.py daemon")


def cmd_status(args):
    cfg = config.load_config()
    print(json.dumps(_call(cfg["port"], "/status"), indent=2))


def cmd_reindex(args):
    """Ask a running daemon to rescan. Indexing in this process instead would
    need its own copy of the model and would race the daemon's writes."""
    cfg = config.load_config()
    res = _call(cfg["port"], "/reindex", method="POST")
    print("reindex started" if res.get("started")
          else "already indexing — nothing to do")


def main():
    ap = argparse.ArgumentParser(prog="lens")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add-root")
    p.add_argument("path")
    p.set_defaults(fn=cmd_add_root)
    sub.add_parser("index").set_defaults(fn=cmd_index)
    sub.add_parser("daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("reindex").set_defaults(fn=cmd_reindex)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
