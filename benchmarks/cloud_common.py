"""Shared cloud harness primitives. Never serialize bearer tokens or signed URLs."""
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlparse
import uuid


def percentile(values, quantile):
    """Linear interpolation (same convention as numpy's default); missing != zero."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    left = math.floor(position)
    return ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * (position - left)


def summary(values):
    return {"samples": len(values), "p50": percentile(values, .5), "p95": percentile(values, .95),
            "p99": percentile(values, .99)}


def write_report(path, report):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, default=str) + "\n")
    temporary.replace(target)


def revision():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def secure_origin(value):
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Cloud endpoints must be HTTPS and must not contain credentials/query/fragment")
    return value.rstrip("/")


def cloud_guard():
    """Use real AWS and fail before writes unless the explicitly selected bucket is disposable."""
    if os.getenv("ALLOW_CLOUD_TEST_WRITES") != "1" or os.getenv("DISPOSABLE_ENVIRONMENT") != "true":
        raise ValueError("Set ALLOW_CLOUD_TEST_WRITES=1 and DISPOSABLE_ENVIRONMENT=true for a disposable AWS dev environment")
    import boto3
    account = required("EXPECTED_AWS_ACCOUNT_ID")
    region = required("AWS_REGION")
    bucket = required("S3_BUCKET")
    api = secure_origin(required("API_URL"))
    aws = boto3.Session(region_name=region)
    identity = aws.client("sts").get_caller_identity()
    if identity["Account"] != account:
        raise ValueError("STS account does not match EXPECTED_AWS_ACCOUNT_ID")
    s3 = aws.client("s3")
    tags = {row["Key"]: row["Value"] for row in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
    if tags.get("Project") != "photoplatform" or tags.get("Environment") != "dev" or tags.get("DisposableEnvironment") != "true":
        raise ValueError("Bucket must have Project=photoplatform, Environment=dev, DisposableEnvironment=true tags")
    # Assert bucket ownership separately: a same-named configured endpoint is not evidence.
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    evidence = {"awsAccountId": account, "region": region, "bucket": bucket,
                "apiHost": urlparse(api).hostname, "bucketTags": tags, "revision": revision(),
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    return aws, evidence


class CloudAPI:
    def __init__(self, token=None):
        self.base = secure_origin(required("API_URL"))
        self.token = token or required("TEST_OWNER_TOKEN")

    def request(self, method, path, token=None, anonymous=False, **kwargs):
        import requests
        headers = dict(kwargs.pop("headers", {}))
        if not anonymous:
            headers["Authorization"] = "Bearer " + (token or self.token)
        return requests.request(method, self.base + path, headers=headers, timeout=60,
                                allow_redirects=False, **kwargs)

    @staticmethod
    def expect(response, accepted=(200,)):
        if response.status_code not in accepted:
            # Response bodies/exceptions can contain credentials or signed URLs.
            raise RuntimeError(f"Unexpected HTTP status {response.status_code}; expected {accepted}")
        return response.json() if response.content else None

    def create(self, payload, *, visibility="PRIVATE", **overrides):
        key = str(uuid.uuid4())
        body = {"filename": "cloud-test.jpg", "contentType": "image/jpeg", "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(), "title": "Cloud verification " + key,
                "description": "Disposable cloud harness fixture", "category": "Benchmark", "tags": "cloud-test",
                "visibility": visibility}
        body.update(overrides)
        upload = self.expect(self.request("POST", "/api/uploads", headers={"Idempotency-Key": key}, json=body))
        validate_s3_url(upload["uploadUrl"])
        return upload

    def complete(self, upload):
        return self.expect(self.request("POST", f"/api/uploads/{upload['uploadId']}/complete"))

    def state(self, upload):
        return self.expect(self.request("GET", f"/api/uploads/{upload['uploadId']}"))

    def wait(self, upload, timeout=600, target="READY"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.state(upload)
            if current["status"] == target:
                return current
            if current["status"] in {"FAILED", "DELETED", "ABORTED"}:
                raise RuntimeError("Unexpected terminal upload status: " + current["status"])
            time.sleep(1)
        raise TimeoutError("Upload did not reach " + target)

    def cleanup(self, upload):
        current = self.state(upload)
        if current["status"] in {"UPLOADING", "ABORTED"}:
            self.expect(self.request("DELETE", f"/api/uploads/{upload['uploadId']}"))
        elif current["status"] not in {"DELETED", "DELETING"}:
            self.expect(self.request("DELETE", f"/api/images/{upload['mediaId']}"), (200, 204))


def validate_s3_url(url):
    parsed = urlparse(secure_origin_url(url))
    bucket = required("S3_BUCKET")
    region = required("AWS_REGION")
    if parsed.hostname not in {f"{bucket}.s3.{region}.amazonaws.com", f"{bucket}.s3.amazonaws.com"}:
        raise ValueError("Presigned upload URL must point to the selected real Amazon S3 bucket")


def secure_origin_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Signed URL must use HTTPS")
    return url


def put(upload, payload, headers=None):
    import requests
    validate_s3_url(upload["uploadUrl"])
    return requests.put(upload["uploadUrl"], headers=headers or upload["headers"], data=payload,
                        timeout=60, allow_redirects=False)


def fixture():
    return (Path(__file__).resolve().parent / "fixtures" / "upload.jpg").read_bytes()


def register_user():
    """One disposable account per virtual user avoids measuring per-owner upload quotas."""
    api = CloudAPI()
    identity = uuid.uuid4().hex
    result = api.expect(api.request("POST", "/api/auth/register", anonymous=True, json={
        "name": "Cloud benchmark", "email": f"cloud-{identity}@test.example", "password": uuid.uuid4().hex}))
    return CloudAPI(result["token"])


def upload_one(api, payload, timeout=600, wait_ready=True):
    """Raw denominators retain failures. Poll latency is explicitly separate from worker time."""
    row = {"attemptId": str(uuid.uuid4()), "success": False, "stage": "create", "bytes": len(payload)}
    started = time.monotonic()
    upload = None
    try:
        t = time.monotonic()
        upload = api.create(payload)
        row.update(uploadId=upload["uploadId"], mediaId=upload["mediaId"], createMs=(time.monotonic() - t) * 1000)
        row["stage"] = "put"
        t = time.monotonic()
        response = put(upload, payload)
        api.expect(response, (200,)) if response.status_code != 200 else None
        row["uploadMs"] = (time.monotonic() - t) * 1000
        row["stage"] = "complete"
        t = time.monotonic()
        api.complete(upload)
        row["completeMs"] = (time.monotonic() - t) * 1000
        completed = time.monotonic()
        if wait_ready:
            row["stage"] = "ready"
            api.wait(upload, timeout=timeout)
            row["completionToReadyObservedMs"] = (time.monotonic() - completed) * 1000
            row["endToEndMs"] = (time.monotonic() - started) * 1000
        row.update(success=True, stage="ready" if wait_ready else "completed")
    except Exception as error:
        row["errorType"] = type(error).__name__
    row["elapsedMs"] = (time.monotonic() - started) * 1000
    # Internal return object holds signed URL; callers must serialize only row.
    return row, upload


def aggregate(rows):
    successful = [row for row in rows if row["success"]]
    result = {"attempted": len(rows), "successful": len(successful), "failed": len(rows) - len(successful),
              "successRate": len(successful) / len(rows) if rows else None}
    for field in ("createMs", "uploadMs", "completeMs", "completionToReadyObservedMs", "endToEndMs"):
        result[field] = summary([row[field] for row in successful if field in row])
    return result


def cost_per_thousand(total_cost, successful_images):
    if total_cost < 0 or successful_images < 0:
        raise ValueError("Cost and image count must be nonnegative")
    return total_cost * 1000 / successful_images if successful_images else None


def database_timings(media_ids):
    """Optional read-only DB evidence; no DB URL or query payload leaves this function."""
    url = os.getenv("BENCHMARK_DATABASE_URL")
    if not url:
        return {"status": "unmeasured", "reason": "BENCHMARK_DATABASE_URL not supplied", "rows": []}
    import psycopg
    with psycopg.connect(url, connect_timeout=10) as db:
        db.execute("SET TRANSACTION READ ONLY")
        db.execute("SET LOCAL statement_timeout='15s'")
        rows = db.execute("""SELECT media_id,status,attempt,
            extract(epoch FROM finished_at-started_at)*1000,
            extract(epoch FROM started_at-created_at)*1000,
            extract(epoch FROM finished_at-created_at)*1000
            FROM media_processing_jobs WHERE job_type='MEDIA_PROCESS' AND media_id=ANY(%s) ORDER BY media_id""",
            (media_ids,)).fetchall()
    names = ("mediaId", "status", "attempt", "lastAttemptProcessingMs", "lastAttemptQueueWaitMs", "jobEndToEndMs")
    data = [dict(zip(names, [float(x) if hasattr(x, "as_tuple") else x for x in row])) for row in rows]
    done = [row for row in data if row["status"] == "DONE"]
    return {"status": "measured", "rows": data,
            "processingMs": summary([r["lastAttemptProcessingMs"] for r in done if r["lastAttemptProcessingMs"] is not None]),
            "queueWaitMs": summary([r["lastAttemptQueueWaitMs"] for r in done if r["lastAttemptQueueWaitMs"] is not None]),
            "definition": "Final attempt finished-started; queue wait is final attempt started-created and includes earlier attempts; not a per-attempt history"}
