"""Safety regressions: digest-only deployments, sidecars, and rollback detection."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deploy", Path(__file__).resolve().parents[1] / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentSafetyTests(unittest.TestCase):
    def test_revision_preserves_sidecar_and_removes_read_only_aws_fields(self):
        original = {
            "family": "api", "taskRoleArn": "role", "revision": 9, "taskDefinitionArn": "old",
            "status": "ACTIVE", "registeredAt": "timestamp",
            "containerDefinitions": [{"name": "api", "image": "old"}, {"name": "otel", "image": "collector"}],
        }
        digest = "repository@sha256:" + "a" * 64
        revised = deploy.task_revision(original, "api", digest)
        self.assertEqual(revised["containerDefinitions"][0]["image"], digest)
        self.assertEqual(revised["containerDefinitions"][1]["image"], "collector")
        self.assertEqual(original["containerDefinitions"][0]["image"], "old")
        self.assertNotIn("revision", revised)
        self.assertNotIn("registeredAt", revised)

    def test_mutable_image_and_missing_container_rejected(self):
        with self.assertRaises(ValueError):
            deploy.task_revision({"containerDefinitions": [{"name": "api"}]}, "api", "repository:latest")
        with self.assertRaises(ValueError):
            deploy.task_revision({"containerDefinitions": [{"name": "otel"}]}, "api", "repository@sha256:" + "a" * 64)

    def test_exact_terraform_configuration_is_selected_and_family_checked(self):
        baseline = {"taskDefinition": {"taskDefinitionArn": "family:12", "family": "family", "status": "ACTIVE"}}
        with patch.object(deploy, "aws", side_effect=[{"taskDefinition": {"family": "family"}}, baseline]) as aws:
            self.assertEqual(deploy.task_baseline("family:9", "family:12"), baseline)
            self.assertEqual(aws.call_args_list[1].args, ("ecs", "describe-task-definition", "--task-definition", "family:12", "--include", "TAGS"))
        with patch.object(deploy, "aws", side_effect=[{"taskDefinition": {"family": "other"}}, baseline]):
            with self.assertRaises(ValueError):
                deploy.task_baseline("other:9", "family:12")

    def test_optional_embedding_worker_reuses_ml_image(self):
        with patch.dict(deploy.os.environ, {"ENABLE_ENCODER": "true", "ENABLE_COLLECTOR": "true", "ECR_COLLECTOR_REPOSITORY": "collector"}, clear=True):
            self.assertIn(("embedding-worker", "encoder"), deploy.deployed_components())
            self.assertIn(("collector", "collector"), deploy.deployed_components())
            self.assertEqual(deploy.service_env("embedding-worker"), "ECS_EMBEDDING_WORKER_SERVICE")

    def deployment_environment(self):
        registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
        return {
            "DEPLOY_SHA": "a" * 40, "EXPECTED_AWS_ACCOUNT_ID": "123456789012",
            "AWS_REGION": "us-east-1", "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/deploy",
            "ECR_API_REPOSITORY": registry + "/api", "ECR_WORKER_REPOSITORY": registry + "/worker",
            "ECS_CLUSTER": "cluster", "ECS_API_SERVICE": "api", "ECS_WORKER_SERVICE": "worker",
            "FRONTEND_BUCKET": "frontend", "FRONTEND_DISTRIBUTION_ID": "distribution",
            "FRONTEND_URL": "https://app.example.test", "VITE_API_BASE_URL": "https://api.example.test",
            "TASK_DEFINITION_PARAMETERS": '{"api":"/tasks/api","worker":"/tasks/worker"}',
        }

    def test_disabled_collector_ignores_retained_ecr_repository(self):
        for flag in ("false", ""):
            env = self.deployment_environment() | {
                "ENABLE_COLLECTOR": flag,
                "ECR_COLLECTOR_REPOSITORY": "123456789012.dkr.ecr.us-east-1.amazonaws.com/collector",
            }
            with self.subTest(flag=flag), patch.dict(deploy.os.environ, env, clear=True):
                deploy.preflight()
                self.assertEqual(deploy.enabled_components(), ["api", "worker"])
                self.assertEqual(deploy.deployed_components(), [("api", "api"), ("worker", "worker")])
                self.assertEqual(deploy.baseline_parameters(), {"api": "/tasks/api", "worker": "/tasks/worker"})

    def test_enabled_collector_requires_repository_service_and_baseline(self):
        env = self.deployment_environment() | {"ENABLE_COLLECTOR": "true"}
        with patch.dict(deploy.os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "ECR_COLLECTOR_REPOSITORY"):
                deploy.preflight()
            deploy.os.environ["ECR_COLLECTOR_REPOSITORY"] = "123456789012.dkr.ecr.us-east-1.amazonaws.com/collector"
            with self.assertRaisesRegex(ValueError, "ECS_COLLECTOR_SERVICE"):
                deploy.preflight()
            deploy.os.environ["ECS_COLLECTOR_SERVICE"] = "collector"
            with self.assertRaisesRegex(ValueError, "task definition parameter for collector"):
                deploy.preflight()
            deploy.os.environ["TASK_DEFINITION_PARAMETERS"] = '{"api":"/tasks/api","worker":"/tasks/worker","collector":"/tasks/collector"}'
            deploy.preflight()
            self.assertIn(("collector", "collector"), deploy.deployed_components())

    def test_healthy_requested_revision_passes(self):
        deploy.verify_rollout({
            "taskDefinition": "expected", "pendingCount": 0, "runningCount": 2, "desiredCount": 2,
            "deployments": [{"status": "PRIMARY", "taskDefinition": "expected", "rolloutState": "COMPLETED"}],
        }, "expected")

    def test_stable_rollback_is_failure(self):
        with self.assertRaises(RuntimeError):
            deploy.verify_rollout({
                "taskDefinition": "old", "pendingCount": 0, "runningCount": 2, "desiredCount": 2,
                "deployments": [{"status": "PRIMARY", "taskDefinition": "old", "rolloutState": "COMPLETED"}],
            }, "expected")

    def test_zero_tasks_is_not_healthy_deployment(self):
        with self.assertRaises(RuntimeError):
            deploy.verify_rollout({
                "taskDefinition": "expected", "pendingCount": 0, "runningCount": 0, "desiredCount": 0,
                "deployments": [{"status": "PRIMARY", "taskDefinition": "expected", "rolloutState": "COMPLETED"}],
            }, "expected")

    def test_frontend_origin_rejects_plaintext_and_embedded_credentials(self):
        for origin in ("http://api.example.test", "https://token@api.example.test", "https://api.example.test/api", "https://api.example.test?token=secret"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                deploy.validate_https(origin, "API")
        deploy.validate_https("https://api.example.test", "API")


if __name__ == "__main__":
    unittest.main()
