# /// script
# dependencies = ["httpx"]
# ///
"""Real recordings for Open Relax's tiles, cached under .fused/cache/audio.

catalog.json — built by research.py — lists, for every tile that plays a
recording, a few Creative Commons clips from Openverse (Freesound, Jamendo,
Wikimedia), each checked to be under 1 MB. A tile, or a card the user has
added, points at one of those clips; the first time it plays this script
downloads the file, and from then on the page reads it straight from the
cache. Normal use therefore never touches Openverse's anonymous quota. Only a
sound the catalogue has nothing for falls back to a live search.

Actions (all return JSON):
  list                     -> {ok, items: {tile id: meta}}   everything already cached
  get id [base] [ov]       -> {ok, meta} cached or freshly downloaded; {ok: false, error} otherwise
                              base: the catalogue sound a card was made from (defaults to id)
                              ov:   the Openverse id of the clip the card chose (defaults to the first)
  forget id                -> drop the cached recording (and the note that nothing was found)
"""
import glob
import io
import json
import os
import re
import threading
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".fused", "cache", "audio")
REL = "./.fused/cache/audio/"                     # how the page addresses a file, via fused.rawUrl
CATALOG = os.path.join(HERE, "catalog.json")
API = "https://api.openverse.org/v1/audio/"
UA = "OpenRelax/1.0 (fused-render page; personal use)"
MAX_BYTES = 1_000_000                             # every clip stays under a megabyte
LICENSE_RANK = {"cc0": 0, "pdm": 0, "by": 1, "by-sa": 2, "by-nc": 3, "by-nc-sa": 4, "by-nd": 5, "by-nc-nd": 6}
EXT = {"mp3": "mp3", "mp32": "mp3", "ogg": "ogg", "oga": "ogg", "m4a": "m4a"}


class Busy(Exception):
    """Openverse answered 429: the anonymous quota is spent for now."""


def _path(sid):
    return os.path.join(CACHE, sid + ".json")


def _load(sid):
    try:
        return json.load(io.open(_path(sid), encoding="utf-8"))
    except Exception:
        return None


def _save(sid, meta):
    tmp = _path(sid) + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(meta))
    os.replace(tmp, _path(sid))


def _all():
    out = {}
    for p in glob.glob(os.path.join(CACHE, "*.json")):
        try:
            m = json.load(io.open(p, encoding="utf-8"))
            out[m["id"]] = m
        except Exception:
            continue
    return out


def _catalog_clips(base):
    try:
        cat = json.load(io.open(CATALOG, encoding="utf-8"))
        return (cat.get("sounds", {}).get(base) or {}).get("clips") or []
    except Exception:
        return []


def _download(url, dest):
    # A temp name of our own: two pages fetching the same clip at once (a tap and a prefetch,
    # or two open windows) must not write one ".part" file, or the second rename fails and
    # that caller falls through to the catalogue's next clip — the tile changing recording.
    tmp = "%s.%d.%d.part" % (dest, os.getpid(), threading.get_ident())
    n = 0
    with httpx.stream("GET", url, headers={"User-Agent": UA}, follow_redirects=True,
                      timeout=httpx.Timeout(15.0, read=25.0)) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if not (ctype.startswith("audio/") or ctype.startswith("video/") or "octet-stream" in ctype):
            raise ValueError("not an audio file (%s)" % ctype)
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(65536):
                n += len(chunk)
                if n > MAX_BYTES:
                    raise ValueError("file too large")
                f.write(chunk)
    if n < 20000:
        os.remove(tmp)
        raise ValueError("file too small to be a recording")
    if os.path.exists(dest):               # another call finished the same clip first: keep theirs
        os.remove(tmp)
        return
    os.replace(tmp, dest)


def _meta_from_clip(sid, base, c):
    """What the page shows under a mixer row: the clip's credit, and where its file is."""
    return {
        "id": sid, "base": base, "file": REL + c["file"], "title": c.get("title") or "untitled",
        "creator": c.get("creator") or "unknown", "creator_url": c.get("creator_url") or "",
        "license": c.get("license") or "", "license_url": c.get("license_url") or "",
        "landing": c.get("landing") or "", "provider": c.get("provider") or "",
        "duration_ms": c.get("duration_ms") or 0, "attribution": c.get("attribution") or "",
        "ov_id": c["ov_id"], "fetched_at": time.time(),
    }


def _from_catalog(sid, base, ov):
    """Download the card's clip — or, when no clip was pinned, the first one that still works."""
    clips = _catalog_clips(base)
    if not clips:
        return None
    if ov:
        clips = [c for c in clips if c["ov_id"] == ov] or clips[:1]
    last = ""
    skipped = []                                    # why each earlier catalogue clip was passed over
    for n, c in enumerate(clips):
        try:
            dest = os.path.join(CACHE, c["file"])
            if not os.path.exists(dest):
                _download(c["url"], dest)
            meta = _meta_from_clip(sid, base, c)
            if skipped:                             # not the first choice: say so, so the page can show it
                meta["choice"] = n + 1
                meta["skipped"] = skipped
            _save(sid, meta)
            return {"ok": True, "meta": meta}
        except Exception as e:
            last = str(e)
            skipped.append({"title": c.get("title") or "untitled", "error": last})
    return {"ok": False, "error": last or "the recording could not be fetched"}


# ---- fallback: a live Openverse search, for sounds the catalogue has nothing for ----

def _search(query, music):
    r = httpx.get(API, params={"q": query, "page_size": 20, "length": "shortest,short"},
                  headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20)
    if r.status_code == 429:
        raise Busy("Openverse is out of anonymous searches for now — try again in a while")
    r.raise_for_status()
    return r.json().get("results", [])


def _score(r, query, music):
    """Higher is better; None means unusable: the same rules research.py applies."""
    if r.get("mature") or not r.get("url"):
        return None
    lic = r.get("license") or ""
    if lic not in LICENSE_RANK or (r.get("filetype") or "").lower() not in EXT:
        return None
    size = r.get("filesize")
    if size is not None and not (0 < size < MAX_BYTES):
        return None
    dur = (r.get("duration") or 0) / 1000.0
    if dur < 15:
        return None
    s = 10.0 - LICENSE_RANK[lic] * 1.5
    if 25 <= dur <= 65:
        s += 3
    prov = r.get("provider") or ""
    s += ({"jamendo": 2, "freesound": 1} if music else {"freesound": 3, "wikimedia_audio": 1, "jamendo": -4}).get(prov, 0)
    words = set(re.findall(r"[a-z]+", (query or "").lower())) - {"the", "a", "of", "on", "in", "and"}
    hay = ((r.get("title") or "") + " " + " ".join(t.get("name", "") for t in (r.get("tags") or []))).lower()
    s += sum(1.5 for w in words if w in hay)
    return s


def _from_search(sid, query, music):
    try:
        results = _search(query, music)
    except Busy as e:
        return {"ok": False, "error": str(e), "busy": True}
    except Exception as e:
        return {"ok": False, "error": "search failed: %s" % e}
    ranked = sorted(((sc, r) for r in results for sc in [_score(r, query, music)] if sc is not None), key=lambda x: -x[0])
    last = ""
    for _, r in ranked[:4]:
        try:
            c = {"ov_id": r["id"], "file": "%s.%s" % (r["id"][:8], EXT[(r.get("filetype") or "").lower()]), "url": r["url"],
                 "title": r.get("title"), "creator": r.get("creator"), "creator_url": r.get("creator_url"), "license": r.get("license"),
                 "license_url": r.get("license_url"), "landing": r.get("foreign_landing_url"), "provider": r.get("provider"),
                 "duration_ms": r.get("duration"), "attribution": r.get("attribution")}
            dest = os.path.join(CACHE, c["file"])
            if not os.path.exists(dest):
                _download(c["url"], dest)
            meta = _meta_from_clip(sid, sid, c)
            meta["query"] = query
            _save(sid, meta)
            return {"ok": True, "meta": meta}
        except Exception as e:
            last = str(e)
    _save(sid, {"id": sid, "none": True, "query": query, "checked_at": time.time(),
                "error": last or "nothing under 1 MB on Openverse"})
    return {"ok": False, "error": last or "nothing under 1 MB on Openverse"}


def _remove_file(meta):
    """Drop the audio file only when no other tile still points at it."""
    if not meta or not meta.get("file"):
        return
    if any(m.get("file") == meta["file"] for m in _all().values() if m.get("id") != meta.get("id")):
        return
    try:
        os.remove(os.path.join(CACHE, os.path.basename(meta["file"])))
    except OSError:
        pass


def main(action: str = "list", id: str = "", base: str = "", ov: str = "", query: str = "", music: bool = False):
    os.makedirs(CACHE, exist_ok=True)
    if action == "list":
        return {"ok": True, "items": _all()}
    if not id:
        return {"ok": False, "error": "no sound id"}
    meta = _load(id)
    if action == "forget":
        _remove_file(meta)
        try:
            os.remove(_path(id))
        except OSError:
            pass
        return {"ok": True}
    if action != "get":
        return {"ok": False, "error": "unknown action %r" % action}
    if meta and not meta.get("none"):
        path = os.path.join(CACHE, os.path.basename(meta["file"]))
        if os.path.exists(path) and os.path.getsize(path) <= MAX_BYTES and (not ov or meta.get("ov_id") == ov):
            return {"ok": True, "meta": meta, "cached": True}
        _remove_file(meta)                 # a clip from before the 1 MB rule, or one the card no longer points at
    got = _from_catalog(id, base or id, ov)
    if got is not None:
        return got
    if meta and meta.get("none") and time.time() - meta.get("checked_at", 0) < 6 * 3600:
        return {"ok": False, "error": meta.get("error") or "nothing found"}      # don't spend a search on it again today
    return _from_search(id, query or id, str(music).lower() == "true")
