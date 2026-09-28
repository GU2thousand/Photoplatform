from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import requests
from prometheus_client import CollectorRegistry, generate_latest


spec = importlib.util.spec_from_file_location("kubernetes_queue_collector", Path(__file__).resolve().parents[1] / "collector.py")
collector = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = collector
spec.loader.exec_module(collector)

NOW = 1800000000.0
SELECTOR = "app.kubernetes.io/instance=release,app.kubernetes.io/component=media-worker"
LABELS = {"app.kubernetes.io/instance": "release", "app.kubernetes.io/component": "media-worker"}


def queue(name, ready=8, inflight=2, timestamp=NOW):
    return {"name": name, "vhost": "/", "state": "running", "messages_ready": ready,
            "messages_unacknowledged": inflight,
            "messages_ready_details": {"samples": [{"sample": ready, "timestamp": timestamp * 1000}]},
            "messages_unacknowledged_details": {"samples": [{"sample": inflight, "timestamp": timestamp * 1000}]}}


def pod(uid, *, ready=True, deleting=False, phase="Running"):
    return {"metadata": {"uid": uid, "namespace": "photos", "labels": dict(LABELS),
                         **({"deletionTimestamp": "2026-01-01T00:00:00Z"} if deleting else {})},
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def pod_list(*pods):
    return {"kind": "PodList", "metadata": {"resourceVersion": "42"}, "items": list(pods)}


def response(payload, status=200, raw=None):
    result = Mock()
    result.status_code = status
    result.iter_content.return_value = [json.dumps(payload).encode() if raw is None else raw]
    return result


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("token-in-file")
        self.settings = collector.Settings("broker.example", "metrics", "secret-do-not-log", "cluster", "photos",
            (collector.Target("release-media-worker", ("media.process", "media.delete"), SELECTOR),),
            token_file=str(self.token))
        self.session, self.stop = Mock(), Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False
        self.subject = collector.Collector(self.settings, session=self.session, stopped=self.stop,
            now=lambda: NOW, monotonic=lambda: 0)
        self.set_data()

    def set_data(self, *responses):
        if not responses:
            responses = (response(queue("media.process", 10, 2)), response(queue("media.delete", 6, 2)),
                         response(pod_list(pod("1"), pod("2"))))
        self.session.get.side_effect = list(responses)

    def fails(self, reason):
        with self.assertRaisesRegex(collector.CollectionError, "^" + reason + "$"):
            self.subject.collect()

    def test_ready_and_inflight_sum_with_ready_pod_denominator(self):
        observations = self.subject.collect()
        self.assertEqual(observations, (collector.Observation("release-media-worker", 16, 4, 2, NOW),))
        state = collector.QueueMetrics(self.settings, now=lambda: NOW)
        state.success(observations)
        registry = CollectorRegistry()
        registry.register(state)
        text = generate_latest(registry).decode()
        self.assertIn('photoplatform_backlog_per_ready_pod{cluster="cluster",deployment="release-media-worker",namespace="photos"} 10.0', text)
        self.assertIn('photoplatform_queue_collector_available{cluster="cluster",namespace="photos"} 1.0', text)

    def test_no_pods_is_real_zero_and_ratio_is_absent(self):
        self.set_data(response(queue("media.process")), response(queue("media.delete")), response(pod_list()))
        observations = self.subject.collect()
        self.assertEqual(observations[0].ready_pods, 0)
        state = collector.QueueMetrics(self.settings, now=lambda: NOW)
        state.success(observations)
        registry = CollectorRegistry()
        registry.register(state)
        text = generate_latest(registry).decode()
        self.assertIn('photoplatform_worker_ready_pods{cluster="cluster",deployment="release-media-worker",namespace="photos"} 0.0', text)
        self.assertNotIn("photoplatform_backlog_per_ready_pod", text)
        self.assertIn('photoplatform_queue_depth{cluster="cluster",deployment="release-media-worker",namespace="photos"} 20.0', text)

    def test_unready_pending_and_terminating_pods_excluded(self):
        self.set_data(response(queue("media.process")), response(queue("media.delete")),
                      response(pod_list(pod("ready"), pod("unready", ready=False), pod("pending", phase="Pending"),
                                        pod("deleting", deleting=True))))
        self.assertEqual(self.subject.collect()[0].ready_pods, 1)

    def test_collection_failure_removes_previous_metrics_and_retains_success_timestamp(self):
        state = collector.QueueMetrics(self.settings, now=lambda: NOW)
        state.success(self.subject.collect())
        self.set_data(response(queue("media.process")), response({}, status=404))
        self.subject.collect = Mock(wraps=self.subject.collect)
        self.stop.is_set.side_effect = [False, False, False, False, False, True]
        with self.assertLogs(collector.LOG, level="WARNING"):
            self.subject.run(state)
        registry = CollectorRegistry()
        registry.register(state)
        text = generate_latest(registry).decode()
        self.assertIn('photoplatform_queue_collector_available{cluster="cluster",namespace="photos"} 0.0', text)
        self.assertIn("photoplatform_queue_collector_last_success_timestamp_seconds", text)
        self.assertNotIn("photoplatform_queue_depth", text)

    def test_stale_snapshot_disappears_without_a_new_collection(self):
        clock = Mock(return_value=NOW)
        state = collector.QueueMetrics(self.settings, now=clock)
        state.success(self.subject.collect())
        clock.return_value = NOW + 91
        self.assertIsNone(state.snapshot()[0])
        self.assertEqual(state.snapshot()[1], NOW)

    def test_sample_age_can_expire_before_success_age(self):
        clock = Mock(return_value=NOW)
        state = collector.QueueMetrics(self.settings, now=clock)
        state.success((collector.Observation("release-media-worker", 2, 3, 1, NOW - 80),))
        clock.return_value = NOW + 11
        self.assertIsNone(state.snapshot()[0])

    def test_missing_counts_never_become_zero(self):
        for bad in (None, True, -1, "3", 1.5):
            with self.subTest(bad=bad):
                self.set_data(response(queue("media.process", bad, 1)))
                self.fails("rabbitmq_missing_queue_count")

    def test_missing_samples_stale_samples_future_samples_rejected(self):
        missing = queue("media.process")
        del missing["messages_ready_details"]
        for bad, reason in ((missing, "rabbitmq_missing_samples"),
                            (queue("media.process", timestamp=NOW - 91), "rabbitmq_stale_samples"),
                            (queue("media.process", timestamp=NOW + 11), "rabbitmq_stale_samples")):
            with self.subTest(reason=reason):
                self.set_data(response(bad))
                self.fails(reason)

    def test_each_count_requires_valid_sample_timestamp(self):
        for timestamp in (True, None, float("nan"), -1, "123"):
            bad = queue("media.process")
            bad["messages_unacknowledged_details"]["samples"][0]["timestamp"] = timestamp
            with self.subTest(timestamp=timestamp):
                self.set_data(response(bad))
                self.fails("rabbitmq_invalid_sample_timestamp")

    def test_freshness_rechecked_after_kubernetes_request(self):
        self.subject.now = Mock(side_effect=[NOW, NOW, NOW, NOW, NOW + 91])
        self.fails("rabbitmq_stale_samples")

    def test_kubernetes_permission_failure_is_not_zero_ready_pods(self):
        self.set_data(response(queue("media.process")), response(queue("media.delete")), response({}, status=403))
        self.fails("kubernetes_http_status")
        self.assertEqual(self.session.get.call_count, 3)

    def test_kubernetes_failure_has_bounded_retry_and_safe_reason(self):
        self.session.get.side_effect = [response(queue("media.process")), response(queue("media.delete"))] + \
            [requests.ConnectionError("secret-do-not-log")] * 3
        self.fails("kubernetes_unavailable")
        self.assertEqual(self.session.get.call_count, 5)
        self.assertEqual(self.stop.wait.call_count, 2)

    def test_redirects_rejected_without_forwarding_credentials(self):
        self.set_data(response({}, status=302))
        self.fails("rabbitmq_http_status")
        self.assertEqual(self.session.get.call_count, 1)
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    def test_invalid_json_and_oversized_response_rejected(self):
        for raw, reason in ((b"invalid", "rabbitmq_invalid_json"),
                            (b"x" * (collector.MAX_RESPONSE_BYTES + 1), "rabbitmq_response_too_large")):
            with self.subTest(reason=reason):
                self.set_data(response(None, raw=raw))
                self.fails(reason)

    def test_queue_identity_and_state_checked(self):
        wrong = queue("other")
        unavailable = queue("media.process")
        unavailable["state"] = "down"
        for bad, reason in ((wrong, "rabbitmq_queue_identity"), (unavailable, "rabbitmq_queue_not_running")):
            with self.subTest(reason=reason):
                self.set_data(response(bad))
                self.fails(reason)

    def test_partial_pod_list_and_wrong_pod_identity_rejected(self):
        partial = pod_list(pod("1"))
        partial["metadata"]["continue"] = "page2"
        wrong_namespace = pod("1")
        wrong_namespace["metadata"]["namespace"] = "other"
        wrong_selector = pod("1")
        wrong_selector["metadata"]["labels"]["app.kubernetes.io/instance"] = "other"
        for bad, reason in ((partial, "kubernetes_pod_list_truncated"),
                            (pod_list(wrong_namespace), "kubernetes_pod_identity"),
                            (pod_list(wrong_selector), "kubernetes_pod_identity"),
                            (pod_list(pod("1"), pod("1")), "kubernetes_pod_identity")):
            with self.subTest(reason=reason):
                self.set_data(response(queue("media.process")), response(queue("media.delete")), response(bad))
                self.fails(reason)

    def test_service_account_token_rotation_is_read_on_each_collection(self):
        self.subject.collect()
        first = self.session.get.call_args.kwargs["headers"]["Authorization"]
        self.token.write_text("rotated-token")
        self.set_data()
        self.subject.collect()
        second = self.session.get.call_args.kwargs["headers"]["Authorization"]
        self.assertEqual(first, "Bearer token-in-file")
        self.assertEqual(second, "Bearer rotated-token")

    def test_missing_token_is_failure_before_kubernetes_request(self):
        self.token.unlink()
        self.fails("kubernetes_token_missing")
        self.assertEqual(self.session.get.call_count, 2)

    def test_kubernetes_list_is_namespaced_selector_bound_and_verified(self):
        self.subject.collect()
        call = self.session.get.call_args
        self.assertTrue(call.args[0].startswith("https://kubernetes.default.svc:443/api/v1/namespaces/photos/pods?"))
        self.assertIn("labelSelector=app.kubernetes.io%2Finstance%3Drelease%2Capp.kubernetes.io%2Fcomponent%3Dmedia-worker", call.args[0])
        self.assertEqual(call.kwargs["verify"], str(collector.SERVICE_ACCOUNT / "ca.crt"))
        self.assertEqual(call.kwargs["timeout"], (5, 5))
        self.assertFalse(self.session.trust_env)

    def test_request_budget_checked_between_response_chunks(self):
        self.subject.monotonic = Mock(side_effect=[0, 0, 46])
        self.fails("collection_too_old")

    def test_wall_clock_deadline_interrupts_blocked_headers(self):
        self.subject.settings = replace(self.settings, max_collection_age=0.05)
        self.session.get.side_effect = lambda *args, **kwargs: time.sleep(5)
        start = time.monotonic()
        self.fails("collection_too_old")
        self.assertLess(time.monotonic() - start, 1)

    def test_optional_embedding_target_has_independent_queue_total_and_labels(self):
        target = collector.Target("release-embedding-worker", ("embedding.compute",), "component=embedding")
        self.subject.settings = replace(self.settings, targets=self.settings.targets + (target,))
        embedding_pod = pod("embedding")
        embedding_pod["metadata"]["labels"] = {"component": "embedding"}
        self.set_data(response(queue("media.process", 10, 2)), response(queue("media.delete", 6, 2)),
                      response(pod_list(pod("1"), pod("2"))), response(queue("embedding.compute", 3, 1)),
                      response(pod_list(embedding_pod)))
        observations = self.subject.collect()
        self.assertEqual([(item.deployment, item.ready + item.inflight, item.ready_pods) for item in observations],
                         [("release-media-worker", 20, 2), ("release-embedding-worker", 4, 1)])


class ConfigurationTests(unittest.TestCase):
    def test_mounted_credentials_take_precedence_and_env_fallback_is_local_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user"
            path.write_text("mounted-user\n")
            self.assertEqual(collector.credentials({"RABBITMQ_USER_FILE": str(path), "RABBITMQ_USER": "ignored"}, "RABBITMQ_USER"), "mounted-user")
            path.unlink()
            env = {"RABBITMQ_USER_FILE": str(path), "RABBITMQ_USER": "local-user", "RUNTIME_MODE": "local"}
            self.assertEqual(collector.credentials(env, "RABBITMQ_USER"), "local-user")
            env["RUNTIME_MODE"] = "production"
            with self.assertRaises(ValueError):
                collector.credentials(env, "RABBITMQ_USER")

    def test_env_contract_and_default_targets(self):
        env = {"RABBITMQ_HOST": "broker.example", "RABBITMQ_USER": "metrics", "RABBITMQ_PASSWORD": "unique-password-do-not-print",
               "RABBITMQ_USER_FILE": "/nonexistent/user", "RABBITMQ_PASSWORD_FILE": "/nonexistent/password",
               "RUNTIME_MODE": "local", "CLUSTER_NAME": "dev", "NAMESPACE": "photos"}
        settings = collector.Settings.from_env(env)
        self.assertEqual(settings.targets[0].deployment, "release-media-worker")
        self.assertEqual(settings.targets[0].queues, ("media.process", "media.delete"))
        self.assertEqual(settings.metrics_port, 9092)
        self.assertEqual(settings.interval, 30)
        self.assertNotIn("unique-password-do-not-print", repr(settings))

    def test_bad_selectors_duplicate_targets_invalid_transports_rejected(self):
        for selector in ("", "a", "a=x,a=y", "a in (x)", "a="):
            if not selector:
                continue  # Target deliberately supplies a safe deployment label default.
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                collector.Target("workers", ("media.process",), selector)
        base = collector.Settings("broker.example", "metrics", "secret", "cluster", "photos",
                                  (collector.Target("workers", ("media.process",)),))
        for changes in ({"targets": base.targets * 2}, {"scheme": "http"}, {"kubernetes_url": "http://localhost:8001"},
                        {"kubernetes_url": "https://user:secret@api.example"}, {"host": "https://broker.example"},
                        {"rabbitmq_ca": False}, {"kubernetes_ca": False}, {"timeout": float("nan")},
                        {"token": "environment-token"}, {"interval": 90}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(base, **changes)
        self.assertEqual(replace(base, scheme="http", runtime_mode="local").scheme, "http")


class HttpEndpointTests(unittest.TestCase):
    def test_partial_request_is_disconnected_and_does_not_block_other_probes(self):
        settings = collector.Settings("broker.example", "metrics", "secret", "cluster", "photos",
                                      (collector.Target("workers", ("media.process",)),))
        metrics = collector.QueueMetrics(settings, now=lambda: NOW)
        registry = CollectorRegistry()
        registry.register(metrics)
        server = collector.MetricsServer(("127.0.0.1", 0), collector.make_handler(registry, metrics, threading.Event()))
        server.request_deadline = 0.1
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = requests.Session()
        session.trust_env = False
        connection = socket.create_connection(server.server_address, timeout=1)
        try:
            connection.sendall(b"GET /healthz HTTP/1.1\r\nX-Drip: ")
            # The unfinished connection must not serialize unrelated probe traffic.
            response = session.get("http://127.0.0.1:" + str(server.server_port) + "/healthz", timeout=1)
            self.assertEqual(response.status_code, 200)
            connection.settimeout(1)
            start = time.monotonic()
            self.assertEqual(connection.recv(1), b"")
            self.assertLess(time.monotonic() - start, 0.5)
        finally:
            connection.close()
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_process_remains_live_while_missing_metrics_fail_readiness(self):
        settings = collector.Settings("broker.example", "metrics", "secret", "cluster", "photos",
                                      (collector.Target("workers", ("media.process",)),))
        metrics = collector.QueueMetrics(settings, now=lambda: NOW)
        registry = CollectorRegistry()
        registry.register(metrics)
        stopped = threading.Event()
        server = collector.MetricsServer(("127.0.0.1", 0), collector.make_handler(registry, metrics, stopped))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        session = requests.Session()
        session.trust_env = False
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            self.assertEqual(session.get(base + "/healthz", timeout=2).status_code, 200)
            self.assertEqual(session.get(base + "/readyz", timeout=2).status_code, 503)
            metrics.success((collector.Observation("workers", 8, 2, 2, NOW),))
            self.assertEqual(session.get(base + "/readyz", timeout=2).status_code, 200)
            self.assertIn("photoplatform_backlog_per_ready_pod", session.get(base + "/metrics", timeout=2).text)
            metrics.failure()
            self.assertEqual(session.get(base + "/healthz", timeout=2).status_code, 200)
            self.assertEqual(session.get(base + "/readyz", timeout=2).status_code, 503)
            self.assertNotIn("photoplatform_queue_depth", session.get(base + "/metrics", timeout=2).text)
        finally:
            session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
