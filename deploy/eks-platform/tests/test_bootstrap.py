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
                 patch.object(platform, "run", return_value="---\n" ) as commands:
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


if __name__ == "__main__":
    unittest.main()
