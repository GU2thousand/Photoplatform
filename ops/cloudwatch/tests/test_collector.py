import base64
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError


spec = importlib.util.spec_from_file_location("collector", Path(__file__).resolve().parents[1] / "collector.py")
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)

NOW = 1800000000.0


def payload(name, ready, inflight):
    return {"name": name, "vhost": "/", "state": "running", "messages_ready": ready,
            "messages_unacknowledged": inflight,
            "messages_ready_details": {"samples": [{"sample": ready, "timestamp": NOW * 1000}]},
            "messages_unacknowledged_details": {"samples": [{"sample": inflight, "timestamp": NOW * 1000}]}}


def response(data):
    result = io.BytesIO(json.dumps(data).encode())
    result.status = 200
    return result


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.settings = collector.Settings("broker.example", "metrics", "secret-do-not-log", "cluster", "workers")
        self.ecs = Mock()
        self.ecs.describe_services.return_value = {"services": [{"serviceName": "workers", "status": "ACTIVE",
            "clusterArn": "arn:aws:ecs:us-east-1:123:cluster/cluster", "runningCount": 4}], "failures": []}
        self.cloudwatch = Mock()
        self.opener = Mock()
        self.set_queues(payload("media.process", 100, 20), payload("media.delete", 8, 4))
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False
        self.subject = collector.Collector(self.settings, self.ecs, self.cloudwatch, opener=self.opener,
            stopped=self.stop, now=lambda: NOW, monotonic=lambda: 10)

    def set_queues(self, *data):
        self.opener.open.side_effect = [response(p) for p in data]

    def fails(self, reason):
        with self.assertRaisesRegex(collector.CollectionError, "^" + reason + "$"):
            self.subject.collect_and_publish()
        self.cloudwatch.put_metric_data.assert_not_called()

    def test_sums_every_configured_queue_ready_and_unacknowledged(self):
        actual = self.subject.collect_and_publish()
        self.assertEqual(actual, {"BacklogPerTask": 33, "QueueDepth": 132, "Ready": 108,
                                  "InFlight": 24, "ActiveTasks": 4})
        request = self.cloudwatch.put_metric_data.call_args.kwargs
        self.assertEqual(request["Namespace"], "Photoplatform/Workers")
        self.assertEqual(len(request["MetricData"]), 5)
        for datum in request["MetricData"]:
            self.assertEqual(datum["Dimensions"], [{"Name": "ClusterName", "Value": "cluster"},
                                                    {"Name": "ServiceName", "Value": "workers"}])
            self.assertEqual(datum["Timestamp"].timestamp(), NOW)
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertIn("/api/queues/%2F/media.process?lengths_age=60&lengths_incr=5",
                      self.opener.open.call_args_list[0].args[0].full_url)

    def test_zero_running_tasks_has_finite_backlog_and_true_active_zero(self):
        self.ecs.describe_services.return_value["services"][0]["runningCount"] = 0
        actual = self.subject.collect_and_publish()
        self.assertEqual(actual["BacklogPerTask"], 132)
        self.assertEqual(actual["ActiveTasks"], 0)

    def test_empty_queues_publish_real_zero(self):
        self.set_queues(payload("media.process", 0, 0), payload("media.delete", 0, 0))
        self.assertEqual(self.subject.collect_and_publish()["BacklogPerTask"], 0)

    def test_inflight_only_still_counts_backlog(self):
        self.set_queues(payload("media.process", 0, 8), payload("media.delete", 0, 0))
        self.assertEqual(self.subject.collect_and_publish()["BacklogPerTask"], 2)

    def test_queue_404_does_not_publish_partial_total_or_retry(self):
        self.opener.open.side_effect = [response(payload("media.process", 5, 1)),
                                      HTTPError("hidden", 404, "secret", None, None)]
        self.fails("rabbitmq_http_status")
        self.assertEqual(self.opener.open.call_count, 2)
        self.ecs.describe_services.assert_not_called()

    def test_http_redirect_rejected(self):
        self.opener.open.side_effect = HTTPError("hidden", 302, "secret", None, None)
        self.fails("rabbitmq_http_status")
        self.assertIsNone(collector.NoRedirects().redirect_request(None, None, 302, "", {}, "http://other"))

    def test_missing_queue_count_is_not_zero(self):
        bad = payload("media.process", 5, 1)
        del bad["messages_unacknowledged"]
        self.set_queues(bad)
        self.fails("rabbitmq_missing_queue_count")

    def test_invalid_count_values(self):
        for count in (None, True, -1, "5", 1.2):
            with self.subTest(count=count):
                self.set_queues(payload("media.process", count, 0))
                self.fails("rabbitmq_missing_queue_count")

    def test_wrong_queue_identity_rejected(self):
        self.set_queues(payload("other", 5, 1))
        self.fails("rabbitmq_queue_identity")

    def test_queue_unavailable_state_rejected(self):
        bad = payload("media.process", 5, 1)
        bad["state"] = "down"
        self.set_queues(bad)
        self.fails("rabbitmq_queue_not_running")

    def test_missing_samples_rejected(self):
        bad = payload("media.process", 5, 1)
        del bad["messages_ready_details"]
        self.set_queues(bad)
        self.fails("rabbitmq_missing_samples")

    def test_stale_samples_rejected(self):
        bad = payload("media.process", 5, 1)
        bad["messages_ready_details"]["samples"][0]["timestamp"] -= 91000
        self.set_queues(bad)
        self.fails("rabbitmq_stale_samples")

    def test_future_samples_rejected(self):
        bad = payload("media.process", 5, 1)
        bad["messages_ready_details"]["samples"][0]["timestamp"] += 11000
        self.set_queues(bad)
        self.fails("rabbitmq_stale_samples")

    def test_collection_elapsed_time_must_be_bounded(self):
        self.subject.monotonic = Mock(side_effect=[0, 46])
        self.fails("collection_too_old")

    def test_sample_freshness_rechecked_after_ecs_call(self):
        self.subject.now = Mock(side_effect=[NOW, NOW, NOW, NOW, NOW + 91])
        self.fails("rabbitmq_stale_samples")

    def test_http_failures_have_bounded_retries(self):
        self.opener.open.side_effect = URLError("secret-do-not-log")
        self.fails("rabbitmq_unavailable")
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual(self.stop.wait.call_count, 2)

    def test_transient_http_failure_recovers(self):
        self.opener.open.side_effect = [HTTPError("hidden", 503, "secret", None, None),
            response(payload("media.process", 5, 1)), response(payload("media.delete", 0, 0))]
        self.assertEqual(self.subject.collect_and_publish()["QueueDepth"], 6)

    def test_ecs_failure_does_not_publish_zero(self):
        self.ecs.describe_services.side_effect = RuntimeError("secret")
        self.fails("ecs_unavailable")

    def test_ecs_failure_list_rejected(self):
        self.ecs.describe_services.return_value["failures"] = [{"reason": "MISSING"}]
        self.fails("ecs_service_missing")

    def test_ecs_missing_running_count_rejected(self):
        del self.ecs.describe_services.return_value["services"][0]["runningCount"]
        self.fails("ecs_missing_running_count")

    def test_ecs_wrong_cluster_rejected(self):
        self.ecs.describe_services.return_value["services"][0]["clusterArn"] = "arn:aws:ecs:region:id:cluster/wrong"
        self.fails("ecs_cluster_identity")

    def test_cloudwatch_error_not_followed_by_zero(self):
        self.cloudwatch.put_metric_data.side_effect = RuntimeError("secret")
        with self.assertRaisesRegex(collector.CollectionError, "cloudwatch_unavailable"):
            self.subject.collect_and_publish()
        self.cloudwatch.put_metric_data.assert_called_once()

    def test_arn_inputs_produce_same_scaling_dimensions(self):
        service = self.ecs.describe_services.return_value["services"][0]
        service["serviceArn"] = "arn:aws:ecs:region:123:service/cluster/workers"
        self.subject.settings = replace(self.settings, cluster=service["clusterArn"], service=service["serviceArn"])
        self.subject.collect_and_publish()
        self.assertEqual(self.cloudwatch.put_metric_data.call_args.kwargs["MetricData"][0]["Dimensions"],
            [{"Name": "ClusterName", "Value": "cluster"}, {"Name": "ServiceName", "Value": "workers"}])

    def test_request_uses_basic_auth_https_timeout_and_encoded_names(self):
        self.subject.settings = replace(self.settings, vhost="/prod", queues=("queue /1",))
        data = payload("queue /1", 0, 1)
        data["vhost"] = "/prod"
        self.set_queues(data)
        self.subject.collect_and_publish()
        request = self.opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://broker.example:443/api/queues/%2Fprod/queue%20%2F1?"))
        self.assertEqual(request.get_header("Authorization"), "Basic " + base64.b64encode(b"metrics:secret-do-not-log").decode())
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 5)

    def test_shutdown_interrupts_retry_without_publish(self):
        self.opener.open.side_effect = URLError("secret")
        self.stop.wait.return_value = True
        self.fails("shutdown")
        self.assertEqual(self.opener.open.call_count, 1)

    def test_run_logs_only_safe_failure_reason(self):
        self.opener.open.side_effect = URLError("secret-do-not-log")
        self.stop.is_set.side_effect = [False, False, False, False, True]
        with self.assertLogs("photoplatform.backlog", level="WARNING") as logs:
            self.subject.run()
        self.assertIn("rabbitmq_unavailable", "".join(logs.output))
        self.assertNotIn("secret-do-not-log", "".join(logs.output))

    def test_settings_env_contract_and_defaults(self):
        env = {"RABBITMQ_HOST": "broker.example", "RABBITMQ_USER": "primary", "RABBITMQ_USERNAME": "alias",
               "RABBITMQ_PASSWORD": "secret", "ECS_CLUSTER": "cluster", "ECS_SERVICE": "workers"}
        actual = collector.Settings.from_env(env)
        self.assertEqual(actual.username, "primary")
        self.assertEqual(actual.queues, ("media.process", "media.delete"))
        self.assertEqual(actual.scheme, "https")
        self.assertEqual(actual.interval, 60)

    def test_duplicate_or_empty_queues_and_bad_hosts_rejected(self):
        for changes in ({"queues": ("x", "x")}, {"queues": ("",)}, {"host": "https://broker.example"},
                        {"host": "user:password@broker.example"}, {"host": "broker.example:443"},
                        {"timeout": float("nan")}, {"attempts": 99}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.settings, **changes)


if __name__ == "__main__":
    unittest.main()
