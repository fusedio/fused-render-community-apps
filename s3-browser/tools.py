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
keys are never accepted as parameters: save_account reads them from a local
credentials file, so they never pass through a tool call.
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

_PUBLIC_FIELDS = ("id", "label", "auth", "profile", "region", "bucket", "prefix", "endpoint_url")

_KEY_FIELDS = {
    "access_key": ("aws_access_key_id", "access key id", "access_key_id"),
    "secret_key": ("aws_secret_access_key", "secret access key", "secret_access_key"),
    "session_token": ("aws_session_token", "session token", "session_token"),
}


def _public(a: dict) -> dict:
    return {k: a.get(k) for k in _PUBLIC_FIELDS if a.get(k) not in (None, "")}


def _find_account(accounts: list, account: str):
    return next((i for i, a in enumerate(accounts) if account in (a.get("id"), a.get("label"))), None)


def _write_accounts(accounts: list) -> None:
    import tempfile

    s3lib.accounts_location()
    fd, tmp = tempfile.mkstemp(dir=s3lib.CACHE_DIR, prefix=".accounts-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, s3lib.ACCOUNTS_PATH)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _uid() -> str:
    import random
    import string
    import time

    digits = string.digits + string.ascii_lowercase
    n, out = int(time.time() * 1000), ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out + "".join(random.choice(digits) for _ in range(5))


def _parse_s3_path(path: str) -> tuple[str, str]:
    rest = path.strip()
    if rest[:5].lower() == "s3://":
        rest = rest[5:]
    bkt, _, key = rest.lstrip("/").partition("/")
    return bkt, (key + "/" if key and not key.endswith("/") else key)


def _read_credentials_file(path: str) -> dict:
    import csv

    with open(os.path.expanduser(path), encoding="utf-8-sig") as f:
        text = f.read()
    rows = list(csv.reader(text.splitlines()))
    pairs = {}
    if len(rows) >= 2 and len(rows[0]) > 1 and len(rows[0]) == len(rows[1]):
        pairs = {k.strip().lower(): v.strip() for k, v in zip(rows[0], rows[1])}
    else:
        for line in text.splitlines():
            k, sep, v = line.partition("=")
            if sep:
                pairs[k.strip().removeprefix("export ").strip().lower()] = v.strip().strip("'\"")
    return {field: next((pairs[n] for n in names if pairs.get(n)), "")
            for field, names in _KEY_FIELDS.items()}


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def list_accounts() -> dict:
    """Saved S3 Browser connections (secrets omitted) and local AWS profiles."""
    return {"accounts": [_public(a) for a in _accounts()], "profiles": s3lib.available_profiles()}


def save_account(account: str = "", label: str | None = None, auth: str | None = None,
                 path: str | None = None, region: str | None = None,
                 endpoint_url: str | None = None, profile: str | None = None,
                 credentials_file: str = "") -> dict:
    """Add a connection, or edit the one named by `account` (id or label).
    When editing, a field left out keeps its stored value and "" clears it."""
    accounts = _accounts()
    prev = None
    if account:
        idx = _find_account(accounts, account)
        if idx is None:
            return _err("NoSuchAccount", f"no saved connection with id or label {account!r}")
        prev = accounts[idx]

    def pick(value, field):
        return value if value is not None else (prev or {}).get(field, "") or ""

    auth = pick(auth, "auth") or "anonymous"
    if auth not in ("anonymous", "profile", "keys"):
        return _err("InvalidAuth", 'auth must be "anonymous", "profile" or "keys"')
    endpoint = pick(endpoint_url, "endpoint_url").strip()
    if endpoint[:5].lower() == "s3://":
        path = endpoint if path is None else path
        endpoint = ""
    if path is not None:
        bkt, pfx = _parse_s3_path(path)
    else:
        bkt, pfx = (prev or {}).get("bucket", ""), (prev or {}).get("prefix", "")
    if auth == "anonymous" and not bkt:
        return _err("MissingPath", "public connections need an S3 path, e.g. s3://my-bucket/")
    prof = pick(profile, "profile") if auth == "profile" else ""

    creds = {"access_key": "", "secret_key": "", "session_token": ""}
    if auth == "keys":
        if credentials_file:
            try:
                creds = _read_credentials_file(credentials_file)
            except OSError as e:
                return _err("CredentialsFileUnreadable", str(e))
        elif prev and prev.get("auth") == "keys":
            creds = {k: prev.get(k, "") for k in creds}
        if not (creds["access_key"] and creds["secret_key"]):
            return _err("MissingKeys", "key auth needs a credentials_file holding both an access "
                        "key id and a secret access key")

    name = pick(label, "label").strip() or bkt or (
        "AWS " + (prof or "default") if auth == "profile" else "AWS account")
    a = {"id": prev["id"] if prev else _uid(), "label": name, "auth": auth,
         "region": pick(region, "region").strip(), "bucket": bkt, "prefix": pfx,
         "endpoint_url": endpoint, "profile": prof, **creds}
    if prev:
        accounts[idx] = a
    else:
        accounts.append(a)
    _write_accounts(accounts)
    return {"saved": _public(a), "created": prev is None}


def delete_account(account: str) -> dict:
    """Remove a saved connection by id or label."""
    accounts = _accounts()
    idx = _find_account(accounts, account)
    if idx is None:
        return _err("NoSuchAccount", f"no saved connection with id or label {account!r}")
    gone = accounts.pop(idx)
    _write_accounts(accounts)
    return {"deleted": {"id": gone.get("id"), "label": gone.get("label")}}


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


def delete_objects(bucket: str, keys: list[str] | None = None, prefixes: list[str] | None = None,
                   account: str = "", profile: str = "", region: str = "",
                   anonymous: bool = False, endpoint_url: str = "") -> dict:
    """Permanently delete objects (`keys`) and whole folders (`prefixes`,
    recursive, up to 5000 objects per folder per call)."""
    conn = (account, profile, region, anonymous, endpoint_url)
    targets, truncated = list(keys or []), False
    for p in prefixes or []:
        if not p.strip("/"):
            return _err("RefusedBucketRoot", "refusing to delete a whole bucket; name a folder prefix")
        r = _call("list_keys", *conn, bucket=bucket, prefix=p if p.endswith("/") else p + "/")
        if "error" in r:
            return r
        targets += r["keys"]
        truncated = truncated or r["truncated"]
    targets = list(dict.fromkeys(targets))
    deleted, errors = 0, []
    for i in range(0, len(targets), 1000):
        r = _call("delete_objects", *conn, bucket=bucket, keys=json.dumps(targets[i:i + 1000]))
        if "error" in r:
            return {**r, "deleted": deleted}
        deleted += len(r["deleted"])
        errors += r["errors"]
    return {"deleted": deleted, "errors": errors, "truncated": truncated}


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
