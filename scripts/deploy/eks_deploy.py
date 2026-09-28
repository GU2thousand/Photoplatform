#!/usr/bin/env python3
"""Release verified immutable images to an existing EKS namespace; never provision infrastructure."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy as shared

RESULTS = Path("deployment-results")
CHART = "deploy/helm/photoplatform"
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


def required(name):
    return shared.required(name)


def run(*args, input=None, timeout=900):
    return subprocess.run(list(args), input=input, capture_output=True, text=True,
                          check=True, timeout=timeout).stdout


def record(name, value):
    RESULTS.mkdir(parents=True, exist_ok=True)
    target = RESULTS / (name + ".json")
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(target)


def aws(*args):
    return json.loads(run("aws", *args, "--output", "json", "--no-cli-pager") or "{}")


def kubectl(*args, input=None):
    return run("kubectl", "--kubeconfig", str(RESULTS / "kubeconfig"),
               "--context", required("EKS_CLUSTER_ARN"), "--namespace", required("EKS_NAMESPACE"),
               *args, input=input)


def enabled_components():
    components = [("api", "backend", "backend/Dockerfile"),
                  ("worker", "worker", "worker/Dockerfile")]
    if os.getenv("ENABLE_ENCODER", "false").lower() == "true":
        components.append(("encoder", "worker", "worker/Dockerfile.ml"))
    configuration = json.loads(os.getenv("EKS_HELM_VALUES_JSON", "{}"))
    if configuration.get("queueCollector", {}).get("enabled") is True:
        components.append(("collector", "ops/kubernetes/queue-collector", "ops/kubernetes/queue-collector/Dockerfile"))
    return components


def preflight():
    account, region = required("EXPECTED_AWS_ACCOUNT_ID"), required("AWS_REGION")
    if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
        raise ValueError("Expected AWS account and region must be explicitly selected")
    if not SHA.fullmatch(required("DEPLOY_SHA")):
        raise ValueError("DEPLOY_SHA must be the full successful current-main Verify SHA")
    if not re.fullmatch(r"[1-9]\d*", required("VERIFY_RUN_ID")):
        raise ValueError("VERIFY_RUN_ID required for exact source provenance")
    if not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", required("AWS_ROLE_ARN")):
        raise ValueError("AWS_ROLE_ARN differs from expected account")
    environment = required("EKS_ENVIRONMENT")
    if environment not in {"dev", "prod"}:
        raise ValueError("EKS_ENVIRONMENT must be dev or prod")
    if environment == "prod" and os.getenv("EKS_PRODUCTION_CUTOVER_APPROVED") != "1":
        raise ValueError("Production cutover requires a separate approved EKS_PRODUCTION_CUTOVER_APPROVED=1 decision")
    if required("EKS_NAMESPACE") != "photoplatform-" + environment:
        raise ValueError("EKS_NAMESPACE must match the selected environment boundary")
    cluster = required("EKS_CLUSTER_NAME")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster):
        raise ValueError("Invalid EKS cluster name")
    if required("EKS_CLUSTER_ARN") != f"arn:aws:eks:{region}:{account}:cluster/{cluster}":
        raise ValueError("EKS_CLUSTER_ARN must match expected account, region and name")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,51}[a-z0-9])?", required("EKS_RELEASE")):
        raise ValueError("Invalid Helm release name")
    for name, _, _ in enabled_components():
        repo = required(f"ECR_{name.upper()}_REPOSITORY")
        if not re.fullmatch(rf"{account}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/[a-z0-9][a-z0-9._/-]*", repo):
            raise ValueError("ECR repository must match expected account and region")
    for name in ("VITE_API_BASE_URL", "FRONTEND_URL"):
        shared.validate_https(required(name), name)
    for name in ("FRONTEND_BUCKET", "FRONTEND_DISTRIBUTION_ID"):
        required(name)
    overrides = json.loads(required("EKS_HELM_VALUES_JSON"))
    if not isinstance(overrides, dict):
        raise ValueError("EKS_HELM_VALUES_JSON must be a JSON object of non-secret chart configuration")
    forbidden = {"release", "environment", "runtimeMode", "migration", "aws"} & set(overrides)
    if forbidden:
        raise ValueError("Identity, migration and image fields are controlled by the release script")
    if set(overrides.get("images", {})) - {"prometheus", "otel"}:
        raise ValueError("Only pinned external telemetry images may be provided in the configuration overlay")
    # Secret references belong in values; secret contents belong exclusively in Secrets Manager.
    def inspect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if re.fullmatch(r"(?i)(password|token|jwtSecret|accessKey|secretKey|privateKey|secretString)", key):
                    raise ValueError("Plain secret content is forbidden in EKS_HELM_VALUES_JSON")
                inspect(value)
        elif isinstance(node, list):
            for value in node:
                inspect(value)
    inspect(overrides)
    record("preflight", {"sha": required("DEPLOY_SHA"), "verify_run_id": required("VERIFY_RUN_ID"),
                         "account": account, "region": region, "environment": environment,
                         "namespace": required("EKS_NAMESPACE"), "cluster": required("EKS_CLUSTER_ARN"),
                         "status": "passed"})


def verify_source():
    if run("git", "rev-parse", "HEAD").strip() != required("DEPLOY_SHA"):
        raise ValueError("Working tree does not match the verified deployment SHA")
    if run("git", "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Tracked source must be unmodified before release")
    repository = required("GITHUB_REPOSITORY")
    if repository != "GU2thousand/Photoplatform":
        raise ValueError("EKS release source must be the canonical Photoplatform repository")
    def github(path):
        request = Request("https://api.github.com/repos/" + repository + path,
                          headers={"Authorization": "Bearer " + required("GITHUB_TOKEN"),
                                   "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    main = github("/branches/main")["commit"]["sha"]
    workflow = github("/actions/workflows/ci.yml")
    verified = github("/actions/runs/" + required("VERIFY_RUN_ID"))
    if main != required("DEPLOY_SHA") or verified.get("head_sha") != main or verified.get("workflow_id") != workflow["id"] or verified.get("head_branch") != "main" or verified.get("head_repository", {}).get("full_name") != repository or verified.get("event") not in {"push", "workflow_dispatch"} or verified.get("conclusion") != "success":
        raise ValueError("Only the current main SHA with its own successful canonical Verify run may release")


def cluster_guard():
    identity = aws("sts", "get-caller-identity")
    if identity["Account"] != required("EXPECTED_AWS_ACCOUNT_ID"):
        raise ValueError("AWS caller account differs from selected release account")
    cluster = aws("eks", "describe-cluster", "--name", required("EKS_CLUSTER_NAME"))["cluster"]
    if cluster["arn"] != required("EKS_CLUSTER_ARN") or cluster["status"] != "ACTIVE":
        raise ValueError("Selected EKS cluster must be the active expected ARN")
    tags = cluster.get("tags", {})
    if tags.get("Project") != "photoplatform" or tags.get("Environment") != required("EKS_ENVIRONMENT"):
        raise ValueError("EKS cluster Project/Environment tags differ from selected boundary")
    run("aws", "eks", "update-kubeconfig", "--name", required("EKS_CLUSTER_NAME"),
        "--region", required("AWS_REGION"), "--kubeconfig", str(RESULTS / "kubeconfig"),
        "--alias", required("EKS_CLUSTER_ARN"))
    (RESULTS / "kubeconfig").chmod(0o600)
    namespace = json.loads(kubectl("get", "namespace", required("EKS_NAMESPACE"), "-o", "json"))
    labels = namespace["metadata"].get("labels", {})
    if labels.get("app.kubernetes.io/part-of") != "photoplatform" or labels.get("photoplatform.io/environment") != required("EKS_ENVIRONMENT"):
        raise ValueError("Namespace labels do not authorize the selected application/environment")
    system = json.loads(kubectl("get", "namespace", "kube-system", "-o", "json"))
    evidence = {"cluster_arn": cluster["arn"], "cluster_version": cluster["version"],
                "namespace": required("EKS_NAMESPACE"), "namespace_uid": namespace["metadata"]["uid"],
                "kube_system_uid": system["metadata"]["uid"], "tags": tags,
                "private_endpoint": cluster["resourcesVpcConfig"]["endpointPrivateAccess"]}
    record("cluster", evidence)
    return evidence


def verify_repository(repo):
    state = aws("ecr", "describe-repositories", "--repository-names", repo.split("/", 1)[1])["repositories"]
    if len(state) != 1 or state[0]["repositoryUri"] != repo or state[0].get("imageTagMutability") != "IMMUTABLE":
        raise ValueError("ECR repository must be the selected immutable repository without mutable exclusions")


def verify_image_manifest(manifest):
    if manifest.get("sha") != required("DEPLOY_SHA") or str(manifest.get("verify_run_id")) != required("VERIFY_RUN_ID"):
        raise ValueError("Image manifest source differs from successful Verify provenance")
    for name, _, _ in enabled_components():
        expected = required(f"ECR_{name.upper()}_REPOSITORY")
        image = manifest.get("images", {}).get(name, "")
        if not image.startswith(expected + "@") or not DIGEST.fullmatch(image.split("@")[-1]):
            raise ValueError("Image manifest requires a digest in the selected ECR repository")
        leaves = manifest.get("runnableDigests", {}).get(name, [])
        if not leaves or any(not DIGEST.fullmatch(leaf) for leaf in leaves):
            raise ValueError("Runnable architecture digest provenance is missing")


def build():
    preflight()
    verify_source()
    cluster_guard()
    tag = required("DEPLOY_SHA") + "-" + required("GITHUB_RUN_ID") + "-" + required("GITHUB_RUN_ATTEMPT")
    if not re.fullmatch(r"[a-f0-9]{40}-[1-9]\d*-[1-9]\d*", tag):
        raise ValueError("Image tag requires a concrete GitHub run and attempt")
    registry = required("ECR_API_REPOSITORY").split("/", 1)[0]
    password = run("aws", "ecr", "get-login-password", "--region", required("AWS_REGION"))
    run("docker", "login", "--username", "AWS", "--password-stdin", registry, input=password)
    manifest = {"sha": required("DEPLOY_SHA"), "verify_run_id": required("VERIFY_RUN_ID"),
                "github_run_id": required("GITHUB_RUN_ID"), "github_run_attempt": required("GITHUB_RUN_ATTEMPT"),
                "images": {}, "runnableDigests": {}, "builds": {}}
    record("images", manifest)
    for name, context, dockerfile in enabled_components():
        repo = required(f"ECR_{name.upper()}_REPOSITORY")
        verify_repository(repo)
        metadata = RESULTS / (name + "-build.json")
        run("docker", "buildx", "build", "--platform", "linux/amd64", "--push", "--provenance=mode=max",
            "--sbom=true", "--metadata-file", str(metadata), "--file", dockerfile,
            "--label", "org.opencontainers.image.revision=" + required("DEPLOY_SHA"),
            "--tag", repo + ":" + tag, context, timeout=2400)
        digest = json.loads(metadata.read_text()).get("containerimage.digest", "")
        if not DIGEST.fullmatch(digest):
            raise ValueError("Build did not produce a valid registry digest")
        registry_digest = aws("ecr", "describe-images", "--repository-name", repo.split("/", 1)[1],
                              "--image-ids", "imageTag=" + tag)["imageDetails"][0]["imageDigest"]
        if digest != registry_digest:
            raise ValueError("ECR digest differs from build provenance")
        image = repo + "@" + digest
        raw = json.loads(run("docker", "buildx", "imagetools", "inspect", image, "--raw"))
        leaves = [m["digest"] for m in raw.get("manifests", [])
                  if m.get("platform", {}).get("os") == "linux" and m.get("platform", {}).get("architecture") == "amd64"]
        if not leaves:
            leaves = [digest] if "config" in raw else []
        if len(leaves) != 1 or not DIGEST.fullmatch(leaves[0]):
            raise ValueError("Build must contain exactly one runnable linux/amd64 manifest")
        # Validate the pushed runnable image's revision, rather than trusting a pre-existing SHA tag.
        run("docker", "pull", "--platform", "linux/amd64", image, timeout=900)
        labels = json.loads(run("docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}"))
        if labels.get("org.opencontainers.image.revision") != required("DEPLOY_SHA"):
            raise ValueError("Pushed OCI image revision does not match Verify SHA")
        manifest["images"][name] = image
        manifest["runnableDigests"][name] = leaves
        manifest["builds"][name] = {"tag": tag, "oci_revision": labels["org.opencontainers.image.revision"],
                                    "manifest_sha256": hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()}
        record("images", manifest)
    verify_image_manifest(manifest)


def release_values(manifest, migrate=False):
    verify_image_manifest(manifest)
    result = json.loads(required("EKS_HELM_VALUES_JSON"))
    result.update(environment=required("EKS_ENVIRONMENT"), runtimeMode="aws",
                  aws={"accountId": required("EXPECTED_AWS_ACCOUNT_ID"), "region": required("AWS_REGION"),
                       "clusterName": required("EKS_CLUSTER_NAME")},
                  release={"commitSha": required("DEPLOY_SHA")},
                  migration={"enabled": migrate, "revision": required("GITHUB_RUN_ID") + "-" + required("GITHUB_RUN_ATTEMPT"),
                             "name": required("EKS_RELEASE") + "-migrate-" + required("GITHUB_RUN_ID") + "-" + required("GITHUB_RUN_ATTEMPT")})
    result.setdefault("images", {})
    for component, key in (("api", "api"), ("worker", "mediaWorker"), ("encoder", "ml"), ("collector", "queueCollector")):
        if component in manifest["images"]:
            repository, digest = manifest["images"][component].split("@")
            result["images"][key] = {"repository": repository, "digest": digest}
    result.setdefault("ml", {})["enabled"] = os.getenv("ENABLE_ENCODER", "false").lower() == "true"
    target = RESULTS / ("migration-values.json" if migrate else "release-values.json")
    target.write_text(json.dumps(result, indent=2) + "\n")
    return target


def helm_args(values):
    return ["--namespace", required("EKS_NAMESPACE"), "--values",
            "deploy/helm/values-" + required("EKS_ENVIRONMENT") + ".yaml", "--values", str(values)]


def validate_migration_job(job, expected_image):
    if job.get("kind") != "Job" or job.get("metadata", {}).get("namespace") != required("EKS_NAMESPACE"):
        raise ValueError("Migration render must be a single namespaced Job")
    spec = job["spec"]
    if not 1 <= spec.get("activeDeadlineSeconds", 0) <= 900 or spec.get("backoffLimit", 99) > 1:
        raise ValueError("Migration requires a bounded deadline and finite retries")
    pod = spec["template"]["spec"]
    containers = pod.get("containers", [])
    if len(containers) != 1 or containers[0].get("image") != expected_image or containers[0].get("command") != ["/app/migrate.sh"]:
        raise ValueError("Migration must use the verified API image and migration-only entrypoint")
    if pod.get("restartPolicy") != "Never":
        raise ValueError("Migration restartPolicy must be Never")


def safe_logs(value):
    # Avoid accidental JDBC/AMQP URL passwords and credentials in exception messages.
    value = re.sub(r"(?i)(\b(?:jdbc:)?(?:postgres(?:ql)?|amqps?)://[^\s:/]+:)[^\s@]+@", r"\1[REDACTED]@", value)
    return re.sub(r"(?i)((?:password|token|secret|access_key|private_key)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)


def migrate():
    preflight()
    verify_source()
    cluster_guard()
    manifest = json.loads((RESULTS / "images.json").read_text())
    values = release_values(manifest, migrate=True)
    rendered = run("helm", "template", required("EKS_RELEASE"), CHART, *helm_args(values),
                   "--show-only", "templates/migration-job.yaml")
    rendered_json = json.loads(kubectl("create", "--dry-run=client", "-f", "-", "-o", "json", input=rendered))
    items = rendered_json.get("items", []) if rendered_json.get("kind") == "List" else [rendered_json]
    # Helm templates usually omit namespace and rely on the release namespace.
    # Make it explicit before validating and applying the isolated migration documents.
    for item in items:
        item.setdefault("metadata", {}).setdefault("namespace", required("EKS_NAMESPACE"))
    jobs = [item for item in items if item.get("kind") == "Job"]
    if len(jobs) != 1:
        raise ValueError("Migration render must contain exactly one migration Job")
    job = jobs[0]
    support = [item for item in items if item.get("kind") != "Job"]
    for item in support:
        if item.get("kind") not in {"ServiceAccount", "SecretProviderClass"} or item.get("metadata", {}).get("name") != required("EKS_RELEASE") + "-migrator" or item.get("metadata", {}).get("namespace") != required("EKS_NAMESPACE"):
            raise ValueError("Only dedicated namespaced migrator identity/secret references may precede migration")
    validate_migration_job(job, manifest["images"]["api"])
    name = job["metadata"]["name"]
    report = {"sha": required("DEPLOY_SHA"), "job": name, "status": "in_progress",
              "github_run_id": required("GITHUB_RUN_ID"), "github_run_attempt": required("GITHUB_RUN_ATTEMPT")}
    record("migration", report)
    try:
        for item in support:
            kubectl("apply", "-f", "-", input=json.dumps(item))
        # Create refuses an old Job with the same run ID; completion cannot come from stale resources.
        created = json.loads(kubectl("create", "-f", "-", "-o", "json", input=json.dumps(job)))
        report["job_uid"] = created["metadata"]["uid"]
        record("migration", report)
        deadline = time.monotonic() + job["spec"]["activeDeadlineSeconds"] + 30
        while True:
            state = json.loads(kubectl("get", "job", name, "-o", "json"))
            if state["metadata"]["uid"] != report["job_uid"]:
                raise ValueError("Migration Job identity changed")
            report["job_status"] = state.get("status", {})
            record("migration", report)
            conditions = state.get("status", {}).get("conditions", [])
            if any(c["type"] == "Failed" and c["status"] == "True" for c in conditions):
                raise RuntimeError("Migration failed; application and frontend release stopped")
            if any(c["type"] == "Complete" and c["status"] == "True" for c in conditions):
                report["status"] = "completed"
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("Migration Job did not complete within its deadline")
            time.sleep(5)
        report["job_status"] = state.get("status", {})
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        try:
            report["logs"] = safe_logs(kubectl("logs", "job/" + name, "--all-containers=true", "--tail=1000"))
        except Exception as exc:
            report["log_error_type"] = type(exc).__name__
        try:
            events = json.loads(kubectl("get", "events", "--field-selector", "involvedObject.name=" + name, "-o", "json"))["items"]
            report["events"] = [{"reason": e.get("reason"), "message": safe_logs(e.get("message", "")),
                                  "count": e.get("count"), "last_timestamp": e.get("lastTimestamp")} for e in events]
        except Exception as exc:
            report["events_error_type"] = type(exc).__name__
        record("migration", report)


def verify_workload(deployment, pods, image, leaves, sha):
    desired = deployment.get("spec", {}).get("replicas", 1)
    status = deployment.get("status", {})
    metadata = deployment["metadata"]
    annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
    if annotations.get("photoplatform.io/revision") != sha:
        raise ValueError("Deployment template does not represent requested SHA")
    if desired < 1 or status.get("observedGeneration", 0) < metadata["generation"] or any(
        status.get(key, 0) != desired for key in ("updatedReplicas", "readyReplicas", "availableReplicas")) or status.get("replicas") != desired:
        raise ValueError("Deployment has not completed the exact requested rollout")
    rows = []
    live = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    if len(live) != desired:
        raise ValueError("Pod set is incomplete or includes stale replicas")
    for pod in live:
        if pod["metadata"].get("annotations", {}).get("photoplatform.io/revision") != sha:
            raise ValueError("Ready Pod revision is stale")
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])):
            raise ValueError("New Pod is not Ready")
        spec_containers = pod["spec"].get("containers", [])
        expected = [c["name"] for c in spec_containers if c.get("image") == image]
        if len(expected) != 1:
            raise ValueError("Pod does not run exactly one expected application image")
        containers = [c for c in pod.get("status", {}).get("containerStatuses", []) if c["name"] == expected[0]]
        if len(containers) != 1 or not containers[0].get("ready"):
            raise ValueError("Application container is not Ready")
        image_id = containers[0].get("imageID", "")
        digest = re.search(r"sha256:[a-f0-9]{64}$", image_id)
        if not digest or digest.group() not in set(leaves + [image.split("@")[-1]]):
            raise ValueError("Running container imageID differs from build provenance")
        rows.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                     "image": image, "image_id": image_id, "restart_count": containers[0].get("restartCount", 0)})
    return {"deployment": metadata["name"], "deployment_uid": metadata["uid"],
            "generation": metadata["generation"], "revision": metadata.get("annotations", {}).get("deployment.kubernetes.io/revision"),
            "pods": rows}


def verify_ingress():
    host = urlparse(required("VITE_API_BASE_URL")).hostname
    rows = json.loads(kubectl("get", "ingresses", "-l", "app.kubernetes.io/instance=" + required("EKS_RELEASE"), "-o", "json"))["items"]
    selected = []
    for ingress in rows:
        paths = [path for rule in ingress["spec"].get("rules", []) if rule.get("host") == host
                 for path in rule.get("http", {}).get("paths", [])
                 if path.get("backend", {}).get("service", {}).get("name") == required("EKS_RELEASE") + "-api"]
        if paths:
            selected.append(ingress)
    if len(selected) != 1:
        raise ValueError("Public HTTPS API origin must match exactly the released API Ingress")
    ingress = selected[0]
    configuration = json.loads(required("EKS_HELM_VALUES_JSON"))
    name = configuration.get("ingress", {}).get("loadBalancerName", "")
    if not name:
        raise ValueError("Dedicated EKS ALB loadBalancerName required to verify public origin ownership")
    balances = aws("elbv2", "describe-load-balancers", "--names", name)["LoadBalancers"]
    if len(balances) != 1 or balances[0].get("State", {}).get("Code") != "active":
        raise ValueError("Released ALB is not active")
    balance = balances[0]
    arn = balance["LoadBalancerArn"]
    if not arn.startswith("arn:aws:elasticloadbalancing:" + required("AWS_REGION") + ":" + required("EXPECTED_AWS_ACCOUNT_ID") + ":loadbalancer/app/"):
        raise ValueError("API ALB differs from the selected account/region")
    hosts = {row.get("hostname") for row in ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])}
    if hosts != {balance["DNSName"]}:
        raise ValueError("Ingress endpoint does not belong to the selected EKS ALB")
    tags = {item["Key"]: item["Value"] for item in aws("elbv2", "describe-tags", "--resource-arns", arn)["TagDescriptions"][0]["Tags"]}
    if tags.get("elbv2.k8s.aws/cluster") != required("EKS_CLUSTER_NAME") or tags.get("ingress.k8s.aws/stack") != required("EKS_NAMESPACE") + "/" + ingress["metadata"]["name"]:
        raise ValueError("ALB controller ownership differs from the released cluster/Ingress")
    import socket
    def addresses(domain):
        return {item[4][0] for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)}
    if not addresses(host) & addresses(balance["DNSName"]):
        raise ValueError("Public API DNS does not resolve to the released ALB")
    return {"ingress_uid": ingress["metadata"]["uid"], "api_host": host, "alb_arn": arn, "alb_dns": balance["DNSName"], "controller_tags": tags}


def rollout():
    preflight()
    verify_source()
    cluster_guard()
    migration = json.loads((RESULTS / "migration.json").read_text())
    if migration.get("status") != "completed" or migration.get("sha") != required("DEPLOY_SHA") or migration.get("github_run_id") != required("GITHUB_RUN_ID") or migration.get("github_run_attempt") != required("GITHUB_RUN_ATTEMPT"):
        raise ValueError("Successful migration of this exact revision required before Helm release")
    manifest = json.loads((RESULTS / "images.json").read_text())
    values = release_values(manifest)
    report = {"sha": required("DEPLOY_SHA"), "status": "in_progress", "workloads": {}}
    record("rollout", report)
    try:
        run("helm", "upgrade", "--install", required("EKS_RELEASE"), CHART, *helm_args(values),
            "--kubeconfig", str(RESULTS / "kubeconfig"), "--kube-context", required("EKS_CLUSTER_ARN"),
            "--atomic", "--wait", "--timeout", "15m", timeout=960)
        components = [("api", "api"), ("media-worker", "worker")]
        if "encoder" in manifest["images"]:
            components += [("encoder", "encoder"), ("embedding-worker", "encoder")]
        for component, image_component in components:
            selector = "app.kubernetes.io/instance=" + required("EKS_RELEASE") + ",app.kubernetes.io/component=" + component
            deployments = json.loads(kubectl("get", "deployments", "-l", selector, "-o", "json"))["items"]
            if len(deployments) != 1:
                raise ValueError("Exactly one matching Deployment required per component")
            pods = json.loads(kubectl("get", "pods", "-l", selector, "-o", "json"))["items"]
            report["workloads"][component] = verify_workload(deployments[0], pods, manifest["images"][image_component],
                                                            manifest["runnableDigests"][image_component], required("DEPLOY_SHA"))
            record("rollout", report)
        endpoints = json.loads(kubectl("get", "endpointslices", "-l", "kubernetes.io/service-name=" + required("EKS_RELEASE") + "-api", "-o", "json"))["items"]
        ready_targets = {endpoint.get("targetRef", {}).get("uid") for item in endpoints for endpoint in item.get("endpoints", [])
                         if endpoint.get("conditions", {}).get("ready") is True}
        expected_targets = {p["uid"] for p in report["workloads"]["api"]["pods"]}
        if ready_targets != expected_targets:
            raise ValueError("API endpoint set does not point exclusively to the released Pod UIDs")
        report["api_endpoint_pod_uids"] = sorted(ready_targets)
        report["public_ingress"] = verify_ingress()
        report["helm"] = json.loads(run("helm", "status", required("EKS_RELEASE"), "--namespace", required("EKS_NAMESPACE"),
                                         "--kubeconfig", str(RESULTS / "kubeconfig"), "--kube-context", required("EKS_CLUSTER_ARN"), "-o", "json"))["version"]
        readiness_url = required("VITE_API_BASE_URL").rstrip("/") + "/readyz"
        with urlopen(readiness_url, timeout=30) as response:
            if response.status != 200 or response.geturl() != readiness_url:
                raise RuntimeError("Public ALB HTTPS readiness failed")
        report["public_api_readiness"] = "passed"
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        record("rollout", report)


def frontend():
    preflight()
    verify_source()
    cluster_guard()
    aws("s3api", "head-bucket", "--bucket", required("FRONTEND_BUCKET"), "--expected-bucket-owner", required("EXPECTED_AWS_ACCOUNT_ID"))
    distribution = aws("cloudfront", "get-distribution", "--id", required("FRONTEND_DISTRIBUTION_ID"))["Distribution"]
    frontend_host = urlparse(required("FRONTEND_URL")).hostname
    aliases = distribution["DistributionConfig"].get("Aliases", {}).get("Items", [])
    if frontend_host not in {distribution["DomainName"], *aliases}:
        raise ValueError("Frontend origin is not owned by the selected CloudFront distribution")
    bucket = required("FRONTEND_BUCKET")
    origins = distribution["DistributionConfig"].get("Origins", {}).get("Items", [])
    if not any(origin.get("DomainName") in {bucket + ".s3.amazonaws.com", bucket + ".s3." + required("AWS_REGION") + ".amazonaws.com"} for origin in origins):
        raise ValueError("Frontend CloudFront distribution does not serve the selected S3 bucket")
    state = json.loads((RESULTS / "rollout.json").read_text())
    business = json.loads((RESULTS / "business-acceptance.json").read_text())
    if any(row.get("sha") != required("DEPLOY_SHA") or row.get("status") != "completed" for row in (state,)) or business.get("status") != "PASS":
        raise ValueError("Exact rollout and real AWS upload/private-authorization acceptance required before frontend publication")
    if business.get("provenance", {}).get("revision") != required("DEPLOY_SHA"):
        raise ValueError("Business acceptance revision differs from released revision")
    shared.preflight = preflight
    shared.frontend()


def business():
    """Run compute-independent real AWS fixtures before frontend publication."""
    preflight()
    verify_source()
    cluster_guard()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cloud_acceptance import Acceptance
    from benchmarks.cloud_common import cloud_guard, revision, write_report
    path = str(RESULTS / "business-acceptance.json")
    report = {"kind": "real-aws-eks-business-release-smoke", "status": "FAIL", "cases": [],
              "full_eks_matrix": "incomplete; this suite does not prove fault recovery, IAM negative cases or production cutover"}
    suite = None
    try:
        if required("EKS_ENVIRONMENT") == "dev":
            session, report["provenance"] = cloud_guard()
        else:
            if os.getenv("ALLOW_PRODUCTION_SMOKE") != "1":
                raise ValueError("Approved production fixture smoke additionally requires ALLOW_PRODUCTION_SMOKE=1")
            import boto3
            session = boto3.Session(region_name=required("AWS_REGION"))
            account, bucket = required("EXPECTED_AWS_ACCOUNT_ID"), required("S3_BUCKET")
            if session.client("sts").get_caller_identity()["Account"] != account:
                raise ValueError("Production fixture AWS account differs")
            s3 = session.client("s3")
            s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
            tags = {item["Key"]: item["Value"] for item in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
            if tags.get("Project") != "photoplatform" or tags.get("Environment") != "prod":
                raise ValueError("Production smoke bucket tags differ from approved environment")
            report["provenance"] = {"revision": revision(), "awsAccountId": account, "bucket": bucket, "bucketTags": tags}
        state = json.loads((RESULTS / "rollout.json").read_text())
        if state.get("sha") != required("DEPLOY_SHA") or state.get("status") != "completed":
            raise ValueError("Released current revision must be proven before business smoke")
        if required("API_URL").rstrip("/") != required("VITE_API_BASE_URL").rstrip("/"):
            raise ValueError("Business suite must target this release's public API origin")
        suite = Acceptance(session, report, path, int(os.getenv("MAX_EXPIRY_WAIT", "1200")))
        for name, case in (("private_bucket_configuration", suite.bucket_controls),
                           ("browser_s3_cors_preflight", suite.browser_preflight),
                           ("checksum_rejected", suite.checksum), ("signed_mime_rejected", suite.mime),
                           ("duplicate_completion", suite.idempotency),
                           ("private_authorization_and_delivery", suite.authorization_and_raw_storage)):
            suite.case(name, case)
        report["status"] = "PASS" if len(report["cases"]) == 6 and all(case["status"] == "PASS" for case in report["cases"]) else "FAIL"
    except Exception as exc:
        report["fatal_error_type"] = type(exc).__name__
    finally:
        if suite:
            report["cleanup"] = suite.cleanup()
            if any(row["cleanup"] == "failed" for row in report["cleanup"]["uploads"]):
                report["status"] = "FAIL"
        write_report(path, report)
    if report["status"] != "PASS":
        raise RuntimeError("Real AWS business release smoke failed; frontend publication stopped")


if __name__ == "__main__":
    commands = {"preflight": preflight, "build": build, "migrate": migrate, "rollout": rollout, "business": business, "frontend": frontend}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit("usage: eks_deploy.py preflight|build|migrate|rollout|business|frontend")
    commands[sys.argv[1]]()
