#!/usr/bin/env python3
"""Bootstrap an already provisioned, private EKS cluster as a platform operator.

Default mode performs only AWS/Kubernetes preflight. --execute installs pinned
platform components. --render-dir renders the same manifests offline from AWS.
No Terraform, account provisioning, or application release is performed here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[1]
GROUP = "photoplatform-deployers"
NAMESPACES = {"dev": "photoplatform-dev", "prod": "photoplatform-prod"}
OBSERVABILITY_RULES = {
    "photoplatform-queue-collector": [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}],
    "photoplatform-prometheus": [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}],
    "photoplatform-kube-state-metrics": [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["list", "watch"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["list", "watch"]},
        {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"], "verbs": ["list", "watch"]}],
}


def run(command: list[str], *, env: dict[str, str] | None = None,
        stdin: str | None = None) -> str:
    result = subprocess.run(command, env=env, input=stdin, text=True,
                            capture_output=True, check=False, timeout=900)
    if result.returncode:
        raise RuntimeError(f"{command[0]} {command[1]} failed: {result.stderr.strip()}")
    return result.stdout


def aws(args: argparse.Namespace, *command: str) -> dict:
    return json.loads(run(["aws", *command, "--region", args.region,
                           "--output", "json", "--no-cli-pager"]))


def validate_args(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"\d{12}", args.account_id):
        raise ValueError("--account-id must be a 12 digit AWS account ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]+-\d+", args.region) or args.region.startswith(("cn-", "us-gov-")):
        raise ValueError("This bootstrap and vendored IAM policy support the commercial AWS partition only")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", args.cluster):
        raise ValueError("Invalid --cluster name")
    if args.cluster != "photoplatform-eks-" + args.environment:
        raise ValueError("--cluster must match the Terraform-owned photoplatform-eks-<environment> cluster")
    if not re.fullmatch(r"vpc-[0-9a-f]{8,17}", args.vpc_id):
        raise ValueError("Invalid --vpc-id")
    if args.execute and args.render_dir:
        raise ValueError("--execute and --render-dir are mutually exclusive")


def validate_cluster(args: argparse.Namespace, identity: dict, cluster: dict,
                     versions: dict) -> None:
    expected_arn = f"arn:aws:eks:{args.region}:{args.account_id}:cluster/{args.cluster}"
    if identity.get("Account") != args.account_id or cluster.get("arn") != expected_arn:
        raise ValueError("AWS caller account or EKS cluster ARN does not match the explicit target")
    if cluster.get("status") != "ACTIVE":
        raise ValueError("EKS cluster must be ACTIVE")
    if cluster.get("version") != versions["kubernetes"]:
        raise ValueError("Cluster Kubernetes version does not match the reviewed platform pin")
    network = cluster.get("resourcesVpcConfig", {})
    if network.get("vpcId") != args.vpc_id:
        raise ValueError("EKS cluster VPC differs from --vpc-id")
    if network.get("endpointPrivateAccess") is not True or network.get("endpointPublicAccess") is not False:
        raise ValueError("EKS API must have private access enabled and public access disabled")
    if not cluster.get("endpoint", "").startswith("https://"):
        raise ValueError("EKS endpoint must use HTTPS")


def preflight(args: argparse.Namespace, versions: dict, kubeconfig: Path) -> dict:
    identity = aws(args, "sts", "get-caller-identity")
    cluster = aws(args, "eks", "describe-cluster", "--name", args.cluster)["cluster"]
    validate_cluster(args, identity, cluster, versions)
    addon = aws(args, "eks", "describe-addon", "--cluster-name", args.cluster,
                "--addon-name", "eks-pod-identity-agent")["addon"]
    if addon.get("status") != "ACTIVE":
        raise ValueError("The Terraform-owned EKS Pod Identity Agent add-on must be ACTIVE")
    associations = aws(args, "eks", "list-pod-identity-associations",
                       "--cluster-name", args.cluster, "--namespace", "kube-system",
                       "--service-account", "aws-load-balancer-controller")["associations"]
    if len(associations) != 1:
        raise ValueError("Exactly one Terraform-owned LBC Pod Identity association is required")
    association = aws(args, "eks", "describe-pod-identity-association",
                      "--cluster-name", args.cluster,
                      "--association-id", associations[0]["associationId"])["association"]
    expected_role = f"arn:aws:iam::{args.account_id}:role/{args.cluster}-load-balancer-controller"
    if association.get("roleArn") != expected_role:
        raise ValueError("LBC Pod Identity role differs from the Terraform-owned target role")
    if association.get("namespace") != "kube-system" or association.get("serviceAccount") != "aws-load-balancer-controller":
        raise ValueError("LBC Pod Identity association targets the wrong ServiceAccount")
    # Never select or mutate the caller's current kubectl context.
    run(["aws", "eks", "update-kubeconfig", "--name", args.cluster,
         "--region", args.region, "--kubeconfig", str(kubeconfig),
         "--alias", cluster["arn"], "--no-cli-pager"])
    env = os.environ | {"KUBECONFIG": str(kubeconfig)}
    # This is a network check against the private API; no public fallback is used.
    server = json.loads(run(["kubectl", "get", "--raw=/version", "--request-timeout=20s"], env=env))
    if server.get("gitVersion", "").split("-", 1)[0].split(".")[:2] != ["v1", versions["kubernetes"].split(".")[1]]:
        raise ValueError("Connected Kubernetes API version does not match the expected EKS version")
    if args.execute and run(["kubectl", "auth", "can-i", "create", "clusterroles"], env=env).strip() != "yes":
        raise ValueError("Platform bootstrap needs the separately authorized platform operator identity")
    return {"account": identity["Account"], "callerArn": identity["Arn"],
            "clusterArn": cluster["arn"], "clusterVersion": cluster["version"],
            "vpcId": args.vpc_id, "privateApi": True,
            "podIdentityAgentVersion": addon["addonVersion"],
            "loadBalancerControllerRoleArn": association["roleArn"]}


def download_chart(component: dict, directory: Path) -> Path:
    target = directory / f'{component["chart"]}-{component["chart_version"]}.tgz'
    with urllib.request.urlopen(component["archive_url"], timeout=60) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if len(archive) > 32 * 1024 * 1024:
        raise ValueError("Unexpectedly large platform chart archive")
    if hashlib.sha256(archive).hexdigest() != component["archive_sha256"]:
        raise ValueError(f'Official {component["chart"]} chart archive checksum differs from versions.json')
    target.write_bytes(archive)
    return target


def chart_settings(name: str, args: argparse.Namespace) -> list[str]:
    if name == "aws_load_balancer_controller":
        values = {"clusterName": args.cluster, "region": args.region, "vpcId": args.vpc_id,
                  "serviceAccount.create": "false", "serviceAccount.name": "aws-load-balancer-controller",
                  "image.tag": "v3.5.0", "enableServiceMutatorWebhook": "false",
                  "enableBackendSecurityGroup": "false", "enableManageBackendSecurityGroupRules": "false",
                  "watchNamespace": NAMESPACES[args.environment],
                  "controllerConfig.featureGates.NLBGatewayAPI": "false",
                  "controllerConfig.featureGates.ALBGatewayAPI": "false",
                  "controllerConfig.featureGates.GatewayListenerSet": "false",
                  "controllerConfig.featureGates.EnableServiceController": "false",
                  "resources.requests.cpu": "100m", "resources.requests.memory": "128Mi",
                  "resources.limits.cpu": "500m", "resources.limits.memory": "512Mi"}
    elif name == "aws_secrets_provider":
        # ASCP bundles the resolved CSI dependency inside the checksummed chart.
        # The application reads CSI files; no Kubernetes Secret synchronization.
        values = {"secrets-store-csi-driver.install": "true",
                  "secrets-store-csi-driver.syncSecret.enabled": "false",
                  "secrets-store-csi-driver.enableSecretRotation": "false"}
    else:
        values = {}
    result = []
    for key, value in values.items():
        result += ["--set", f"{key}={value}"]
    return result


def validate_lbc_render(rendered: str) -> None:
    """Reject a chart/value mismatch that would reintroduce controller SG ownership."""
    for flag in ["enable-backend-security-group", "enable-manage-backend-security-group-rules"]:
        matches = re.findall(r"(?m)^\s*-\s*[\"']?--" + flag + r"=([^\s\"']+)", rendered)
        if matches != ["false"]:
            raise ValueError(f"Rendered LBC must explicitly disable --{flag}; Terraform owns security groups/rules")


def namespace_manifest(args: argparse.Namespace, versions: dict) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {
        "name": NAMESPACES[args.environment], "labels": {
            "app.kubernetes.io/part-of": "photoplatform",
            "photoplatform.io/environment": args.environment,
            "pod-security.kubernetes.io/enforce": "restricted",
            "pod-security.kubernetes.io/enforce-version": "v" + versions["kubernetes"],
            "pod-security.kubernetes.io/audit": "restricted",
            "pod-security.kubernetes.io/warn": "restricted"}}}, indent=2) + "\n"


def additional_rbac(namespace: str) -> dict[str, str]:
    """Bootstrap owns these identities; the application chart owns no RBAC."""
    if namespace not in NAMESPACES.values():
        raise ValueError("RBAC can be provisioned only for the fixed application namespaces")
    name = namespace + "-namespace-reader"
    namespace_read = {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": name}, "rules": [
             {"apiGroups": [""], "resources": ["namespaces"],
              "resourceNames": [namespace, "kube-system"], "verbs": ["get"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": name},
         "subjects": [{"kind": "Group", "name": GROUP, "apiGroup": "rbac.authorization.k8s.io"}],
         "roleRef": {"kind": "ClusterRole", "name": name, "apiGroup": "rbac.authorization.k8s.io"}}]}
    observations = []
    for service_account, rules in OBSERVABILITY_RULES.items():
        role_name = service_account + "-read"
        observations += [
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
             "metadata": {"name": role_name, "namespace": namespace}, "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
             "metadata": {"name": role_name, "namespace": namespace},
             "subjects": [{"kind": "ServiceAccount", "name": service_account, "namespace": namespace}],
             "roleRef": {"kind": "Role", "name": role_name, "apiGroup": "rbac.authorization.k8s.io"}}]
    return {"namespace-read-rbac.json": json.dumps(namespace_read, indent=2) + "\n",
            "observability-rbac.json": json.dumps({"apiVersion": "v1", "kind": "List", "items": observations}, indent=2) + "\n"}


def static_manifests(args: argparse.Namespace, versions: dict) -> dict[str, str]:
    return {"kube-system-network-policy.yaml": (ROOT / "kube-system-network-policy.yaml").read_text(),
            "namespace.yaml": namespace_manifest(args, versions),
            "aws-load-balancer-controller-serviceaccount.yaml":
                (ROOT / "aws-load-balancer-controller-serviceaccount.yaml").read_text(),
            "release-rbac.yaml": (ROOT / "release-rbac.yaml").read_text().replace(
                "__NAMESPACE__", NAMESPACES[args.environment])} | additional_rbac(NAMESPACES[args.environment])


def verify_release_rbac(env: dict[str, str], namespace: str) -> None:
    impersonate = ["--as=photoplatform-rbac-check", "--as-group=" + GROUP]
    checks = [("create", "deployments.apps", namespace, "yes"),
              ("create", "jobs.batch", namespace, "yes"),
              ("create", "roles.rbac.authorization.k8s.io", namespace, "no"),
              ("create", "rolebindings.rbac.authorization.k8s.io", namespace, "no"),
              ("create", "clusterroles.rbac.authorization.k8s.io", None, "no"),
              ("get", "secrets", "kube-system", "no"),
              ("create", "pods", namespace, "no"),
              ("get", "namespaces/" + namespace, None, "yes"),
              ("get", "namespaces/kube-system", None, "yes"),
              ("get", "namespaces/default", None, "no"),
              ("list", "namespaces", None, "no"),
              ("get", "nodes", None, "no")]
    verify_permissions(env, impersonate, checks, "Deployment")


def verify_permissions(env: dict[str, str], impersonate: list[str],
                       checks: list[tuple[str, str, str | None, str]], label: str) -> None:
    for verb, resource, scope, expected in checks:
        command = ["kubectl", *impersonate, "auth", "can-i", verb, resource]
        if scope:
            command += ["--namespace", scope]
        # kubectl returns status 1 for a correct negative permission test.
        result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
        if result.stdout.strip() != expected or result.returncode not in (0, 1):
            raise ValueError(f"{label} RBAC check failed: {verb} {resource} in {scope or 'cluster'}")


def verify_observability_rbac(env: dict[str, str], namespace: str) -> None:
    for service_account, rules in OBSERVABILITY_RULES.items():
        checks = []
        for rule in rules:
            suffix = "." + rule["apiGroups"][0] if rule["apiGroups"][0] else ""
            for resource in rule["resources"]:
                checks += [(verb, resource + suffix, namespace, "yes") for verb in rule["verbs"]]
        checks += [("get", "secrets", namespace, "no"),
                   ("create", "roles.rbac.authorization.k8s.io", namespace, "no"),
                   ("patch", "deployments.apps", namespace, "no"),
                   ("list", "pods", "kube-system", "no")]
        if service_account != "photoplatform-prometheus":
            checks.append(("get", "pods", namespace, "no"))
        if service_account == "photoplatform-queue-collector":
            checks.append(("watch", "pods", namespace, "no"))
        if service_account != "photoplatform-kube-state-metrics":
            checks += [("list", "deployments.apps", namespace, "no"),
                       ("list", "horizontalpodautoscalers.autoscaling", namespace, "no")]
        verify_permissions(env, [f"--as=system:serviceaccount:{namespace}:{service_account}"], checks, service_account)


def bootstrap(args: argparse.Namespace) -> None:
    validate_args(args)
    versions = json.loads((ROOT / "versions.json").read_text())
    policy = REPO_ROOT / "infra/modules/eks/policies/aws-load-balancer-controller.json"
    if hashlib.sha256(policy.read_bytes()).hexdigest() != versions["iam_policy"]["sha256"]:
        raise ValueError("Vendored LBC IAM policy differs from the reviewed official policy")
    binaries = ["helm"] if args.render_dir else ["aws", "kubectl"] + (["helm"] if args.execute else [])
    if any(not shutil.which(binary) for binary in binaries):
        raise ValueError("Required binaries: " + ", ".join(binaries))
    evidence = {"startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                "mode": "render" if args.render_dir else "execute" if args.execute else "preflight",
                "namespace": NAMESPACES[args.environment], "platformVersions": versions}
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="photoplatform-platform-") as temp:
            tempdir = Path(temp)
            env = os.environ | {"KUBECONFIG": str(tempdir / "kubeconfig")}
            if not args.render_dir:
                evidence["preflight"] = preflight(args, versions, tempdir / "kubeconfig")
                if not args.execute:
                    evidence["status"] = "preflight-passed"
                    print("Private EKS preflight passed; no cluster resources changed. Use --execute as the platform operator to bootstrap.")
                    return
            manifests = static_manifests(args, versions)
            if args.render_dir:
                renderdir = Path(args.render_dir)
                renderdir.mkdir(mode=0o700, parents=True, exist_ok=True)
                for name, content in manifests.items():
                    (renderdir / name).write_text(content)
            else:
                for content in manifests.values():
                    run(["kubectl", "apply", "--server-side", "--field-manager=photoplatform-platform", "-f", "-"], env=env, stdin=content)
                verify_release_rbac(env, NAMESPACES[args.environment])
                verify_observability_rbac(env, NAMESPACES[args.environment])
            for name, component in versions["components"].items():
                chart = download_chart(component, tempdir)
                settings = chart_settings(name, args)
                # Helm does not update CRDs on upgrade. Apply only CRDs from the
                # verified archive (including bundled dependencies), never main.
                crds = run(["helm", "show", "crds", str(chart)])
                rendered = run(["helm", "template", component["release"], str(chart),
                                "--namespace", "kube-system", "--include-crds",
                                "--kube-version", versions["kubernetes"] + ".0", *settings])
                if name == "aws_load_balancer_controller":
                    validate_lbc_render(rendered)
                if args.render_dir:
                    (renderdir / f"{name}.yaml").write_text(rendered)
                    (renderdir / f"{name}.yaml").chmod(0o600)
                else:
                    if crds.strip():
                        run(["kubectl", "apply", "--server-side", "--field-manager=photoplatform-platform", "-f", "-"], env=env, stdin=crds)
                    run(["helm", "upgrade", "--install", component["release"], str(chart),
                         "--namespace", "kube-system", "--skip-crds", "--atomic", "--wait",
                         "--timeout", "10m", "--history-max", "5", *settings], env=env)
            if not args.render_dir:
                for workload in versions["rollout_targets"]:
                    run(["kubectl", "rollout", "status", workload, "--namespace", "kube-system", "--timeout=600s"], env=env)
                evidence["workloads"] = json.loads(run(["kubectl", "get", *versions["rollout_targets"], "--namespace", "kube-system", "-o", "json"], env=env))
            evidence["status"] = "rendered" if args.render_dir else "bootstrapped"
            print(f'Platform {evidence["status"]}; evidence: {evidence_dir / "bootstrap.json"}')
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["failure"] = str(exc)
        raise
    finally:
        evidence["finishedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (evidence_dir / "bootstrap.json").write_text(json.dumps(evidence, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, env_name in [("cluster", "EKS_CLUSTER_NAME"), ("region", "AWS_REGION"),
                           ("vpc-id", "EKS_VPC_ID"), ("account-id", "EXPECTED_AWS_ACCOUNT_ID")]:
        default = os.environ.get(env_name)
        parser.add_argument("--" + flag, default=default, required=not default,
                            help="Explicit target; defaults from " + env_name)
    parser.add_argument("--environment", choices=NAMESPACES, default=os.environ.get("EKS_ENVIRONMENT", "dev"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Install platform resources after preflight")
    mode.add_argument("--render-dir", help="Download/checksum/render locally without AWS or kubectl")
    parser.add_argument("--evidence-dir", default="work/eks-platform-evidence")
    bootstrap(parser.parse_args())


if __name__ == "__main__":
    main()
