"""Failure boundaries for durable media jobs; no network or running services needed."""
import hashlib
import io
import unittest
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from PIL import Image

from app.imaging import InvalidImage
from app.runtime import Worker


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.worker = object.__new__(Worker)
        self.worker.s3 = MagicMock()
        self.worker.bucket = "test-private-bucket"
        self.worker.encoder = None
        self.job = {"id": uuid.uuid4(), "media_id": 17, "asset_version": 1,
                    "pipeline_version": "media-v1", "claim_token": uuid.uuid4(),
                    "attempt": 1, "job_type": "MEDIA_PROCESS", "traceparent": None,
                    "created_at": datetime.now(timezone.utc), "status": "QUEUED"}
        output = io.BytesIO()
        Image.new("RGB", (30, 20), "orange").save(output, "JPEG")
        self.data = output.getvalue()
        self.image = {"id": 17, "asset_version": 1, "deleted_at": None}
        self.session = {"object_key": "staging/session/original", "expected_bytes": len(self.data),
                        "expected_sha256": hashlib.sha256(self.data).hexdigest(),
                        "content_type": "image/jpeg", "created_at": datetime.now(timezone.utc)}
        self.conn = MagicMock()
        self.conn.transaction.side_effect = lambda: nullcontext()
        self.conn.__enter__.return_value = self.conn
        self.worker.read = MagicMock(return_value=self.data)

    def rows(self, *rows):
        """Set rows for SELECT/RETURNING statements; all SQL remains inspectable."""
        results = iter(rows)
        def execute(sql, params=None):
            cursor = MagicMock()
            if sql.lstrip().startswith("SELECT") or "RETURNING" in sql:
                cursor.fetchone.return_value = next(results)
            return cursor
        self.conn.execute.side_effect = execute

    def statements(self):
        return [(call.args[0], call.args[1] if len(call.args) > 1 else None)
                for call in self.conn.execute.call_args_list]

    def assert_no_variant_commit(self):
        self.assertFalse(any("INSERT INTO media_variants" in sql for sql, _ in self.statements()))
        self.assertFalse(any("processing_status='READY'" in sql for sql, _ in self.statements()))

    def test_checksum_mismatch_never_writes_any_object_or_metadata(self):
        self.session["expected_sha256"] = "0" * 64
        self.rows(self.image, self.session)
        with self.assertRaises(InvalidImage):
            self.worker.process(self.conn, self.job)
        self.worker.s3.put_object.assert_not_called()
        self.assert_no_variant_commit()

    def test_size_mismatch_never_writes_any_object(self):
        self.session["expected_bytes"] += 1
        self.rows(self.image, self.session)
        with self.assertRaises(InvalidImage):
            self.worker.process(self.conn, self.job)
        self.worker.s3.put_object.assert_not_called()

    def test_decoded_type_must_match_upload_contract(self):
        self.session["content_type"] = "image/png"
        self.rows(self.image, self.session)
        with self.assertRaises(InvalidImage):
            self.worker.process(self.conn, self.job)
        self.worker.s3.put_object.assert_not_called()

    def test_deleted_or_superseded_media_cancels_before_reading_source(self):
        for image in (dict(self.image, deleted_at=datetime.now(timezone.utc)),
                      dict(self.image, asset_version=2)):
            with self.subTest(image=image):
                self.conn.reset_mock()
                self.rows(image)
                self.worker.process(self.conn, self.job)
                self.worker.read.assert_not_called()
                self.worker.s3.put_object.assert_not_called()
                self.assertEqual(self.statements()[-1][1][0], "CANCELLED")

    def test_delete_during_processing_cannot_publish_ready_metadata(self):
        self.rows(self.image, self.session, dict(self.image, deleted_at=datetime.now(timezone.utc)))
        self.worker.process(self.conn, self.job)
        self.assertEqual(self.worker.s3.put_object.call_count, 5)
        self.assert_no_variant_commit()
        self.assertEqual(self.statements()[-1][1][0], "CANCELLED")

    def test_partial_object_write_does_not_publish_ready_metadata(self):
        self.rows(self.image, self.session)
        self.worker.s3.put_object.side_effect = [None, OSError("temporary storage outage")]
        with self.assertRaises(OSError):
            self.worker.process(self.conn, self.job)
        self.assert_no_variant_commit()

    def test_delete_storage_failure_preserves_rows_for_retry(self):
        self.rows({"deleted_at": datetime.now(timezone.utc)})
        self.worker.s3.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "first"}, {"Key": "second"}]}]
        self.worker.s3.delete_object.side_effect = [None, OSError("offline")]
        with self.assertRaises(OSError):
            self.worker.delete(self.conn, dict(self.job, job_type="DELETE"))
        self.assertEqual(len(self.statements()), 1)

    def test_delete_walks_all_pages_before_committing_deleted(self):
        self.rows({"deleted_at": datetime.now(timezone.utc)})
        self.worker.s3.get_paginator.return_value.paginate.return_value = [
            {"Contents": [{"Key": "first"}]}, {}, {"Contents": [{"Key": "second"}]}]
        self.worker.delete(self.conn, dict(self.job, job_type="DELETE"))
        self.assertEqual([c.kwargs["Key"] for c in self.worker.s3.delete_object.call_args_list],
                         ["first", "second"])
        self.assertTrue(any("processing_status='DELETED'" in sql for sql, _ in self.statements()))
        self.assertEqual(self.statements()[-1][1][0], "DONE")

    def test_delete_without_tombstone_never_removes_storage(self):
        self.rows({"deleted_at": None})
        self.worker.delete(self.conn, dict(self.job, job_type="DELETE"))
        self.worker.s3.get_paginator.assert_not_called()
        self.assertEqual(self.statements()[-1][1][0], "CANCELLED")

    def test_transient_storage_error_retries_then_dead_letters(self):
        error = ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")
        for attempt, expected in ((1, "RETRY"), (3, "DLQ")):
            with self.subTest(attempt=attempt):
                self.conn.reset_mock()
                self.worker.fail(self.conn, dict(self.job, attempt=attempt), error)
                statements = self.statements()
                job_update = next(params for sql, params in statements if "UPDATE media_processing_jobs" in sql)
                self.assertEqual(job_update[:2], (expected, "STORAGE_UNAVAILABLE"))
                self.assertTrue(any("last_published_at=NULL" in sql for sql, _ in statements))
                image_updates = [sql for sql, _ in statements if "UPDATE image_assets" in sql]
                self.assertEqual(len(image_updates), int(expected == "DLQ"))
                if image_updates:
                    self.assertIn("deleted_at IS NULL", image_updates[0])

    def test_missing_source_is_permanent_and_embedding_failure_is_independent(self):
        error = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        self.worker.fail(self.conn, dict(self.job, job_type="EMBED"), error)
        statements = self.statements()
        job_update = next(params for sql, params in statements if "UPDATE media_processing_jobs" in sql)
        self.assertEqual(job_update[:2], ("DLQ", "OBJECT_MISSING"))
        image_updates = [sql for sql, _ in statements if "UPDATE image_assets" in sql]
        self.assertIn("embedding_status='FAILED'", image_updates[0])
        self.assertNotIn("processing_status", image_updates[0])

    def test_duplicate_running_delivery_does_not_execute_without_media_lock(self):
        self.rows(self.job, {"locked": False})
        self.worker.process = MagicMock()
        with patch("app.runtime.connection", return_value=self.conn):
            self.assertTrue(self.worker.handle(self.job["id"]))
        self.worker.process.assert_not_called()
        self.assertFalse(any("UPDATE media_processing_jobs" in sql for sql, _ in self.statements()))

    def test_durable_failure_save_error_is_propagated_for_broker_redelivery(self):
        self.rows(self.job, {"locked": True}, self.job, {"pg_advisory_unlock": True})
        self.worker.process = MagicMock(side_effect=OSError("storage outage"))
        self.worker.fail = MagicMock(side_effect=OSError("database outage"))
        with patch("app.runtime.connection", return_value=self.conn):
            with self.assertRaises(OSError):
                self.worker.handle(self.job["id"])
        self.assertTrue(any("pg_advisory_unlock" in sql for sql, _ in self.statements()))

    def test_read_enforces_both_reported_and_actual_byte_limits(self):
        del self.worker.read
        for length, payload in ((11, b"a"), (2, b"a" * 11)):
            with self.subTest(length=length):
                body = MagicMock()
                body.__enter__.return_value = body
                body.read.return_value = payload
                self.worker.s3.get_object.return_value = {"ContentLength": length, "Body": body}
                with patch("app.runtime.MAX_BYTES", 10), self.assertRaises(InvalidImage):
                    self.worker.read("source")
                body.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
