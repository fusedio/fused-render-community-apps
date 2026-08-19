"""Object localizer for the S3 Browser preview.

Downloads an object to a content-addressed local cache and returns its path, so
the page can point a fused-render `/explorer/embed/<path>` iframe at it and get
the native viewer for that file type (PNG, TIFF, PDF, CSV, Parquet, GeoJSON,
text — whatever fused-render knows how to render). The cache key folds in the
ETag, so a changed object re-downloads and object versions never collide.

Returns one of:
  {"local_path": str, "size": int, "content_type": str, "filename": str, "cached": bool}
  {"too_large": True, "size": int, "filename": str}     # UI confirms, re-calls with force
Errors use the same envelope as s3.py: {"error": {code, message, http_status}}.
"""
import hashlib
import os

import s3lib

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".fused-render", "cache",
                         "s3_browser", "preview")


def _safe_basename(key: str) -> str:
    name = key.replace("\\", "/").rstrip("/").split("/")[-1]
    name = "".join(c for c in name if c not in '<>:"/\\|?*').strip()
    return name or "object"


def main(bucket: str = "", key: str = "", account_id: str = "", profile: str = "",
         region: str = "", anonymous: bool = False, endpoint_url: str = "",
         max_bytes: int = 52428800, force: bool = False):
    try:
        client = s3lib.client(s3lib.resolve(account_id, profile, region, anonymous, endpoint_url))
        head = client.head_object(Bucket=bucket, Key=key)
        size = head.get("ContentLength", 0)
        ct = head.get("ContentType", "")
        etag = (head.get("ETag") or "").strip('"')
        filename = _safe_basename(key)

        if size > max_bytes and not force:
            return {"too_large": True, "size": size, "filename": filename}

        digest = hashlib.sha1(f"{bucket}/{key}/{etag}".encode("utf-8")).hexdigest()[:16]
        dest_dir = os.path.join(CACHE_DIR, digest)
        local_path = os.path.join(dest_dir, filename)
        if os.path.exists(local_path):
            return {"local_path": local_path, "size": size, "content_type": ct,
                    "filename": filename, "cached": True}

        os.makedirs(dest_dir, exist_ok=True)
        tmp = local_path + ".part"
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        with open(tmp, "wb") as fh:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                fh.write(chunk)
        os.replace(tmp, local_path)
        return {"local_path": local_path, "size": size, "content_type": ct,
                "filename": filename, "cached": False}
    except Exception as e:  # noqa: BLE001
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise
