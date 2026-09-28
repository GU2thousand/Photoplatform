"""GC ownership and durable late-write recovery without PostgreSQL or S3."""
import unittest
import uuid
from unittest.mock import MagicMock

from app.garbage import collect_garbage


def attempt(media_id=42, pipeline="media-v1"):
    claim = uuid.uuid4()
    return {"claim_token": claim, "job_id": uuid.uuid4(), "media_id": media_id,
            "object_prefix": f"media/{media_id}/v1/{pipeline}/{claim}/"}


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.locked = True
        self.active = False
        self.referenced = False
        self.pid = 123
        self.available = True
        self.session_checks = 0
        self.lose_at_check = None
        self.change_pid_at_check = None
        self.before_protection = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        if not self.available:
            raise RuntimeError("connection lost with secret=must-not-log")
        if "SELECT 1 AS alive" in sql:
            self.session_checks += 1
            if self.session_checks == self.lose_at_check:
                self.available = False
                raise RuntimeError("connection lost with secret=must-not-log")
            if self.session_checks == self.change_pid_at_check:
                self.pid += 1
            return Cursor({"alive": 1, "backend_pid": self.pid})
        if "FROM media_object_attempts" in sql:
            return Cursor(rows=self.rows)
        if "pg_try_advisory_lock" in sql:
            return Cursor({"locked": self.locked})
        if "AS active" in sql:
            if self.before_protection:
                self.before_protection()
            return Cursor({"active": self.active, "referenced": self.referenced})
        if "UPDATE media_object_attempts" in sql or "pg_advisory_unlock" in sql:
            return Cursor()
        raise AssertionError("Unexpected SQL")

    @property
    def reschedules(self):
        return [parameters for sql, parameters in self.calls if "UPDATE media_object_attempts" in sql]


class GarbageCollectionTests(unittest.TestCase):
    def setUp(self):
        self.attempt = attempt()
        self.conn = FakeConnection([self.attempt])
        self.factory = MagicMock(return_value=self.conn)
        self.s3 = MagicMock()
        self.s3.get_paginator.return_value.paginate.return_value = []
        self.prefix = "photos/" + self.attempt["object_prefix"]

    def collect(self, batch_size=100):
        return collect_garbage(self.factory, self.s3, "photo-bucket", lambda key: "photos/" + key,
                               batch_size=batch_size)

    def test_active_claim_and_referenced_prefix_are_protected_and_rescheduled(self):
        for protected in ("active", "referenced"):
            with self.subTest(protected=protected):
                self.setUp()
                setattr(self.conn, protected, True)
                result = self.collect()
                self.assertEqual(result["protected"], 1)
                self.assertEqual(self.conn.reschedules, [(60 if protected == "active" else 3600, self.attempt["claim_token"])])
                self.s3.get_paginator.assert_not_called()
                self.s3.delete_object.assert_not_called()
                sql, parameters = next((sql, params) for sql, params in self.conn.calls if "AS active" in sql)
                self.assertIn("status='RUNNING' AND lease_until>now()", sql)
                self.assertEqual(parameters[:3], (self.attempt["job_id"], self.attempt["media_id"],
                                                  self.attempt["claim_token"]))
                self.assertEqual(parameters[-1], self.attempt["object_prefix"] + "%")

    def test_lock_miss_defers_one_minute_without_storage_io_to_prevent_starvation(self):
        self.conn.locked = False
        result = self.collect()
        self.assertEqual(result["lock_missed"], 1)
        self.assertEqual(self.conn.reschedules, [(self.attempt["claim_token"],)])
        reschedule_sql = next(sql for sql, _ in self.conn.calls if "UPDATE media_object_attempts" in sql)
        self.assertIn("interval '1 minute'", reschedule_sql)
        self.s3.get_paginator.assert_not_called()
        self.s3.delete_object.assert_not_called()

    def test_lock_miss_session_failure_preserves_due_row_and_stops_sweep(self):
        self.conn.locked = False
        self.conn.lose_at_check = 2
        result = self.collect()
        self.assertEqual(result["failed"], 1)
        self.assertFalse(self.conn.reschedules)
        self.s3.get_paginator.assert_not_called()
        self.s3.delete_object.assert_not_called()
        self.factory.assert_called_once_with()

    def test_deletes_all_versions_and_delete_markers_from_every_page(self):
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": self.prefix + "small.webp", "VersionId": "old"},
                          {"Key": self.prefix + "small.webp", "VersionId": "latest"}],
             "DeleteMarkers": [{"Key": self.prefix + "original", "VersionId": "marker"}]},
            {"Versions": [{"Key": self.prefix + "medium.webp", "VersionId": "null"}]}]
        result = self.collect()
        self.assertEqual(result["swept"], 1)
        self.assertEqual(self.s3.delete_object.call_count, 4)
        self.assertEqual([call.kwargs["VersionId"] for call in self.s3.delete_object.call_args_list],
                         ["old", "latest", "marker", "null"])
        self.s3.get_paginator.assert_called_once_with("list_object_versions")
        self.s3.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="photo-bucket", Prefix=self.prefix)
        self.assertEqual(self.conn.reschedules, [(3600, self.attempt["claim_token"])])
        self.assertFalse(any("DELETE FROM media_object_attempts" in sql for sql, _ in self.conn.calls))
        self.factory.assert_called_once_with()

    def test_session_loss_after_listing_stops_before_any_delete(self):
        def pages():
            self.conn.available = False
            yield {"Versions": [{"Key": self.prefix + "small.webp", "VersionId": "v1"}]}

        self.s3.get_paginator.return_value.paginate.return_value = pages()
        with self.assertLogs("app.garbage", level="WARNING") as logs:
            result = self.collect()
        self.assertEqual(result["failed"], 1)
        self.s3.delete_object.assert_not_called()
        self.assertFalse(self.conn.reschedules)
        self.factory.assert_called_once_with()
        self.assertNotIn("must-not-log", " ".join(logs.output))
        self.assertFalse(any("pg_advisory_unlock" in sql for sql, _ in self.conn.calls))

    def test_mid_page_session_loss_stops_remaining_deletes_and_preserves_due_row(self):
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": self.prefix + "a", "VersionId": "v1"},
                          {"Key": self.prefix + "b", "VersionId": "v2"}]}]
        self.s3.delete_object.side_effect = lambda **kwargs: setattr(self.conn, "available", False)
        result = self.collect()
        self.assertEqual(result["failed"], 1)
        self.s3.delete_object.assert_called_once()
        self.assertFalse(self.conn.reschedules)
        self.factory.assert_called_once_with()

    def test_different_backend_pid_cannot_conceal_a_replaced_session(self):
        self.conn.change_pid_at_check = 2
        result = self.collect()
        self.assertEqual(result["failed"], 1)
        self.s3.get_paginator.assert_not_called()
        self.s3.delete_object.assert_not_called()
        self.assertFalse(self.conn.reschedules)
        self.assertEqual(sum("pg_try_advisory_lock" in sql for sql, _ in self.conn.calls), 1)
        self.factory.assert_called_once_with()

    def test_storage_failure_keeps_attempt_due_and_does_not_log_secrets(self):
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": self.prefix + "a", "VersionId": "v1"}]}]
        self.s3.delete_object.side_effect = RuntimeError("S3 token=must-not-log")
        with self.assertLogs("app.garbage", level="WARNING") as logs:
            result = self.collect()
        self.assertEqual(result["failed"], 1)
        self.assertFalse(self.conn.reschedules)
        self.assertIn("RuntimeError", " ".join(logs.output))
        self.assertNotIn("must-not-log", " ".join(logs.output))
        self.assertTrue(any("pg_advisory_unlock" in sql for sql, _ in self.conn.calls))

    def test_new_reference_during_listing_prevents_deletion(self):
        def pages():
            self.conn.referenced = True
            yield {"Versions": [{"Key": self.prefix + "a", "VersionId": "v1"}]}

        self.s3.get_paginator.return_value.paginate.return_value = pages()
        result = self.collect()
        self.assertEqual(result["protected"], 1)
        self.s3.delete_object.assert_not_called()
        self.assertEqual(self.conn.reschedules, [(3600, self.attempt["claim_token"])])

    def test_retained_registry_deletes_second_wave_puts_after_successful_sweep(self):
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": self.prefix + "small.webp", "VersionId": "first-wave"}]}]
        self.assertEqual(self.collect()["swept"], 1)
        self.s3.delete_object.assert_called_once_with(Bucket="photo-bucket",
                                                    Key=self.prefix + "small.webp", VersionId="first-wave")
        # The same retained DB attempt is due again after its scheduled delay.
        # A PUT that was still in flight can arrive after the first deletion.
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": self.prefix + "late.webp", "VersionId": "late"}]}]
        self.assertEqual(self.collect()["swept"], 1)
        self.assertEqual(self.s3.delete_object.call_count, 2)
        self.assertEqual(self.s3.delete_object.call_args.kwargs,
                         {"Bucket": "photo-bucket", "Key": self.prefix + "late.webp", "VersionId": "late"})
        self.assertEqual(len(self.conn.reschedules), 2)
        self.assertFalse(any("DELETE FROM media_object_attempts" in sql for sql, _ in self.conn.calls))

    def test_due_batch_is_bounded_and_like_wildcards_are_literal(self):
        self.attempt = attempt(pipeline="media_test%v1")
        self.conn.rows = [self.attempt]
        self.conn.referenced = True
        self.collect(batch_size=10000)
        due_sql, due_parameters = next((sql, params) for sql, params in self.conn.calls
                                      if "FROM media_object_attempts" in sql)
        self.assertIn("next_cleanup_at<=now()", due_sql)
        self.assertEqual(due_parameters, (100,))
        protection_parameters = next(params for sql, params in self.conn.calls if "AS active" in sql)
        self.assertIn("media\\_test\\%v1", protection_parameters[-1])

    def test_listing_outside_attempt_prefix_is_never_deleted(self):
        self.s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": "photos/media/another/original", "VersionId": "v1"}]}]
        result = self.collect()
        self.assertEqual(result["failed"], 1)
        self.s3.delete_object.assert_not_called()
        self.assertFalse(self.conn.reschedules)


if __name__ == "__main__":
    unittest.main()
