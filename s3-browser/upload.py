"""Multipart upload for the S3 Browser.

Large files upload through S3's multipart API, proxied by Python — so there is no
browser->S3 request and thus no bucket CORS to configure, and private buckets
just work. The page slices the file and base64-encodes each part; this file
drives the S3 side: `start` opens the multipart upload, `part` uploads one part,
`complete` finalizes it (`abort` cancels). The S3 UploadId and the per-part
ETags live in a job file between calls, since each runPython call is a fresh
process.
"""
import base64
import json
import os
import tempfile

import s3lib

JOB_DIR = os.path.join(tempfile.gettempdir(), "s3_browser_uploads")


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


def main(action: str = "start", account_id: str = "", profile: str = "", region: str = "",
         anonymous: bool = False, endpoint_url: str = "", bucket: str = "", key: str = "",
         content_type: str = "", part_number: int = 0, content_b64: str = "", job: str = ""):
    os.makedirs(JOB_DIR, exist_ok=True)
    try:
        client = s3lib.client(s3lib.resolve(account_id, profile, region, anonymous, endpoint_url))

        if action == "start":
            kw = {"Bucket": bucket, "Key": key}
            if content_type:
                kw["ContentType"] = content_type
            resp = client.create_multipart_upload(**kw)
            import uuid
            job = uuid.uuid4().hex[:12]
            _write(_job_path(job), {"bucket": bucket, "key": key,
                                    "upload_id": resp["UploadId"], "parts": []})
            return {"job": job, "upload_id": resp["UploadId"]}

        if action == "part":
            path = _job_path(job)
            if not os.path.exists(path):
                return {"error": {"code": "NoJob", "message": "upload session not found"}}
            st = _read(path)
            resp = client.upload_part(Bucket=st["bucket"], Key=st["key"], PartNumber=part_number,
                                      UploadId=st["upload_id"], Body=base64.b64decode(content_b64))
            st["parts"] = [p for p in st["parts"] if p["PartNumber"] != part_number]
            st["parts"].append({"PartNumber": part_number, "ETag": resp["ETag"]})
            _write(path, st)
            return {"part_number": part_number, "done_parts": len(st["parts"])}

        if action == "complete":
            path = _job_path(job)
            if not os.path.exists(path):
                return {"error": {"code": "NoJob", "message": "upload session not found"}}
            st = _read(path)
            parts = sorted(st["parts"], key=lambda p: p["PartNumber"])
            resp = client.complete_multipart_upload(
                Bucket=st["bucket"], Key=st["key"], UploadId=st["upload_id"],
                MultipartUpload={"Parts": parts})
            os.remove(path)
            return {"key": st["key"], "etag": (resp.get("ETag") or "").strip('"'), "parts": len(parts)}

        if action == "abort":
            path = _job_path(job)
            if os.path.exists(path):
                st = _read(path)
                client.abort_multipart_upload(Bucket=st["bucket"], Key=st["key"], UploadId=st["upload_id"])
                os.remove(path)
            return {"aborted": True}

        return {"error": {"code": "UnknownAction", "message": action}}
    except Exception as e:  # noqa: BLE001
        if s3lib.is_botocore_error(e):
            return s3lib.envelope(e)
        raise
