"""Concurrent signal check for every channel in a category playlist.

A channel counts as responsive only when there is actually something to
play: the URL serves an HLS playlist whose (cheapest) media playlist lists
segments and whose newest segment answers with bytes — or it serves raw
MPEG-TS / MP4 / DASH, which the player handles through the ffmpeg bridge.
A 200 with an HTML login page, an empty body or a playlist of dead segments
is a failure, with the reason recorded.

Running records live in health.parquet: total tries, total fails, whether
the channel was responsive on the most recent check, what it served and
the last error.
"""
import asyncio
import os
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

import channels
import paths
import streams

CACHE_DIR = paths.CACHE_DIR
PARQUET_PATH = os.path.join(CACHE_DIR, "health.parquet")

TIMEOUT = 5          # per HTTP request, seconds
CONCURRENCY = 192    # probes are tiny; dead hosts just sit in connect()

SCHEMA = pa.schema([
    ("url", pa.string()),
    ("name", pa.string()),
    ("group", pa.string()),
    ("tries", pa.int64()),
    ("fails", pa.int64()),
    ("fail_pct", pa.float64()),
    ("responsive_now", pa.bool_()),
    ("last_checked", pa.string()),
    ("kind", pa.string()),
    ("last_error", pa.string()),
])


def _fetch(url: str, cap: int):
    try:
        return streams.fetch(url, cap=cap, timeout=TIMEOUT), ""
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, streams.describe_error(e)


def probe(url: str) -> dict:
    """{"ok": bool, "kind": str, "error": str} — see module docstring."""
    f, err = _fetch(url, 8192)
    if f is None:
        return {"ok": False, "kind": "dead", "error": err}
    kind = streams.classify_bytes(f.body, f.content_type)
    if kind not in streams.ALIVE_KINDS:
        return {"ok": False, "kind": kind,
                "error": f"serves {streams.KIND_LABEL.get(kind, kind)}, not a stream"}
    if kind not in streams.BROWSER_KINDS:
        return {"ok": True, "kind": kind, "error": ""}
    if f.truncated:
        f, err = _fetch(url, 4_000_000)
        if f is None:
            return {"ok": False, "kind": kind, "error": err}
    text, murl = streams.decode_text(f.body), f.final_url
    if "#EXT-X-STREAM-INF" in text:
        vurl = streams.pick_variant(text, murl)
        if not vurl:
            return {"ok": False, "kind": kind, "error": "master playlist lists no variants"}
        f2, err = _fetch(vurl, 4_000_000)
        if f2 is None:
            return {"ok": False, "kind": kind, "error": f"variant playlist: {err}"}
        text, murl = streams.decode_text(f2.body), f2.final_url
        if not text.startswith("#EXTM3U"):
            return {"ok": False, "kind": kind, "error": "variant playlist is not a playlist"}
    m = streams.parse_media(text)
    if not m["segments"]:
        return {"ok": False, "kind": kind, "error": "playlist lists no segments"}
    seg = urllib.parse.urljoin(murl, m["segments"][-1] if m["live"] else m["segments"][0])
    fs, err = _fetch(seg, 2048)
    if fs is None:
        return {"ok": False, "kind": kind, "error": f"segment: {err}"}
    if not fs.body:
        return {"ok": False, "kind": kind, "error": "segment is empty"}
    return {"ok": True, "kind": kind, "error": ""}


def _probe(url: str) -> bool:
    """Back-compat boolean probe."""
    return probe(url)["ok"]


async def _check_all(chans):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(ch):
        async with sem:
            return ch, await asyncio.to_thread(probe, ch["url"])

    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(CONCURRENCY))
    return await asyncio.gather(*(one(ch) for ch in chans))


def _load_records() -> dict:
    if not os.path.exists(PARQUET_PATH):
        return {}
    try:
        rows = pq.read_table(PARQUET_PATH).to_pylist()
    except Exception:  # unreadable file counts as no history
        return {}
    for r in rows:
        r.setdefault("kind", "")
        r.setdefault("last_error", "")
        r["kind"] = r["kind"] or ""
        r["last_error"] = r["last_error"] or ""
    return {r["url"]: r for r in rows}


def _save_records(records: dict):
    rows = sorted(records.values(), key=lambda r: -r["fail_pct"])
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = PARQUET_PATH + f".{os.getpid()}.tmp"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), tmp)
    os.replace(tmp, PARQUET_PATH)


def _info(result) -> dict:
    """Accept a bool or a probe()/grab() dict."""
    if isinstance(result, dict):
        return {"ok": bool(result.get("ok")), "kind": result.get("kind") or "",
                "error": result.get("error") or result.get("note", "") or ""}
    return {"ok": bool(result), "kind": "", "error": ""}


def update_records(results) -> dict:
    """Fold (channel, result) pairs into health.parquet; return the summary."""
    records = _load_records()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    ok_count = 0
    for ch, result in results:
        info = _info(result)
        rec = records.get(ch["url"])
        if rec is None:
            rec = {"url": ch["url"], "name": ch["name"], "group": ch["group"],
                   "tries": 0, "fails": 0, "kind": "", "last_error": ""}
            records[ch["url"]] = rec
        rec["tries"] += 1
        if info["ok"]:
            ok_count += 1
        else:
            rec["fails"] += 1
        rec["responsive_now"] = info["ok"]
        rec["fail_pct"] = round(100.0 * rec["fails"] / rec["tries"], 1)
        rec["last_checked"] = now
        if info["kind"]:
            rec["kind"] = info["kind"]
        rec["last_error"] = "" if info["ok"] else info["error"]

    _save_records(records)

    unresponsive = sorted((r for r in records.values() if r["fails"] > 0),
                          key=lambda r: -r["fail_pct"])
    checked = len(results)
    print(f"checked {checked}, responsive {ok_count}, tracked failures {len(unresponsive)}")
    return {
        "checked": checked,
        "responsive_now": ok_count,
        "health_pct": round(100.0 * ok_count / checked, 1) if checked else 100.0,
        "unresponsive": unresponsive,
        "records": sorted(records.values(), key=lambda r: -r["fail_pct"]),
        "last_checked": now,
    }


def main() -> dict:
    chans = channels.main()["channels"]
    results = asyncio.run(_check_all(chans))
    return update_records(results)


if __name__ == "__main__":
    import json
    print(json.dumps(main(), indent=2)[:2000])
