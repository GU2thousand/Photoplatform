#!/usr/bin/env python3
"""Disposable kind acceptance with real dependencies and immutable local images.

This is Kubernetes runtime evidence, not EKS, AWS IAM, ALB, CloudFront or
NetworkPolicy enforcement evidence. It never contacts/provisions an AWS account.
The registry only listens on loopback; cleanup addresses only resources created
by this process. Kubeconfig and random credentials are excluded from artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import quote
from urllib.request import urlopen

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/photoplatform"
NODE_IMAGE = "kindest/node:v1.34.11@sha256:44e222ee2132dab25ff87301682f89eb82c7880ea3a1bf543bfe9708fd08d67d"
NAMESPACE = "photoplatform-dev"
RELEASE = "photo"
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
SILO = "docker.io/pgsty/silo:RELEASE.2026-09-16T00-00-00Z@sha256:635197cb9f36d01bee221d34d1c7d7960f6a95c48b0b6c01d99cd13bdae51a46"
BASELINE_SOURCE_SHA = "baa6f4bed7e32e52340cf8281e390de1fc15ea76"


def sanitize(text):
    text = re.sub(r"(?i)(\b(?:jdbc:)?(?:postgres(?:ql)?|amqps?)://[^\s:/]+:)[^\s@]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(?i)((?:password|token|secret|access_key|private_key)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)


class Runtime:
    def __init__(self, evidence):
        self.evidence = evidence.resolve()
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.scratch = tempfile.TemporaryDirectory(prefix="photoplatform-kind-")
        self.work = Path(self.scratch.name)
        self.cluster = "photoplatform-ci-" + secrets.token_hex(4)
        self.registry = self.cluster + "-registry"
        self.kubeconfig = self.work / "kubeconfig"
        self.env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}
        self.sha = self.command("git", "rev-parse", "HEAD").strip()
        if not re.fullmatch(r"[a-f0-9]{40}", self.sha):
            raise ValueError("A full Git revision is required")
        self.cluster_created = False
        self.registry_created = False
        self.forwards = []
        self.values = self.work / "release-values.json"
        self.manifest = {
            "scope": "disposable-kind-kubernetes-runtime",
            "not_validated": ["AWS EKS", "AWS IAM / Pod Identity / CSI", "ALB", "CloudFront", "managed RDS / MQ", "NetworkPolicy enforcement", "real CLIP", "production endpoint"],
            "sha": self.sha, "working_tree_dirty": bool(self.command("git", "status", "--porcelain").strip()),
            "github_run_id": os.getenv("GITHUB_RUN_ID"), "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "cluster": self.cluster, "namespace": NAMESPACE, "node_image": NODE_IMAGE,
            "images": {}, "cases": [], "status": "RUNNING",
            "kubernetes_case_totals": {"denominator": 9, "passed": 0, "failed": 0, "skipped": 0, "not_run": 9},
        }
        self.save("manifest", self.manifest)

    def save(self, name, value):
        (self.evidence / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")

    def command(self, *args, input=None, timeout=900, env=None):
        result = subprocess.run(args, cwd=ROOT, env=env or self.env, input=input,
                                text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            output = sanitize(result.stdout + result.stderr)
            (self.evidence / "command-failure.log").open("a").write(" ".join(args[:5]) + "\n" + output + "\n")
            raise RuntimeError(f"{args[0]} failed ({result.returncode}): {output[-3000:]}")
        return result.stdout

    def stream(self, name, *args, env=None, timeout=1800):
        # Retain full result output and exit code, including failed cases.
        try:
            result = subprocess.run(args, cwd=ROOT, env=env or self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            (self.evidence / (name + ".log")).write_text(sanitize(output))
            self.save(name + "-result", {"exit_code": None, "status": "TIMEOUT", "timeout_seconds": timeout})
            raise
        output = sanitize(result.stdout)
        (self.evidence / (name + ".log")).write_text(output)
        print(output, flush=True)
        self.save(name + "-result", {"exit_code": result.returncode})
        if result.returncode:
            raise RuntimeError(f"{name} failed ({result.returncode}); see retained full log")

    def k(self, *args, input=None, timeout=600):
        return self.command("kubectl", "--kubeconfig", str(self.kubeconfig), "--context", "kind-" + self.cluster,
                            "--namespace", NAMESPACE, *args, input=input, timeout=timeout)

    def apply(self, resource):
        # Secret payloads are sent on stdin and never written to a log/file.
        self.k("apply", "-f", "-", input=json.dumps(resource))

    def stage(self, name, action):
        row = {"name": name, "started_at": time.time(), "status": "RUNNING"}
        self.manifest["cases"].append(row)
        self.save("manifest", self.manifest)
        print(f"Starting {name}", flush=True)
        try:
            action()
            row["status"] = "PASS"
        except Exception as exc:
            row.update(status="FAIL", error_type=type(exc).__name__, error=sanitize(str(exc)))
            raise
        finally:
            row["finished_at"] = time.time()
            self.save("manifest", self.manifest)

    def build_images(self):
        # Random resource name plus absence check makes cleanup ownership explicit.
        inspection = subprocess.run(["docker", "inspect", self.registry], capture_output=True)
        if inspection.returncode == 0:
            raise ValueError("Disposable registry name is already in use")
        self.registry_created = True
        self.command("docker", "run", "-d", "--name", self.registry, "--label", "photoplatform.disposable=true",
                     "-p", "127.0.0.1:5001:5000", "registry:3", timeout=300)
        baseline_context = self.baseline_adapter()
        for component, context, revision in (("api", "backend", self.sha), ("worker", "worker", self.sha),
                                             ("baseline_api", str(baseline_context), BASELINE_SOURCE_SHA)):
            repository = "localhost:5001/photoplatform-" + component
            tagged = repository + ":" + revision
            self.stream(component + "-build", "docker", "build", "--label", "org.opencontainers.image.revision=" + revision,
                        "--tag", tagged, context, timeout=1800)
            self.stream(component + "-push-local", "docker", "push", tagged, timeout=600)
            refs = json.loads(self.command("docker", "image", "inspect", tagged, "--format", "{{json .RepoDigests}}"))
            refs = [r for r in refs if r.startswith(repository + "@")]
            if len(refs) != 1 or not DIGEST.fullmatch(refs[0].split("@")[-1]):
                raise ValueError("Local registry did not return exactly one immutable image reference")
            labels = json.loads(self.command("docker", "image", "inspect", tagged, "--format", "{{json .Config.Labels}}"))
            if labels.get("org.opencontainers.image.revision") != revision:
                raise ValueError("Built image revision differs from source SHA")
            self.manifest["images"][component] = refs[0]
            self.save("manifest", self.manifest)

    def baseline_adapter(self):
        """Compile the pinned baseline business code with an explicit probe/package adapter.

        Extra DB columns/migrations remain from the new release. This proves
        business-code schema compatibility; it is not an untouched old image.
        """
        resolved = self.command("git", "rev-parse", BASELINE_SOURCE_SHA + "^{commit}").strip()
        if resolved != BASELINE_SOURCE_SHA:
            raise ValueError("Pinned baseline source commit is not available")
        archive = self.work / "baseline.tar"
        with archive.open("wb") as target:
            subprocess.run(["git", "archive", "--format=tar", BASELINE_SOURCE_SHA, "backend/"], cwd=ROOT,
                           stdout=target, stderr=subprocess.PIPE, check=True, timeout=60)
        context = self.work / "baseline"
        context.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(context, filter="data")
        backend = context / "backend"
        adapters = {}
        for filename in ("Dockerfile", "start.sh", "migrate.sh"):
            shutil.copy2(ROOT / "backend" / filename, backend / filename)
            adapters[filename] = "current packaging / mounted-secret entrypoint"
        security = backend / "src/main/java/com/generatecloud/app/config/SecurityConfig.java"
        original = security.read_text()
        old = '"/actuator/prometheus", "/readyz").permitAll()'
        if original.count(old) != 1:
            raise ValueError("Pinned baseline probe adapter anchor differs")
        security.write_text(original.replace(old, '"/actuator/prometheus", "/readyz", "/livez").permitAll()'))
        adapters[str(security.relative_to(backend))] = "permit independent /livez probe only"
        properties = backend / "src/main/resources/application.properties"
        properties.write_text(properties.read_text() + "\n# Disposable kind probe/stop compatibility adapter.\n"
            "management.endpoint.health.group.liveness.include=livenessState\n"
            "management.endpoint.health.group.liveness.additional-path=server:/livez\n"
            "server.shutdown=graceful\nspring.lifecycle.timeout-per-shutdown-phase=30s\n")
        adapters[str(properties.relative_to(backend))] = "independent liveness endpoint and graceful stop only"
        hashes = {filename: hashlib.sha256((backend / filename).read_bytes()).hexdigest() for filename in adapters}
        adapter_sha = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        self.manifest["baseline_compatibility_image"] = {"source_sha": BASELINE_SOURCE_SHA, "packaging_source_sha": self.sha,
            "adapter_sha256": adapter_sha, "adapter_files": adapters, "adapter_file_sha256": hashes,
            "scope": "pinned baseline business code with explicit probe/packaging adapter; not untouched historical image"}
        self.save("manifest", self.manifest)
        return backend

    def cluster_setup(self):
        if self.cluster in self.command("kind", "get", "clusters").splitlines():
            raise ValueError("Disposable cluster name is already in use")
        config = self.work / "kind.yaml"
        config.write_text("kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\nnodes:\n- role: control-plane\n- role: worker\n- role: worker\n")
        self.cluster_created = True
        self.stream("kind-create", "kind", "create", "cluster", "--name", self.cluster,
                    "--kubeconfig", str(self.kubeconfig), "--config", str(config), "--image", NODE_IMAGE, "--wait", "180s", timeout=600)
        self.kubeconfig.chmod(0o600)
        self.command("docker", "network", "connect", "kind", self.registry)
        for node in self.command("kind", "get", "nodes", "--name", self.cluster).splitlines():
            directory = "/etc/containerd/certs.d/localhost:5001"
            self.command("docker", "exec", node, "mkdir", "-p", directory)
            self.command("docker", "exec", "-i", node, "cp", "/dev/stdin", directory + "/hosts.toml",
                         input=f'[host."http://{self.registry}:5000"]\n')
        # Dependencies have explicit tolerations; application replicas must stay
        # on the two worker nodes so a whole worker-node drain is meaningful.
        self.k("taint", "nodes", self.cluster + "-control-plane", "node-role.kubernetes.io/control-plane:NoSchedule", "--overwrite")
        self.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE,
                    "labels": {"app.kubernetes.io/part-of": "photoplatform", "photoplatform.io/environment": "dev", "photoplatform.io/disposable": "true"}}})
        self.manifest["kube_system_uid"] = json.loads(self.k("get", "namespace", "kube-system", "-o", "json"))["metadata"]["uid"]
        self.manifest["namespace_uid"] = json.loads(self.k("get", "namespace", NAMESPACE, "-o", "json"))["metadata"]["uid"]
        self.manifest["versions"] = {tool: self.command(*args).strip() for tool, args in {
            "kind": ("kind", "version"), "helm": ("helm", "version", "--short"),
            "kubernetes": ("kubectl", "--kubeconfig", str(self.kubeconfig), "version", "-o", "json")}.items()}
        self.save("manifest", self.manifest)

    def secret(self, name, data):
        self.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": NAMESPACE}, "type": "Opaque", "stringData": data})

    def dependency(self, name, image, ports, env, command, probe, mount):
        labels = {"app.kubernetes.io/part-of": "photoplatform", "app.kubernetes.io/component": "dependency", "dependency": name}
        self.apply({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name},
                    "spec": {"selector": {"dependency": name}, "ports": [{"name": "p" + str(p), "port": p, "targetPort": p} for p in ports]}})
        container = {"name": name, "image": image, "ports": [{"containerPort": p} for p in ports], "env": env,
                     "volumeMounts": [{"name": "data", "mountPath": mount}],
                     "readinessProbe": {**probe, "periodSeconds": 5, "timeoutSeconds": 5, "failureThreshold": 60},
                     "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "2", "memory": "1Gi"}}}
        if command:
            container["args"] = command
        self.apply({"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": name, "labels": labels},
                    "spec": {"serviceName": name, "replicas": 1, "selector": {"matchLabels": {"dependency": name}},
                             "template": {"metadata": {"labels": labels}, "spec": {"containers": [container],
                                 "nodeSelector": {"kubernetes.io/hostname": self.cluster + "-control-plane"},
                                 "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
                                                 {"key": "node-role.kubernetes.io/master", "operator": "Exists", "effect": "NoSchedule"}]}},
                             "volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}}}]}})

    def dependencies(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        if compose["services"]["minio"]["image"] != SILO:
            raise ValueError("kind S3-compatible dependency must match the maintained Compose image digest")
        self.db_password, self.migration_password = secrets.token_hex(24), secrets.token_hex(24)
        self.mq_password, self.storage_password = secrets.token_hex(24), secrets.token_hex(24)
        self.secret("kind-dependencies", {"POSTGRES_PASSWORD": self.migration_password, "RABBITMQ_DEFAULT_PASS": self.mq_password, "MINIO_ROOT_PASSWORD": self.storage_password})
        def ref(name):
            return {"name": name, "valueFrom": {"secretKeyRef": {"name": "kind-dependencies", "key": name}}}
        self.dependency("postgres", "pgvector/pgvector:0.8.2-pg16", [5432], [
            {"name": "POSTGRES_DB", "value": "generatecloud"}, {"name": "POSTGRES_USER", "value": "photomigrator"}, ref("POSTGRES_PASSWORD"),
            {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}], None,
            {"exec": {"command": ["pg_isready", "-U", "photomigrator", "-d", "generatecloud"]}}, "/var/lib/postgresql/data")
        self.dependency("rabbitmq", "rabbitmq:4.3.6-management-alpine", [5672, 15672], [
            {"name": "RABBITMQ_DEFAULT_USER", "value": "generatecloud"}, ref("RABBITMQ_DEFAULT_PASS")], None,
            {"exec": {"command": ["rabbitmq-diagnostics", "-q", "check_running"]}}, "/var/lib/rabbitmq")
        self.dependency("minio", SILO, [9000], [{"name": "MINIO_ROOT_USER", "value": "minioadmin"}, ref("MINIO_ROOT_PASSWORD")],
                        ["server", "/data", "--console-address", ":9001"], {"httpGet": {"path": "/minio/health/live", "port": 9000}}, "/data")
        self.manifest["dependencies"] = {"postgres": "pgvector/pgvector:0.8.2-pg16", "rabbitmq": "rabbitmq:4.3.6-management-alpine", "s3_compatible": SILO,
            "differences_from_aws": "Disposable PVCs, local plaintext connections, static random S3 credentials; no managed-service/IAM equivalence."}
        for name in ("postgres", "rabbitmq", "minio"):
            self.k("rollout", "status", "statefulset/" + name, "--timeout=300s", timeout=330)
        common = {"RABBITMQ_USER": "generatecloud", "RABBITMQ_PASSWORD": self.mq_password,
                  "STORAGE_ACCESS_KEY": "minioadmin", "STORAGE_SECRET_KEY": self.storage_password}
        self.secret("photoplatform-api-local", {**common, "SPRING_DATASOURCE_USERNAME": "generatecloud", "SPRING_DATASOURCE_PASSWORD": self.db_password, "APP_JWT_SECRET": secrets.token_hex(48)})
        self.secret("photoplatform-worker-local", {**common, "DATABASE_USER": "generatecloud", "DATABASE_PASSWORD": self.db_password})
        self.secret("photoplatform-migrator-local", {"MIGRATOR_DATABASE_USERNAME": "photomigrator", "MIGRATOR_DATABASE_PASSWORD": self.migration_password})
        images = {key: dict(zip(("repository", "digest"), value.split("@"))) for key, value in {
            "api": self.manifest["images"]["api"], "mediaWorker": self.manifest["images"]["worker"]}.items()}
        self.release_values = {"release": {"commitSha": self.sha}, "images": images, "migration": {"enabled": False},
            "config": {"storagePublicEndpoint": "http://127.0.0.1:19000", "corsAllowedOrigins": "http://localhost:5173"}}
        self.values.write_text(json.dumps(self.release_values))
        self.manifest["config_sha256"] = hashlib.sha256(self.values.read_bytes()).hexdigest()
        self.manifest["chart_sha256"] = hashlib.sha256(b"".join(path.relative_to(CHART).as_posix().encode() + path.read_bytes() for path in sorted(CHART.rglob("*")) if path.is_file())).hexdigest()
        self.save("manifest", self.manifest)

    def helm_options(self, values=None):
        return ["--namespace", NAMESPACE, "--values", str(ROOT / "deploy/helm/values-local.yaml"), "--values", str(values or self.values)]

    def migration(self, name, bad_database=False):
        values = self.work / (name + ".json")
        overlay = {**self.release_values, "migration": {"enabled": True, "name": name, "revision": name,
                    "activeDeadlineSeconds": 120, "backoffLimit": 0}}
        if bad_database:
            overlay["config"] = {**overlay["config"], "databaseName": "deliberately_absent_database"}
        values.write_text(json.dumps(overlay))
        rendered = self.command("helm", "template", RELEASE, str(CHART), *self.helm_options(values), "--show-only", "templates/migration-job.yaml")
        resources = [r for r in yaml.safe_load_all(rendered) if r]
        jobs = [r for r in resources if r["kind"] == "Job"]
        if len(jobs) != 1:
            raise ValueError("Exactly one migration-only Job required")
        job = jobs[0]
        if job["metadata"]["name"] != name or job["spec"]["backoffLimit"] != 0 or job["spec"]["activeDeadlineSeconds"] > 120:
            raise ValueError("Migration has not respected bounded disposable settings")
        pod = job["spec"]["template"]["spec"]
        if pod["containers"][0]["image"] != self.manifest["images"]["api"] or pod["containers"][0].get("command") != ["/app/migrate.sh"]:
            raise ValueError("Migration must execute the exact API digest in migration-only mode")
        for resource in resources:
            if resource["kind"] != "Job":
                if resource["kind"] != "ServiceAccount":
                    raise ValueError("Local migration allows only its isolated ServiceAccount as support")
                self.apply(resource)
        created = json.loads(self.k("create", "-f", "-", "-o", "json", input=json.dumps(job)))
        report = {"name": name, "uid": created["metadata"]["uid"], "sha": self.sha, "status": "RUNNING", "expected_failure": bad_database}
        self.save(name, report)
        deadline = time.monotonic() + 155
        try:
            while time.monotonic() < deadline:
                state = json.loads(self.k("get", "job", name, "-o", "json"))
                if state["metadata"]["uid"] != report["uid"]:
                    raise ValueError("Migration Job identity changed")
                conditions = state.get("status", {}).get("conditions", [])
                if any(c["type"] == "Failed" and c["status"] == "True" for c in conditions):
                    report["status"] = "FAIL"
                    break
                if any(c["type"] == "Complete" and c["status"] == "True" for c in conditions):
                    report["status"] = "PASS"
                    break
                time.sleep(2)
            else:
                report["status"] = "TIMEOUT"
            report["job_status"] = state.get("status", {})
            return report
        finally:
            try:
                (self.evidence / (name + ".log")).write_text(sanitize(self.k("logs", "job/" + name, "--all-containers=true")))
            finally:
                self.save(name, report)

    def sql(self, sql):
        return self.k("exec", "-i", "postgres-0", "--", "psql", "-U", "photomigrator", "-d", "generatecloud", "-v", "ON_ERROR_STOP=1", "-A", "-t", input=sql)

    def history(self):
        return self.sql("SELECT installed_rank,version,description,type,script,checksum,success FROM flyway_schema_history ORDER BY installed_rank;\n").strip()

    def deploy(self, migration):
        if migration.get("sha") != self.sha or migration.get("status") != "PASS":
            raise ValueError("Successful migration of the exact source revision required before Helm release")
        # Verify the completed Job still exists with the recorded UID.
        state = json.loads(self.k("get", "job", migration["name"], "-o", "json"))
        if state["metadata"]["uid"] != migration["uid"] or not any(c["type"] == "Complete" and c["status"] == "True" for c in state.get("status", {}).get("conditions", [])):
            raise ValueError("Migration completion evidence no longer matches its Job")
        self.stream("helm-release", "helm", "upgrade", "--install", RELEASE, str(CHART), *self.helm_options(), "--wait", "--timeout", "8m", timeout=510)

    def migration_and_release(self):
        first = self.migration("photo-migrate-first")
        if first["status"] != "PASS":
            raise RuntimeError("Initial migration failed; no application rollout permitted")
        # Runtime account has DML only; it does not own or migrate the schema.
        self.sql("CREATE ROLE generatecloud LOGIN PASSWORD '" + self.db_password + "';\n"
                 "GRANT CONNECT ON DATABASE generatecloud TO generatecloud;\nGRANT USAGE ON SCHEMA public TO generatecloud;\n"
                 "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO generatecloud;\n"
                 "REVOKE ALL ON flyway_schema_history FROM generatecloud;\n"
                 "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO generatecloud;\n"
                 "ALTER DEFAULT PRIVILEGES FOR ROLE photomigrator IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO generatecloud;\n"
                 "ALTER DEFAULT PRIVILEGES FOR ROLE photomigrator IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO generatecloud;\n")
        before = self.history()
        repeat = self.migration("photo-migrate-repeat")
        after = self.history()
        self.save("migration-repeat", {"before": before, "after": after, "same_history": before == after, "job": repeat})
        if repeat["status"] != "PASS" or before != after:
            raise RuntimeError("Migration repeat was not idempotent")
        self.deploy(first)

    def port_forward(self, resource, local, remote):
        log = (self.evidence / (resource.replace("/", "-") + "-port-forward.log")).open("w")
        process = subprocess.Popen(["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", "kind-" + self.cluster, "--namespace", NAMESPACE,
                                    "port-forward", "--address", "127.0.0.1", resource, f"{local}:{remote}"], cwd=ROOT, env=self.env, stdout=log, stderr=log)
        self.forwards.append((process, log))
        return process

    def business_tests(self):
        self.port_forward("service/photo-api", 18081, 8080)
        self.port_forward("service/postgres", 15543, 5432)
        self.port_forward("service/minio", 19000, 9000)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urlopen("http://127.0.0.1:18081/readyz", timeout=3) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise TimeoutError("API port-forward never became ready")
        test_env = {**self.env, "TEST_API_URL": "http://127.0.0.1:18081", "TEST_DATABASE_URL": f"postgresql://generatecloud:{quote(self.db_password)}@127.0.0.1:15543/generatecloud",
            "TEST_STORAGE_ENDPOINT": "http://127.0.0.1:19000", "MINIO_PASSWORD": self.storage_password,
            "ALLOW_INTEGRATION_WRITES": "1", "ALLOW_KIND_FAULTS": "1", "KIND_CLUSTER": self.cluster,
            "KIND_API_IMAGE": self.manifest["images"]["api"], "KIND_WORKER_IMAGE": self.manifest["images"]["worker"],
            "KIND_SOURCE_SHA": self.sha, "KIND_WS_ORIGIN": "http://localhost:5173", "KIND_EVIDENCE_DIR": str(self.evidence),
            "KIND_BASELINE_API_IMAGE": self.manifest["images"]["baseline_api"], "KIND_BASELINE_SOURCE_SHA": BASELINE_SOURCE_SHA,
            "KIND_BASELINE_ADAPTER_SHA": self.manifest["baseline_compatibility_image"]["adapter_sha256"],
            "KIND_HELM_VALUES_FILE": str(self.values), "KIND_HELM_LOCAL_VALUES_FILE": str(ROOT / "deploy/helm/values-local.yaml")}
        self.stream("integration", sys.executable, "scripts/integration_test.py", env=test_env, timeout=900)
        self.stream("durable-faults", sys.executable, "scripts/fault_tests.py", env=test_env, timeout=900)
        # Fault harness owns its own API/DB forwards across pod replacements.
        for process, log in self.forwards:
            process.terminate()
            process.wait(timeout=10)
            log.close()
        self.forwards.clear()
        try:
            self.stream("kubernetes-faults", sys.executable, "scripts/kubernetes/kind_tests.py", env=test_env, timeout=1800)
            self.stream("kubernetes-extended-faults", sys.executable, "scripts/kubernetes/kind_extended_tests.py", env=test_env, timeout=2400)
        finally:
            self.kind_case_totals()

    def kind_case_totals(self):
        # A skipped later suite remains in the denominator after an early failure.
        suites, totals = [], {"denominator": 9, "passed": 0, "failed": 0, "skipped": 0, "not_run": 0}
        for filename, count in (("kind-summary.json", 5), ("kind-extended-summary.json", 4)):
            path = self.evidence / filename
            if not path.is_file():
                suites.append({"summary": filename, "denominator": count, "status": "NOT_RUN"})
                totals["not_run"] += count
                continue
            summary = json.loads(path.read_text())
            cases = summary.get("cases", [])
            passed = sum(row.get("status") == "PASS" for row in cases)
            failed = sum(row.get("status") == "FAIL" for row in cases)
            if (summary.get("denominator") != count or len(cases) != count or passed + failed != count
                    or summary.get("passed") != passed or summary.get("failed") != failed or summary.get("skipped") != 0):
                raise ValueError("Malformed mandatory Kubernetes case denominator: " + filename)
            totals["passed"] += passed
            totals["failed"] += failed
            suites.append({"summary": filename, "denominator": count, "passed": passed, "failed": failed, "skipped": 0,
                           "cleanup_errors": summary.get("cleanup_errors", [])})
        self.manifest["kubernetes_suites"] = suites
        self.manifest["kubernetes_case_totals"] = totals
        self.save("manifest", self.manifest)

    def migration_failure_gate(self):
        before = json.loads(self.k("get", "deployment", "photo-api", "photo-media-worker", "-o", "json"))
        history = self.history()
        failed = self.migration("photo-migrate-deliberate-failure", bad_database=True)
        if failed["status"] != "FAIL":
            raise RuntimeError("Intentionally invalid migration did not fail")
        refused = False
        try:
            self.deploy(failed)
        except ValueError:
            refused = True
        after = json.loads(self.k("get", "deployment", "photo-api", "photo-media-worker", "-o", "json"))
        same = [(r["metadata"]["uid"], r["metadata"]["generation"], r["spec"]) for r in before["items"]] == [(r["metadata"]["uid"], r["metadata"]["generation"], r["spec"]) for r in after["items"]]
        self.save("migration-failure-gate", {"job": failed, "rollout_refused": refused, "deployments_unchanged": same, "history_unchanged": history == self.history()})
        if not refused or not same or history != self.history():
            raise RuntimeError("Failed migration changed the application release or database history")
        self.k("rollout", "status", "deployment/photo-api", "--timeout=60s")
        self.k("rollout", "status", "deployment/photo-media-worker", "--timeout=60s")
        self.port_forward("service/photo-api", 18084, 8080)
        self.wait_http("http://127.0.0.1:18084/readyz")
        self.wait_http("http://127.0.0.1:18084/api/public/summary")
        self.save("failed-migration-existing-api-smoke", {"readiness": 200, "public_business_endpoint": 200, "release_generation_unchanged": same})

    @staticmethod
    def wait_http(url, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=6) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(1)
        raise TimeoutError("Existing API did not serve the required endpoint after failed migration")

    def collect(self):
        if not self.cluster_created:
            return
        for name, args in {"pods": ("get", "pods", "-o", "json"), "deployments": ("get", "deployments", "-o", "json"),
            "jobs": ("get", "jobs", "-o", "json"), "events": ("get", "events", "-o", "json"),
            "endpointslices": ("get", "endpointslices", "-o", "json"), "nodes": ("get", "nodes", "-o", "json")}.items():
            try:
                (self.evidence / (name + ".json")).write_text(sanitize(self.k(*args)))
            except Exception as exc:
                self.save(name + "-collection-error", {"error_type": type(exc).__name__})
        try:
            pods = json.loads(self.k("get", "pods", "-o", "json"))["items"]
            for pod in pods:
                name = pod["metadata"]["name"]
                for previous in (False, True):
                    try:
                        logs = self.k("logs", name, "--all-containers=true", *(["--previous"] if previous else []))
                        (self.evidence / (name + ("-previous" if previous else "") + ".log")).write_text(sanitize(logs))
                    except Exception:
                        pass
        except Exception:
            pass

    def close(self):
        for process, log in self.forwards:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            log.close()
        self.collect()
        # Only exact random names successfully created by this process may be removed.
        if self.cluster_created:
            self.command("kind", "delete", "cluster", "--name", self.cluster, timeout=180)
        if self.registry_created:
            self.command("docker", "rm", "--force", self.registry)
        self.scratch.cleanup()

    def run(self):
        try:
            for name, action in (("build-immutable-local-images", self.build_images), ("create-disposable-kind", self.cluster_setup),
                ("real-dependencies-and-secret-files", self.dependencies), ("migration-only-repeat-and-rollout", self.migration_and_release),
                ("real-pipeline-and-kubernetes-faults", self.business_tests), ("migration-failure-prevents-rollout", self.migration_failure_gate)):
                self.stage(name, action)
            self.manifest["status"] = "PASS"
        except Exception:
            self.manifest["status"] = "FAIL"
            raise
        finally:
            self.manifest["case_totals"] = {state: sum(c["status"] == state for c in self.manifest["cases"]) for state in ("PASS", "FAIL", "RUNNING")}
            self.manifest["case_totals"]["planned"] = 6
            self.manifest["case_totals"]["not_run"] = 6 - len(self.manifest["cases"])
            self.save("manifest", self.manifest)
            try:
                self.close()
                self.manifest["cleanup"] = {"status": "PASS", "owned_resources_removed": True}
            except Exception as exc:
                self.manifest["status"] = "FAIL"
                self.manifest["cleanup"] = {"status": "FAIL", "error_type": type(exc).__name__, "error": sanitize(str(exc))}
                raise
            finally:
                self.save("manifest", self.manifest)
                (self.evidence / "summary.md").write_text(f"Kubernetes runtime acceptance: {self.manifest['status']}\n\n"
                    f"Source SHA: `{self.sha}`. Scope: disposable kind with real PostgreSQL/pgvector, RabbitMQ and S3-compatible SILO.\n\n"
                    f"Stages: `{json.dumps(self.manifest['case_totals'])}`. See all stage logs, migration records and Kubernetes case results; failed samples are retained.\n\n"
                    f"Mandatory Kubernetes fault cases: `{json.dumps(self.manifest['kubernetes_case_totals'])}`.\n\n"
                    "This run does not validate AWS EKS, Pod Identity/IAM, Secrets Store CSI, ALB, CloudFront, managed services, NetworkPolicy enforcement, real CLIP, or a production endpoint.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path("artifacts/kubernetes"))
    args = parser.parse_args()
    if os.getenv("ALLOW_KIND_FAULTS") != "1":
        raise SystemExit("Set ALLOW_KIND_FAULTS=1 to authorize a disposable local kind cluster and destructive tests within it")
    Runtime(args.evidence_dir).run()
