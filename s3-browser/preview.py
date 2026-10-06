"""Object localizer for the S3 Browser preview.

Downloads an object to a content-addressed local cache and returns its path, so
the page can point a fused-render `/explorer/embed/<path>` iframe at it and get
the native viewer for that file type (PNG, TIFF, PDF, CSV, Parquet, GeoJSON,
text — whatever fused-render knows how to render). The cache key folds in the
ETag, so a changed object re-downloads and object versions never collide.
The cache is capped at CACHE_LIMIT, least-recently-used entries evicted first.

Returns one of:
  {"local_path": str, "size": int, "content_type": str, "filename": str, "cached": bool}
  {"too_large": True, "size": int, "filename": str}     # UI confirms, re-calls with force
Errors use the same envelope as s3.py: {"error": {code, message, http_status}}.
"""
import hashlib
import os
import shutil

import s3lib

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".fused-render", "cache",
                         "s3_browser", "preview")
CACHE_LIMIT = 2 * 1024 ** 3      # bytes kept across previews before LRU eviction


def _safe_basename(key: str) -> str:
    name = key.replace("\\", "/").rstrip("/").split("/")[-1]
    name = "".join(c for c in name if c not in '<>:"/\\|?*').strip()
    return name or "object"


def _digest(bucket, key, etag):
    return hashlib.sha1(f"{bucket}/{key}/{etag}".encode("utf-8")).hexdigest()[:16]


def _prune(keep: str) -> None:
    """Evict least-recently-used entries until the cache fits CACHE_LIMIT, so
    previewing many large objects can't fill the disk. `keep` is never evicted."""
    entries, total = [], 0
    for d in os.scandir(CACHE_DIR):
        if not d.is_dir():
            continue
        size = sum(f.stat().st_size for f in os.scandir(d.path) if f.is_file())
        entries.append((d.stat().st_mtime, d.path, size))
        total += size
    for _, path, size in sorted(entries):
        if total <= CACHE_LIMIT:
            break
        if path != keep:
            shutil.rmtree(path, ignore_errors=True)
            total -= size


def main(bucket: str = "", key: str = "", account_id: str = "", profile: str = "",
         region: str = "", anonymous: bool = False, endpoint_url: str = "",
         max_bytes: int = 52428800, force: bool = False, etag: str = "", size: int = -1):
    """`etag`/`size` are optional hints from the listing: with them a cache hit
    or a too-large refusal needs no S3 request at all. Without them it's one
    GetObject (its headers carry size and ETag), never a separate HeadObject."""
    filename = _safe_basename(key)
    size = int(size)
    if size > max_bytes and not force:
        return {"too_large": True, "size": size, "filename": filename}
    if etag:
        dest_dir = os.path.join(CACHE_DIR, _digest(bucket, key, etag))
        local_path = os.path.join(dest_dir, filename)
        if os.path.exists(local_path):
            os.utime(dest_dir)                       # mark recently used for _prune
            return {"local_path": local_path, "size": os.path.getsize(local_path),
                    "content_type": "", "filename": filename, "cached": True}
    try:
        client = s3lib.client(s3lib.resolve(account_id, profile, region, anonymous, endpoint_url))
        resp = client.get_object(Bucket=bucket, Key=key)
        body = resp["Body"]
        size = resp.get("ContentLength", 0)
        ct = resp.get("ContentType", "")
        if size > max_bytes and not force:
            body.close()
            return {"too_large": True, "size": size, "filename": filename}

        dest_dir = os.path.join(CACHE_DIR, _digest(bucket, key, (resp.get("ETag") or "").strip('"')))
        local_path = os.path.join(dest_dir, filename)
        if os.path.exists(local_path):
            body.close()
            os.utime(dest_dir)
            return {"local_path": local_path, "size": size, "content_type": ct,
                    "filename": filename, "cached": True}

        os.makedirs(dest_dir, exist_ok=True)
        tmp = local_path + ".part"
        with open(tmp, "wb") as fh:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                fh.write(chunk)
        os.replace(tmp, local_path)
        _prune(keep=dest_dir)
        return {"local_path": local_path, "size": size, "content_type": ct,
                "filename": filename, "cached": False}
    except Exception as e:  # noqa: BLE001
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise
