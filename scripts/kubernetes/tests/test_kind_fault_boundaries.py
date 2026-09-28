"""Pure boundary/evidence regressions; never invoke kubectl, Docker, or a database."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "kind_fault_tests", Path(__file__).resolve().parents[1] / "kind_tests.py")
faults = importlib.util.module_from_spec(spec)
spec.loader.exec_module(faults)


class EnvironmentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.kubeconfig = Path(self.scratch.name) / "kubeconfig"
        self.kubeconfig.write_text("disposable fixture")
        self.environment = {
            "ALLOW_KIND_FAULTS": "1", "ALLOW_INTEGRATION_WRITES": "1",
            "KIND_CLUSTER": "photoplatform-ci-test-123",
            "KIND_NAMESPACE": "photoplatform-dev", "KUBECONFIG": str(self.kubeconfig),
            "TEST_API_URL": "http://127.0.0.1:18081",
            "TEST_DATABASE_URL": "postgresql://fixture:fixture@127.0.0.1:15543/photo",
            "TEST_STORAGE_ENDPOINT": "http://127.0.0.1:19000",
            "KIND_EVIDENCE_DIR": self.scratch.name, "KIND_SOURCE_SHA": "a" * 40,
            "KIND_API_IMAGE": "localhost:5001/photo-api@sha256:" + "b" * 64,
            "KIND_WORKER_IMAGE": "localhost:5001/photo-worker@sha256:" + "c" * 64,
        }

    def test_guard_accepts_explicit_disposable_loopback_fixture(self):
        self.assertEqual(faults.validate_environment(self.environment),
                         "kind-photoplatform-ci-test-123")

    def test_guard_accepts_loopback_host_variants(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                candidate = dict(self.environment, TEST_API_URL=f"http://{host}:18081",
                                 TEST_STORAGE_ENDPOINT=f"http://{host}:19000",
                                 TEST_DATABASE_URL=f"postgresql://fixture:fixture@{host}:15543/photo")
                faults.validate_environment(candidate)

    def test_fifteen_unsafe_environment_permutations_fail_closed(self):
        changes = (
            ("ALLOW_KIND_FAULTS", "0"),
            ("ALLOW_INTEGRATION_WRITES", "0"),
            ("KIND_CLUSTER", "production"),
            ("KIND_CLUSTER", "photoplatform-ci-../production"),
            ("KIND_NAMESPACE", "photoplatform-prod"),
            ("KUBECONFIG", ""),
            ("KUBECONFIG", str(self.kubeconfig) + os.pathsep + str(self.kubeconfig)),
            ("KUBECONFIG", str(Path(self.scratch.name) / "absent")),
            ("TEST_API_URL", "https://api.production.example"),
            ("TEST_DATABASE_URL", "postgresql://fixture:fixture@rds.example/photo"),
            ("TEST_STORAGE_ENDPOINT", "https://s3.amazonaws.com"),
            ("KIND_EVIDENCE_DIR", ""),
            ("KIND_SOURCE_SHA", "main"),
            ("KIND_API_IMAGE", "localhost:5001/photo-api:latest"),
            ("KIND_WORKER_IMAGE", "localhost:5001/photo-worker@sha256:bad"),
        )
        self.assertEqual(len(changes), 15)
        for key, value in changes:
            with self.subTest(key=key, value=value):
                candidate = dict(self.environment)
                candidate[key] = value
                with self.assertRaises(AssertionError):
                    faults.validate_environment(candidate)


class KubernetesIdentityBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.context = "kind-photoplatform-ci-test-123"
        self.config = {
            "current-context": self.context,
            "contexts": [{"name": self.context,
                          "context": {"cluster": self.context, "user": self.context}}],
            "clusters": [{"name": self.context,
                          "cluster": {"server": "https://127.0.0.1:6443"}}],
            "users": [{"name": self.context, "user": {"client-certificate-data": "fixture"}}],
        }
        self.namespace = {"metadata": {"name": faults.NAMESPACE, "labels": {
            "app.kubernetes.io/part-of": "photoplatform",
            "photoplatform.io/disposable": "true", "photoplatform.io/environment": "dev"}}}

    def test_exact_loopback_context_and_disposable_namespace_pass(self):
        faults.validate_kubeconfig(self.config, self.context)
        faults.validate_namespace(self.namespace)

    def test_foreign_or_ambiguous_kubeconfig_is_rejected(self):
        changes = (
            lambda value: value.update({"current-context": "production"}),
            lambda value: value["contexts"].append({"name": "production"}),
            lambda value: value["contexts"][0].update({"name": "production"}),
            lambda value: value["clusters"].append({"name": "production", "cluster": {}}),
            lambda value: value["clusters"][0]["cluster"].update({"server": "https://eks.production.example"}),
            lambda value: value["clusters"][0]["cluster"].update({"server": "http://127.0.0.1:6443"}),
            lambda value: value["clusters"][0]["cluster"].update({"insecure-skip-tls-verify": True}),
            lambda value: value["users"].append({"name": "production", "user": {}}),
            lambda value: value["users"][0]["user"].update({"exec": {"command": "aws"}}),
            lambda value: value["users"][0]["user"].update({"auth-provider": {"name": "gcp"}}),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                candidate = copy.deepcopy(self.config)
                change(candidate)
                with self.assertRaises(AssertionError):
                    faults.validate_kubeconfig(candidate, self.context)

    def test_namespace_name_and_each_required_label_are_enforced(self):
        candidate = copy.deepcopy(self.namespace)
        candidate["metadata"]["name"] = "photoplatform-prod"
        with self.assertRaises(AssertionError):
            faults.validate_namespace(candidate)
        for label in self.namespace["metadata"]["labels"]:
            for unsafe in (None, "false", "production"):
                with self.subTest(label=label, value=unsafe):
                    candidate = copy.deepcopy(self.namespace)
                    if unsafe is None:
                        del candidate["metadata"]["labels"][label]
                    else:
                        candidate["metadata"]["labels"][label] = unsafe
                    with self.assertRaises(AssertionError):
                        faults.validate_namespace(candidate)


class WorkerSignalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.container_id = "d" * 64
        self.worker = {"name": "photo-media-worker-test", "uid": "exact-pod-uid",
                       "containers": [{"name": "media-worker",
                                       "container_id": "containerd://" + self.container_id}]}
        self.inspected = {
            "status": {"id": self.container_id, "state": "CONTAINER_RUNNING",
                       "metadata": {"name": "media-worker"},
                       "labels": {"io.kubernetes.pod.uid": self.worker["uid"],
                                  "io.kubernetes.pod.name": self.worker["name"],
                                  "io.kubernetes.pod.namespace": faults.NAMESPACE}},
            "info": {"pid": 12345},
        }

    def test_exact_running_claimed_container_target_passes(self):
        self.assertEqual(faults.validate_worker_container(self.worker, self.inspected),
                         (self.container_id, 12345))

    def test_runtime_identity_mismatches_and_node_init_pid_are_rejected(self):
        changes = (
            lambda value: value["status"].update({"id": "e" * 64}),
            lambda value: value["status"].update({"state": "CONTAINER_EXITED"}),
            lambda value: value["status"]["metadata"].update({"name": "api"}),
            lambda value: value["status"]["labels"].update({"io.kubernetes.pod.uid": "replacement-pod-uid"}),
            lambda value: value["status"]["labels"].update({"io.kubernetes.pod.name": "another-worker"}),
            lambda value: value["status"]["labels"].update({"io.kubernetes.pod.namespace": "photoplatform-prod"}),
            lambda value: value["info"].update({"pid": 1}),
            lambda value: value["info"].update({"pid": 0}),
            lambda value: value["info"].update({"pid": "12345"}),
            lambda value: value["info"].update({"pid": True}),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                candidate = copy.deepcopy(self.inspected)
                change(candidate)
                with self.assertRaises(AssertionError):
                    faults.validate_worker_container(self.worker, candidate)

    def test_ambiguous_worker_container_or_invalid_container_reference_is_rejected(self):
        for reference in ("docker://" + self.container_id, "containerd://short", "", "containerd://" + "D" * 64):
            with self.subTest(reference=reference):
                candidate = copy.deepcopy(self.worker)
                candidate["containers"][0]["container_id"] = reference
                with self.assertRaises(AssertionError):
                    faults.validate_worker_container(candidate, self.inspected)
        for containers in ([], [{"name": "api", "container_id": "containerd://" + self.container_id}],
                           self.worker["containers"] * 2):
            with self.subTest(containers=containers):
                candidate = copy.deepcopy(self.worker)
                candidate["containers"] = containers
                with self.assertRaises(AssertionError):
                    faults.validate_worker_container(candidate, self.inspected)


class EvidenceTests(unittest.TestCase):
    def test_jsonl_preserves_failed_case_details_and_elapsed_observations(self):
        with tempfile.TemporaryDirectory() as scratch, patch.dict(os.environ, {}, clear=True):
            evidence = faults.Evidence(scratch)
            evidence.record("case_started", case="database_outage_readiness_liveness")
            evidence.record("case_finished", case="database_outage_readiness_liveness", status="FAIL",
                            error="unexpected readiness\nsecond line", observations=[200, 503],
                            pod={"uid": "immutable-pod-uid", "restarts": 0})
            rows = [json.loads(line) for line in evidence.timeline.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["status"], "FAIL")
            self.assertEqual(rows[1]["observations"], [200, 503])
            self.assertEqual(rows[1]["pod"]["uid"], "immutable-pod-uid")
            self.assertEqual(rows[1]["error"], "unexpected readiness\nsecond line")
            self.assertGreaterEqual(rows[1]["elapsed_seconds"], rows[0]["elapsed_seconds"])
            self.assertTrue(rows[0]["utc"].endswith("+00:00"))

    def test_credentials_and_dynamic_tokens_are_redacted_without_corrupting_json(self):
        environment = {"FIXTURE_PASSWORD": "database-secret-value", "FIXTURE_SECRET": "storage-secret-value",
                       "FIXTURE_TOKEN": "environment-token-value"}
        with tempfile.TemporaryDirectory() as scratch, patch.dict(os.environ, environment, clear=True):
            evidence = faults.Evidence(scratch)
            evidence.secrets.add("dynamic-ticket-value")
            evidence.record("failed_request", detail=(
                "database-secret-value storage-secret-value environment-token-value dynamic-ticket-value "
                "postgresql://fixture:unregistered-credential@127.0.0.1/photo "
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.signature"))
            raw = evidence.timeline.read_text()
            row = json.loads(raw)
            self.assertEqual(row["event"], "failed_request")
            for secret in (*environment.values(), "dynamic-ticket-value", "unregistered-credential",
                           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOjF9.signature"):
                self.assertNotIn(secret, raw)
            self.assertIn("[REDACTED]", row["detail"])
            self.assertIn("[JWT REDACTED]", row["detail"])


if __name__ == "__main__":
    unittest.main()
