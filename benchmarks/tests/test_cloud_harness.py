import importlib.util
import contextlib
import copy
import io
import os
from pathlib import Path
import unittest
import sys
from unittest.mock import Mock, patch

from benchmarks.cloud_common import aggregate, cost_per_thousand, percentile, secure_origin, validate_s3_url
from benchmarks.pgvector_comparison import plan_uses_index, recall_at_k, vectors
from benchmarks.ecs_worker_scaling import ServiceControl
from benchmarks.cloud_common import upload_one

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("cloud_acceptance", ROOT / "scripts" / "cloud_acceptance.py")
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class CloudMetricTests(unittest.TestCase):
    def test_missing_metrics_never_become_zero_or_success(self):
        self.assertIsNone(percentile([], .95))
        self.assertEqual(aggregate([])["successRate"], None)
        self.assertIsNone(cost_per_thousand(10, 0))

    def test_failed_attempts_stay_in_denominator_but_not_success_latency(self):
        result = aggregate([{"success": True, "endToEndMs": 20}, {"success": False, "endToEndMs": 500}])
        self.assertEqual((result["attempted"], result["successful"], result["failed"], result["successRate"]), (2, 1, 1, .5))
        self.assertEqual(result["endToEndMs"]["p95"], 20)
        self.assertEqual(result["endToEndMs"]["samples"], 1)

    def test_percentile_and_cost_units(self):
        self.assertAlmostEqual(percentile([0, 10, 20, 30, 40], .95), 38)
        self.assertEqual(cost_per_thousand(2, 500), 4)
        with self.assertRaises(ValueError):
            cost_per_thousand(-1, 2)

    def test_storage_failure_is_not_completion_or_credential_leak(self):
        api=Mock()
        api.create.return_value={"uploadId":"test-upload","mediaId":1,"uploadUrl":"https://private.example/?secret=signature"}
        api.expect.side_effect=RuntimeError("HTTP 500 at https://private.example/?secret=signature")
        with patch("benchmarks.cloud_common.put", return_value=Mock(status_code=500)):
            row, upload=upload_one(api,b"image")
        self.assertFalse(row["success"])
        self.assertEqual(row["stage"],"put")
        self.assertEqual(row["errorType"],"RuntimeError")
        self.assertNotIn("signature",str(row))
        api.complete.assert_not_called()

    def test_autoscaler_restored_even_when_task_capacity_does_not_recover(self):
        control=ServiceControl.__new__(ServiceControl)
        control.original_count=2
        control.target={"MinCapacity":1,"MaxCapacity":8,"SuspendedState":{"DynamicScalingInSuspended":False,"DynamicScalingOutSuspended":False,"ScheduledScalingSuspended":True}}
        control.resource="service/cluster/worker"
        control.scale=Mock(side_effect=TimeoutError("Capacity unavailable"))
        control.scaler=Mock()
        with self.assertRaises(TimeoutError):
            control.restore()
        restored=control.scaler.register_scalable_target.call_args.kwargs
        self.assertEqual(restored["SuspendedState"],control.target["SuspendedState"])
        self.assertEqual((restored["MinCapacity"],restored["MaxCapacity"]),(1,8))

    def test_database_observer_failure_preserves_completed_upload_attempts(self):
        from benchmarks import cloud_load
        saved=[]
        api=Mock()
        with patch.object(sys,"argv",["cloud_load.py","--users","1","--iterations","1"]), \
             patch.object(cloud_load,"cloud_guard",return_value=(None,{})), \
             patch.object(cloud_load,"fixture",return_value=b"image"), \
             patch.object(cloud_load,"register_user",return_value=api), \
             patch.object(cloud_load,"upload_one",return_value=({"success":True,"mediaId":1,"endToEndMs":42},{"mediaId":1})), \
             patch.object(cloud_load,"database_timings",side_effect=RuntimeError("observer unavailable")), \
             patch.object(cloud_load,"write_report",side_effect=lambda path,report:saved.append(copy.deepcopy(report))), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud_load.main(),1)
        self.assertEqual(saved[-1]["status"],"FAIL")
        self.assertEqual(saved[-1]["cohorts"][0]["summary"]["attempted"],1)
        self.assertEqual(saved[-1]["cohorts"][0]["rawAttempts"][0]["endToEndMs"],42)
        api.cleanup.assert_called_once()


class EndpointTests(unittest.TestCase):
    def test_cloud_guards_reject_local_and_wrong_bucket(self):
        for value in ("http://localhost", "https://user:pass@example.test", "https://example.test?token=secret"):
            with self.assertRaises(ValueError):
                secure_origin(value)
        with patch.dict(os.environ, {"S3_BUCKET": "test-bucket", "AWS_REGION": "us-east-1"}):
            validate_s3_url("https://test-bucket.s3.us-east-1.amazonaws.com/x?X-Amz-Signature=redacted")
            for value in ("http://localhost:9000/test-bucket/x", "https://other.s3.amazonaws.com/x", "https://test-bucket.s3.amazonaws.com.attacker.invalid/x"):
                with self.assertRaises(ValueError):
                    validate_s3_url(value)

    def test_real_expiry_parse_and_fail_closed_when_unknown(self):
        self.assertEqual(acceptance.expires_at("https://cdn.test/x?Expires=1800000000"), 1800000000)
        self.assertEqual(acceptance.expires_at("https://s3.test/x?X-Amz-Date=20260921T000000Z&X-Amz-Expires=60"), 1789948860)
        with self.assertRaises(ValueError):
            acceptance.expires_at("https://cdn.test/x?Signature=only")


class VectorEvidenceTests(unittest.TestCase):
    def test_recall_uses_exact_ground_truth_and_no_duplicate_credit(self):
        self.assertEqual(recall_at_k([1, 1, 1], [1, 2]), .5)
        self.assertEqual(recall_at_k([2, 1], [1, 2]), 1)
        with self.assertRaises(ValueError):
            recall_at_k([], [])

    def test_nested_query_plan_requires_actual_named_index(self):
        self.assertTrue(plan_uses_index([{"Plan": {"Plans": [{"Node Type": "Index Scan", "Index Name": "items_hnsw"}]}}], "items_hnsw"))
        self.assertFalse(plan_uses_index([{"Plan": {"Node Type": "Seq Scan"}}], "items_hnsw"))

    def test_corpus_deterministic_and_independent_of_batch_size(self):
        import numpy as np
        a = np.concatenate([v for _, v in vectors(30, 8, 42, batch=5)])
        b = np.concatenate([v for _, v in vectors(30, 8, 42, batch=7)])
        np.testing.assert_array_equal(a, b)
        np.testing.assert_allclose(np.linalg.norm(a, axis=1), 1, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
