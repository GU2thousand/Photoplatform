"""Deterministic recovery tests; ONLY for a disposable, running pipeline stack.

Requires the same TEST_API_URL, TEST_DATABASE_URL, and ALLOW_INTEGRATION_WRITES=1
as integration_test.py. Does not stop or restart services. It deliberately alters
only media created by these tests to simulate lost delivery, stale leases, and
historical jobs. Run after normal integration tests and outside benchmarks.
"""
import time
import os
import boto3
import unittest
import uuid

import psycopg

import integration_test as integration

DB = integration.DB
request = integration.request


class PipelineFaults(unittest.TestCase):
    setUpClass = classmethod(integration.PipelineIntegration.setUpClass.__func__)
    create = integration.PipelineIntegration.create
    put = integration.PipelineIntegration.put
    complete = integration.PipelineIntegration.complete
    wait = integration.PipelineIntegration.wait
    ready = integration.PipelineIntegration.ready

    def test_retry_missing_image_returns_not_found(self):
        response = request("POST", "/api/images/9223372036854775807/retry", self.owner)
        self.assertEqual(response.status_code, 404, response.text)

    def test_expired_worker_lease_is_recovered_from_outbox(self):
        upload = self.ready()
        with psycopg.connect(DB, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("UPDATE image_assets SET processing_status='PROCESSING' WHERE id=%s", (upload["mediaId"],))
                conn.execute("""UPDATE media_processing_jobs SET status='RUNNING',attempt=1,
                    lease_until=now()-interval '1 minute',claim_token=%s,finished_at=NULL
                    WHERE media_id=%s AND job_type='MEDIA_PROCESS'""", (uuid.uuid4(), upload["mediaId"]))
                conn.execute("""UPDATE media_outbox SET last_published_at=now()-interval '6 minutes'
                    WHERE job_id IN (SELECT id FROM media_processing_jobs WHERE media_id=%s)""", (upload["mediaId"],))
            self.wait(upload)
            row = conn.execute("SELECT status,attempt FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS'",
                               (upload["mediaId"],)).fetchone()
            self.assertEqual(row, ("DONE", 2))
            self.assertEqual(conn.execute("SELECT count(*) FROM media_variants WHERE media_id=%s",
                                          (upload["mediaId"],)).fetchone()[0], 5)

    def test_delete_while_worker_cannot_claim_does_not_publish_variants(self):
        upload, _, data = self.create()
        self.put(upload, data)
        with psycopg.connect(DB, autocommit=True) as lock:
            lock.execute("SELECT pg_advisory_lock(%s)", (upload["mediaId"],))
            try:
                self.complete(upload)
                response = request("DELETE", f"/api/images/{upload['mediaId']}", self.owner)
                self.assertEqual(response.status_code, 200, response.text)
            finally:
                lock.execute("SELECT pg_advisory_unlock(%s)", (upload["mediaId"],))
            # A busy-lock delivery can already have been ACKed; explicitly make the
            # durable record due so this test doesn't wait for its five-minute watchdog.
            lock.execute("""UPDATE media_outbox SET last_published_at=NULL WHERE job_id IN
                (SELECT id FROM media_processing_jobs WHERE media_id=%s)""", (upload["mediaId"],))
            self.wait(upload, "DELETED")
            self.assertEqual(lock.execute("SELECT count(*) FROM media_variants WHERE media_id=%s",
                                           (upload["mediaId"],)).fetchone()[0], 0)
            self.assertEqual(lock.execute("SELECT status FROM media_processing_jobs WHERE media_id=%s AND job_type='MEDIA_PROCESS'",
                                           (upload["mediaId"],)).fetchone()[0], "CANCELLED")

    def test_versioned_media_erasure_and_legacy_dead_letter_recovery(self):
        s3 = boto3.client("s3", endpoint_url=os.getenv("TEST_STORAGE_ENDPOINT", "http://localhost:19000"),
                          region_name="us-east-1", aws_access_key_id="minioadmin",
                          aws_secret_access_key=os.getenv("MINIO_PASSWORD", "minioadmin"))
        bucket = "generatecloud-assets"
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        upload = self.ready()
        prefix = f"generate-cloud/media/{upload['mediaId']}/"
        key = prefix + "historical"
        for payload in (b"old", b"new"):
            s3.put_object(Bucket=bucket, Key=key, Body=payload)
        s3.delete_object(Bucket=bucket, Key=key)
        before = s3.list_object_versions(Bucket=bucket, Prefix=prefix)
        self.assertGreaterEqual(len(before.get("Versions", [])), 7)
        self.assertTrue(before.get("DeleteMarkers"))
        with psycopg.connect(DB, autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (upload["mediaId"],))
            try:
                response = request("DELETE", f"/api/images/{upload['mediaId']}", self.owner)
                self.assertEqual(response.status_code, 200, response.text)
                # An old worker exhausted retries. The scheduler must recover this
                # without an owner retry endpoint or an operator replay command.
                conn.execute("""UPDATE media_processing_jobs SET status='DLQ',attempt=3,
                    finished_at=now()-interval '2 hours',last_error_code='STORAGE_UNAVAILABLE'
                    WHERE media_id=%s AND job_type='DELETE'""", (upload["mediaId"],))
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (upload["mediaId"],))
            self.wait(upload, "DELETED", timeout=90)
        after = s3.list_object_versions(Bucket=bucket, Prefix=prefix)
        self.assertFalse(after.get("Versions"))
        self.assertFalse(after.get("DeleteMarkers"))

    def test_retry_does_not_restart_historical_pipeline_version(self):
        upload = self.ready()
        old_job = uuid.uuid4()
        with psycopg.connect(DB, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("UPDATE image_assets SET processing_status='FAILED' WHERE id=%s", (upload["mediaId"],))
                conn.execute("""INSERT INTO media_processing_jobs
                    (id,media_id,job_type,asset_version,pipeline_version,status,attempt,last_error_code)
                    VALUES(%s,%s,'MEDIA_PROCESS',1,'retired-pipeline','DLQ',3,'INVALID_IMAGE')""", (old_job, upload["mediaId"]))
                conn.execute("INSERT INTO media_outbox(job_id) VALUES(%s)", (old_job,))
            response = request("POST", f"/api/images/{upload['mediaId']}/retry", self.owner)
            self.assertEqual(response.status_code, 200, response.text)
            self.wait(upload)
            self.assertEqual(conn.execute("SELECT status,attempt FROM media_processing_jobs WHERE id=%s", (old_job,)).fetchone(),
                             ("DLQ", 3))

    def test_terminal_staging_cleanup_resweeps_after_late_put(self):
        upload, _, data = self.create()
        self.put(upload, data)
        response = request("DELETE", f"/api/uploads/{upload['uploadId']}", self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        with psycopg.connect(DB, autocommit=True) as conn:
            # Represents a PUT that finished after a previous cleanup: object exists,
            # but the row says its first cleanup has already completed.
            conn.execute("""UPDATE upload_sessions SET expires_at=now()-interval '3 hours',
                cleaned_at=now()-interval '2 hours' WHERE id=%s""", (upload["uploadId"],))
            end = time.monotonic() + 90
            while time.monotonic() < end:
                fresh = conn.execute("SELECT cleaned_at>now()-interval '2 minutes' FROM upload_sessions WHERE id=%s",
                                     (upload["uploadId"],)).fetchone()[0]
                if fresh:
                    break
                time.sleep(.5)
            else:
                self.fail("Terminal staging object was never reswept after its first cleanup")
            retry = request("POST", f"/api/images/{upload['mediaId']}/retry", self.owner)
            self.assertEqual(retry.status_code, 400, retry.text)

    def test_deleted_prefix_reconciliation_runs_once_then_waits_an_hour(self):
        upload = self.ready()
        response = request("DELETE", f"/api/images/{upload['mediaId']}", self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.wait(upload, "DELETED")
        with psycopg.connect(DB, autocommit=True) as conn:
            old = conn.execute("""UPDATE media_processing_jobs SET finished_at=now()-interval '2 hours'
                WHERE media_id=%s AND job_type='DELETE' RETURNING id,finished_at""", (upload["mediaId"],)).fetchone()
            end = time.monotonic() + 90
            while time.monotonic() < end:
                row = conn.execute("SELECT status,finished_at FROM media_processing_jobs WHERE id=%s", (old[0],)).fetchone()
                if row[0] == "DONE" and row[1] > old[1]:
                    break
                time.sleep(.5)
            else:
                self.fail("Old completed DELETE was not replayed by reconciliation")
            completed_at = row[1]
            # A full second scheduler interval must not requeue the same tombstone.
            time.sleep(35)
            row = conn.execute("SELECT status,finished_at FROM media_processing_jobs WHERE id=%s", (old[0],)).fetchone()
            self.assertEqual(row, ("DONE", completed_at))
            self.assertEqual(request("GET", f"/api/files/{upload['mediaId']}/url", self.owner).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
