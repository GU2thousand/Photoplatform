"""Render real Helm templates and reject unsafe runtime/deployment contracts.

Fixtures use reserved example hosts and synthetic digests for validation only;
they do not represent published images, AWS inventory or deployment evidence.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "deploy/helm/photoplatform"
HELM = os.environ.get("HELM", "helm")


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def fixture(environment="dev", local=False, all_features=False):
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    file = "values-local.yaml" if local else f"values-{environment}.yaml"
    values = merge(values, yaml.safe_load((ROOT / "deploy/helm" / file).read_text()))
    values["release"]["commitSha"] = "a" * 40
    values["config"].update(databaseHost="database.example.invalid", rabbitmqHost="broker.example.invalid",
                            storageBucket="photoplatform-example", corsAllowedOrigins="https://web.example.invalid")
    if not local:
        values["aws"].update(accountId="123456789012", region="us-east-1", clusterName=f"photoplatform-{environment}")
        values["network"].update(ingressCidrs=["10.0.0.0/24"], databaseCidrs=["10.0.1.0/24"],
                                brokerCidrs=["10.0.2.0/24"], kubernetesApiCidrs=["10.0.3.0/24"])
        values["ingress"].update(host="api.example.invalid", certificateArn="arn:aws:acm:us-east-1:123456789012:certificate/example")
        for role, secret in values["secrets"].items():
            if isinstance(secret, dict):
                secret.update(arn=f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{role}-example", versionId="b" * 32)
    for name, image in values["images"].items():
        if name not in {"prometheus", "otel", "kubeStateMetrics"}:
            image["repository"] = f"localhost:5001/{name.lower()}" if local else f"123456789012.dkr.ecr.us-east-1.amazonaws.com/{name.lower()}"
        image["digest"] = "sha256:" + "c" * 64
    if all_features:
        values["ml"]["enabled"] = True
        values["secrets"]["api"]["keys"].append("ENCODER_TOKEN")
        values["queueCollector"].update(enabled=True, rbacProvisioned=True)
        values["telemetry"].update(enabled=True, rbacProvisioned=True, otlpExporterEndpoint="https://otel.example.invalid")
        values["capacity"]["databaseConnectionBudget"] = 120
    return values


def render(values, namespace=None, release="photoplatform", show_only=None, output_path=None):
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "values.json"
        file.write_text(json.dumps(values))
        cmd = [HELM, "template", release, str(CHART), "--namespace", namespace or f"photoplatform-{values['environment']}",
               "--values", str(file), "--kube-version", "1.34.11"]
        if show_only:
            cmd += ["--show-only", show_only]
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=30, cwd=ROOT)
        if output_path and result.returncode == 0:
            Path(output_path).write_text(result.stdout)
        return result


class ChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which(HELM) and not Path(HELM).is_file():
            raise RuntimeError("Helm CLI is required; do not silently skip chart validation")

    def docs(self, values):
        result = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_schema_valid_for_all_environment_fixtures(self):
        schema = json.loads((CHART / "values.schema.json").read_text())
        jsonschema.Draft7Validator.check_schema(schema)
        for values in [fixture(), fixture("prod"), fixture(local=True), fixture("prod", all_features=True)]:
            with self.subTest(mode=values["runtimeMode"], environment=values["environment"], ml=values["ml"]["enabled"]):
                jsonschema.Draft7Validator(schema).validate(values)
                self.docs(values)

    def test_pod_contracts_and_private_services(self):
        docs = self.docs(fixture("prod", all_features=True))
        for doc in docs:
            self.assertEqual(doc["metadata"]["labels"]["app.kubernetes.io/name"], "photoplatform")
            self.assertEqual(doc["metadata"]["labels"]["app.kubernetes.io/instance"], "photoplatform")
        deployments = [doc for doc in docs if doc["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 8)  # API/media/encoder/embedding/collector/Prometheus/OTEL/state metrics
        for deployment in deployments:
            pod = deployment["spec"]["template"]["spec"]
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            for container in pod["containers"]:
                self.assertIn("@sha256:", container["image"])
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                env = {item["name"]: item for item in container.get("env", [])}
                self.assertEqual(env["POD_UID"]["valueFrom"]["fieldRef"]["fieldPath"], "metadata.uid")
                self.assertIn("requests", container["resources"])
                self.assertIn("limits", container["resources"])
        services = [doc for doc in docs if doc["kind"] == "Service"]
        self.assertTrue(all(service["spec"]["type"] == "ClusterIP" for service in services))
        self.assertNotIn(9091, [port["port"] for service in services for port in service["spec"]["ports"]])
        ingress = next(doc for doc in docs if doc["kind"] == "Ingress")
        self.assertEqual(ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/ssl-redirect"], "443")
        self.assertEqual(ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"], {"name": "http"})
        with_groups = fixture()
        with_groups["ingress"]["securityGroups"] = ["sg-1234567890abcdef1"]
        ingress = next(doc for doc in self.docs(with_groups) if doc["kind"] == "Ingress")
        self.assertEqual(ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/manage-backend-security-group-rules"], "false")

    def test_dependency_faults_cannot_trigger_worker_liveness(self):
        docs = self.docs(fixture())
        worker = next(doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("media-worker"))
        container = worker["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["livenessProbe"]["exec"]["command"][-1], "live")
        self.assertEqual(container["readinessProbe"]["exec"]["command"][-1], "readiness")
        self.assertGreaterEqual(worker["spec"]["template"]["spec"]["terminationGracePeriodSeconds"], 120)
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(env["WORKER_SHUTDOWN_GRACE_SECONDS"]["value"], "100")

    def test_self_contained_migration_render(self):
        result = render(fixture(), show_only="templates/migration-job.yaml")
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
        self.assertEqual({doc["kind"] for doc in docs}, {"ServiceAccount", "SecretProviderClass", "Job"})
        job = next(doc for doc in docs if doc["kind"] == "Job")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["/app/migrate.sh"])
        self.assertLessEqual(job["spec"]["activeDeadlineSeconds"], 900)
        self.assertLessEqual(job["spec"]["backoffLimit"], 1)
        self.assertNotIn("envFrom", container)
        env = {item["name"]: item for item in container["env"]}
        self.assertNotIn("SPRING_DATASOURCE_PASSWORD_FILE", env)
        self.assertIn("sslmode=verify-full&sslrootcert=/app/certs/global-bundle.pem", env["MIGRATOR_DATABASE_URL"]["value"])

    def test_ml_cache_is_per_pod_and_tainted_node_schedule_is_explicit(self):
        docs = self.docs(fixture(all_features=True))
        for deployment in [doc for doc in docs if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith(("encoder", "embedding-worker"))]:
            pod = deployment["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeSelector"], {"photoplatform.io/workload": "ml"})
            self.assertIn({"key": "workload", "operator": "Equal", "value": "ml", "effect": "NoSchedule"}, pod["tolerations"])
            volume = next(volume for volume in pod["volumes"] if volume["name"] == "model-cache")
            self.assertEqual(volume["emptyDir"]["sizeLimit"], "5Gi")
            env = {item["name"]: item for item in pod["containers"][0]["env"]}
            self.assertTrue(env["TORCH_HOME"]["value"].startswith("/home/worker/.cache/"))

    def test_aws_profiles_seed_schema_and_secret_versions_cannot_drift(self):
        docs = self.docs(fixture())
        config = next(doc["data"] for doc in docs if doc["kind"] == "ConfigMap")
        self.assertEqual(config["SPRING_PROFILES_ACTIVE"], "aws,eks")
        self.assertEqual(config["APP_SEED_ENABLED"], "false")
        self.assertEqual(config["SPRING_FLYWAY_ENABLED"], "false")
        self.assertEqual(config["JPA_DDL_AUTO"], "validate")
        self.assertFalse(any(doc["kind"] == "Secret" for doc in docs))
        for secret in [doc for doc in docs if doc["kind"] == "SecretProviderClass"]:
            parameters = secret["spec"]["parameters"]
            self.assertEqual(parameters["usePodIdentity"], "true")
            objects = yaml.safe_load(parameters["objects"])
            self.assertEqual(objects[0]["objectVersion"], "b" * 32)

    def test_local_mode_is_separate_and_no_aws_identity_claimed(self):
        docs = self.docs(fixture(local=True))
        self.assertFalse(any(doc["kind"] == "Ingress" for doc in docs))
        self.assertFalse(any(doc["kind"] == "SecretProviderClass" for doc in docs))
        for doc in docs:
            if doc["kind"] in {"Deployment", "Job"}:
                self.assertNotIn("nodeSelector", doc["spec"]["template"]["spec"])
        config = next(doc["data"] for doc in docs if doc["kind"] == "ConfigMap")
        self.assertEqual(config["SPRING_PROFILES_ACTIVE"], "kubernetes-local")
        self.assertEqual(config["STORAGE_PROVIDER"], "minio")

    def test_telemetry_is_one_metrics_scraper_and_has_real_state_source(self):
        docs = self.docs(fixture(all_features=True))
        telemetry = next(doc["data"] for doc in docs if doc["kind"] == "ConfigMap" and "prometheus.yaml" in doc.get("data", {}))
        config = yaml.safe_load(telemetry["prometheus.yaml"])
        self.assertEqual(len(config["scrape_configs"]), 1)
        self.assertEqual(config["scrape_configs"][0]["kubernetes_sd_configs"][0]["namespaces"]["names"], ["photoplatform-dev"])
        otel = yaml.safe_load(telemetry["otel-collector.yaml"])
        self.assertNotIn("prometheus", otel["receivers"])
        self.assertFalse(any(doc["kind"] in {"Role", "RoleBinding", "ClusterRole"} for doc in docs))
        self.assertTrue(any(doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("kube-state-metrics") for doc in docs))

    def test_pod_identity_endpoint_allowed_and_imds_not_allowed(self):
        docs = self.docs(fixture())
        policy = next(doc for doc in docs if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"].endswith("dns-and-aws"))
        blocks = [rule.get("ipBlock", {}).get("cidr") for egress in policy["spec"]["egress"] for rule in egress.get("to", [])]
        self.assertIn("169.254.170.23/32", blocks)
        self.assertNotIn("169.254.169.254/32", blocks)

    def test_packaged_observability_files_are_identical(self):
        for name in ["otel-collector.yaml", "prometheus-rules.yaml", "dashboard.json"]:
            self.assertEqual((CHART / "files" / name).read_bytes(), (ROOT / "ops/kubernetes" / name).read_bytes(), name)

    def test_unsafe_overlays_are_rejected(self):
        cases = [
            ("tagged latest", {"images": {"api": {"repository": "123456789012.dkr.ecr.us-east-1.amazonaws.com/api:latest"}}}),
            ("missing digest", {"images": {"api": {"digest": ""}}}),
            ("wrong ECR account", {"images": {"api": {"repository": "999999999999.dkr.ecr.us-east-1.amazonaws.com/api"}}}),
            ("wrong region", {"aws": {"region": "us-west-2"}}),
            ("missing mounted secret", {"secrets": {"api": {"keys": ["APP_JWT_SECRET"]}}}),
            ("mutable secret version", {"secrets": {"api": {"versionId": ""}}}),
            ("wrong secret account", {"secrets": {"api": {"arn": "arn:aws:secretsmanager:us-east-1:999999999999:secret:bad"}}}),
            ("static AWS storage credentials", {"secrets": {"api": {"keys": fixture()["secrets"]["api"]["keys"] + ["STORAGE_ACCESS_KEY"]}}}),
            ("demo JWT in values", {"config": {"APP_JWT_SECRET": "generate-cloud-demo-secret-key-for-local-development-please-change"}}),
            ("seed enabled", {"config": {"APP_SEED_ENABLED": True}}),
            ("auto migration enabled", {"config": {"SPRING_FLYWAY_ENABLED": True}}),
            ("DB TLS disabled", {"config": {"databaseSslMode": "disable"}}),
            ("MQ TLS disabled", {"config": {"rabbitmqTls": False}}),
            ("storage endpoint AWS override", {"config": {"storageEndpoint": "http://localhost:9000"}}),
            ("network disabled", {"network": {"enabled": False}}),
            ("DB budget exceeded", {"capacity": {"databaseConnectionBudget": 10}}),
            ("drain budget exceeded", {"mediaWorker": {"drainSeconds": 200}}),
            ("migration infinite retries", {"migration": {"backoffLimit": 2}}),
            ("migration no deadline", {"migration": {"activeDeadlineSeconds": 0}}),
            ("collector missing RBAC", {"queueCollector": {"enabled": True}}),
            ("telemetry missing RBAC", {"telemetry": {"enabled": True}}),
            ("unbounded worker replicas", {"mediaWorker": {"replicas": 5}}),
            ("migrator runtime secret", {"secrets": {"migrator": {"arn": fixture()["secrets"]["api"]["arn"]}}}),
            ("unknown runtime override", {"env": {"SPRING_PROFILES_ACTIVE": "eks"}}),
        ]
        for label, overlay in cases:
            with self.subTest(label=label):
                self.assertNotEqual(render(merge(fixture(), overlay)).returncode, 0, label)

    def test_prod_boundaries_and_model_version_rejected(self):
        overlays = [
            {"runtimeMode": "local"}, {"api": {"replicas": 1}}, {"api": {"exposeInstanceId": True}},
            {"api": {"autoscaling": {"minReplicas": 1}}}, {"ingress": {"enabled": False}},
            {"topology": {"enabled": False}}, {"network": {"databaseCidrs": ["0.0.0.0/0"]}},
        ]
        for overlay in overlays:
            with self.subTest(overlay=overlay):
                self.assertNotEqual(render(merge(fixture("prod"), overlay)).returncode, 0)
        self.assertNotEqual(render(fixture(), namespace="photoplatform-prod").returncode, 0)
        self.assertNotEqual(render(fixture(), release="other-release").returncode, 0)
        self.assertNotEqual(render(merge(fixture(all_features=True), {"config": {"modelVersion": "unaudited"}})).returncode, 0)


if __name__ == "__main__":
    unittest.main()
