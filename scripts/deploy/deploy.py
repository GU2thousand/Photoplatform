#!/usr/bin/env python3
"""Deploy a verified revision to already-provisioned AWS resources using OIDC credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
from urllib.request import urlopen

RESULTS = Path("deployment-results")
TASK_FIELDS = frozenset({
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "pidMode", "ipcMode", "proxyConfiguration", "inferenceAccelerators", "ephemeralStorage",
    "runtimePlatform", "enableFaultInjection",
})
COMPONENTS = {
    "api": ("backend", "backend/Dockerfile"),
    "worker": ("worker", "worker/Dockerfile"),
    "encoder": ("worker", "worker/Dockerfile.ml"),
    "collector": ("ops/cloudwatch", "ops/cloudwatch/Dockerfile"),
}


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


def enabled_components() -> list[str]:
    names = ["api", "worker"]
    if os.environ.get("ENABLE_ENCODER", "").lower() == "true":
        names.append("encoder")
    if os.environ.get("ENABLE_COLLECTOR", "").lower() == "true":
        names.append("collector")
    return names


def deployed_components() -> list[tuple[str, str]]:
    components = [(name, name) for name in enabled_components()]
    if "encoder" in enabled_components():
        components.append(("embedding-worker", "encoder"))
    return components


def service_env(component: str) -> str:
    return f"ECS_{component.upper().replace('-', '_')}_SERVICE"


def baseline_parameters() -> dict[str, str]:
    parameters = json.loads(required("TASK_DEFINITION_PARAMETERS"))
    if not isinstance(parameters, dict):
        raise ValueError("TASK_DEFINITION_PARAMETERS must be the Terraform output JSON map")
    selected = {}
    for component, _ in deployed_components():
        value = parameters.get(component)
        if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
            raise ValueError(f"Missing or invalid SSM task definition parameter for {component}")
        selected[component] = value
    if len(set(selected.values())) != len(selected):
        raise ValueError("Each service requires a distinct task definition baseline parameter")
    return selected


def validate_https(value: str, name: str) -> None:
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError(f"{name} must be a public HTTPS origin without credentials, path, query or fragment")


def preflight() -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", required("DEPLOY_SHA")):
        raise ValueError("DEPLOY_SHA must be the full verified Git commit SHA")
    account = required("EXPECTED_AWS_ACCOUNT_ID")
    if not re.fullmatch(r"\d{12}", account):
        raise ValueError("EXPECTED_AWS_ACCOUNT_ID must be a 12-digit AWS account id")
    region = required("AWS_REGION")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", region):
        raise ValueError("Invalid AWS_REGION")
    role = required("AWS_ROLE_ARN")
    if not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", role):
        raise ValueError("AWS_ROLE_ARN must be an IAM role in the expected AWS account")
    for name in enabled_components():
        repo = required(f"ECR_{name.upper()}_REPOSITORY")
        if not re.fullmatch(rf"{account}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/[a-z0-9][a-z0-9._/-]*", repo):
            raise ValueError(f"ECR_{name.upper()}_REPOSITORY must be a repository URL in the expected account and region")
    if os.environ.get("DEPLOY_SERVICES", "true").lower() == "true":
        for name in ["ECS_CLUSTER", "FRONTEND_BUCKET", "FRONTEND_DISTRIBUTION_ID"]:
            required(name)
        for component, _ in deployed_components():
            required(service_env(component))
        baseline_parameters()
        for name in ["VITE_API_BASE_URL", "FRONTEND_URL"]:
            validate_https(required(name), name)
    print("Deployment configuration valid")


def aws(*args: str) -> dict:
    command = ["aws", *args, "--output", "json", "--no-cli-pager"]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    if args[0] == "s3":
        print(result.stdout, end="")
        return {}
    return json.loads(result.stdout) if result.stdout.strip() else {}


def record(name: str, value: dict) -> None:
    RESULTS.mkdir(exist_ok=True)
    target = RESULTS / f"{name}.json"
    target.write_text(json.dumps(value, indent=2) + "\n")


def verify_account() -> None:
    if aws("sts", "get-caller-identity")["Account"] != required("EXPECTED_AWS_ACCOUNT_ID"):
        raise RuntimeError("AWS credential account differs from the configured deployment account")


def existing_digest(repo_name: str, sha: str) -> str | None:
    try:
        response = aws("ecr", "describe-images", "--repository-name", repo_name, "--image-ids", f"imageTag={sha}")
    except subprocess.CalledProcessError as error:
        # Permission, connectivity and repository errors must not be misclassified as absent images.
        if "ImageNotFoundException" in error.stderr:
            return None
        raise
    return response["imageDetails"][0]["imageDigest"]


def build() -> None:
    preflight()
    verify_account()
    sha = required("DEPLOY_SHA")
    manifest = {"sha": sha, "images": {}}
    registry = required("ECR_API_REPOSITORY").split("/", 1)[0]
    password = subprocess.run(["aws", "ecr", "get-login-password"], check=True, capture_output=True).stdout
    subprocess.run(["docker", "login", "--username", "AWS", "--password-stdin", registry], input=password, check=True)
    for name in enabled_components():
        context, dockerfile = COMPONENTS[name]
        repo = required(f"ECR_{name.upper()}_REPOSITORY")
        digest = existing_digest(repo.split("/", 1)[1], sha)
        if digest is None:
            subprocess.run([
                "docker", "buildx", "build", "--platform", "linux/amd64", "--push", "--file", dockerfile,
                "--label", f"org.opencontainers.image.revision={sha}", "--tag", f"{repo}:{sha}", context,
            ], check=True)
            digest = existing_digest(repo.split("/", 1)[1], sha)
        if not digest or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise RuntimeError(f"No valid immutable image digest returned for {name}")
        manifest["images"][name] = f"{repo}@{digest}"
        record("images", manifest)
    print(json.dumps(manifest, indent=2))


def task_revision(definition: dict, component: str, image: str) -> dict:
    task = {key: value for key, value in definition.items() if key in TASK_FIELDS}
    # Deep copy keeps the original task and any observability sidecars untouched.
    task = json.loads(json.dumps(task))
    containers = [container for container in task.get("containerDefinitions", []) if container["name"] == component]
    if len(containers) != 1:
        raise ValueError(f"Expected exactly one {component} container in the ECS task")
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Deployment requires an immutable image digest")
    containers[0]["image"] = image
    return task


def load_baseline_arns() -> dict[str, str]:
    parameters = baseline_parameters()
    result = aws("ssm", "get-parameters", "--names", *parameters.values())
    if result.get("InvalidParameters"):
        raise ValueError("Terraform baseline parameters are missing; apply reviewed infrastructure first")
    values = {item["Name"]: item for item in result.get("Parameters", [])}
    expected = rf"arn:aws:ecs:{re.escape(required('AWS_REGION'))}:{required('EXPECTED_AWS_ACCOUNT_ID')}:task-definition/[A-Za-z0-9_-]+:[0-9]+"
    arns = {}
    for component, parameter in parameters.items():
        value = values.get(parameter, {})
        if value.get("Type") != "String" or not re.fullmatch(expected, value.get("Value", "")):
            raise ValueError(f"Invalid Terraform task definition baseline for {component}")
        arns[component] = value["Value"]
    return arns


def task_baseline(current_arn: str, baseline_arn: str) -> dict:
    active = aws("ecs", "describe-task-definition", "--task-definition", current_arn)["taskDefinition"]
    baseline = aws("ecs", "describe-task-definition", "--task-definition", baseline_arn, "--include", "TAGS")
    definition = baseline["taskDefinition"]
    if (definition.get("family") != active["family"] or definition.get("status") != "ACTIVE"
            or definition.get("taskDefinitionArn") != baseline_arn):
        raise ValueError("Terraform baseline must be an ACTIVE revision of the service's current task family")
    return baseline


def service_state(cluster: str, service: str) -> dict:
    response = aws("ecs", "describe-services", "--cluster", cluster, "--services", service)
    if response.get("failures") or len(response.get("services", [])) != 1:
        raise RuntimeError(f"Existing ECS service required: {service}; provision services after image bootstrap")
    return response["services"][0]


def verify_rollout(state: dict, expected: str) -> None:
    primary = [deployment for deployment in state.get("deployments", []) if deployment.get("status") == "PRIMARY"]
    if (state.get("taskDefinition") != expected or len(primary) != 1
            or primary[0].get("taskDefinition") != expected or primary[0].get("rolloutState") != "COMPLETED"
            or state.get("pendingCount", 0) != 0 or state.get("runningCount") != state.get("desiredCount")
            or state.get("desiredCount", 0) < 1):
        raise RuntimeError("ECS did not complete the requested revision; a stable rollback is not a successful deployment")


def rollout() -> None:
    preflight()
    verify_account()
    manifest = json.loads((RESULTS / "images.json").read_text())
    if manifest["sha"] != required("DEPLOY_SHA"):
        raise ValueError("Image manifest revision differs from the verified deployment revision")
    cluster = required("ECS_CLUSTER")
    baselines = load_baseline_arns()
    report = {"sha": manifest["sha"], "cluster": cluster, "services": {}, "status": "in_progress"}
    record("rollout", report)
    try:
        prepared = {}
        for name, image_component in deployed_components():
            service = required(service_env(name))
            state = service_state(cluster, service)
            if state.get("deploymentController", {}).get("type", "ECS") != "ECS":
                raise ValueError(f"{service} must use ECS rolling deployments")
            previous = state["taskDefinition"]
            definition = task_baseline(previous, baselines[name])
            revision = task_revision(definition["taskDefinition"], name, manifest["images"][image_component])
            if definition.get("tags"):
                revision["tags"] = [tag for tag in definition["tags"] if not tag["key"].startswith("aws:")]
            prepared[name] = revision
            report["services"][name] = {"service": service, "previous_revision": previous,
                "baseline_revision": definition["taskDefinition"]["taskDefinitionArn"], "status": "validated"}
        record("rollout", report)
        # Validate every service first, then register all revisions before changing live services.
        for name, revision in prepared.items():
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
                json.dump(revision, handle)
                handle.flush()
                expected = aws("ecs", "register-task-definition", "--cli-input-json", f"file://{handle.name}")["taskDefinition"]["taskDefinitionArn"]
            report["services"][name]["expected_revision"] = expected
            record("rollout", report)
        for evidence in report["services"].values():
            aws("ecs", "update-service", "--cluster", cluster, "--service", evidence["service"], "--task-definition", evidence["expected_revision"],
                "--deployment-configuration", "minimumHealthyPercent=100,maximumPercent=200,deploymentCircuitBreaker={enable=true,rollback=true}")
            evidence["status"] = "deploying"
            record("rollout", report)
        # Check each exact revision after the waiter: ECS can become stable by rolling back.
        for name, evidence in report["services"].items():
            aws("ecs", "wait", "services-stable", "--cluster", cluster, "--services", evidence["service"])
            verify_rollout(service_state(cluster, evidence["service"]), evidence["expected_revision"])
            evidence["status"] = "completed"
            record("rollout", report)
        report["status"] = "completed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        record("rollout", report)


def frontend() -> None:
    preflight()
    verify_account()
    bucket = required("FRONTEND_BUCKET")
    distribution = required("FRONTEND_DISTRIBUTION_ID")
    destination = f"s3://{bucket}/"
    # Keep old hashed assets so an open browser tab survives a frontend rollout.
    aws("s3", "sync", "frontend/dist/", destination, "--exclude", "*", "--include", "assets/*",
        "--cache-control", "public,max-age=31536000,immutable")
    aws("s3", "sync", "frontend/dist/", destination, "--exclude", "assets/*", "--exclude", "index.html",
        "--cache-control", "public,max-age=300")
    aws("s3", "cp", "frontend/dist/index.html", destination + "index.html", "--cache-control", "no-cache,must-revalidate")
    invalidation = aws("cloudfront", "create-invalidation", "--distribution-id", distribution, "--paths", "/*")
    invalidation_id = invalidation["Invalidation"]["Id"]
    report = {"sha": required("DEPLOY_SHA"), "distribution_id": distribution, "invalidation_id": invalidation_id, "status": "in_progress"}
    record("frontend", report)
    try:
        aws("cloudfront", "wait", "invalidation-completed", "--distribution-id", distribution, "--id", invalidation_id)
        expected = Path("frontend/dist/index.html").read_bytes()
        with urlopen(required("FRONTEND_URL"), timeout=30) as response:
            served = response.read()
            if response.status != 200 or served != expected:
                raise RuntimeError("CloudFront did not serve the exact frontend index from this release")
        report["index_sha256"] = hashlib.sha256(served).hexdigest()
        with urlopen(required("VITE_API_BASE_URL").rstrip("/") + "/readyz", timeout=30) as response:
            if response.status != 200:
                raise RuntimeError("The public HTTPS API readiness check failed")
        report["api_readiness"] = "passed"
        report["status"] = "completed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        record("frontend", report)



if __name__ == "__main__":
    commands = {"preflight": preflight, "build": build, "rollout": rollout, "frontend": frontend}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit("usage: deploy.py preflight|build|rollout|frontend")
    commands[sys.argv[1]]()
