"""Release safety: exact source, bounded migration, immutable images and live Pod identity."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("eks_deploy", Path(__file__).resolve().parents[1] / "eks_deploy.py")
eks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eks)


class EKSReleaseSafety(unittest.TestCase):
    def environment(self, mode="dev"):
        registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
        return {"DEPLOY_SHA": "a" * 40, "VERIFY_RUN_ID": "123", "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
                "AWS_REGION": "us-east-1", "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/deploy",
                "EKS_CLUSTER_NAME": "photos-" + mode,
                "EKS_CLUSTER_ARN": "arn:aws:eks:us-east-1:123456789012:cluster/photos-" + mode,
                "EKS_NAMESPACE": "photoplatform-" + mode, "EKS_ENVIRONMENT": mode,
                "EKS_RELEASE": "photos", "EKS_HELM_VALUES_JSON": "{}",
                "ECR_API_REPOSITORY": registry + "/api", "ECR_WORKER_REPOSITORY": registry + "/worker",
                "VITE_API_BASE_URL": "https://api.example.test", "FRONTEND_URL": "https://app.example.test",
                "FRONTEND_BUCKET": "photos-frontend", "FRONTEND_DISTRIBUTION_ID": "ABC",
                "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "test",
                "GITHUB_REPOSITORY": "GU2thousand/Photoplatform"}

    def manifest(self):
        env = self.environment()
        return {"sha": "a" * 40, "verify_run_id": "123",
                "images": {"api": env["ECR_API_REPOSITORY"] + "@sha256:" + "b" * 64,
                           "worker": env["ECR_WORKER_REPOSITORY"] + "@sha256:" + "c" * 64},
                "runnableDigests": {"api": ["sha256:" + "d" * 64], "worker": ["sha256:" + "e" * 64]}}

    def test_preflight_rejects_wrong_account_namespace_and_plain_secrets(self):
        for changes in ({"EKS_NAMESPACE": "default"}, {"EKS_CLUSTER_ARN": "arn:aws:eks:us-east-1:999999999999:cluster/photos-dev"},
                        {"AWS_ROLE_ARN": "arn:aws:iam::999999999999:role/deploy"},
                        {"EKS_HELM_VALUES_JSON": '{"secrets":{"api":{"password":"unsafe"}}}'},
                        {"EKS_HELM_VALUES_JSON": '{"images":{"api":{"repository":"unapproved"}}}'}):
            with self.subTest(changes=changes), patch.dict(os.environ, self.environment() | changes, clear=True), patch.object(eks, "record"):
                with self.assertRaises(ValueError):
                    eks.preflight()

    def test_production_requires_separate_approved_cutover(self):
        with patch.dict(os.environ, self.environment("prod"), clear=True), patch.object(eks, "record"):
            with self.assertRaisesRegex(ValueError, "separate approved"):
                eks.preflight()
            os.environ["EKS_PRODUCTION_CUTOVER_APPROVED"] = "1"
            eks.preflight()

    def test_source_gate_refuses_new_main_or_old_successful_run(self):
        env = self.environment()
        valid = {"head_sha": env["DEPLOY_SHA"], "workflow_id": 7, "head_branch": "main",
                 "head_repository": {"full_name": env["GITHUB_REPOSITORY"]}, "event": "push", "conclusion": "success"}
        for main, changes in (("b" * 40, {}), ("a" * 40, {"head_sha": "b" * 40}),
                              ("a" * 40, {"event": "pull_request"}), ("a" * 40, {"conclusion": "failure"}),
                              ("a" * 40, {"head_repository": {"full_name": "fork/Photoplatform"}})):
            responses = [io.BytesIO(json.dumps(body).encode()) for body in
                         ({"commit": {"sha": main}}, {"id": 7}, valid | changes)]
            with self.subTest(changes=changes, main=main), patch.dict(os.environ, env, clear=True), patch.object(eks, "run", side_effect=["a" * 40, ""]), patch.object(eks, "urlopen", side_effect=responses):
                with self.assertRaises(ValueError):
                    eks.verify_source()

    def test_mutable_image_and_missing_leaf_provenance_rejected(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            eks.verify_image_manifest(self.manifest())
            for change in ("mutable", "provenance", "sha"):
                manifest = self.manifest()
                if change == "mutable":
                    manifest["images"]["api"] = self.environment()["ECR_API_REPOSITORY"] + ":latest"
                elif change == "provenance":
                    manifest["runnableDigests"]["api"] = []
                else:
                    manifest["sha"] = "b" * 40
                with self.subTest(change=change), self.assertRaises(ValueError):
                    eks.verify_image_manifest(manifest)

    def workload(self):
        image = self.manifest()["images"]["api"]
        annotations = {"photoplatform.io/revision": "a" * 40}
        deployment = {"metadata": {"name": "photos-api", "uid": "deployment", "generation": 5,
                                   "annotations": {"deployment.kubernetes.io/revision": "3"}},
                      "spec": {"replicas": 1, "template": {"metadata": {"annotations": annotations}}},
                      "status": {"observedGeneration": 5, "updatedReplicas": 1, "replicas": 1,
                                 "readyReplicas": 1, "availableReplicas": 1}}
        pod = {"metadata": {"name": "pod-a", "uid": "pod-uid", "annotations": annotations},
               "spec": {"containers": [{"name": "api", "image": image}]},
               "status": {"conditions": [{"type": "Ready", "status": "True"}],
                          "containerStatuses": [{"name": "api", "ready": True,
                                                 "imageID": "containerd://sha256:" + "d" * 64}]}}
        return deployment, pod, image

    def test_runtime_leaf_digest_passes_but_stale_ready_pod_fails(self):
        deployment, pod, image = self.workload()
        result = eks.verify_workload(deployment, [pod], image, ["sha256:" + "d" * 64], "a" * 40)
        self.assertEqual(result["pods"][0]["uid"], "pod-uid")
        for change in ("revision", "digest", "generation", "surplus"):
            current, live = copy.deepcopy(deployment), copy.deepcopy(pod)
            if change == "revision":
                live["metadata"]["annotations"]["photoplatform.io/revision"] = "b" * 40
            elif change == "digest":
                live["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "f" * 64
            elif change == "generation":
                current["status"]["observedGeneration"] = 4
            pods = [live, live] if change == "surplus" else [live]
            with self.subTest(change=change), self.assertRaises(ValueError):
                eks.verify_workload(current, pods, image, ["sha256:" + "d" * 64], "a" * 40)

    def test_migration_contract_and_failed_migration_prevent_helm(self):
        job = {"kind": "Job", "metadata": {"namespace": "photoplatform-dev", "name": "photos-migrate-456-1"},
               "spec": {"activeDeadlineSeconds": 900, "backoffLimit": 1,
                        "template": {"spec": {"restartPolicy": "Never", "containers": [
                            {"image": self.manifest()["images"]["api"], "command": ["/app/migrate.sh"]}]}}}}
        with patch.dict(os.environ, self.environment(), clear=True):
            eks.validate_migration_job(job, self.manifest()["images"]["api"])
            invalid = copy.deepcopy(job)
            invalid["spec"]["template"]["spec"]["containers"][0]["command"] = ["/app/start.sh"]
            with self.assertRaises(ValueError):
                eks.validate_migration_job(invalid, self.manifest()["images"]["api"])
            with tempfile.TemporaryDirectory() as directory, patch.object(eks, "RESULTS", Path(directory)), patch.object(eks, "preflight"), patch.object(eks, "verify_source"), patch.object(eks, "cluster_guard"), patch.object(eks, "run") as run:
                (Path(directory) / "migration.json").write_text(json.dumps({"sha": "a" * 40, "status": "failed"}))
                with self.assertRaisesRegex(ValueError, "Successful migration"):
                    eks.rollout()
                run.assert_not_called()

    def test_failure_log_redacts_passwords(self):
        logs = "jdbc:postgresql://user:topsecret@db/photos password=topsecret token=abc"
        safe = eks.safe_logs(logs)
        self.assertNotIn("topsecret", safe)
        self.assertNotIn("token=abc", safe)


if __name__ == "__main__":
    unittest.main()
