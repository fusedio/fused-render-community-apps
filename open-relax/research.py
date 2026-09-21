# /// script
# dependencies = ["httpx"]
# ///
"""Build catalog.json: for every tile that can play a real recording, up to six
Creative Commons clips from Openverse, each verified to be under 1 MB.

The page never searches Openverse itself any more — it reads this catalogue and
sounds.py downloads the clip a card points at. Run this again to refresh:

    uv run research.py            # resume: only sounds missing from catalog.json
    uv run research.py --force    # start over

Openverse allows an anonymous client 20 searches a minute and 200 a day; the
script paces itself against the rate-limit headers, checkpoints as it goes and
stops cleanly when the day's quota is spent (run it again tomorrow to finish).
A search's twenty results are pooled, and a sound whose query words are all
present in at least four pooled clips takes those instead of spending a search
of its own — the wind, rain and noise families share most of what they find.

Size is checked twice: Openverse's own filesize first (it matches the download
byte for byte for Freesound), then a HEAD request on every clip kept, which also
catches Jamendo tracks whose size Openverse doesn't know.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
OUT = os.path.join(HERE, "catalog.json")
API = "https://api.openverse.org/v1/audio/"
UA = "OpenRelax/1.0 (fused-render page; personal use)"
MAX_BYTES = 1_000_000                 # strictly under one megabyte
MIN_MS = 15_000                       # anything shorter loops too obviously
KEEP = 6                              # clips offered per sound in the picker
POOL_MIN = 4                          # pooled full matches that make a search unnecessary
CATS = ("nature", "city", "melody", "asmr")          # brainwaves stay pure tones
LICENSE_RANK = {"cc0": 0, "pdm": 0, "by": 1, "by-sa": 2, "by-nc": 3, "by-nc-sa": 4, "by-nd": 5, "by-nc-nd": 6}
EXT = {"mp3": "mp3", "mp32": "mp3", "ogg": "ogg", "oga": "ogg", "m4a": "m4a"}   # what decodeAudioData takes; wav/flac under 1 MB are seconds long
STOP = {"the", "a", "of", "on", "in", "and", "ambience", "ambient", "sound", "sounds", "asmr"}
# Searched first so the tiles a row opens with, and the presets, are covered before the quota runs low.
PRIORITY = ["rain", "fire", "thunder", "owls", "catpurring", "ocean", "forest", "river", "night", "waves", "wind", "waterfall", "crickets",
            "white", "grandfatherclock", "brown", "fan", "guitar", "eternity", "tibetanbowls", "harmony", "india", "softpiano", "lounge",
            "om", "zen", "singingbowl", "vinylcrackle", "heartbeat"]


def sounds_from_page():
    """The S("id", "Name", "cat", "icon", …) calls and the QUERIES map, lifted straight out of index.html."""
    html = open(PAGE, encoding="utf-8").read()
    calls = re.findall(r'S\("([a-z0-9_]+)", "([^"]+)", "([a-z]+)", "([a-z0-9_]+)"', html)
    q0 = html.index("const QUERIES = {")
    q1 = html.index("};", q0)
    queries = dict(re.findall(r'\b([a-z0-9_]+): "([^"]+)"', html[q0:q1]))
    out = []
    for sid, name, cat, ic in calls:
        if cat in CATS:
            out.append({"id": sid, "name": name, "cat": cat, "ic": ic, "query": queries.get(sid, name)})
    order = {sid: i for i, sid in enumerate(PRIORITY)}
    out.sort(key=lambda s: (order.get(s["id"], len(PRIORITY)),))     # stable: catalogue order within each tier
    return out


def words(q):
    return set(re.findall(r"[a-z]+", q.lower())) - STOP


def hay(r):
    return ((r.get("title") or "") + " " + " ".join(t.get("name", "") for t in (r.get("tags") or []))).lower()


def usable(r):
    """A clip we could offer at all; size is settled later by HEAD when Openverse doesn't know it."""
    if r.get("mature") or not r.get("url") or (r.get("license") or "") not in LICENSE_RANK:
        return False
    if (r.get("filetype") or "").lower() not in EXT:
        return False
    if (r.get("duration") or 0) < MIN_MS:
        return False
    size = r.get("filesize")
    return size is None or 0 < size < MAX_BYTES


def score(r, query, music):
    dur = (r.get("duration") or 0) / 1000.0
    s = 10.0 - LICENSE_RANK[r["license"]] * 1.5
    if 25 <= dur <= 65:
        s += 3
    s += min(dur, 65) / 65                    # longer loops seam less often
    prov = r.get("provider") or ""
    s += ({"jamendo": 2, "freesound": 1} if music else {"freesound": 3, "wikimedia_audio": 1, "jamendo": -4}).get(prov, 0)
    h = hay(r)
    s += sum(1.5 for w in words(query) if w in h)
    return s


def clip(r, sid):
    ext = EXT[(r.get("filetype") or "").lower()]
    return {
        "ov_id": r["id"], "file": "%s.%s" % (r["id"][:8], ext), "url": r["url"], "title": r.get("title") or "untitled",
        "creator": r.get("creator") or "unknown", "creator_url": r.get("creator_url") or "",
        "license": r.get("license") or "", "license_url": r.get("license_url") or "", "landing": r.get("foreign_landing_url") or "",
        "provider": r.get("provider") or "", "duration_ms": r.get("duration") or 0, "bytes": r.get("filesize") or 0,
        "attribution": r.get("attribution") or "", "tags": [t.get("name", "") for t in (r.get("tags") or [])][:10],
    }


class Quota(Exception):
    """Openverse's daily allowance is used up: stop and keep what we have."""


class Searcher:
    def __init__(self):
        self.client = httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=25)
        self.burst_left = 20
        self.day_left = 200
        self.searches = 0

    def search(self, query):
        for attempt in range(4):
            if self.burst_left <= 1:
                print("      … pausing a minute for the rate limit", flush=True)
                time.sleep(62)
            r = self.client.get(API, params={"q": query, "page_size": 20, "length": "shortest,short"})
            self.burst_left = int(r.headers.get("x-ratelimit-available-anon_burst", 20))
            self.day_left = int(r.headers.get("x-ratelimit-available-anon_sustained", 200))
            if r.status_code == 429:
                if self.day_left <= 0:
                    raise Quota()
                time.sleep(65)
                continue
            r.raise_for_status()
            self.searches += 1
            if self.day_left <= 0:
                raise Quota()
            return r.json().get("results", [])
        raise RuntimeError("Openverse kept answering 429 for %r" % query)


def head_size(c):
    """(bytes, ok): what the CDN really serves — the size Openverse reports, or nothing for Jamendo."""
    try:
        with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20) as h:
            r = h.head(c["url"])
            if r.status_code >= 400:
                r = h.get(c["url"], headers={"Range": "bytes=0-0"})      # some hosts refuse HEAD
            ct = r.headers.get("content-type", "")
            n = r.headers.get("content-range", "").rpartition("/")[2] or r.headers.get("content-length", "")
            n = int(n) if n.isdigit() else 0
            ok = r.status_code < 400 and (ct.startswith("audio/") or "octet-stream" in ct) and 0 < n < MAX_BYTES
            return n, ok
    except Exception:
        return 0, False


def verify(clips):
    with ThreadPoolExecutor(8) as ex:
        sizes = list(ex.map(head_size, clips))
    kept = []
    for c, (n, ok) in zip(clips, sizes):
        if ok:
            c["bytes"] = n
            kept.append(c)
    return kept


def main():
    force = "--force" in sys.argv
    sounds = sounds_from_page()
    catalog = {"version": 1, "max_bytes": MAX_BYTES, "sounds": {}}
    if os.path.exists(OUT) and not force:
        catalog = json.load(open(OUT, encoding="utf-8"))
    done = catalog["sounds"]
    pool = {}                                    # ov_id -> raw-ish result used for reuse across sounds
    for entry in done.values():                  # a resumed run pools what earlier runs found
        for c in entry["clips"]:
            pool[c["ov_id"]] = {"id": c["ov_id"], "url": c["url"], "title": c["title"], "creator": c["creator"], "creator_url": c["creator_url"],
                                "license": c["license"], "license_url": c["license_url"], "foreign_landing_url": c["landing"], "provider": c["provider"],
                                "duration": c["duration_ms"], "filesize": c["bytes"], "attribution": c["attribution"], "filetype": c["file"].rsplit(".", 1)[1],
                                "tags": [{"name": t} for t in c["tags"]]}
    todo = [s for s in sounds if s["id"] not in done]
    print("%d sounds, %d already in the catalogue, %d to research" % (len(sounds), len(done), len(todo)), flush=True)

    def save():
        catalog["built"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        catalog["sounds"] = dict(sorted(done.items()))
        tmp = OUT + ".tmp"
        json.dump(catalog, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        os.replace(tmp, OUT)

    sx = Searcher()
    pooled = 0
    try:
        for i, s in enumerate(todo, 1):
            music = s["cat"] == "melody"
            w = words(s["query"])
            full = [r for r in pool.values() if all(x in hay(r) for x in w)] if w else []
            if len(full) >= POOL_MIN:
                results, via = full, "pool"
                pooled += 1
            else:
                results, via = sx.search(s["query"]), "search"
                for r in results:
                    if usable(r):
                        pool[r["id"]] = r
                results = results + [r for r in full if r["id"] not in {x["id"] for x in results}]
            cands = sorted((r for r in results if usable(r)), key=lambda r: -score(r, s["query"], music))
            seen, picked = set(), []
            for r in cands:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    picked.append(clip(r, s["id"]))
                if len(picked) >= KEEP + 3:      # a few spares in case HEAD drops some
                    break
            kept = verify(picked)[:KEEP]
            done[s["id"]] = {"name": s["name"], "cat": s["cat"], "query": s["query"], "via": via, "clips": kept, "checked_at": time.time()}
            print("[%3d/%3d] %-18s %-6s %d clips   (day quota left %d)" % (i, len(todo), s["id"], via, len(kept), sx.day_left), flush=True)
            if i % 10 == 0:
                save()
    except Quota:
        print("Openverse's daily quota is spent — run again tomorrow to research the remaining %d sounds" % (len(todo) - len([t for t in todo if t["id"] in done])), flush=True)
    finally:
        save()
    have = sum(1 for e in done.values() if e["clips"])
    print("catalogue: %d sounds, %d with clips, %d with none · %d searches, %d from the pool" % (len(done), have, len(done) - have, sx.searches, pooled), flush=True)


if __name__ == "__main__":
    main()
