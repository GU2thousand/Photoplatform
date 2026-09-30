"""Release safety: exact source, bounded migration, immutable images and live Pod identity."""
import copy
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
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
                "EKS_RELEASE": "photoplatform", "EKS_HELM_VALUES_JSON": "{}",
                "ECR_API_REPOSITORY": registry + "/api", "ECR_WORKER_REPOSITORY": registry + "/worker",
                "VITE_API_BASE_URL": "https://api.example.test", "FRONTEND_URL": "https://app.example.test",
                "API_URL": "https://api.example.test", "CLOUD_FRONTEND_ORIGIN": "https://app.example.test",
                "S3_BUCKET": "photos-media", "STORAGE_PREFIX": "photos", "CLOUDFRONT_DOMAIN": "media.example.test",
                "TEST_OWNER_TOKEN": "owner-fixture", "TEST_OTHER_TOKEN": "other-fixture", "TEST_ADMIN_TOKEN": "admin-fixture",
                "ALLOW_CLOUD_TEST_WRITES": "1", "DISPOSABLE_ENVIRONMENT": "true",
                "FRONTEND_BUCKET": "photos-frontend", "FRONTEND_DISTRIBUTION_ID": "ABC",
                "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "test",
                "GITHUB_REPOSITORY": "GU2thousand/Photoplatform"}

    def manifest(self):
        env = self.environment()
        return {"sha": "a" * 40, "verify_run_id": "123",
                "images": {"api": env["ECR_API_REPOSITORY"] + "@sha256:" + "b" * 64,
                           "worker": env["ECR_WORKER_REPOSITORY"] + "@sha256:" + "c" * 64},
                "runnableDigests": {"api": ["sha256:" + "d" * 64], "worker": ["sha256:" + "e" * 64]}}

    def test_wrong_caller_stops_release_before_cluster_or_kubeconfig_access(self):
        account = "123456789012"
        for caller in (f"arn:aws:iam::{account}:root", f"arn:aws:iam::{account}:user/deploy",
                       f"arn:aws:sts::{account}:assumed-role/admin/run",
                       f"arn:aws:sts::{account}:assumed-role/deploy-extra/run",
                       f"arn:aws:sts::{account}:assumed-role/deploy/run/extra",
                       "arn:aws:sts::999999999999:assumed-role/deploy/run", ""):
            with self.subTest(caller=caller), patch.dict(os.environ, self.environment(), clear=True), \
                    patch.object(eks, "record") as record, \
                    patch.object(eks, "aws", return_value={"Account": account, "Arn": caller}) as aws, \
                    patch.object(eks, "run") as run, patch.object(eks, "kubectl") as kubectl:
                with self.assertRaisesRegex(ValueError, "configured AWS_ROLE_ARN"):
                    eks.cluster_guard()
                aws.assert_called_once_with("sts", "get-caller-identity")
                run.assert_not_called()
                kubectl.assert_not_called()
                self.assertEqual(record.call_args.args[1]["status"], "failed")

    def test_identity_uses_pathless_sts_role_session_and_fixed_region(self):
        env = {key: self.environment()[key] for key in ("EXPECTED_AWS_ACCOUNT_ID", "AWS_REGION", "AWS_ROLE_ARN")}
        env["AWS_ROLE_ARN"] = "arn:aws:iam::123456789012:role/platform/deploy+ci.test"
        env["AWS_DEFAULT_REGION"] = "us-west-2"
        caller = {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/deploy+ci.test/GitHub-456"}
        with patch.dict(os.environ, env, clear=True), patch.object(eks, "run", return_value=json.dumps(caller)) as run, \
                patch.object(eks, "record"):
            evidence = eks.verify_identity()
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(evidence["configured_role_arn"], env["AWS_ROLE_ARN"])
            run.assert_called_once_with("aws", "sts", "get-caller-identity", "--region", "us-east-1",
                                        "--output", "json", "--no-cli-pager")

    def test_caller_account_field_mismatch_fails_before_cluster_access(self):
        caller = {"Account": "999999999999", "Arn": "arn:aws:sts::999999999999:assumed-role/deploy/run"}
        with patch.dict(os.environ, self.environment(), clear=True), patch.object(eks, "record") as record, \
                patch.object(eks, "aws", return_value=caller) as aws, patch.object(eks, "run") as run:
            with self.assertRaisesRegex(ValueError, "caller account differs"):
                eks.cluster_guard()
            aws.assert_called_once_with("sts", "get-caller-identity")
            run.assert_not_called()
            self.assertEqual(record.call_args.args[1]["status"], "failed")

    def test_identity_invalid_target_never_calls_aws(self):
        for changes in ({"EXPECTED_AWS_ACCOUNT_ID": ""}, {"AWS_REGION": ""}, {"AWS_ROLE_ARN": ""},
                        {"AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/deploy/"},
                        {"AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/" + "a" * 65},
                        {"EXPECTED_AWS_ACCOUNT_ID": "１２３４５６７８９０１２"},
                        {"AWS_ROLE_ARN": "arn:aws:iam::999999999999:role/deploy"}):
            with self.subTest(changes=changes), patch.dict(os.environ, self.environment() | changes, clear=True), \
                    patch.object(eks, "aws") as aws, patch.object(eks, "record") as record:
                with self.assertRaises(ValueError):
                    eks.verify_identity()
                aws.assert_not_called()
                self.assertEqual(record.call_args.args[1]["status"], "failed")

    def test_invalid_identity_configuration_replaces_stale_passed_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(eks, "RESULTS", Path(directory)), \
                patch.dict(os.environ, {}, clear=True), patch.object(eks, "aws") as aws:
            stale = Path(directory) / "aws-identity.json"
            stale.write_text('{"status":"passed","caller_arn":"old"}')
            with self.assertRaises(ValueError):
                eks.verify_identity()
            report = json.loads(stale.read_text())
            self.assertEqual(report["status"], "failed")
            self.assertNotIn("caller_arn", report)
            aws.assert_not_called()

    def test_identity_cli_is_read_only_and_preserves_failure_without_secret_output(self):
        env = {key: self.environment()[key] for key in ("EXPECTED_AWS_ACCOUNT_ID", "AWS_REGION", "AWS_ROLE_ARN")}
        env["AWS_SECRET_ACCESS_KEY"] = "never-record-this-credential"
        script = Path(eks.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_aws = root / "aws"
            fake_aws.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                "assert sys.argv[1:] == ['sts','get-caller-identity','--region','us-east-1','--output','json','--no-cli-pager']\n"
                "from pathlib import Path\nPath('aws-command.json').write_text(json.dumps(sys.argv[1:]))\n"
                "if os.environ.get('FAKE_AWS_ERROR'):\n sys.stderr.write(os.environ['FAKE_AWS_ERROR']); sys.exit(255)\n"
                "print(os.environ['IDENTITY_RESPONSE'])\n")
            fake_aws.chmod(0o755)
            for mode in ("valid", "wrong-role", "credentials-error"):
                caller = "deploy" if mode == "valid" else "admin"
                current = env | {"PATH": str(root), "IDENTITY_RESPONSE": json.dumps({"Account": "123456789012",
                    "Arn": f"arn:aws:sts::123456789012:assumed-role/{caller}/run"})}
                if mode == "credentials-error":
                    current["FAKE_AWS_ERROR"] = "never-record-this-credential"
                with self.subTest(mode=mode):
                    result = subprocess.run([sys.executable, str(script), "identity"], cwd=root,
                                            env=current, text=True, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode == 0, mode == "valid")
                    report = json.loads((root / "deployment-results/aws-identity.json").read_text())
                    self.assertEqual(report["status"], "passed" if mode == "valid" else "failed")
                    self.assertNotIn("never-record-this-credential", json.dumps(report) + result.stdout + result.stderr)
                    self.assertEqual(set(json.loads((root / "aws-command.json").read_text())[:2]),
                                     {"sts", "get-caller-identity"})

    def test_preflight_rejects_wrong_account_namespace_and_plain_secrets(self):
        for changes in ({"EKS_NAMESPACE": "default"}, {"EKS_RELEASE": "other"}, {"EKS_CLUSTER_ARN": "arn:aws:eks:us-east-1:999999999999:cluster/photos-dev"},
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
            with self.assertRaisesRegex(ValueError, "smoke must be separately approved"):
                eks.preflight()
            os.environ["ALLOW_PRODUCTION_SMOKE"] = "1"
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

    @contextmanager
    def release_fixture(self, collector=True, defect=None):
        """Run the release proof against synthetic cluster snapshots, without AWS or Helm."""
        env = self.environment()
        manifest = self.manifest()
        if collector:
            env["EKS_HELM_VALUES_JSON"] = '{"queueCollector":{"enabled":true}}'
            env["ECR_COLLECTOR_REPOSITORY"] = env["ECR_API_REPOSITORY"].rsplit("/", 1)[0] + "/collector"
            manifest["images"]["collector"] = env["ECR_COLLECTOR_REPOSITORY"] + "@sha256:" + "f" * 64
            manifest["runnableDigests"]["collector"] = ["sha256:" + "1" * 64]

        def cluster_snapshot(*args):
            resource = args[1]
            if resource == "endpointslices":
                return json.dumps({"items": [{"endpoints": [{"targetRef": {"uid": "pod-api-uid"},
                                    "conditions": {"ready": True}, "addresses": ["10.0.1.23"]}]}]})
            component = args[3].split("app.kubernetes.io/component=", 1)[1]
            image_component = {"api": "api", "media-worker": "worker", "queue-collector": "collector"}[component]
            deployment, pod, _ = self.workload()
            pod = copy.deepcopy(pod)
            deployment["metadata"].update(name="photos-" + component, uid="deployment-" + component)
            pod["metadata"].update(name="pod-" + component, uid="pod-" + component + "-uid")
            pod["spec"]["containers"][0]["image"] = manifest["images"][image_component]
            pod["status"]["containerStatuses"][0]["imageID"] = "containerd://" + manifest["runnableDigests"][image_component][0]
            if component == "queue-collector":
                if defect == "revision":
                    pod["metadata"]["annotations"]["photoplatform.io/revision"] = "b" * 40
                elif defect == "digest":
                    pod["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "9" * 64
                elif defect == "unready":
                    pod["status"]["conditions"][0]["status"] = "False"
                elif defect == "missing" and resource == "deployments":
                    return '{"items":[]}'
            return json.dumps({"items": [deployment if resource == "deployments" else pod]})

        response = io.BytesIO()
        response.status = 200
        response.geturl = lambda: env["VITE_API_BASE_URL"] + "/readyz"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env, clear=True), \
                patch.object(eks, "RESULTS", Path(directory)), patch.object(eks, "verify_source"), \
                patch.object(eks, "cluster_guard"), patch.object(eks, "kubectl", side_effect=cluster_snapshot) as command, \
                patch.object(eks, "run", side_effect=["v3.test", "", '{"version":4}']), \
                patch.object(eks, "wait_ingress", return_value={"healthy_target_ips": ["10.0.1.23"]}) as ingress, \
                patch.object(eks, "urlopen", return_value=response) as public_api:
            root = Path(directory)
            (root / "images.json").write_text(json.dumps(manifest))
            (root / "migration.json").write_text(json.dumps({"status": "completed", "sha": env["DEPLOY_SHA"],
                "github_run_id": env["GITHUB_RUN_ID"], "github_run_attempt": env["GITHUB_RUN_ATTEMPT"]}))
            yield root, command, ingress, public_api

    def test_release_proves_enabled_collector_live_image_and_revision(self):
        with self.release_fixture() as (root, _, _, _):
            eks.rollout()
            report = json.loads((root / "rollout.json").read_text())
            self.assertEqual(report["status"], "completed")
            collector = report["workloads"]["queue-collector"]
            self.assertEqual(collector["deployment_uid"], "deployment-queue-collector")
            self.assertEqual(collector["pods"][0]["uid"], "pod-queue-collector-uid")
            self.assertEqual(collector["pods"][0]["image_id"], "containerd://sha256:" + "1" * 64)

    def test_collector_release_failure_stops_before_public_api_checks(self):
        for defect, message in (("revision", "revision is stale"), ("digest", "imageID differs"),
                                ("unready", "not Ready"), ("missing", "Exactly one matching Deployment")):
            with self.subTest(defect=defect), self.release_fixture(defect=defect) as (root, _, ingress, public_api):
                with self.assertRaisesRegex(ValueError, message):
                    eks.rollout()
                report = json.loads((root / "rollout.json").read_text())
                self.assertEqual(report["status"], "failed")
                self.assertNotIn("public_api_readiness", report)
                ingress.assert_not_called()
                public_api.assert_not_called()

    def test_disabled_collector_does_not_require_or_query_collector(self):
        with self.release_fixture(collector=False) as (root, command, _, _):
            eks.rollout()
            report = json.loads((root / "rollout.json").read_text())
            self.assertEqual(report["status"], "completed")
            self.assertEqual(set(report["workloads"]), {"api", "media-worker"})
            self.assertFalse(any("queue-collector" in str(call.args) for call in command.call_args_list))

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

    def test_release_refuses_bootstrap_cni_or_missing_pod_identity(self):
        addons = [{"name": name, "status": "ACTIVE", "configuration": {}}
                  for name in ("vpc-cni", "coredns", "kube-proxy", "eks-pod-identity-agent")]
        addons[0]["configuration"] = {"enableNetworkPolicy": True, "env": {"NETWORK_POLICY_ENFORCING_MODE": "strict"}}
        eks.validate_addons(addons)
        for change in ("standard", "inactive", "agent"):
            current = copy.deepcopy(addons)
            if change == "standard":
                current[0]["configuration"]["env"]["NETWORK_POLICY_ENFORCING_MODE"] = "standard"
            elif change == "inactive":
                current[0]["status"] = "UPDATING"
            else:
                current.pop()
            with self.subTest(change=change), self.assertRaises(ValueError):
                eks.validate_addons(current)

    def test_ingress_waits_for_controller_but_never_retries_authorization_or_scope_errors(self):
        with patch.object(eks, "verify_ingress", side_effect=[ValueError("Released ALB is not active"), {"healthy": True}]) as verify, patch.object(eks.time, "sleep"):
            self.assertEqual(eks.wait_ingress({"10.0.1.3"}), {"healthy": True})
            self.assertEqual(verify.call_count, 2)
        for failure in (ValueError("API ALB differs from the selected account/region"),
                        subprocess.CalledProcessError(255, ["aws"], stderr="AccessDenied")):
            with self.subTest(failure=type(failure).__name__), patch.object(eks, "verify_ingress", side_effect=failure), patch.object(eks.time, "sleep") as sleep:
                with self.assertRaises(type(failure)):
                    eks.wait_ingress({"10.0.1.3"})
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
