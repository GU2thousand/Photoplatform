"""Cloud mutation boundary tests use no real AWS, kubeconfig, HTTP or credentials."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from benchmarks import eks_worker_scaling as scaling
from scripts import eks_acceptance, eks_common, eks_failure


SHA = "a" * 40
IMAGE = "012345678901.dkr.ecr.us-east-1.amazonaws.com/api@sha256:" + "b" * 64
WORKER = IMAGE.replace("/api@", "/worker@")
ARN = "arn:aws:eks:us-east-1:012345678901:cluster/dev"
POD_UID = "9780354f-3cd3-4f45-b056-50d6c7d4eb9c"
ENV = {"EKS_CLUSTER_NAME": "dev", "EKS_CLUSTER_ARN": ARN, "EKS_NAMESPACE": "photoplatform-dev",
    "EKS_KUBE_SYSTEM_UID": "cluster-identity", "AWS_REGION": "us-east-1", "EXPECTED_AWS_ACCOUNT_ID": "012345678901",
    "API_URL": "https://dev.example.com", "ALLOW_EKS_SCALING": "1", "ALLOW_EKS_FAILURE_INJECTION": "1"}
MANIFEST = {"sha": SHA, "images": {"api": IMAGE, "worker": WORKER},
            "runnableDigests": {"api": ["sha256:" + "b" * 64], "worker": ["sha256:" + "b" * 64]}}


class FakeCluster:
    def __init__(self):
        self.cluster = {"arn": ARN, "endpoint": "https://dev.eks.amazonaws.com", "certificateAuthority": {"data": "expected-ca"},
            "status": "ACTIVE", "version": "1.34", "tags": {"Project": "photoplatform", "Environment": "dev", "DisposableEnvironment": "true"}}
        self.config = {"clusters": [{"cluster": {"server": self.cluster["endpoint"], "certificate-authority-data": "expected-ca"}}]}
        self.namespace = {"metadata": {"uid": "namespace-identity", "labels": {
            "app.kubernetes.io/part-of": "photoplatform", "photoplatform.io/environment": "dev", "photoplatform.io/disposable": "true"}}}
        self.system_uid = "cluster-identity"
        self.deps, self.pods = {}, {}
        self.hpas, self.calls = [], []
        for component, image in (("api", IMAGE), ("media-worker", WORKER)):
            labels = {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": "photoplatform", "app.kubernetes.io/component": component}
            template = {"metadata": {"labels": labels, "annotations": {"photoplatform.io/revision": SHA}},
                        "spec": {"containers": [{"name": component, "image": image}]}}
            self.deps[component] = {"metadata": {"name": "photoplatform-" + component, "uid": "dep-" + component,
                "resourceVersion": "42", "generation": 1}, "spec": {"replicas": 1, "template": template},
                "status": {"observedGeneration": 1, "updatedReplicas": 1, "readyReplicas": 1}}
            pod = copy.deepcopy(template)
            pod["metadata"].update(name="photoplatform-" + component + "-pod", uid=POD_UID,
                                  ownerReferences=[{"controller": True, "kind": "ReplicaSet", "uid": "rs-" + component}])
            pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [{"name": component, "imageID": image, "ready": True, "state": {"running": {}}, "restartCount": 0}]}
            self.pods[component] = pod
        self.aws = Mock()
        self.aws.client.return_value.describe_cluster.side_effect = lambda **kwargs: {"cluster": self.cluster}

    def run(self, context, namespace, *args, body=None):
        self.calls.append((context, namespace, args, body))
        if args[0] == "config":
            return json.dumps(self.config)
        if args[:2] == ("get", "namespace"):
            return json.dumps({"metadata": {"uid": self.system_uid}} if args[2] == "kube-system" else self.namespace)
        component = "media-worker" if any("component=media-worker" in a for a in args) else "api"
        if args[:2] == ("get", "deployments"):
            data = [self.deps[component]]
        elif args[:2] == ("get", "replicasets"):
            data = [{"metadata": {"uid": "rs-" + component, "ownerReferences": [
                {"controller": True, "kind": "Deployment", "uid": self.deps[component]["metadata"]["uid"]}]}}]
        elif args[:2] == ("get", "pods"):
            data = [self.pods[component]]
        elif args[:2] == ("get", "hpa"):
            data = self.hpas
        elif args[:2] == ("get", "ingresses"):
            data = [{"spec": {"rules": [{"host": "dev.example.com", "http": {"paths": [
                {"backend": {"service": {"name": "photoplatform-api"}}}]}}]}}]
        elif args[:2] == ("get", "services"):
            data = [{"metadata": {"name": "photoplatform-api"}, "spec": {
                "selector": self.deps["api"]["spec"]["template"]["metadata"]["labels"]}}]
        elif args[0] in {"delete", "scale"}:
            return "{}"
        else:
            raise AssertionError(f"Unexpected mock operation: {args}")
        return json.dumps({"items": data})

    def control(self):
        return eks_common.EKSControl(self.aws, MANIFEST, SHA)

    def mutations(self):
        return [call for call in self.calls if call[2][0] in {"delete", "scale"}]


class HarnessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cluster = FakeCluster()
        self.run = patch.object(eks_common, "kubectl", side_effect=self.cluster.run)
        self.run.start()
        self.addCleanup(self.run.stop)

    def test_actual_cluster_endpoint_and_ca_are_required_before_any_write(self):
        for property_name, bad in (("server", "https://other.eks.amazonaws.com"), ("certificate-authority-data", "other-ca"),
                                   ("insecure-skip-tls-verify", True)):
            with self.subTest(property_name=property_name):
                original = copy.deepcopy(self.cluster.config)
                self.cluster.config["clusters"][0]["cluster"][property_name] = bad
                with self.assertRaises(ValueError):
                    self.cluster.control()
                self.cluster.config = original
        self.assertEqual(self.cluster.mutations(), [])

    def test_production_or_untagged_cluster_rejected_before_kubernetes_access(self):
        self.cluster.cluster["tags"]["Environment"] = "prod"
        with self.assertRaises(ValueError):
            self.cluster.control()
        self.assertEqual(self.cluster.calls, [])

    def test_wrong_account_arn_rejected(self):
        self.cluster.cluster["arn"] = ARN.replace("012345678901", "111111111111")
        with self.assertRaises(ValueError):
            self.cluster.control()
        self.assertEqual(self.cluster.calls, [])

    def test_namespace_and_cluster_uid_are_separately_pinned(self):
        self.cluster.system_uid = "replaced-cluster"
        with self.assertRaises(ValueError):
            self.cluster.control()
        self.cluster.system_uid = "cluster-identity"
        self.cluster.namespace["metadata"]["labels"].pop("photoplatform.io/disposable")
        with self.assertRaises(ValueError):
            self.cluster.control()
        self.assertEqual(self.cluster.mutations(), [])

    def test_wrong_deployment_revision_rejected(self):
        self.cluster.deps["media-worker"]["spec"]["template"]["metadata"]["annotations"]["photoplatform.io/revision"] = "c" * 40
        with self.assertRaises(ValueError):
            self.cluster.control()

    def test_selected_release_provenance_contains_real_uid_and_digest(self):
        control = self.cluster.control()
        state = control.state("media-worker")
        self.assertEqual(state["pods"][0]["uid"], POD_UID)
        self.assertEqual(state["pods"][0]["imageIDs"], [WORKER])
        self.assertEqual(control.cluster_evidence["namespaceUid"], "namespace-identity")
        self.assertTrue(all(c[0] == ARN and c[1] == "photoplatform-dev" for c in self.cluster.calls))
        self.assertEqual(self.cluster.aws.client.call_args[0], ("eks",))

    def test_running_pod_wrong_image_id_rejected(self):
        self.cluster.pods["media-worker"]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "c" * 64
        control = self.cluster.control()
        with self.assertRaises(ValueError):
            control.state("media-worker")

    def test_platform_leaf_digest_is_permitted_only_by_manifest_provenance(self):
        manifest = copy.deepcopy(MANIFEST)
        leaf = "sha256:" + "c" * 64
        manifest["runnableDigests"] = {"worker": [leaf]}
        self.cluster.pods["media-worker"]["status"]["containerStatuses"][0]["imageID"] = "containerd://" + leaf
        control = eks_common.EKSControl(self.cluster.aws, manifest, SHA)
        self.assertEqual(control.state("media-worker")["pods"][0]["imageIDs"], ["containerd://" + leaf])

    def test_label_spoofed_pod_without_verified_owner_rejected(self):
        self.cluster.pods["media-worker"]["metadata"]["ownerReferences"][0]["uid"] = "other-rs"
        control = self.cluster.control()
        with self.assertRaises(ValueError):
            control.state("media-worker")

    def test_api_host_must_route_to_the_guarded_api_service(self):
        with patch.dict(os.environ, {"API_URL": "https://other.example.com"}):
            with self.assertRaises(ValueError):
                self.cluster.control()

    def test_changed_pod_uid_rejected_without_delete(self):
        control = self.cluster.control()
        with patch.object(eks_failure, "kubectl", side_effect=self.cluster.run):
            with self.assertRaises(ValueError):
                eks_failure.delete_exact_pod(control, "photoplatform-media-worker-pod", "bbb0a560-bd23-4e51-b3cd-742d0fa2b7bf")
        self.assertEqual(self.cluster.mutations(), [])

    def test_pod_deletion_sends_server_side_uid_precondition_exact_namespace(self):
        control = self.cluster.control()
        with patch.object(eks_failure, "kubectl", side_effect=self.cluster.run):
            result = eks_failure.delete_exact_pod(control, "photoplatform-media-worker-pod", POD_UID)
        mutations = self.cluster.mutations()
        self.assertEqual(len(mutations), 1)
        self.assertEqual(mutations[0][2], ("delete", "--raw", "/api/v1/namespaces/photoplatform-dev/pods/photoplatform-media-worker-pod", "-f", "-"))
        self.assertEqual(mutations[0][3]["preconditions"], {"uid": POD_UID})
        self.assertNotIn("gracePeriodSeconds", mutations[0][3])
        self.assertEqual(result["pod"]["uid"], POD_UID)

    def test_fault_requires_explicit_switch_before_kubernetes_mutation(self):
        control = self.cluster.control()
        with patch.dict(os.environ, {"ALLOW_EKS_FAILURE_INJECTION": "0"}):
            with self.assertRaises(ValueError):
                eks_failure.delete_exact_pod(control, "photoplatform-media-worker-pod", POD_UID)
        self.assertEqual(self.cluster.mutations(), [])

    def test_running_job_requires_exact_pod_owner_and_an_unexpired_claim(self):
        db = Mock()
        db.__enter__ = Mock(return_value=db)
        db.__exit__ = Mock(return_value=False)
        job_id = "f4d37a9d-b796-41e8-9bf7-9b9794a8e9e7"
        valid = [job_id, 12, "RUNNING", "photoplatform-media-worker-pod", 1, "future-lease", True, True]
        with patch.dict(os.environ, {"BENCHMARK_DATABASE_URL": "postgresql://sensitive"}), \
                patch.dict("sys.modules", {"psycopg": Mock(connect=Mock(return_value=db))}):
            for changed_index, bad_value in ((3, "another-pod"), (6, False), (7, False), (2, "DONE")):
                bad = valid.copy()
                bad[changed_index] = bad_value
                db.execute.return_value.fetchall.return_value = [bad]
                with self.assertRaises(ValueError):
                    eks_failure.running_job(12, job_id, "photoplatform-media-worker-pod")
            db.execute.return_value.fetchall.return_value = [valid]
            result = eks_failure.running_job(12, job_id, "photoplatform-media-worker-pod")
        self.assertEqual(result["workerId"], "photoplatform-media-worker-pod")
        self.assertNotIn("claimToken", result)
        self.assertTrue(any(call.args == ("SET TRANSACTION READ ONLY",) for call in db.execute.call_args_list))

    def test_matching_hpa_rejected_without_replica_write(self):
        control = self.cluster.control()
        self.cluster.hpas = [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "photoplatform-media-worker"}}}]
        with self.assertRaises(ValueError):
            scaling.WorkerControl(control)
        self.assertEqual(self.cluster.mutations(), [])

    def test_scale_uses_replica_and_resource_version_preconditions(self):
        worker = scaling.WorkerControl(self.cluster.control())
        with patch.object(scaling, "kubectl", side_effect=self.cluster.run):
            worker.request_scale(2)
        self.assertEqual(self.cluster.mutations()[0][2], ("scale", "deployment/photoplatform-media-worker", "--replicas=2", "--current-replicas=1", "--resource-version=42"))
        self.assertEqual(worker.original_count, 1)

    def test_replaced_deployment_refuses_scaling_including_restoration(self):
        worker = scaling.WorkerControl(self.cluster.control())
        self.cluster.deps["media-worker"]["metadata"]["uid"] = "new-deployment"
        with self.assertRaises(ValueError):
            worker.request_scale(2)
        self.assertEqual(self.cluster.mutations(), [])


class ReportAndSafetyTests(unittest.TestCase):
    def test_mutable_image_or_mismatched_sha_refused(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV):
            path = Path(directory) / "images.json"
            path.write_text(json.dumps(MANIFEST))
            with self.assertRaises(ValueError):
                eks_common.load_manifest(path, "c" * 40)
            manifest = copy.deepcopy(MANIFEST)
            manifest["images"]["worker"] = "repo:latest"
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                eks_common.load_manifest(path, SHA)

    def test_foreign_registry_and_missing_platform_provenance_refused(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV):
            path = Path(directory) / "images.json"
            manifest = copy.deepcopy(MANIFEST)
            manifest["images"]["worker"] = WORKER.replace("012345678901", "111111111111")
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                eks_common.load_manifest(path, SHA)
            manifest = copy.deepcopy(MANIFEST)
            del manifest["runnableDigests"]["worker"]
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                eks_common.load_manifest(path, SHA)

    def test_failed_and_unconfirmed_attempts_remain_in_denominator(self):
        trial = {"attempted": 5, "rawUploads": [{"success": x} for x in [True, True, True, False, False]],
            "terminal": [{"status": "READY"}, {"status": "FAILED"}]}
        totals = scaling.trial_totals(trial)
        self.assertEqual(totals, {"attempted": 5, "prepared": 3, "successful": 1, "preparationFailedOrUnobserved": 2,
            "terminalFailures": 1, "pendingOrUnobserved": 1, "failedOrUnconfirmed": 4, "successRate": .2})

    def test_business_pass_cannot_pass_full_eks_matrix(self):
        rows = eks_acceptance.matrix_results(True)
        self.assertEqual(sum(r["status"] == "PASS" for r in rows), 1)
        self.assertTrue(any(r["scenario"] == "cross_pod_websocket_and_reconnect" and r["status"] == "NOT_RUN" for r in rows))

    def test_missing_queue_gauge_is_not_a_zero(self):
        response = Mock(status_code=200)
        response.json.return_value = {"messages": 0, "messages_ready": 0, "consumers": 1}
        env = {"RABBITMQ_MANAGEMENT_URL": "https://mq.example.com", "RABBITMQ_USERNAME": "user", "RABBITMQ_PASSWORD": "secret"}
        with patch.dict(os.environ, env), patch.dict("sys.modules", {"requests": Mock(get=Mock(return_value=response))}):
            with self.assertRaises(ValueError):
                eks_common.queue_snapshot()

    def test_failed_kubectl_does_not_expose_credential_bearing_error(self):
        result = subprocess.CompletedProcess([], 1, "", "Bearer SUPERSECRET https://signed.example/?token=secret")
        with patch.object(subprocess, "run", return_value=result) as run:
            with self.assertRaises(RuntimeError) as caught:
                eks_common.kubectl(ARN, "dev", "get", "pods")
        self.assertNotIn("secret", str(caught.exception).lower())
        self.assertEqual(run.call_args[0][0][:5], ["kubectl", "--context", ARN, "--namespace", "dev"])
        self.assertTrue(run.call_args[1]["capture_output"])

    def test_restore_plan_persisted_and_original_count_restored_after_failed_mutation(self):
        worker = Mock(name="worker")
        worker.name, worker.uid, worker.original_count = "media", "dep-uid", 2
        worker.scale.side_effect = RuntimeError("mutation response lost")
        worker.restore.return_value = {"replicas": 2}
        eks = Mock(arn=ARN, namespace="dev")
        writes = []
        def save(path, report):
            writes.append(copy.deepcopy(report))
        with patch.dict(os.environ, {"ALLOW_EKS_SCALING": "1"}), patch.object(scaling, "eks_guard", return_value=(Mock(), eks, {})), \
                patch.object(scaling, "WorkerControl", return_value=worker), patch.object(scaling, "queue_snapshot", return_value={"messages": 0}), \
                patch.object(scaling, "fixture", return_value=b"fixture"), patch.object(scaling, "write_report", side_effect=save), \
                patch("sys.argv", ["eks_worker_scaling.py", "--images", "1", "--counts", "1"]):
            self.assertEqual(scaling.main(), 1)
        self.assertEqual(writes[0]["restorePlan"]["replicas"], 2)
        self.assertEqual(writes[0]["trials"], [])
        worker.restore.assert_called_once()
        self.assertEqual(writes[-1]["restoreStatus"], "PASS")
        self.assertEqual(writes[-1]["trials"][0]["failedOrUnconfirmed"], 1)


if __name__ == "__main__":
    unittest.main()
