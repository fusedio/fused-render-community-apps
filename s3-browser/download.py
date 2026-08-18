"""Recursive local download for the S3 Browser.

Saves selected files and folders to a local directory, preserving the key path.
A folder download can far exceed fused-render's 60 s call limit, so the work is
**chunked**: `plan` expands the selection and writes a job file; `step`
downloads the next bounded batch (time- and count-capped) and reports progress;
the page loops `step` until done. No OS subprocess is involved — each call is an
ordinary bounded `runPython`, which is what makes it reliable inside the app's
executor.
"""
import json
import os
import tempfile
import time

import s3lib

JOB_DIR = os.path.join(tempfile.gettempdir(), "s3_browser_downloads")
BATCH_FILES = 50
BATCH_SECONDS = 30.0


def _job_path(job: str) -> str:
    return os.path.join(JOB_DIR, f"{job}.json")


def _write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _safe_local(dest_dir: str, key: str) -> str:
    """Map an S3 key to a local path that cannot escape dest_dir — S3 keys may
    contain '..' or backslashes, so drop traversal parts and verify the result
    stays inside the destination."""
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    local = os.path.join(dest_dir, *parts)
    dest_real = os.path.realpath(dest_dir)
    if os.path.commonpath([dest_real, os.path.realpath(local)]) != dest_real:
        raise ValueError("refusing to write outside destination: " + key)
    return local


def _expand(client, bucket, keys, prefixes, sizes=None):
    sizes = sizes or []
    items = [{"key": k, "size": sizes[i] if i < len(sizes) else None} for i, k in enumerate(keys)]
    for pfx in prefixes:
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": pfx, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for o in resp.get("Contents", []):
                if not o["Key"].endswith("/"):
                    items.append({"key": o["Key"], "size": o["Size"]})
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:            # truncated but no cursor (non-conforming endpoint) — stop
                break
    return items


def main(action: str = "plan", account_id: str = "", profile: str = "", region: str = "",
         anonymous: bool = False, endpoint_url: str = "", bucket: str = "",
         keys: str = "", prefixes: str = "", sizes: str = "", dest_dir: str = "", job: str = ""):
    os.makedirs(JOB_DIR, exist_ok=True)

    if action == "default_dir":
        return {"dir": os.path.join(os.path.expanduser("~"), "Downloads")}

    try:
        if action == "plan":
            if not dest_dir:
                return {"error": {"code": "NoDestination", "message": "choose a destination folder"}}
            client = s3lib.client(s3lib.resolve(account_id, profile, region, anonymous, endpoint_url))
            items = _expand(client, bucket, json.loads(keys or "[]"), json.loads(prefixes or "[]"),
                            json.loads(sizes or "[]"))
            import uuid
            job = uuid.uuid4().hex[:12]
            _write(_job_path(job), {
                "status": "ready", "dest": dest_dir, "bucket": bucket,
                "conn": {"account_id": account_id, "profile": profile, "region": region,
                         "anonymous": bool(anonymous), "endpoint_url": endpoint_url},
                "items": items, "total_files": len(items),
                "total_bytes": sum(i["size"] or 0 for i in items),
                "done_files": 0, "done_bytes": 0, "errors": [],
            })
            return {"job": job, "total_files": len(items),
                    "total_bytes": sum(i["size"] or 0 for i in items), "dest": dest_dir}

        if action == "step":
            path = _job_path(job)
            if not os.path.exists(path):
                return {"error": {"code": "NoJob", "message": "download job not found"}}
            st = _read(path)
            c = st["conn"]
            client = s3lib.client(s3lib.resolve(c["account_id"], c["profile"], c["region"],
                                                c["anonymous"], c["endpoint_url"]))
            items, bucket = st["items"], st["bucket"]
            i, started, n = st["done_files"], time.time(), 0
            current = ""
            while i < len(items) and n < BATCH_FILES and (time.time() - started) < BATCH_SECONDS:
                key = items[i]["key"]
                current = key
                try:
                    local = _safe_local(st["dest"], key)
                    os.makedirs(os.path.dirname(local) or st["dest"], exist_ok=True)
                    body = client.get_object(Bucket=bucket, Key=key)["Body"]
                    with open(local, "wb") as fh:
                        for chunk in iter(lambda: body.read(1024 * 1024), b""):
                            fh.write(chunk)
                    st["done_bytes"] += os.path.getsize(local)
                except Exception as e:  # noqa: BLE001
                    st["errors"].append({"key": key, "message": str(e)[:200]})
                i += 1
                n += 1
            st["done_files"] = i
            st["status"] = "done" if i >= len(items) else "running"
            _write(path, st)
            if st["status"] == "done":
                try:
                    os.remove(path)
                except OSError:
                    pass
            return {"status": st["status"], "done_files": i, "total_files": len(items),
                    "done_bytes": st["done_bytes"], "total_bytes": st["total_bytes"],
                    "current": current, "errors": st["errors"], "dest": st["dest"]}

        return {"error": {"code": "UnknownAction", "message": action}}
    except Exception as e:  # noqa: BLE001
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise
