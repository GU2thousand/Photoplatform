"""Platform safety regressions; no AWS requests or cluster mutation."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("eks_platform", Path(__file__).resolve().parents[1] / "bootstrap.py")
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


class PlatformBootstrapSafetyTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(account_id="123456789012", region="us-east-1",
            cluster="photoplatform-eks-dev", vpc_id="vpc-0123456789abcdef0",
            environment="dev", execute=False, render_dir=None)
        self.versions = json.loads((platform.ROOT / "versions.json").read_text())
        self.identity = {"Account": self.args.account_id}
        self.cluster = {"arn": "arn:aws:eks:us-east-1:123456789012:cluster/photoplatform-eks-dev",
            "status": "ACTIVE", "version": self.versions["kubernetes"],
            "endpoint": "https://example.private.eks.amazonaws.com",
            "resourcesVpcConfig": {"vpcId": self.args.vpc_id,
                "endpointPrivateAccess": True, "endpointPublicAccess": False}}

    def test_valid_private_cluster_and_explicit_target(self):
        platform.validate_args(self.args)
        platform.validate_cluster(self.args, self.identity, self.cluster, self.versions)

    def test_wrong_cloud_target_or_public_api_rejected_before_mutation(self):
        for path, value in [("arn", "arn:aws:eks:us-east-1:999999999999:cluster/photoplatform-eks-dev"),
                            ("version", "1.33"), ("status", "CREATING"), ("endpoint", "http://insecure")]:
            with self.subTest(path=path):
                cluster = copy.deepcopy(self.cluster)
                cluster[path] = value
                with self.assertRaises(ValueError):
                    platform.validate_cluster(self.args, self.identity, cluster, self.versions)
        for path, value in [("vpcId", "vpc-11111111"),
                            ("endpointPublicAccess", True), ("endpointPrivateAccess", False)]:
            with self.subTest(path=path):
                cluster = copy.deepcopy(self.cluster)
                cluster["resourcesVpcConfig"][path] = value
                with self.assertRaises(ValueError):
                    platform.validate_cluster(self.args, self.identity, cluster, self.versions)
        with self.assertRaises(ValueError):
            platform.validate_cluster(self.args, {"Account": "999999999999"}, self.cluster, self.versions)

    def test_environment_confusion_and_unsupported_partitions_rejected(self):
        for key, value in [("environment", "prod"), ("region", "cn-north-1"),
                           ("account_id", "123"), ("cluster", "cluster;terraform destroy")]:
            with self.subTest(key=key):
                args = copy.deepcopy(self.args)
                setattr(args, key, value)
                with self.assertRaises(ValueError):
                    platform.validate_args(args)

    def test_altered_chart_fails_before_any_chart_file_is_written(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b"tampered chart"
        with tempfile.TemporaryDirectory() as temp, patch.object(platform.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "checksum"):
                platform.download_chart(self.versions["components"]["aws_load_balancer_controller"], Path(temp))
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_pinned_policy_is_official_and_bootstrap_does_not_run_cloud_changes_in_render_mode(self):
        policy = platform.REPO_ROOT / "infra/modules/eks/policies/aws-load-balancer-controller.json"
        self.assertEqual(hashlib.sha256(policy.read_bytes()).hexdigest(), self.versions["iam_policy"]["sha256"])
        with tempfile.TemporaryDirectory() as temp:
            args = copy.deepcopy(self.args)
            args.render_dir = str(Path(temp) / "render")
            args.evidence_dir = str(Path(temp) / "evidence")
            with patch.object(platform.shutil, "which", return_value="helm"), \
                 patch.object(platform, "preflight", side_effect=AssertionError("AWS preflight in render mode")), \
                 patch.object(platform, "download_chart", return_value=Path(temp) / "chart.tgz"), \
                 patch.object(platform, "run", return_value="---\n        - --enable-backend-security-group=false\n        - --enable-manage-backend-security-group-rules=false\n" ) as commands:
                platform.bootstrap(args)
            self.assertTrue(all(command.args[0][0] == "helm" for command in commands.call_args_list))
            self.assertEqual(json.loads((Path(args.evidence_dir) / "bootstrap.json").read_text())["status"], "rendered")

    def test_namespaced_rbac_verification_checks_rejected_permissions(self):
        # A permissive existing binding must cause bootstrap to fail, even when
        # its new namespace Role looks correct in source.
        permissive = argparse.Namespace(stdout="yes\n", returncode=0)
        with patch.object(platform.subprocess, "run", return_value=permissive):
            with self.assertRaisesRegex(ValueError, "roles.rbac"):
                platform.verify_release_rbac({}, "photoplatform-dev")

    def test_namespace_identity_reader_can_get_exactly_two_namespace_objects(self):
        for namespace in ["photoplatform-dev", "photoplatform-prod"]:
            documents = platform.additional_rbac(namespace)
            resources = json.loads(documents["namespace-read-rbac.json"])["items"]
            role, binding = resources
            self.assertEqual(role["kind"], "ClusterRole")
            self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["namespaces"],
                "resourceNames": [namespace, "kube-system"], "verbs": ["get"]}])
            self.assertEqual(binding["subjects"], [{"kind": "Group", "name": "photoplatform-deployers",
                "apiGroup": "rbac.authorization.k8s.io"}])
            self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        with self.assertRaises(ValueError):
            platform.additional_rbac("default")

    def test_workload_roles_are_namespace_scoped_and_read_only_for_fixed_serviceaccounts(self):
        resources = json.loads(platform.additional_rbac("photoplatform-dev")["observability-rbac.json"])["items"]
        roles = {resource["metadata"]["name"]: resource for resource in resources if resource["kind"] == "Role"}
        bindings = [resource for resource in resources if resource["kind"] == "RoleBinding"]
        expected = {
            "photoplatform-queue-collector": {("", "pods", "list")},
            "photoplatform-prometheus": {("", "pods", verb) for verb in ["get", "list", "watch"]},
            "photoplatform-kube-state-metrics": {("", "pods", verb) for verb in ["list", "watch"]}
                | {("apps", "deployments", verb) for verb in ["list", "watch"]}
                | {("autoscaling", "horizontalpodautoscalers", verb) for verb in ["list", "watch"]},
        }
        self.assertEqual(len(resources), 6)
        self.assertEqual(len(bindings), 3)
        for binding in bindings:
            self.assertEqual(binding["metadata"]["namespace"], "photoplatform-dev")
            self.assertEqual(binding["roleRef"]["kind"], "Role")
            self.assertEqual(len(binding["subjects"]), 1)
            subject = binding["subjects"][0]
            self.assertEqual(subject["kind"], "ServiceAccount")
            self.assertEqual(subject["namespace"], "photoplatform-dev")
            role = roles[binding["roleRef"]["name"]]
            self.assertEqual(role["metadata"]["namespace"], "photoplatform-dev")
            actual = {(group, resource, verb) for rule in role["rules"]
                for group in rule["apiGroups"] for resource in rule["resources"] for verb in rule["verbs"]}
            self.assertEqual(actual, expected[subject["name"]])

    def test_permission_verification_detects_preexisting_broad_namespace_reads(self):
        def response(command, **kwargs):
            index = command.index("can-i")
            verb, resource = command[index + 1:index + 3]
            allowed = (verb == "create" and resource in ["deployments.apps", "jobs.batch"])
            allowed |= verb == "get" and resource in ["namespaces/photoplatform-dev", "namespaces/kube-system", "namespaces/default"]
            return argparse.Namespace(stdout="yes\n" if allowed else "no\n", returncode=0 if allowed else 1)
        with patch.object(platform.subprocess, "run", side_effect=response):
            with self.assertRaisesRegex(ValueError, "namespaces/default"):
                platform.verify_release_rbac({}, "photoplatform-dev")

    def test_observability_verification_rejects_unexpected_secret_access(self):
        permissive = argparse.Namespace(stdout="yes\n", returncode=0)
        with patch.object(platform.subprocess, "run", return_value=permissive):
            with self.assertRaisesRegex(ValueError, "secrets"):
                platform.verify_observability_rbac({}, "photoplatform-dev")

    def test_chart_render_must_disable_controller_security_group_management(self):
        valid = "        - --enable-backend-security-group=false\n        - --enable-manage-backend-security-group-rules=false\n"
        platform.validate_lbc_render(valid)
        for invalid in ["---\n", valid.replace("enable-backend-security-group=false", "enable-backend-security-group=true"),
                        valid.replace("enable-manage-backend-security-group-rules=false", "enable-manage-backend-security-group-rules=true")]:
            with self.subTest(render=invalid), self.assertRaisesRegex(ValueError, "Terraform owns"):
                platform.validate_lbc_render(invalid)


if __name__ == "__main__":
    unittest.main()
