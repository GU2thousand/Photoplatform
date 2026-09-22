"""Real AWS acceptance, with raw pass/fail evidence and no signed URLs in artifacts.

Uses pre-provisioned disposable owner, unrelated user, and admin tokens. No DB writes.
Run: python scripts/cloud_acceptance.py --output benchmarks/results/cloud-acceptance.json
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs, urlparse, urlunparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, cloud_guard, fixture, put, required, write_report


def expires_at(url):
    """Canned CloudFront policy / real S3 URL expiry, not a locally modified signature."""
    query = parse_qs(urlparse(url).query)
    if "Expires" in query:
        return float(query["Expires"][0])
    if "X-Amz-Date" in query and "X-Amz-Expires" in query:
        return datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp() + int(query["X-Amz-Expires"][0])
    raise ValueError("Expected a canned CloudFront signed URL or SigV4 S3 presigned URL")


def require_status(response, allowed):
    if response.status_code not in allowed:
        raise AssertionError(f"HTTP {response.status_code}; expected {sorted(allowed)}")
    return response.status_code


class Acceptance:
    def __init__(self, aws, report, output, max_wait):
        self.api = CloudAPI()
        self.aws = aws
        self.report = report
        self.output = output
        self.max_wait = max_wait
        self.uploads = []
        self.payload = fixture()
        self.other = required("TEST_OTHER_TOKEN")
        self.admin = required("TEST_ADMIN_TOKEN")
        self.cdn = required("CLOUDFRONT_DOMAIN").removeprefix("https://").rstrip("/")
        if len({self.api.token, self.other, self.admin}) != 3:
            raise ValueError("Owner, other and admin must use three different tokens")

    def case(self, name, operation):
        started = time.monotonic()
        result = {"name": name}
        try:
            result.update(status="PASS", evidence=operation() or {})
        except Exception as exc:
            result.update(status="FAIL", errorType=type(exc).__name__)
            # Only our assertion errors contain safe HTTP codes / fixed messages.
            if isinstance(exc, (AssertionError, TimeoutError, ValueError)):
                result["reason"] = str(exc)
        result["elapsedSeconds"] = time.monotonic() - started
        self.report["cases"].append(result)
        write_report(self.output, self.report)
        print(f"{result['status']}: {name}", flush=True)

    def create(self, **kwargs):
        upload = self.api.create(self.payload, **kwargs)
        self.uploads.append(upload)
        return upload

    def ready(self, **kwargs):
        upload = self.create(**kwargs)
        require_status(put(upload, self.payload), {200})
        self.api.complete(upload)
        self.api.wait(upload)
        return upload

    def get_url(self, upload, anonymous=False):
        delivery = self.api.expect(self.api.request("GET", f"/api/files/{upload['mediaId']}/url", anonymous=anonymous))
        url = delivery["url"]
        if urlparse(url).scheme != "https" or urlparse(url).hostname != self.cdn:
            raise AssertionError("Authorized delivery must use the selected CloudFront domain")
        if not {"Signature", "Key-Pair-Id", "Expires"}.issubset(parse_qs(urlparse(url).query)):
            raise AssertionError("CloudFront URL lacks canned-policy signature parameters")
        return url

    def wait_expiry(self, url):
        expiry = expires_at(url)
        delay = max(0, expiry + 3 - time.time())
        if delay > self.max_wait:
            raise AssertionError("Actual signed URL TTL exceeds --max-expiry-wait; test not executed")
        while time.time() < expiry + 3:
            time.sleep(min(1, expiry + 3 - time.time()))
        return expiry

    def bucket_controls(self):
        s3 = self.aws.client("s3")
        bucket = required("S3_BUCKET")
        public = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
        if not all(public.get(name) for name in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
            raise AssertionError("All four S3 Block Public Access settings must be enabled")
        version = s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if version != "Enabled":
            raise AssertionError("S3 versioning must be Enabled")
        encryption = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
        if not encryption:
            raise AssertionError("S3 encryption must be configured")
        rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        prefix = "/".join(filter(None, [os.getenv("STORAGE_PREFIX", "").strip("/"), "staging/"]))
        cleanup = [r for r in rules if r.get("Status") == "Enabled" and
                   (r.get("Filter", {}).get("Prefix") == prefix or r.get("Prefix") == prefix) and
                   r.get("Expiration", {}).get("Days") == 1 and r.get("NoncurrentVersionExpiration")]
        if not cleanup:
            raise AssertionError("Expected enabled staging-prefix one-day expiration and noncurrent-version cleanup")
        return {"publicAccessBlock": public, "versioning": version, "encryption": encryption,
                "stagingLifecycleRules": cleanup, "lifecycleExecution": "unmeasured; S3 expiration is asynchronous, not exactly 24 hours"}

    def checksum(self):
        upload = self.create()
        status = require_status(put(upload, b"x" + self.payload[1:]), {400, 403})
        return {"statusCode": status, "sameLengthCorruptPayload": True}

    def browser_preflight(self):
        import requests
        from benchmarks.cloud_common import secure_origin
        origin = secure_origin(required("CLOUD_FRONTEND_ORIGIN"))
        upload = self.create()
        # Content-Length is browser controlled; all returned non-safelisted request headers
        # must survive an actual S3 preflight, including checksum, conditional PUT and metadata.
        requested = sorted(name.lower() for name in upload["headers"] if name.lower() not in {"host", "content-length"})
        response = requests.options(upload["uploadUrl"], headers={"Origin": origin,
            "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": ",".join(requested)},
            timeout=30, allow_redirects=False)
        require_status(response, {200, 204})
        if response.headers.get("Access-Control-Allow-Origin") not in {origin, "*"}:
            raise AssertionError("S3 preflight did not authorize the frontend origin")
        methods = {s.strip().upper() for s in response.headers.get("Access-Control-Allow-Methods", "").split(",")}
        allowed = {s.strip().lower() for s in response.headers.get("Access-Control-Allow-Headers", "").split(",")}
        if "PUT" not in methods or ("*" not in allowed and not set(requested).issubset(allowed)):
            raise AssertionError("S3 CORS does not allow every returned signed upload header")
        return {"statusCode": response.status_code, "origin": origin, "requestedHeaders": requested,
                "allowedHeaders": sorted(allowed), "allowedMethods": sorted(methods)}

    def mime(self):
        upload = self.create()
        headers = dict(upload["headers"])
        for name in list(headers):
            if name.lower() == "content-type":
                headers[name] = "application/octet-stream"
        status = require_status(put(upload, self.payload, headers), {400, 403})
        return {"wrongSignedMimeStatusCode": status}

    def invalid_declarations(self):
        statuses = {}
        for name, overrides in (("unsupportedMime", {"contentType": "application/javascript"}),
                                ("oversizedDeclaration", {"size": int(os.getenv("UPLOAD_MAX_BYTES", 15 * 1024 * 1024)) + 1})):
            body = {"filename": "invalid.jpg", "contentType": "image/jpeg", "size": len(self.payload),
                    "sha256": "0" * 64, "title": "Cloud negative validation", "visibility": "PRIVATE", **overrides}
            statuses[name] = require_status(self.api.request("POST", "/api/uploads",
                headers={"Idempotency-Key": str(uuid.uuid4())}, json=body), {400})
        return statuses

    def actual_size(self):
        # The checksum still matches the full payload: failure cannot be attributed to checksum corruption.
        upload = self.create(size=len(self.payload) - 1)
        response = put(upload, self.payload)
        if response.status_code == 200:
            completion = self.api.request("POST", f"/api/uploads/{upload['uploadId']}/complete")
            status = require_status(completion, {400})
            return {"putStatusCode": 200, "completeStatusCode": status, "enforcement": "completion metadata validation"}
        return {"putStatusCode": require_status(response, {400, 403}), "enforcement": "S3 signature bound length"}

    def wrong_image_bytes(self):
        upload = self.create(contentType="image/png")
        require_status(put(upload, self.payload), {200})
        self.api.complete(upload)
        failed = self.api.wait(upload, target="FAILED")
        if failed.get("errorCode") != "INVALID_IMAGE":
            raise AssertionError("Decoded MIME mismatch must fail as INVALID_IMAGE")
        return {"status": failed["status"], "errorCode": failed["errorCode"]}

    def idempotency(self):
        from concurrent.futures import ThreadPoolExecutor
        upload = self.create()
        require_status(put(upload, self.payload), {200})
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.api.complete(upload), range(3)))
        if any(row["mediaId"] != upload["mediaId"] for row in results):
            raise AssertionError("Duplicate completion returned different media IDs")
        self.api.wait(upload)
        from benchmarks.cloud_common import database_timings
        database = database_timings([upload["mediaId"]])
        if database["status"] == "measured" and len(database["rows"]) != 1:
            raise AssertionError("Duplicate completion created multiple processing jobs")
        return {"mediaId": upload["mediaId"], "concurrentCompletions": len(results), "database": database}

    def authorization_and_raw_storage(self):
        import requests
        upload = self.create()
        require_status(put(upload, self.payload), {200})
        status = require_status(self.api.request("POST", f"/api/uploads/{upload['uploadId']}/complete", token=self.other), {403, 404})
        unsigned = urlunparse(urlparse(upload["uploadUrl"])._replace(query=""))
        raw = require_status(requests.get(unsigned, timeout=30, allow_redirects=False), {403})
        self.api.complete(upload)
        self.api.wait(upload)
        path = f"/api/files/{upload['mediaId']}/url"
        unrelated = require_status(self.api.request("GET", path, token=self.other), {403})
        anonymous = require_status(self.api.request("GET", path, anonymous=True), {403})
        signed = self.get_url(upload)
        valid = require_status(requests.get(signed, timeout=30, allow_redirects=False), {200})
        unsigned_cdn = urlunparse(urlparse(signed)._replace(query=""))
        no_signature = require_status(requests.get(unsigned_cdn, timeout=30, allow_redirects=False), {403})
        return {"unauthorizedCompletionStatus": status, "rawS3Status": raw, "unrelatedApiStatus": unrelated,
                "anonymousApiStatus": anonymous, "validSignedCloudFrontStatus": valid, "unsignedCloudFrontStatus": no_signature}

    def moderation(self):
        import requests
        upload = self.ready(visibility="PUBLIC")
        path = f"/api/files/{upload['mediaId']}/url"
        pending = require_status(self.api.request("GET", path, anonymous=True), {403})
        self.api.expect(self.api.request("PATCH", f"/api/images/{upload['mediaId']}/moderation?status=APPROVED", token=self.admin))
        url = self.get_url(upload, anonymous=True)
        approved = require_status(requests.get(url, timeout=30, allow_redirects=False), {200})
        self.api.expect(self.api.request("PATCH", f"/api/images/{upload['mediaId']}/moderation?status=REJECTED", token=self.admin))
        rejected = require_status(self.api.request("GET", path, anonymous=True), {403})
        return {"pendingAnonymousApiStatus": pending, "approvedCloudFrontStatus": approved, "rejectedAnonymousApiStatus": rejected}

    def expiry_and_deletion(self):
        import requests
        # Create both URLs before waiting so the real expiration test does not double the TTL.
        expired_upload = self.create()
        media = self.ready()
        url = self.get_url(media)
        require_status(requests.get(url, timeout=30, allow_redirects=False), {200})  # Warm edge cache.
        self.api.expect(self.api.request("DELETE", f"/api/images/{media['mediaId']}"), (200, 204))
        new_grant = require_status(self.api.request("GET", f"/api/files/{media['mediaId']}/url"), {403, 404})
        self.api.wait(media, target="DELETED")
        cached = requests.get(url, timeout=30, allow_redirects=False).status_code
        self.wait_expiry(url)
        cdn = require_status(requests.get(url, timeout=30, allow_redirects=False), {403})
        self.wait_expiry(expired_upload["uploadUrl"])
        s3 = require_status(put(expired_upload, self.payload), {403})
        return {"newGrantAfterDeleteStatus": new_grant, "oldGrantImmediatelyAfterDeleteStatus": cached,
                "expiredCloudFrontStatus": cdn, "expiredPresignedPutStatus": s3,
                "revocationContract": "New grants denied immediately; previously issued CloudFront grants bounded by their actual signed TTL"}

    def cleanup(self):
        rows = []
        for upload in self.uploads:
            try:
                self.api.cleanup(upload)
                rows.append({"mediaId": upload["mediaId"], "cleanup": "requested"})
            except Exception as exc:
                rows.append({"mediaId": upload["mediaId"], "cleanup": "failed", "errorType": type(exc).__name__})
        return {"uploads": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="benchmarks/results/cloud-acceptance.json")
    parser.add_argument("--max-expiry-wait", type=int, default=1200)
    args = parser.parse_args()
    report = {"kind": "real-aws-cloud-acceptance", "cases": [], "status": "FAIL"}
    suite = None
    try:
        aws, provenance = cloud_guard()
        report["provenance"] = provenance
        suite = Acceptance(aws, report, args.output, args.max_expiry_wait)
        for name, test in (("private_bucket_configuration", suite.bucket_controls),
                           ("actual_browser_s3_cors_preflight", suite.browser_preflight),
                           ("wrong_checksum_rejected", suite.checksum),
                           ("wrong_signed_mime_rejected", suite.mime),
                           ("invalid_mime_and_oversize_declarations_rejected", suite.invalid_declarations),
                           ("actual_payload_size_enforced", suite.actual_size),
                           ("decoded_mime_mismatch_rejected", suite.wrong_image_bytes),
                           ("duplicate_completion_idempotent", suite.idempotency),
                           ("authorization_private_s3_and_cloudfront", suite.authorization_and_raw_storage),
                           ("public_moderation_gate", suite.moderation),
                           ("actual_expiry_and_bounded_deletion", suite.expiry_and_deletion)):
            suite.case(name, test)
        report["status"] = "PASS" if all(row["status"] == "PASS" for row in report["cases"]) else "FAIL"
    except Exception as exc:
        report["fatalErrorType"] = type(exc).__name__
    finally:
        if suite:
            report["cleanup"] = suite.cleanup()
            if any(row["cleanup"] == "failed" for row in report["cleanup"]["uploads"]):
                report["status"] = "FAIL"
        write_report(args.output, report)
    print(f"Cloud acceptance: {report['status']}; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
