"""S3 Browser backend — a botocore-based S3 client layer for fused-render.

Every S3 operation the UI needs flows through one dispatcher, `main(action=...)`.

- **One connection model.** A request addresses a store either by a saved-account
  id (`account_id`, resolved from the git-ignored accounts.json) or by the
  non-secret fields directly (`profile`/`region`/`anonymous`/`endpoint`). Raw
  keys never travel as params — see s3lib for why and how.
- **One error envelope.** Expected S3 failures (AccessDenied, NoSuchBucket, a
  wrong region) return `{"error": {code, message, http_status}}` so the page can
  render a friendly state. Only genuine bugs raise and hit the red overlay.
- **Pagination from day one.** `list_objects` speaks continuation tokens.

botocore drives every call; pandas/pyarrow back the Parquet preview — all in
fused-render's bundled interpreter, so this folder has no pyproject.toml.
Anonymous mode is what lets it all be exercised against public AWS Open Data
buckets with no account.
"""
import base64
import json

import s3lib

# Under the warm-worker runtime (fused.engine) this module stays imported across
# calls, so botocore clients are cached per connection here instead of being
# rebuilt every call — that client build is ~1s of a cold call. Keyed by the
# fields that define the client; the page drops the whole worker (engine.forget)
# when a connection's credentials change, so a stale client can't outlive an edit.
# Under plain /api/run each call is a fresh process, so this is simply empty.
_CLIENT_CACHE = {}


def _cached_client(conn):
    key = (conn["profile"], conn["region"], conn["anonymous"],
           conn["endpoint_url"], conn["access_key"])
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = s3lib.client(conn)
        _CLIENT_CACHE[key] = client
    return client


def _leaf(key: str, delimiter: str) -> str:
    d = delimiter or "/"
    return key.rstrip(d).rsplit(d, 1)[-1]


def _resolve_region(client, bucket: str):
    """The bucket's home region, read from the x-amz-bucket-region header S3
    returns on HeadBucket — present on both success and a region redirect, and
    readable anonymously."""
    from botocore.exceptions import ClientError

    def _from(meta):
        return (meta or {}).get("HTTPHeaders", {}).get("x-amz-bucket-region")

    try:
        return _from(client.head_bucket(Bucket=bucket).get("ResponseMetadata"))
    except ClientError as e:
        return _from(e.response.get("ResponseMetadata"))


# ---- actions -------------------------------------------------------------

def _list_profiles(client, **_):
    return {"profiles": s3lib.available_profiles()}


def _list_buckets(client, **_):
    resp = client.list_buckets()
    return {
        "buckets": [
            {"name": b["Name"], "creation_date": b["CreationDate"].isoformat()}
            for b in resp.get("Buckets", [])
        ],
        "owner": resp.get("Owner", {}).get("DisplayName"),
    }


def _bucket_region(client, bucket, **_):
    return {"bucket": bucket, "region": _resolve_region(client, bucket)}


def _bucket_info(client, bucket, **_):
    """Bucket-level properties, each fetched independently — a denial on one
    (common on locked-down buckets) still lets the others through."""
    from botocore.exceptions import ClientError

    out = {"bucket": bucket, "region": _resolve_region(client, bucket)}

    try:
        v = client.get_bucket_versioning(Bucket=bucket)
        out["versioning"] = {"status": v.get("Status", "Disabled"), "mfa_delete": v.get("MFADelete")}
    except ClientError as e:
        out["versioning"] = {"error": e.response.get("Error", {}).get("Code")}

    try:
        enc = client.get_bucket_encryption(Bucket=bucket)
        rule = (enc.get("ServerSideEncryptionConfiguration", {}).get("Rules") or [{}])[0]
        d = rule.get("ApplyServerSideEncryptionByDefault", {})
        out["encryption"] = {"algorithm": d.get("SSEAlgorithm"), "kms_key": d.get("KMSMasterKeyID"),
                             "bucket_key": rule.get("BucketKeyEnabled")}
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        out["encryption"] = {"none": True} if code == "ServerSideEncryptionConfigurationNotFoundError" else {"error": code}

    try:
        pab = client.get_public_access_block(Bucket=bucket).get("PublicAccessBlockConfiguration", {})
        out["public_access_block"] = {
            "block_public_acls": pab.get("BlockPublicAcls"),
            "ignore_public_acls": pab.get("IgnorePublicAcls"),
            "block_public_policy": pab.get("BlockPublicPolicy"),
            "restrict_public_buckets": pab.get("RestrictPublicBuckets"),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        out["public_access_block"] = {"none": True} if code == "NoSuchPublicAccessBlockConfiguration" else {"error": code}

    return out


_PUBLIC_URIS = ("http://acs.amazonaws.com/groups/global/AllUsers",
                "http://acs.amazonaws.com/groups/global/AuthenticatedUsers")


def _security_scan(client, bucket, **_):
    """Assess a bucket's public exposure. Each check degrades to an 'unknown'
    finding on AccessDenied rather than failing the whole scan."""
    from botocore.exceptions import ClientError

    findings, public = [], False

    def add(level, title, detail=""):
        findings.append({"level": level, "title": title, "detail": detail})

    try:
        pab = client.get_public_access_block(Bucket=bucket).get("PublicAccessBlockConfiguration", {})
        flags = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
        off = [f for f in flags if not pab.get(f)]
        if off:
            add("warn", "Public Access Block incomplete", "Off: " + ", ".join(off))
        else:
            add("ok", "Block Public Access fully enabled")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        add("warn" if code == "NoSuchPublicAccessBlockConfiguration" else "unknown",
            "No Public Access Block" if code == "NoSuchPublicAccessBlockConfiguration" else "Public Access Block: " + str(code))

    try:
        grants = client.get_bucket_acl(Bucket=bucket).get("Grants", [])
        pub = [g for g in grants if g.get("Grantee", {}).get("URI") in _PUBLIC_URIS]
        if pub:
            public = True
            perms = sorted({g.get("Permission") for g in pub})
            add("high", "Bucket ACL grants public access", "Public grants: " + ", ".join(perms))
        else:
            add("ok", "No public ACL grants")
    except ClientError as e:
        add("unknown", "Bucket ACL: " + str(e.response.get("Error", {}).get("Code")))

    try:
        if client.get_bucket_policy_status(Bucket=bucket).get("PolicyStatus", {}).get("IsPublic"):
            public = True
            add("high", "Bucket policy makes the bucket public")
        else:
            add("ok", "Bucket policy is not public")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        add("ok" if code == "NoSuchBucketPolicy" else "unknown",
            "No bucket policy" if code == "NoSuchBucketPolicy" else "Bucket policy status: " + str(code))

    try:
        client.get_bucket_encryption(Bucket=bucket)
        add("ok", "Default encryption enabled")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        add("warn" if code == "ServerSideEncryptionConfigurationNotFoundError" else "unknown",
            "No default encryption" if code == "ServerSideEncryptionConfigurationNotFoundError" else "Encryption: " + str(code))

    return {"bucket": bucket, "public": public, "findings": findings}


def _set_versioning(client, bucket, status, **_):
    if status not in ("Enabled", "Suspended"):
        return {"error": {"code": "InvalidVersioning", "message": status}}
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": status})
    return {"bucket": bucket, "versioning": status}


def _json_deep(o):
    import datetime
    if isinstance(o, dict):
        return {k: _json_deep(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_json_deep(v) for v in o]
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    return o


_CONFIG_EMPTY_CODES = {"NoSuchBucketPolicy", "NoSuchCORSConfiguration", "NoSuchLifecycleConfiguration"}


def _get_bucket_config(client, bucket, config_type, **_):
    from botocore.exceptions import ClientError

    try:
        if config_type == "policy":
            return {"type": "policy", "value": client.get_bucket_policy(Bucket=bucket).get("Policy")}
        if config_type == "cors":
            return {"type": "cors", "value": _json_deep(client.get_bucket_cors(Bucket=bucket).get("CORSRules", []))}
        if config_type == "lifecycle":
            return {"type": "lifecycle", "value": _json_deep(client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", []))}
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in _CONFIG_EMPTY_CODES:
            return {"type": config_type, "value": None}
        raise
    return {"error": {"code": "BadConfigType", "message": config_type}}


def _put_bucket_config(client, bucket, config_type, config, **_):
    if config_type == "policy":
        client.put_bucket_policy(Bucket=bucket, Policy=config)
    elif config_type == "cors":
        client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": json.loads(config)})
    elif config_type == "lifecycle":
        client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": json.loads(config)})
    else:
        return {"error": {"code": "BadConfigType", "message": config_type}}
    return {"ok": True, "type": config_type}


def _delete_bucket_config(client, bucket, config_type, **_):
    if config_type == "policy":
        client.delete_bucket_policy(Bucket=bucket)
    elif config_type == "cors":
        client.delete_bucket_cors(Bucket=bucket)
    elif config_type == "lifecycle":
        client.delete_bucket_lifecycle(Bucket=bucket)
    else:
        return {"error": {"code": "BadConfigType", "message": config_type}}
    return {"ok": True, "type": config_type}


def _list_objects(client, conn, bucket, prefix, delimiter, token, max_keys, **_):
    # If the caller doesn't know the bucket's region, resolve it in THIS
    # subprocess and rebuild the client, instead of forcing a whole separate
    # bucket_region round-trip before the page can list (halves a bucket switch).
    out_region = conn["region"]
    if not out_region and not conn["endpoint_url"] and not token:
        out_region = _resolve_region(client, bucket) or ""
        if out_region:
            client = _cached_client({**conn, "region": out_region})
    kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": max_keys}
    if delimiter:
        kw["Delimiter"] = delimiter
    if token:
        kw["ContinuationToken"] = token
    resp = client.list_objects_v2(**kw)

    folders = [
        {"prefix": p["Prefix"], "name": _leaf(p["Prefix"], delimiter)}
        for p in resp.get("CommonPrefixes", [])
    ]
    files = []
    for o in resp.get("Contents", []):
        if o["Key"] == prefix:  # the folder's own zero-byte placeholder
            continue
        files.append({
            "key": o["Key"],
            "name": _leaf(o["Key"], delimiter or "/"),
            "size": o["Size"],
            "last_modified": o["LastModified"].isoformat(),
            "storage_class": o.get("StorageClass", "STANDARD"),
            "etag": o.get("ETag", "").strip('"'),
        })
    return {
        "bucket": bucket,
        "region": out_region or None,
        "prefix": prefix,
        "delimiter": delimiter,
        "folders": folders,
        "files": files,
        "next_token": resp.get("NextContinuationToken"),
        "is_truncated": resp.get("IsTruncated", False),
        "key_count": resp.get("KeyCount", len(files)),
    }


def _list_keys(client, bucket, prefix, **_):
    """Every object key under a prefix (recursive), capped, for bulk delete."""
    keys, token, cap, truncated = [], None, 5000, False
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = client.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if not resp.get("IsTruncated"):   # last page — done regardless of count
            break
        if len(keys) >= cap:              # more remain but we've hit the cap
            truncated = True
            break
        token = resp.get("NextContinuationToken")
        if not token:                     # truncated but no cursor (non-conforming endpoint) — stop
            truncated = True
            break
    return {"keys": keys[:cap], "truncated": truncated}


def _head_object(client, bucket, key, **_):
    resp = client.head_object(Bucket=bucket, Key=key)
    return {
        "key": key,
        "size": resp.get("ContentLength"),
        "content_type": resp.get("ContentType"),
        "last_modified": resp["LastModified"].isoformat() if resp.get("LastModified") else None,
        "etag": (resp.get("ETag") or "").strip('"'),
        "storage_class": resp.get("StorageClass", "STANDARD"),
        "version_id": resp.get("VersionId"),
        "cache_control": resp.get("CacheControl"),
        "content_disposition": resp.get("ContentDisposition"),
        "content_encoding": resp.get("ContentEncoding"),
        "sse": resp.get("ServerSideEncryption"),
        "metadata": resp.get("Metadata", {}),
    }


def _get_tags(client, bucket, key, **_):
    resp = client.get_object_tagging(Bucket=bucket, Key=key)
    return {"tags": [{"key": t["Key"], "value": t["Value"]} for t in resp.get("TagSet", [])]}


def _put_tags(client, bucket, key, tags, **_):
    tagset = [{"Key": t["key"], "Value": t["value"]} for t in tags]
    client.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": tagset})
    return {"key": key, "count": len(tagset)}


def _list_versions(client, bucket, key, max_keys, **_):
    resp = client.list_object_versions(Bucket=bucket, Prefix=key, MaxKeys=max_keys)
    versions = [
        {
            "version_id": v.get("VersionId"),
            "size": v.get("Size"),
            "last_modified": v["LastModified"].isoformat() if v.get("LastModified") else None,
            "is_latest": v.get("IsLatest", False),
            "storage_class": v.get("StorageClass", "STANDARD"),
            "etag": (v.get("ETag") or "").strip('"'),
        }
        for v in resp.get("Versions", []) if v.get("Key") == key
    ]
    markers = [
        {"version_id": d.get("VersionId"),
         "last_modified": d["LastModified"].isoformat() if d.get("LastModified") else None,
         "is_latest": d.get("IsLatest", False)}
        for d in resp.get("DeleteMarkers", []) if d.get("Key") == key
    ]
    return {"key": key, "versions": versions, "delete_markers": markers}


def _presign(client, conn, bucket, key, method, expires, disposition, version_id, **_):
    if conn["anonymous"] and method == "get":
        from urllib.parse import quote

        if conn["endpoint_url"]:                       # S3-compatible (MinIO/Wasabi/R2): path-style
            base = f"{conn['endpoint_url'].rstrip('/')}/{bucket}/{quote(key)}"
        else:
            region = conn["region"]
            host = f"{bucket}.s3.{region}.amazonaws.com" if region else f"{bucket}.s3.amazonaws.com"
            base = f"https://{host}/{quote(key)}"
        vq = f"?versionId={quote(version_id)}" if version_id else ""
        return {"url": base + vq, "expires": None, "signed": False}
    params = {"Bucket": bucket, "Key": key}
    if disposition:
        params["ResponseContentDisposition"] = disposition
    if version_id:
        params["VersionId"] = version_id
    op = "put_object" if method == "put" else "get_object"
    url = client.generate_presigned_url(op, Params=params, ExpiresIn=expires)
    return {"url": url, "expires": expires, "signed": True}


def _restore_version(client, bucket, key, version_id, **_):
    # "Restore" a prior version by copying it back onto the key — that becomes
    # the new current version, non-destructively (the old versions remain).
    client.copy_object(Bucket=bucket, Key=key,
                       CopySource={"Bucket": bucket, "Key": key, "VersionId": version_id})
    return {"key": key, "restored_from": version_id}


def _delete_objects(client, bucket, keys, **_):
    objs = [{"Key": k} for k in keys]
    resp = client.delete_objects(Bucket=bucket, Delete={"Objects": objs, "Quiet": False})
    return {
        "deleted": [d["Key"] for d in resp.get("Deleted", [])],
        "errors": [
            {"key": e.get("Key"), "code": e.get("Code"), "message": e.get("Message")}
            for e in resp.get("Errors", [])
        ],
    }


def _create_folder(client, bucket, prefix, name, delimiter, **_):
    d = delimiter or "/"
    folder_key = prefix + name.strip(d) + d
    client.put_object(Bucket=bucket, Key=folder_key, Body=b"")
    return {"created": folder_key}


def _upload(client, bucket, key, content_b64, content_type, **_):
    body = base64.b64decode(content_b64) if content_b64 else b""
    kw = {"Bucket": bucket, "Key": key, "Body": body}
    if content_type:
        kw["ContentType"] = content_type
    resp = client.put_object(**kw)
    return {"key": key, "etag": (resp.get("ETag") or "").strip('"'), "size": len(body)}


def _rename_object(client, bucket, key, src_key, **_):
    from botocore.exceptions import ClientError

    # Refuse to rename onto an existing object — copy_object would silently
    # overwrite it. (HeadObject on a missing key raises a 404.)
    try:
        client.head_object(Bucket=bucket, Key=key)
        return {"error": {"code": "DestinationExists", "message": f"an object named '{key}' already exists"}}
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "404":
            raise
    client.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": src_key})
    client.delete_object(Bucket=bucket, Key=src_key)
    return {"renamed": key, "from": src_key}


# S3 changes an object's storage class by copying it onto itself with the new
# class; MetadataDirective=COPY keeps its metadata. Archived objects (GLACIER /
# DEEP_ARCHIVE) must be restored before they can be copied — that surfaces as a
# normal error envelope.
STORAGE_CLASSES = ["STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING",
                   "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE", "REDUCED_REDUNDANCY"]


def _change_storage_class(client, bucket, key, storage_class, **_):
    if storage_class not in STORAGE_CLASSES:
        return {"error": {"code": "InvalidStorageClass", "message": storage_class}}
    client.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": key},
                       StorageClass=storage_class, MetadataDirective="COPY")
    return {"key": key, "storage_class": storage_class}


_ACTIONS = {
    "list_profiles": _list_profiles,
    "list_buckets": _list_buckets,
    "bucket_region": _bucket_region,
    "bucket_info": _bucket_info,
    "security_scan": _security_scan,
    "set_versioning": _set_versioning,
    "get_bucket_config": _get_bucket_config,
    "put_bucket_config": _put_bucket_config,
    "delete_bucket_config": _delete_bucket_config,
    "list_objects": _list_objects,
    "list_keys": _list_keys,
    "head_object": _head_object,
    "get_tags": _get_tags,
    "put_tags": _put_tags,
    "list_versions": _list_versions,
    "presign": _presign,
    "delete_objects": _delete_objects,
    "create_folder": _create_folder,
    "upload": _upload,
    "rename_object": _rename_object,
    "change_storage_class": _change_storage_class,
    "restore_version": _restore_version,
}


def main(
    action: str = "list_objects",
    # connection
    account_id: str = "",
    profile: str = "",
    region: str = "",
    anonymous: bool = False,
    endpoint_url: str = "",
    # object addressing
    bucket: str = "",
    prefix: str = "",
    key: str = "",
    # listing
    delimiter: str = "/",
    token: str = "",
    max_keys: int = 1000,
    # mutations
    keys: str = "",
    name: str = "",
    content_b64: str = "",
    content_type: str = "",
    src_key: str = "",
    tags: str = "",
    storage_class: str = "",
    version_id: str = "",
    status: str = "",
    config_type: str = "",
    config: str = "",
    # presign
    method: str = "get",
    expires: int = 3600,
    disposition: str = "",
):
    if action == "accounts_path":            # no client/network needed
        return s3lib.accounts_location()

    fn = _ACTIONS.get(action)
    if fn is None:
        return {"error": {"code": "UnknownAction", "message": f"unknown action: {action}"}}

    try:
        conn = s3lib.resolve(account_id, profile, region, anonymous, endpoint_url)
        client = _cached_client(conn)
        return fn(
            client,
            conn=conn,
            bucket=bucket,
            prefix=prefix,
            key=key,
            delimiter=delimiter,
            token=token,
            max_keys=max_keys,
            keys=json.loads(keys) if keys else [],
            name=name,
            content_b64=content_b64,
            content_type=content_type,
            src_key=src_key,
            tags=json.loads(tags) if tags else [],
            storage_class=storage_class,
            version_id=version_id,
            status=status,
            config_type=config_type,
            config=config,
            method=method,
            expires=expires,
            disposition=disposition,
        )
    except Exception as e:  # noqa: BLE001 — expected S3 errors become a friendly envelope
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise
