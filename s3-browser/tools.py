"""MCP tools that let a Claude session use the S3 Browser.

Published by `fused app serve <this folder>` from `mcp.toml` beside this file,
one tool per function below. Each is a thin, typed wrapper over the same
backend the page uses (s3.py's `main(action=...)`, preview.py, download.py),
so behaviour, credential handling and the `{"error": {...}}` envelope match
the UI exactly.

Every tool addresses a store the same way: `account` is a saved connection
from the S3 Browser sidebar (its id or its label — see list_accounts), and
`profile` / `region` / `anonymous` / `endpoint_url` override or replace it.
With none of them set the default AWS credential chain is used. Raw access
keys are never accepted as parameters; save them as a connection in the app.
"""

from __future__ import annotations

import json
import os
import sys

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

# Aliased: tool functions below (download, ...) would otherwise shadow them.
import download as _download  # noqa: E402
import preview as _preview  # noqa: E402
import s3  # noqa: E402
import s3lib  # noqa: E402

TEXT_LIMIT = 256 * 1024


def _accounts():
    try:
        with open(s3lib.ACCOUNTS_PATH, encoding="utf-8") as f:
            return json.load(f).get("accounts", [])
    except (FileNotFoundError, ValueError):
        return []


def _account_id(account: str) -> str:
    if not account:
        return ""
    for a in _accounts():
        if account in (a.get("id"), a.get("label")):
            return a.get("id", "")
    return account


def _conn(account, profile, region, anonymous, endpoint_url):
    return {"account_id": _account_id(account), "profile": profile, "region": region,
            "anonymous": anonymous, "endpoint_url": endpoint_url}


def _call(action, account="", profile="", region="", anonymous=False, endpoint_url="", **kw):
    return s3.main(action=action, **_conn(account, profile, region, anonymous, endpoint_url), **kw)


# ---- connections ------------------------------------------------------------

def list_accounts() -> dict:
    """Saved S3 Browser connections (secrets omitted) and local AWS profiles."""
    keep = ("id", "label", "auth", "profile", "region", "bucket", "endpoint_url")
    return {"accounts": [{k: a.get(k) for k in keep if a.get(k) not in (None, "")} for a in _accounts()],
            "profiles": s3lib.available_profiles()}


# ---- buckets ----------------------------------------------------------------

def list_buckets(account: str = "", profile: str = "", region: str = "",
                 anonymous: bool = False, endpoint_url: str = "") -> dict:
    """List the buckets the credentials can see."""
    return _call("list_buckets", account, profile, region, anonymous, endpoint_url)


def bucket_info(bucket: str, account: str = "", profile: str = "", region: str = "",
                anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Bucket region, versioning, default encryption and public-access-block."""
    return _call("bucket_info", account, profile, region, anonymous, endpoint_url, bucket=bucket)


def security_scan(bucket: str, account: str = "", profile: str = "", region: str = "",
                  anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Assess a bucket's public exposure (ACLs, policy status, PAB, encryption)."""
    return _call("security_scan", account, profile, region, anonymous, endpoint_url, bucket=bucket)


def set_versioning(bucket: str, status: str, account: str = "", profile: str = "",
                   region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Turn bucket versioning on or off. status: "Enabled" or "Suspended"."""
    return _call("set_versioning", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, status=status)


def get_bucket_config(bucket: str, config_type: str, account: str = "", profile: str = "",
                      region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Read a bucket's "policy", "cors" or "lifecycle" config (value null when unset)."""
    return _call("get_bucket_config", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, config_type=config_type)


def put_bucket_config(bucket: str, config_type: str, config: str, account: str = "",
                      profile: str = "", region: str = "", anonymous: bool = False,
                      endpoint_url: str = "") -> dict:
    """Replace a bucket's "policy" (policy JSON document), "cors" (JSON array of
    CORSRules) or "lifecycle" (JSON array of Rules) config."""
    return _call("put_bucket_config", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, config_type=config_type, config=config)


def delete_bucket_config(bucket: str, config_type: str, account: str = "", profile: str = "",
                         region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Remove a bucket's "policy", "cors" or "lifecycle" config."""
    return _call("delete_bucket_config", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, config_type=config_type)


# ---- objects ----------------------------------------------------------------

def list_objects(bucket: str, prefix: str = "", token: str = "", max_keys: int = 1000,
                 delimiter: str = "/", account: str = "", profile: str = "", region: str = "",
                 anonymous: bool = False, endpoint_url: str = "") -> dict:
    """One folder level: sub-folders and objects under `prefix`. Pass the
    returned next token as `token` for the next page; delimiter "" lists recursively."""
    return _call("list_objects", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, prefix=prefix, token=token, max_keys=max_keys, delimiter=delimiter)


def list_keys(bucket: str, prefix: str = "", account: str = "", profile: str = "",
              region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Every object key under a prefix, recursively (capped at 5000)."""
    return _call("list_keys", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, prefix=prefix)


def head_object(bucket: str, key: str, account: str = "", profile: str = "", region: str = "",
                anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Object properties: size, type, modified, etag, storage class, encryption, metadata."""
    return _call("head_object", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, key=key)


def read_object(bucket: str, key: str, max_bytes: int = 52428800, account: str = "",
                profile: str = "", region: str = "", anonymous: bool = False,
                endpoint_url: str = "") -> dict:
    """Fetch an object to a local cache file and return its path. Text objects
    up to 256 KB also come back inline as `text`. Larger than max_bytes is refused."""
    c = _conn(account, profile, region, anonymous, endpoint_url)
    out = _preview.main(bucket=bucket, key=key, max_bytes=max_bytes, **c)
    path = out.get("local_path") if isinstance(out, dict) else None
    if path and out.get("size", 0) <= TEXT_LIMIT:
        try:
            with open(path, encoding="utf-8") as f:
                out["text"] = f.read()
        except (UnicodeDecodeError, OSError):
            pass
    return out


def upload_file(bucket: str, key: str, local_path: str, content_type: str = "",
                account: str = "", profile: str = "", region: str = "",
                anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Upload a file from this machine to s3://bucket/key (multipart for large files)."""
    import mimetypes

    try:
        client = s3lib.client(s3lib.resolve(**_conn(account, profile, region, anonymous, endpoint_url)))
        ct = content_type or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
        size = os.path.getsize(os.path.expanduser(local_path))
        with open(os.path.expanduser(local_path), "rb") as fh:
            if size <= 64 * 1024 * 1024:
                r = client.put_object(Bucket=bucket, Key=key, Body=fh, ContentType=ct)
                return {"key": key, "size": size, "etag": (r.get("ETag") or "").strip('"')}
            mp = client.create_multipart_upload(Bucket=bucket, Key=key, ContentType=ct)
            parts, n = [], 1
            try:
                for chunk in iter(lambda: fh.read(32 * 1024 * 1024), b""):
                    p = client.upload_part(Bucket=bucket, Key=key, UploadId=mp["UploadId"],
                                           PartNumber=n, Body=chunk)
                    parts.append({"PartNumber": n, "ETag": p["ETag"]})
                    n += 1
                r = client.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=mp["UploadId"],
                                                     MultipartUpload={"Parts": parts})
            except Exception:
                client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=mp["UploadId"])
                raise
            return {"key": key, "size": size, "etag": (r.get("ETag") or "").strip('"')}
    except Exception as e:  # noqa: BLE001
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise


def put_text(bucket: str, key: str, text: str, content_type: str = "text/plain",
             account: str = "", profile: str = "", region: str = "",
             anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Write a small text object (overwrites an existing key)."""
    import base64

    return _call("upload", account, profile, region, anonymous, endpoint_url, bucket=bucket,
                 key=key, content_b64=base64.b64encode(text.encode("utf-8")).decode("ascii"),
                 content_type=content_type)


def download(bucket: str, dest_dir: str = "~/Downloads", keys: list[str] | None = None,
             prefixes: list[str] | None = None, account: str = "", profile: str = "",
             region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Download objects (`keys`) and whole folders (`prefixes`, recursive) into
    dest_dir on this machine, keeping the key paths. Returns totals and errors."""
    c = _conn(account, profile, region, anonymous, endpoint_url)
    plan = _download.main(action="plan", bucket=bucket, keys=json.dumps(keys or []),
                         prefixes=json.dumps(prefixes or []), dest_dir=os.path.expanduser(dest_dir), **c)
    if "error" in plan:
        return plan
    st = {"status": "ready"}
    while st.get("status") not in ("done",) and "error" not in st:
        st = _download.main(action="step", job=plan["job"], **c)
    return st


def create_folder(bucket: str, name: str, prefix: str = "", account: str = "",
                  profile: str = "", region: str = "", anonymous: bool = False,
                  endpoint_url: str = "") -> dict:
    """Create an empty folder `name` inside `prefix`."""
    return _call("create_folder", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, prefix=prefix, name=name)


def rename_object(bucket: str, src_key: str, dest_key: str, account: str = "", profile: str = "",
                  region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Rename/move an object within a bucket. Refuses to overwrite an existing key."""
    return _call("rename_object", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, key=dest_key, src_key=src_key)


def delete_objects(bucket: str, keys: list[str], account: str = "", profile: str = "",
                   region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Permanently delete objects by key (up to 1000; use list_keys to expand a folder)."""
    return _call("delete_objects", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, keys=json.dumps(keys))


def change_storage_class(bucket: str, key: str, storage_class: str, account: str = "",
                         profile: str = "", region: str = "", anonymous: bool = False,
                         endpoint_url: str = "") -> dict:
    """Move an object to STANDARD, STANDARD_IA, ONEZONE_IA, INTELLIGENT_TIERING,
    GLACIER_IR, GLACIER, DEEP_ARCHIVE or REDUCED_REDUNDANCY."""
    return _call("change_storage_class", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, key=key, storage_class=storage_class)


def presign_url(bucket: str, key: str, expires: int = 3600, method: str = "get",
                version_id: str = "", account: str = "", profile: str = "", region: str = "",
                anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Shareable URL for an object, valid `expires` seconds. method "put" gives an
    upload URL. Public (anonymous) objects get a plain unsigned URL."""
    return _call("presign", account, profile, region, anonymous, endpoint_url, bucket=bucket,
                 key=key, expires=expires, method=method, version_id=version_id)


# ---- tags & versions --------------------------------------------------------

def get_tags(bucket: str, key: str, account: str = "", profile: str = "", region: str = "",
             anonymous: bool = False, endpoint_url: str = "") -> dict:
    """An object's tags as [{key, value}]."""
    return _call("get_tags", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, key=key)


def set_tags(bucket: str, key: str, tags: dict[str, str], account: str = "", profile: str = "",
             region: str = "", anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Replace an object's tags with `tags` ({name: value}; {} clears them)."""
    return _call("put_tags", account, profile, region, anonymous, endpoint_url, bucket=bucket,
                 key=key, tags=json.dumps([{"key": k, "value": v} for k, v in tags.items()]))


def list_versions(bucket: str, key: str, max_keys: int = 1000, account: str = "",
                  profile: str = "", region: str = "", anonymous: bool = False,
                  endpoint_url: str = "") -> dict:
    """An object's versions and delete markers (versioned buckets)."""
    return _call("list_versions", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, key=key, max_keys=max_keys)


def restore_version(bucket: str, key: str, version_id: str, account: str = "",
                    profile: str = "", region: str = "", anonymous: bool = False,
                    endpoint_url: str = "") -> dict:
    """Make an older version current again by copying it onto the key (non-destructive)."""
    return _call("restore_version", account, profile, region, anonymous, endpoint_url,
                 bucket=bucket, key=key, version_id=version_id)
