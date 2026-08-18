"""Concurrent health check for all channels in sports.m3u.

Probes every stream URL concurrently and keeps a running record in
health.parquet — ONLY for channels that have failed at least once.
Each record tracks total tries, total fails, and whether the channel
was responsive on the most recent check.
"""
import asyncio
import os
import ssl
import urllib.request
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

import channels
import paths

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = paths.CACHE_DIR
PARQUET_PATH = os.path.join(CACHE_DIR, "health.parquet")

TIMEOUT = 8
CONCURRENCY = 128
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

SCHEMA = pa.schema([
    ("url", pa.string()),
    ("name", pa.string()),
    ("group", pa.string()),
    ("tries", pa.int64()),
    ("fails", pa.int64()),
    ("fail_pct", pa.float64()),
    ("responsive_now", pa.bool_()),
    ("last_checked", pa.string()),
])


def _probe(url: str) -> bool:
    """True if the stream URL answers with a 2xx/3xx and some body."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
            if not (200 <= resp.status < 400):
                return False
            return len(resp.read(512)) > 0
    except Exception:
        return False


async def _check_all(channels):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(ch):
        async with sem:
            ok = await asyncio.to_thread(_probe, ch["url"])
            return ch, ok

    return await asyncio.gather(*(one(ch) for ch in channels))


def _load_records() -> dict:
    if not os.path.exists(PARQUET_PATH):
        return {}
    return {r["url"]: r for r in pq.read_table(PARQUET_PATH).to_pylist()}


def _save_records(records: dict):
    rows = sorted(records.values(), key=lambda r: -r["fail_pct"])
    os.makedirs(CACHE_DIR, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), PARQUET_PATH)


def update_records(results) -> dict:
    """Fold (channel, ok) results into health.parquet; return the summary."""
    records = _load_records()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for ch, ok in results:
        rec = records.get(ch["url"])
        if rec is None:
            rec = {"url": ch["url"], "name": ch["name"], "group": ch["group"],
                   "tries": 0, "fails": 0}
            records[ch["url"]] = rec
        rec["tries"] += 1
        if not ok:
            rec["fails"] += 1
        rec["responsive_now"] = ok
        rec["fail_pct"] = round(100.0 * rec["fails"] / rec["tries"], 1)
        rec["last_checked"] = now

    _save_records(records)

    unresponsive = sorted((r for r in records.values() if r["fails"] > 0),
                          key=lambda r: -r["fail_pct"])
    checked = len(results)
    ok_count = sum(1 for _, ok in results if ok)
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
