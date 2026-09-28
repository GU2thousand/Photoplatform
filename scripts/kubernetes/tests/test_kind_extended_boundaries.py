"""Extended fault boundaries: no kubectl, Docker, database or AWS calls."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
spec = importlib.util.spec_from_file_location("kind_extended_tests", DIRECTORY / "kind_extended_tests.py")
extended = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extended)


class MarkerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.job = {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "media_id": 17,
                    "claim_token": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}
        self.worker = {"name": "photo-media-worker-test", "uid": "exact-worker-uid"}
        self.marker = {"job_id": self.job["id"], "media_id": 17, "claim_token": self.job["claim_token"],
                       "pod_name": self.worker["name"], "pod_uid": self.worker["uid"],
                       "stage": "after_write", "session_pid": 8123}

    def test_exact_original_claim_session_and_pod_marker_is_accepted(self):
        self.assertEqual(extended.validate_marker(self.marker, self.job, self.worker, "after_write"), self.marker)

    def test_stale_or_foreign_marker_never_authorizes_session_termination(self):
        changes = (("job_id", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"), ("media_id", 18),
                   ("claim_token", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                   ("pod_name", "another-worker"), ("pod_uid", "replacement-uid"),
                   ("stage", "before_write"), ("session_pid", 1), ("session_pid", True),
                   ("session_pid", "8123"), ("session_pid", None))
        for key, value in changes:
            with self.subTest(key=key, value=value):
                with self.assertRaises(AssertionError):
                    extended.validate_marker({**self.marker, key: value}, self.job, self.worker, "after_write")


class HelmRollbackBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.values = Path(self.scratch.name) / "values.json"
        self.local_file = Path(self.scratch.name) / "local.json"
        self.current = {"release": {"commitSha": "a" * 40}, "migration": {"enabled": False},
                        "images": {"api": {"repository": "localhost:5001/api", "digest": "sha256:" + "b" * 64},
                                   "mediaWorker": {"repository": "localhost:5001/worker", "digest": "sha256:" + "c" * 64}}}
        self.local = {"runtimeMode": "local", "environment": "dev", "secrets": {"provider": "kubernetes"},
                      "ingress": {"enabled": False}}
        self.environment = {"KIND_HELM_VALUES_FILE": str(self.values), "KIND_HELM_LOCAL_VALUES_FILE": str(self.local_file),
                            "KIND_SOURCE_SHA": "a" * 40,
                            "KIND_API_IMAGE": "localhost:5001/api@sha256:" + "b" * 64,
                            "KIND_WORKER_IMAGE": "localhost:5001/worker@sha256:" + "c" * 64,
                            "KIND_BASELINE_API_IMAGE": "localhost:5001/baseline@sha256:" + "d" * 64,
                            "KIND_BASELINE_SOURCE_SHA": "e" * 40, "KIND_BASELINE_ADAPTER_SHA": "f" * 64}
        self.write()

    def write(self):
        self.values.write_text(json.dumps(self.current))
        self.local_file.write_text(json.dumps(self.local))

    def test_explicit_local_overlay_and_distinct_adapted_baseline_are_accepted(self):
        self.assertEqual(extended.validate_helm_files(self.environment), self.current)

    def test_rollback_cannot_use_cloud_overlay_or_implicitly_run_schema_migration(self):
        for key, value in (("runtimeMode", "aws"), ("environment", "prod"),
                           ("secrets", {"provider": "csi"}), ("ingress", {"enabled": True})):
            original = copy.deepcopy(self.local)
            with self.subTest(key=key):
                self.local[key] = value
                self.write()
                with self.assertRaises(AssertionError):
                    extended.validate_helm_files(self.environment)
            self.local = original
        self.current["migration"]["enabled"] = True
        self.write()
        with self.assertRaises(AssertionError):
            extended.validate_helm_files(self.environment)

    def test_overlay_cannot_switch_untested_image_or_revision(self):
        for key in ("api", "mediaWorker"):
            original = copy.deepcopy(self.current)
            with self.subTest(image=key):
                self.current["images"][key]["digest"] = "sha256:" + "0" * 64
                self.write()
                with self.assertRaises(AssertionError):
                    extended.validate_helm_files(self.environment)
            self.current = original
        self.current["release"]["commitSha"] = "0" * 40
        self.write()
        with self.assertRaises(AssertionError):
            extended.validate_helm_files(self.environment)

    def test_same_binary_rollback_or_missing_adapter_provenance_is_rejected(self):
        for key, value in (("KIND_BASELINE_API_IMAGE", "baseline:latest"),
                           ("KIND_BASELINE_API_IMAGE", self.environment["KIND_API_IMAGE"]),
                           ("KIND_BASELINE_SOURCE_SHA", "a" * 40),
                           ("KIND_BASELINE_SOURCE_SHA", "main"),
                           ("KIND_BASELINE_ADAPTER_SHA", "")):
            with self.subTest(key=key):
                with self.assertRaises(AssertionError):
                    extended.validate_helm_files({**self.environment, key: value})


class WorkerPodBoundaryTests(unittest.TestCase):
    def setUp(self):
        image = "localhost:5001/worker@sha256:" + "b" * 64
        self.env = {"KIND_WORKER_IMAGE": image, "KIND_SOURCE_SHA": "a" * 40,
                    "TEST_DATABASE_URL": "postgresql://fixture:fixture@127.0.0.1:15543/photo"}
        self.worker = {"name": "photo-media-worker-test", "uid": "exact-worker-uid"}
        self.pod = {"metadata": {"name": self.worker["name"], "uid": self.worker["uid"],
                     "labels": {"app.kubernetes.io/instance": "photo", "app.kubernetes.io/component": "media-worker"},
                     "annotations": {"photoplatform.io/revision": "a" * 40}},
                    "spec": {"containers": [{"name": "media-worker", "image": image}]},
                    "status": {"containerStatuses": [{"name": "media-worker", "imageID": image}]}}

    def subject(self, pod):
        with patch.dict(os.environ, self.env, clear=True):
            harness = extended.ExtendedHarness(Mock(), "kind-photoplatform-ci-test")
        harness.guard = Mock()
        harness.data = Mock(return_value=pod)
        return harness

    def test_only_exact_pod_uid_release_and_runtime_digest_can_receive_hooks(self):
        changes = (lambda p: p["metadata"].update(uid="replaced"),
                   lambda p: p["metadata"]["labels"].update({"app.kubernetes.io/instance": "other"}),
                   lambda p: p["metadata"]["labels"].update({"app.kubernetes.io/component": "api"}),
                   lambda p: p["metadata"]["annotations"].update({"photoplatform.io/revision": "c" * 40}),
                   lambda p: p["spec"]["containers"][0].update(image="worker:latest"),
                   lambda p: p["status"]["containerStatuses"][0].update(imageID="worker:foreign"))
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(self.subject(self.pod).guard_worker(self.worker)["uid"], self.worker["uid"])
            for index, change in enumerate(changes):
                with self.subTest(index=index):
                    candidate = copy.deepcopy(self.pod)
                    change(candidate)
                    with self.assertRaises(AssertionError):
                        self.subject(candidate).guard_worker(self.worker)

    def test_hook_path_cannot_escape_disposable_tmp_directory(self):
        harness = self.subject(self.pod)
        harness.pod_python = Mock()
        for filename in ("../escape", "/tmp/escape", "media-0.before_write.block", "media-1.unknown.block"):
            with self.subTest(filename=filename):
                with self.assertRaises(AssertionError):
                    harness.hook_file(self.worker, filename)
        harness.pod_python.assert_not_called()

    def test_release_never_executes_into_unowned_pending_replacement(self):
        harness = self.subject(self.pod)
        filename = "media-17.before_write.block"
        peer = {"name": "ready-original-peer", "uid": "peer-uid"}
        pending = {"name": "new-pending-pod", "uid": "new-pending-uid"}
        harness.snapshot = Mock(return_value=[pending, peer, self.worker])
        harness.pods = Mock(return_value=[])
        harness.owned_barrier_pods = {filename: {peer["uid"], self.worker["uid"]}}
        harness.hook_file = Mock()
        harness.release(self.worker, {}, filename)
        self.assertEqual([call.args for call in harness.hook_file.call_args_list],
                         [(peer, filename, "remove"), (self.worker, filename, "remove")])

    def test_cleanup_drops_vanished_tmp_volume_and_never_touches_new_pod(self):
        harness = self.subject(self.pod)
        filename = "media-17.after_write.block"
        harness.snapshot = Mock(return_value=[{"name": "new-pending-pod", "uid": "new-uid"}])
        harness.pods = Mock(return_value=[])
        harness.owned_barriers = {filename}
        harness.owned_barrier_pods = {filename: {"deleted-pod-uid"}}
        harness.hook_file = Mock()
        self.assertEqual(harness.release_owned_barriers(), [])
        harness.hook_file.assert_not_called()
        self.assertEqual(harness.owned_barriers, set())


if __name__ == "__main__":
    unittest.main()
