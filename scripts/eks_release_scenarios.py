"""One guarded disposable-dev EKS release rehearsal per invocation.

Requires the ordinary eks_guard variables, ALLOW_EKS_RELEASE_SCENARIOS=1 and
EKS_RELEASE_SCENARIO_LOCK_ID identifying the operator's exclusive maintenance
window. Helm has no compare-and-swap rollback API: independent CLI writers MUST
be excluded. Evidence contains resource identities/hashes, never manifests,
Helm values, Secret data, arbitrary logs, credentials or signed URLs.

rollout: graceful rolling replacement of API and media Pods, then restoration
of the temporary template annotation. Does not test node drain or SIGKILL.
migration-failure: exact migration-only API image rejects an invalid JDBC port,
with no database destination or credentials. Not failed SQL/database outage.
rollback: prior exact successful Helm revision, followed by the original exact
revision in finally. Application versions only; never a schema downgrade.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.cloud_common import CloudAPI, fixture, upload_one, write_report
from scripts.eks_common import COMPONENTS, DIGEST, eks_guard, kubectl, load_manifest, safe_name


COMPONENT_SET = ("api", "media-worker")
MARKER = "photoplatform.io/release-scenario"
MIGRATION_ENV = {
    "MIGRATOR_DATABASE_URL": "jdbc:postgresql://127.0.0.1:65536/eks_no_target?connectTimeout=3&socketTimeout=3",
    "MIGRATOR_DATABASE_USERNAME": "eks_no_target", "MIGRATOR_DATABASE_PASSWORD": "eks_no_target",
    "SPRING_PROFILES_ACTIVE": "kubernetes-local", "APP_SEED_ENABLED": "false"}
NAMESPACED_KINDS = {"Deployment", "Service", "ConfigMap", "Ingress", "ServiceAccount",
    "SecretProviderClass", "PodDisruptionBudget", "HorizontalPodAutoscaler", "NetworkPolicy", "Role", "RoleBinding"}
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                "seccompProfile": {"type": "RuntimeDefault"}}
CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mutation_guard():
    if os.getenv("ALLOW_EKS_RELEASE_SCENARIOS") != "1":
        raise ValueError("Set ALLOW_EKS_RELEASE_SCENARIOS=1 for disposable dev release rehearsals")
    lock = os.getenv("EKS_RELEASE_SCENARIO_LOCK_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", lock):
        raise ValueError("EKS_RELEASE_SCENARIO_LOCK_ID must identify the exclusive maintenance window")
    return lock


def helm(control, *args, timeout=45):
    # Prevent Helm-only environment overrides from bypassing EKSControl's checked
    # kubeconfig endpoint/CA. Secret driver is required for pinned storage metadata.
    env = {key: value for key, value in os.environ.items() if not key.startswith("HELM_KUBE")}
    env["HELM_DRIVER"] = "secret"
    result = subprocess.run(["helm", "--kube-context", control.arn, "--namespace", control.namespace, *args],
        env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Scoped Helm operation failed")
    return result.stdout


def boundary(control):
    namespace = control.json("get", "namespace", control.namespace, "-o", "json")
    labels = namespace["metadata"].get("labels", {})
    if (namespace["metadata"]["uid"] != control.namespace_uid or namespace["metadata"].get("deletionTimestamp") or
            labels.get("app.kubernetes.io/part-of") != "photoplatform" or
            labels.get("photoplatform.io/environment") != "dev" or labels.get("photoplatform.io/disposable") != "true"):
        raise ValueError("Disposable namespace identity changed")


def deployment_identity(control, component):
    deployment = control.deployment(component)
    return {"name": deployment["metadata"]["name"], "uid": deployment["metadata"]["uid"],
        "resourceVersion": deployment["metadata"]["resourceVersion"], "generation": deployment["metadata"]["generation"],
        "specSha256": digest(deployment["spec"]), "image": control.manifest["images"][COMPONENTS[component]],
        "sha": control.sha, "replicas": deployment["spec"].get("replicas", 1)}


def fixed_replicas(control, names):
    hpas = control.json("get", "hpa", "-o", "json")["items"]
    if any(item.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment" and
           item["spec"]["scaleTargetRef"].get("name") in names for item in hpas):
        raise ValueError("Controlled release rehearsal rejects matching HPAs; disable them in the reviewed dev release first")


def ready(control, component, expected_uid, replicas, timeout, old_pods=()):
    deadline = time.monotonic() + timeout
    while True:
        deployment = control.deployment(component)
        try:
            state = control.state(component)
        except ValueError as error:
            # Helm --wait can return while old Pods are still terminating.
            # EKSControl deliberately rejects those prior SHA/images. They must
            # disappear before this scenario can pass; other identity failures
            # are never treated as a transient rollout condition.
            if str(error) != "Pod template does not match the selected SHA/digest":
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("Prior-version Pods did not disappear") from None
            time.sleep(3)
            continue
        status = deployment.get("status", {})
        if state["uid"] != expected_uid or state["replicas"] != replicas:
            raise ValueError("Deployment UID or desired replicas changed")
        if (state["observedGeneration"] >= state["generation"] and
                all(status.get(key, 0) == replicas for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")) and
                len(state["pods"]) == replicas and all(p["ready"] and not p["terminating"] for p in state["pods"]) and
                not set(old_pods) & {p["uid"] for p in state["pods"]}):
            if component == "api":
                try:
                    state["endpointSlices"] = api_endpoints(control, state)
                except ValueError as error:
                    if str(error) != "API EndpointSlice membership differs from the exact Ready Pod set":
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("API EndpointSlice membership did not settle") from None
                    time.sleep(3)
                    continue
            return state
        if time.monotonic() >= deadline:
            raise TimeoutError("Exact Deployment replacement did not settle")
        time.sleep(3)


def api_endpoints(control, state):
    services = control.json("get", "services", "-l", control.selector("api"), "-o", "json")["items"]
    labels = control.deployment("api")["spec"]["template"]["metadata"]["labels"]
    services = [service for service in services if service["spec"].get("selector") and
        all(labels.get(key) == value for key, value in service["spec"]["selector"].items())]
    if len(services) != 1:
        raise ValueError("Expected one guarded API Service")
    service = services[0]
    slices = control.json("get", "endpointslices", "-l", "kubernetes.io/service-name=" + service["metadata"]["name"], "-o", "json")["items"]
    uids = set()
    for item in slices:
        if not any(owner.get("kind") == "Service" and owner.get("uid") == service["metadata"]["uid"]
                   for owner in item["metadata"].get("ownerReferences", [])):
            raise ValueError("EndpointSlice is not owned by the guarded API Service")
        for endpoint in item.get("endpoints", []):
            if endpoint.get("conditions", {}).get("ready") is not True:
                continue
            target = endpoint.get("targetRef", {})
            if target.get("kind") != "Pod" or target.get("namespace", control.namespace) != control.namespace or endpoint.get("conditions", {}).get("terminating"):
                raise ValueError("Ready API endpoint is not a nonterminating namespaced Pod")
            uids.add(target.get("uid"))
    if uids != {pod["uid"] for pod in state["pods"]}:
        raise ValueError("API EndpointSlice membership differs from the exact Ready Pod set")
    return {"service": service["metadata"]["name"], "serviceUid": service["metadata"]["uid"], "readyPodUids": sorted(uids)}


def business_probe(timeout):
    api, upload = CloudAPI(), None
    result = {"status": "FAIL", "cleanupStatus": "NOT_NEEDED"}
    try:
        payload = fixture()
        result["fixture"] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        row, upload = upload_one(api, payload, timeout=timeout)
        result["attempt"] = row
        if row["success"]:
            result["status"] = "PASS"
    except Exception as error:
        result["errorType"] = type(error).__name__
    finally:
        if upload:
            try:
                api.cleanup(upload)
                api.wait(upload, timeout=min(timeout, 300), target="DELETED")
                result["cleanupStatus"] = "PASS"
            except Exception as error:
                result.update(cleanupStatus="FAIL", cleanupErrorType=type(error).__name__, status="FAIL")
    return result


class HelmPin:
    def __init__(self, control):
        self.control = control
        latest = self.latest()
        if latest["status"] != "deployed":
            raise ValueError("Helm release must be settled and deployed")
        self.revision = latest["revision"]
        self.storage = self.storage_identity(self.revision)

    def history(self):
        rows = json.loads(helm(self.control, "history", self.control.release, "--max", "1024", "--output", "json"))
        result = [{"revision": int(row["revision"]), "status": row["status"]} for row in rows]
        if not result or any(row["revision"] < 1 for row in result) or len({r["revision"] for r in result}) != len(result):
            raise ValueError("Invalid Helm revision history")
        return sorted(result, key=lambda row: row["revision"])

    def latest(self):
        return self.history()[-1]

    def storage_identity(self, revision):
        name = f"sh.helm.release.v1.{self.control.release}.v{revision}"
        item = self.control.json("get", "secret", name, "-o", "json")
        metadata = item["metadata"]
        labels = metadata.get("labels", {})
        if (item.get("type") != "helm.sh/release.v1" or labels.get("owner") != "helm" or
                labels.get("name") != self.control.release or labels.get("version") != str(revision) or
                metadata.get("namespace", self.control.namespace) != self.control.namespace or metadata.get("deletionTimestamp")):
            raise ValueError("Helm release storage does not match the pinned namespace/revision")
        return {"name": name, "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"], "revision": revision}

    def unchanged(self):
        boundary(self.control)
        if self.latest() != {"revision": self.revision, "status": "deployed"} or self.storage_identity(self.revision) != self.storage:
            raise ValueError("Helm release changed during rehearsal")

    def snapshot(self, revision, manifest):
        import yaml
        text = helm(self.control, "get", "manifest", self.control.release, "--revision", str(revision))
        resources = [item for item in yaml.safe_load_all(text) if item]
        values = json.loads(helm(self.control, "get", "values", self.control.release, "--revision", str(revision), "--all", "--output", "json"))
        if (values.get("environment") != "dev" or values.get("runtimeMode") != "aws" or
                values.get("release", {}).get("commitSha") != manifest["sha"] or values.get("migration", {}).get("enabled") is not False):
            raise ValueError("Only migration-disabled AWS dev application releases can be rehearsed")
        if helm(self.control, "get", "hooks", self.control.release, "--revision", str(revision)).strip():
            raise ValueError("Release rehearsal refuses Helm hooks")
        objects, components = {}, {}
        for item in resources:
            metadata = item.get("metadata", {})
            labels = metadata.get("labels", {})
            name = safe_name(metadata.get("name", ""))
            if (item.get("kind") not in NAMESPACED_KINDS or metadata.get("namespace", self.control.namespace) != self.control.namespace or
                    labels.get("app.kubernetes.io/name") != "photoplatform" or labels.get("app.kubernetes.io/instance") != self.control.release or
                    not name.startswith(self.control.release + "-") or "helm.sh/hook" in metadata.get("annotations", {})):
                raise ValueError("Helm manifest contains an unsupported or out-of-scope resource")
            if item["kind"] == "RoleBinding" and item.get("roleRef", {}).get("kind") != "Role":
                raise ValueError("Release rehearsal refuses ClusterRole bindings")
            key = item["kind"] + "/" + name
            if key in objects:
                raise ValueError("Duplicate Helm resource identity")
            objects[key] = item
            component = labels.get("app.kubernetes.io/component")
            if item["kind"] == "Deployment" and component in COMPONENTS:
                if component in components or COMPONENTS[component] not in manifest["images"]:
                    raise ValueError("Ambiguous or unpinned application Deployment")
                template = item["spec"]["template"]
                if (template["metadata"].get("annotations", {}).get("photoplatform.io/revision") != manifest["sha"] or
                        len(template["spec"].get("containers", [])) != 1 or
                        template["spec"]["containers"][0].get("image") != manifest["images"][COMPONENTS[component]]):
                    raise ValueError("Helm application template differs from its exact SHA/digest manifest")
                selected_digest = manifest["images"][COMPONENTS[component]].split("@")[-1]
                annotations = template["metadata"]["annotations"]
                if "photoplatform.io/image-digest" in annotations and annotations["photoplatform.io/image-digest"] != selected_digest:
                    raise ValueError("Helm Pod annotation differs from the pinned image digest")
                for variable in template["spec"]["containers"][0].get("env", []):
                    if variable.get("name") == "APP_RELEASE_SHA" and variable.get("value") != manifest["sha"]:
                        raise ValueError("Helm API reported revision differs from the pinned source")
                    if variable.get("name") == "OTEL_RESOURCE_ATTRIBUTES" and not variable.get("value", "").endswith(
                            f",service.version={manifest['sha']},container.image.id={selected_digest}"):
                        raise ValueError("Helm telemetry revision differs from the pinned source/image")
                components[component] = key
        if any(component not in components for component in COMPONENT_SET):
            raise ValueError("Helm manifest must contain API and media Deployments")
        if "encoder" in manifest["images"] and any(component not in components for component in ("encoder", "embedding-worker")):
            raise ValueError("ML manifest requires both pinned encoder and embedding Deployments")
        return {"revision": revision, "objects": objects, "values": values, "components": components,
                "manifestSha256": digest(objects), "valuesSha256": digest(values), "storage": self.storage_identity(revision)}


def release_summary(snapshot):
    return {key: snapshot[key] for key in ("revision", "manifestSha256", "valuesSha256", "storage")} | {
        "resources": sorted(snapshot["objects"])}


def normalized_resource(resource):
    result = copy.deepcopy(resource)
    def normalize_metadata(metadata):
        for key in ("photoplatform.io/commit", "photoplatform.io/revision"):
            metadata.get("annotations", {}).pop(key, None)
        metadata.get("labels", {}).pop("helm.sh/chart", None)
    normalize_metadata(result["metadata"])
    component = result["metadata"]["labels"].get("app.kubernetes.io/component")
    if result["kind"] == "Deployment" and component in COMPONENTS:
        template = result["spec"]["template"]
        annotations = template["metadata"].get("annotations", {})
        sha = annotations.get("photoplatform.io/revision")
        container = template["spec"]["containers"][0]
        selected_digest = container["image"].split("@")[-1]
        normalize_metadata(template["metadata"])
        if "photoplatform.io/image-digest" in annotations:
            annotations["photoplatform.io/image-digest"] = "VERIFIED_APPLICATION_DIGEST"
        for variable in container.get("env", []):
            if variable.get("name") == "APP_RELEASE_SHA" and variable.get("value") == sha:
                variable["value"] = "VERIFIED_SOURCE_SHA"
            if variable.get("name") == "OTEL_RESOURCE_ATTRIBUTES":
                suffix = f",service.version={sha},container.image.id={selected_digest}"
                if variable.get("value", "").endswith(suffix):
                    variable["value"] = variable["value"][:-len(suffix)] + ",service.version=VERIFIED_SOURCE_SHA,container.image.id=VERIFIED_APPLICATION_DIGEST"
        container["image"] = "VERIFIED_APPLICATION_IMAGE"
    return result


def compatible_snapshots(current, previous):
    if set(current["objects"]) != set(previous["objects"]):
        raise ValueError("Rollback must preserve the exact namespaced resource identity set")
    for key in current["objects"]:
        if normalized_resource(current["objects"][key]) != normalized_resource(previous["objects"][key]):
            raise ValueError("Rollback rehearsal permits application image/SHA changes only")
    for key in ("aws", "secrets", "config", "environment", "runtimeMode"):
        if current["values"].get(key) != previous["values"].get(key):
            raise ValueError("Rollback must preserve dependency and Secret references")


class RollingReplacement:
    def __init__(self, control, pin):
        self.control, self.pin = control, pin
        self.token = uuid.uuid4().hex
        self.original = {component: control.deployment(component) for component in COMPONENT_SET}
        self.before = {component: control.state(component) for component in COMPONENT_SET}
        fixed_replicas(control, {deployment["metadata"]["name"] for deployment in self.original.values()})
        self.changed = []
        if any(MARKER in dep["spec"]["template"]["metadata"].get("annotations", {}) for dep in self.original.values()):
            raise ValueError("A prior rollout scenario annotation requires operator recovery first")

    def checked(self, component):
        self.pin.unchanged()
        deployment = self.control.deployment(component)
        expected = self.original[component]
        fixed_replicas(self.control, {dep["metadata"]["name"] for dep in self.original.values()})
        spec = copy.deepcopy(deployment["spec"])
        spec["template"]["metadata"]["annotations"].pop(MARKER, None)
        if deployment["metadata"]["uid"] != expected["metadata"]["uid"] or spec != expected["spec"]:
            raise ValueError("Deployment identity/spec changed outside the scenario")
        marker = deployment["spec"]["template"]["metadata"]["annotations"].get(MARKER)
        if marker not in (None, self.token):
            raise ValueError("Deployment scenario annotation changed externally")
        return deployment

    def patch(self, component, restore=False):
        mutation_guard()
        deployment = self.checked(component)
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        if restore and MARKER not in annotations:
            return
        path = "/spec/template/metadata/annotations/" + MARKER.replace("/", "~1")
        operations = [{"op": "test", "path": "/metadata/uid", "value": deployment["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
            {"op": "test", "path": "/spec/template/metadata/annotations/photoplatform.io~1revision", "value": self.control.sha}]
        operations.append({"op": "remove", "path": path} if restore else {"op": "add", "path": path, "value": self.token})
        kubectl(self.control.arn, self.control.namespace, "patch", "deployment", deployment["metadata"]["name"],
            "--type=json", "--patch", json.dumps(operations))

    def restore(self, timeout):
        results = []
        for component in reversed(self.changed):
            row = {"component": component, "status": "FAIL"}
            try:
                self.patch(component, restore=True)
                self.checked(component)
                row["deployment"] = ready(self.control, component, self.before[component]["uid"], self.before[component]["replicas"], timeout)
                row["status"] = "PASS"
            except Exception as error:
                row["errorType"] = type(error).__name__
            results.append(row)
        return results


def rollout_scenario(control, report, output, timeout):
    pin = HelmPin(control)
    replacement = RollingReplacement(control, pin)
    report["restorePlan"] = {"helm": pin.storage, "deployments": {component: deployment_identity(control, component) |
        {"replicas": replacement.before[component]["replicas"], "removeAnnotation": MARKER} for component in COMPONENT_SET}}
    report["steps"] = []
    write_report(output, report)
    try:
        for component in COMPONENT_SET:
            replacement.changed.append(component)  # Response loss can still mean mutation happened.
            replacement.patch(component)
            old = replacement.before[component]
            state = ready(control, component, old["uid"], old["replicas"], timeout, [p["uid"] for p in old["pods"]])
            replacement.checked(component)
            probe = business_probe(timeout)
            report["steps"].append({"component": component, "deployment": state, "business": probe})
            write_report(output, report)
            if probe["status"] != "PASS":
                raise RuntimeError("Business probe did not recover after rolling replacement")
        report["scenarioStatus"] = "PASS"
    finally:
        report["restoration"] = replacement.restore(timeout)
        report["restoreStatus"] = "PASS" if all(row["status"] == "PASS" for row in report["restoration"]) else "FAIL"
        write_report(output, report)


def migration_clone(control, source, name, timeout):
    metadata = source.get("metadata", {})
    pod = source.get("spec", {}).get("template", {}).get("spec", {})
    template = source.get("spec", {}).get("template", {})
    labels = metadata.get("labels", {})
    containers = pod.get("containers", [])
    if (source.get("kind") != "Job" or metadata.get("namespace") != control.namespace or metadata.get("deletionTimestamp") or
            labels.get("app.kubernetes.io/name") != "photoplatform" or
            labels.get("app.kubernetes.io/instance") != control.release or labels.get("app.kubernetes.io/component") != "migrator" or
            metadata.get("annotations", {}).get("photoplatform.io/revision") != control.sha or
            not 1 <= source.get("spec", {}).get("activeDeadlineSeconds", 0) <= 900 or source.get("spec", {}).get("backoffLimit", 99) not in (0, 1) or
            template.get("metadata", {}).get("annotations", {}).get("photoplatform.io/revision") != control.sha or
            len(containers) != 1 or containers[0].get("image") != control.manifest["images"]["api"] or
            containers[0].get("command") != ["/app/migrate.sh"] or containers[0].get("args") or
            pod.get("restartPolicy") != "Never" or pod.get("serviceAccountName") != control.release + "-migrator" or
            pod.get("initContainers") or pod.get("ephemeralContainers") or pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC") or
            not any(condition.get("type") == "Complete" and condition.get("status") == "True" for condition in source.get("status", {}).get("conditions", []))):
        raise ValueError("Source must be an exact-SHA completed migration-only Job from the guarded release")
    container = {key: copy.deepcopy(containers[0][key]) for key in ("name", "image", "command", "resources", "securityContext") if key in containers[0]}
    container.update(imagePullPolicy="IfNotPresent", terminationMessagePath="/dev/termination-log", terminationMessagePolicy="File",
                     env=[{"name": key, "value": value} for key, value in MIGRATION_ENV.items()],
                     volumeMounts=[{"name": "tmp", "mountPath": "/tmp"}])
    if pod.get("securityContext") != POD_SECURITY or container.get("securityContext") != CONTAINER_SECURITY:
        raise ValueError("Migration source lacks the reviewed restrictive security contexts")
    spec = {"containers": [container], "restartPolicy": "Never", "automountServiceAccountToken": False,
            "serviceAccountName": name, "terminationGracePeriodSeconds": 10,
            "securityContext": copy.deepcopy(POD_SECURITY), "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]}
    if pod.get("nodeSelector"):
        spec["nodeSelector"] = copy.deepcopy(pod["nodeSelector"])
    clean_labels = {"app.kubernetes.io/name": "photoplatform", "app.kubernetes.io/instance": control.release,
        "app.kubernetes.io/component": "migration-failure-probe"}
    annotations = {"photoplatform.io/revision": control.sha}
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": safe_name(name), "namespace": control.namespace, "labels": clean_labels},
        "spec": {"activeDeadlineSeconds": min(timeout, 120), "backoffLimit": 0,
            "template": {"metadata": {"labels": clean_labels, "annotations": annotations}, "spec": spec}}}


def failed_migration_evidence(control, name, uid, expected, timeout):
    deadline = time.monotonic() + min(timeout, 120) + 30
    while True:
        job = control.json("get", "job", name, "-o", "json")
        if job["metadata"]["uid"] != uid or job["spec"]["template"]["spec"]["containers"] != expected["spec"]["template"]["spec"]["containers"]:
            raise ValueError("Migration failure Job identity/template changed")
        conditions = job.get("status", {}).get("conditions", [])
        if any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions):
            raise ValueError("Intentionally invalid migration unexpectedly completed")
        if any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Migration probe did not reach a bounded failure")
        time.sleep(2)
    pods = control.json("get", "pods", "-l", "job-name=" + name, "-o", "json")["items"]
    if len(pods) != 1:
        raise ValueError("Expected exactly one non-retried migration probe Pod")
    pod = pods[0]
    if not any(owner.get("kind") == "Job" and owner.get("uid") == uid and owner.get("controller")
               for owner in pod["metadata"].get("ownerReferences", [])):
        raise ValueError("Failure Pod is not owned by the exact probe Job")
    spec = pod["spec"]
    wanted = expected["spec"]["template"]["spec"]
    if (spec.get("containers") != wanted["containers"] or spec.get("initContainers") or spec.get("ephemeralContainers") or
            spec.get("hostNetwork") or spec.get("hostPID") or spec.get("hostIPC") or spec.get("automountServiceAccountToken") is not False or
            spec.get("volumes") != wanted["volumes"] or spec.get("serviceAccountName") != wanted["serviceAccountName"] or
            spec.get("securityContext") != wanted["securityContext"] or spec.get("imagePullSecrets")):
        raise ValueError("Admission changed the sanitized no-secret migration Pod")
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if len(statuses) != 1:
        raise ValueError("Migration container status is missing or ambiguous")
    status = statuses[0]
    terminated = status.get("state", {}).get("terminated", {})
    allowed = {control.manifest["images"]["api"].split("@")[-1], *control.manifest["runnableDigests"]["api"]}
    ids = DIGEST.findall(status.get("imageID", ""))
    if (len(ids) != 1 or ids[0] not in allowed or type(terminated.get("exitCode")) is not int or
            terminated["exitCode"] == 0 or terminated.get("reason") != "Error"):
        raise ValueError("Failure must be a nonzero executed verified migration image, not infrastructure/deadline failure")
    logs = kubectl(control.arn, control.namespace, "logs", pod["metadata"]["name"], "--container", wanted["containers"][0]["name"], "--tail=200")
    markers = {"flywayException": "org.flywaydb.core" in logs and "FlywaySqlException" in logs,
               "invalidJdbcUrl": "Unable to parse URL" in logs and "127.0.0.1:65536" in logs,
               "migrationEntrypoint": "MigrationApplication.migrate" in logs}
    if not all(markers.values()):
        raise ValueError("Nonzero exit lacks real Flyway/JDBC migration-only failure evidence")
    return {"job": name, "jobUid": uid, "pod": pod["metadata"]["name"], "podUid": pod["metadata"]["uid"],
        "image": wanted["containers"][0]["image"], "imageID": status["imageID"], "exitCode": terminated["exitCode"],
        "reason": terminated["reason"], "markers": markers, "logSha256": hashlib.sha256(logs.encode()).hexdigest(),
        "connectionScope": "Invalid TCP port 65536 rejected before a database connection; no credentials/Secret references"}


def delete_probe_resource(control, name, uid, kind):
    mutation_guard()
    boundary(control)
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}, "propagationPolicy": "Foreground"}
    if kind == "job":
        path = f"/apis/batch/v1/namespaces/{control.namespace}/jobs/{safe_name(name)}"
    elif kind == "serviceaccount":
        path = f"/api/v1/namespaces/{control.namespace}/serviceaccounts/{safe_name(name)}"
    else:
        raise ValueError("Unsupported probe cleanup resource")
    kubectl(control.arn, control.namespace, "delete", "--raw", path, "-f", "-", body=options)


def migration_scenario(control, report, output, timeout, source_name, source_uid):
    pin = HelmPin(control)
    source = control.json("get", "job", safe_name(source_name), "-o", "json")
    if source["metadata"]["uid"] != source_uid:
        raise ValueError("Migration source UID differs from the explicitly selected Job")
    # Job names also become label values; keep the unique probe below 63 chars
    # even when the selected Helm release uses its maximum name length.
    name = control.release[:30].rstrip("-.") + "-migration-failure-" + uuid.uuid4().hex[:12]
    job = migration_clone(control, source, name, timeout)
    service_account = {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
        "metadata": {"name": name, "namespace": control.namespace, "labels": copy.deepcopy(job["metadata"]["labels"])}}
    before = {component: deployment_identity(control, component) for component in COMPONENT_SET}
    report["restorePlan"] = {"cleanupJob": name, "clusterArn": control.arn, "namespace": control.namespace,
        "cleanupServiceAccount": name, "sourceJob": source_name, "sourceJobUid": source_uid, "sourceTemplateSha256": digest(source["spec"]["template"]),
        "helm": pin.storage, "deployments": before, "applicationReleaseInvoked": False}
    report["failureTemplateSha256"] = digest(job)
    write_report(output, report)
    uid, service_account_uid, attempted, account_attempted = None, None, False, False
    try:
        mutation_guard()
        pin.unchanged()
        fresh = control.json("get", "job", source_name, "-o", "json")
        if fresh["metadata"]["uid"] != source_uid or fresh["metadata"]["resourceVersion"] != source["metadata"]["resourceVersion"]:
            raise ValueError("Migration source changed before cloning")
        account_attempted = True
        account = json.loads(kubectl(control.arn, control.namespace, "create", "-f", "-", "-o", "json", body=service_account))
        service_account_uid = account["metadata"]["uid"]
        if account.get("automountServiceAccountToken") is not False or account["metadata"].get("annotations") or account.get("imagePullSecrets"):
            raise ValueError("Admission changed the fresh no-token probe ServiceAccount")
        report["restorePlan"]["cleanupServiceAccountUid"] = service_account_uid
        write_report(output, report)
        pin.unchanged()
        attempted = True
        created = json.loads(kubectl(control.arn, control.namespace, "create", "-f", "-", "-o", "json", body=job))
        uid = created["metadata"]["uid"]
        report["restorePlan"]["cleanupJobUid"] = uid
        write_report(output, report)
        report["failure"] = failed_migration_evidence(control, name, uid, job, timeout)
        pin.unchanged()
        after = {component: deployment_identity(control, component) for component in COMPONENT_SET}
        report["deploymentsAfter"] = after
        # ResourceVersion can change from status updates. Spec, UID, generation,
        # revision and immutable image must stay exactly unchanged.
        if any({k: v for k, v in before[c].items() if k != "resourceVersion"} !=
               {k: v for k, v in after[c].items() if k != "resourceVersion"} for c in COMPONENT_SET):
            raise ValueError("Application Deployment changed during failed migration proof")
        report["business"] = business_probe(timeout)
        if report["business"]["status"] != "PASS":
            raise RuntimeError("Unchanged current application did not pass business probe")
        report["scenarioStatus"] = "PASS"
    finally:
        report["restoreStatus"] = "PASS"
        if attempted:
            try:
                if uid is None:
                    # A create response timeout can still have created this unique
                    # name. Discover it, validate our exact template, then UID-delete.
                    candidate = control.json("get", "job", name, "-o", "json")
                    if candidate["spec"]["template"]["spec"]["containers"] != job["spec"]["template"]["spec"]["containers"]:
                        raise ValueError("Response-lost Job differs from the planned probe")
                    uid = candidate["metadata"]["uid"]
                delete_probe_resource(control, name, uid, "job")
                report["cleanup"] = {"job": {"name": name, "uid": uid, "status": "DELETE_ACCEPTED", "semantics": "Foreground UID-preconditioned deletion; completion observed separately"}}
                kubectl(control.arn, control.namespace, "wait", "--for=delete", "job/" + name, f"--timeout={min(timeout, 30)}s")
                report["cleanup"]["job"]["status"] = "PASS"
            except Exception as error:
                report.update(restoreStatus="FAIL", cleanup={"job": {"name": name, "uid": uid, "status": "FAIL", "errorType": type(error).__name__}})
        if account_attempted:
            try:
                if service_account_uid is None:
                    candidate = control.json("get", "serviceaccount", name, "-o", "json")
                    if candidate.get("automountServiceAccountToken") is not False or candidate["metadata"].get("labels") != service_account["metadata"]["labels"]:
                        raise ValueError("Response-lost ServiceAccount differs from the planned probe")
                    service_account_uid = candidate["metadata"]["uid"]
                delete_probe_resource(control, name, service_account_uid, "serviceaccount")
                kubectl(control.arn, control.namespace, "wait", "--for=delete", "serviceaccount/" + name, f"--timeout={min(timeout, 30)}s")
                report.setdefault("cleanup", {})["serviceAccount"] = {"name": name, "uid": service_account_uid, "status": "PASS"}
            except Exception as error:
                report["restoreStatus"] = "FAIL"
                report.setdefault("cleanup", {})["serviceAccount"] = {"name": name, "uid": service_account_uid, "status": "FAIL", "errorType": type(error).__name__}
        write_report(output, report)


def verify_helm_result(pin, expected_revision, snapshot):
    boundary(pin.control)
    if pin.latest() != {"revision": expected_revision, "status": "deployed"}:
        raise ValueError("Helm operation did not produce its exact expected deployed revision")
    import yaml
    text = helm(pin.control, "get", "manifest", pin.control.release, "--revision", str(expected_revision))
    items = [item for item in yaml.safe_load_all(text) if item]
    objects = {item["kind"] + "/" + item["metadata"]["name"]: item for item in items}
    if len(items) != len(objects) or digest(objects) != snapshot["manifestSha256"]:
        raise ValueError("Resulting Helm revision does not match the pinned exact manifest")
    values = json.loads(helm(pin.control, "get", "values", pin.control.release, "--revision", str(expected_revision), "--all", "--output", "json"))
    if digest(values) != snapshot["valuesSha256"] or helm(pin.control, "get", "hooks", pin.control.release, "--revision", str(expected_revision)).strip():
        raise ValueError("Resulting Helm revision does not match the pinned exact values/hooks")
    return pin.storage_identity(expected_revision)


def helm_rollback(control, revision, timeout):
    mutation_guard()
    if type(revision) is not int or revision < 1 or type(timeout) is not int or not 1 <= timeout <= 900:
        raise ValueError("Rollback requires a positive exact numeric revision and bounded timeout")
    boundary(control)
    helm(control, "rollback", control.release, str(revision), "--no-hooks", "--wait", "--timeout", f"{timeout}s", timeout=timeout + 30)


def verified_runtime(aws, control, manifest, identities, timeout):
    # Helm --wait first completes mixed-version rollout; then EKSControl checks
    # every running imageID against the correct version's platform provenance.
    from scripts.eks_common import EKSControl
    selected = EKSControl(aws, manifest, manifest["sha"])
    return {component: ready(selected, component, identities[component]["uid"],
        identities[component]["replicas"], timeout) for component in identities}


def rollback_scenario(aws, control, report, output, timeout, revision, previous_path):
    previous_sha = json.loads(Path(previous_path).read_text()).get("sha", "")
    previous_manifest = load_manifest(previous_path, previous_sha)
    image_keys = {"api", "worker", "encoder"} & set(previous_manifest["images"]) & set(control.manifest["images"])
    if previous_sha == control.sha or not any(previous_manifest["images"][key] != control.manifest["images"][key] for key in image_keys):
        raise ValueError("Application rollback requires a different previous source SHA and application image")
    pin = HelmPin(control)
    successful = [row["revision"] for row in pin.history() if row["revision"] < pin.revision and row["status"] == "superseded"]
    if not successful or revision != max(successful):
        raise ValueError("Select the exact most recent retained successful previous Helm revision")
    current = pin.snapshot(pin.revision, control.manifest)
    previous = pin.snapshot(revision, previous_manifest)
    compatible_snapshots(current, previous)
    identities = {component: deployment_identity(control, component) for component in current["components"]}
    fixed_replicas(control, {identity["name"] for identity in identities.values()})
    report["restorePlan"] = {"original": release_summary(current), "previous": release_summary(previous),
        "restoreExactRevision": pin.revision, "deployments": identities, "schemaRollback": False}
    write_report(output, report)
    attempted = False
    try:
        pin.unchanged()
        if pin.storage_identity(revision) != previous["storage"]:
            raise ValueError("Previous Helm revision storage changed")
        fixed_replicas(control, {identity["name"] for identity in identities.values()})
        attempted = True
        helm_rollback(control, revision, timeout)
        report["rollbackStorage"] = verify_helm_result(pin, pin.revision + 1, previous)
        report["previousRuntime"] = verified_runtime(aws, control, previous_manifest, identities, timeout)
        report["previousBusiness"] = business_probe(timeout)
        if report["previousBusiness"]["status"] != "PASS":
            raise RuntimeError("Prior application did not pass real business probe")
        report["scenarioStatus"] = "PASS"
    finally:
        if attempted:
            try:
                latest = pin.latest()
                if latest["revision"] == pin.revision:
                    pin.unchanged()  # No new revision: failure before Helm modified storage.
                    restored_revision = pin.revision
                elif latest["revision"] == pin.revision + 1 and latest["status"] in {"deployed", "failed", "pending-rollback"}:
                    # Refuse overwriting an unrelated external release. Pin exact
                    # original UID and content again; status/RV may change by our rollback.
                    initial = pin.snapshot(pin.revision, control.manifest)
                    interim = pin.snapshot(latest["revision"], previous_manifest)
                    if (initial["storage"]["uid"] != current["storage"]["uid"] or
                            initial["manifestSha256"] != current["manifestSha256"] or initial["valuesSha256"] != current["valuesSha256"] or
                            interim["manifestSha256"] != previous["manifestSha256"] or interim["valuesSha256"] != previous["valuesSha256"] or
                            pin.latest() != latest):
                        raise ValueError("Pinned Helm content or concurrent latest revision changed before restore")
                    fixed_replicas(control, {identity["name"] for identity in identities.values()})
                    helm_rollback(control, pin.revision, timeout)
                    restored_revision = pin.revision + 2
                else:
                    raise ValueError("External Helm revision detected; refuse to overwrite it during restoration")
                report["restoredStorage"] = verify_helm_result(pin, restored_revision, current)
                report["currentRuntime"] = verified_runtime(aws, control, control.manifest, identities, timeout)
                report["restoredBusiness"] = business_probe(timeout)
                if report["restoredBusiness"]["status"] != "PASS":
                    raise RuntimeError("Restored current application did not pass business probe")
                report["restoreStatus"] = "PASS"
            except Exception as error:
                report.update(restoreStatus="FAIL", restoreErrorType=type(error).__name__)
        write_report(output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("rollout", "migration-failure", "rollback"))
    parser.add_argument("--migration-job-name")
    parser.add_argument("--migration-job-uid")
    parser.add_argument("--rollback-revision", type=int)
    parser.add_argument("--previous-image-manifest")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 900:
        parser.error("timeout must be between 1 and 900 seconds")
    if args.scenario == "migration-failure" and not (args.migration_job_name and args.migration_job_uid):
        parser.error("migration-failure requires an exact completed --migration-job-name and --migration-job-uid")
    if args.scenario == "rollback" and not (args.rollback_revision and args.rollback_revision > 0 and args.previous_image_manifest):
        parser.error("rollback requires a positive --rollback-revision and --previous-image-manifest")
    if ((args.scenario != "migration-failure" and (args.migration_job_name or args.migration_job_uid)) or
            (args.scenario != "rollback" and (args.rollback_revision is not None or args.previous_image_manifest))):
        parser.error("Supply only the selected scenario's arguments")
    report = {"kind": "real-aws-eks-release-rehearsal", "scenario": args.scenario, "status": "FAIL",
        "scenarioStatus": "FAIL", "restoreStatus": "NOT_NEEDED", "matrixStatus": "INCOMPLETE",
        "limitations": ["One namespace-scoped disposable dev scenario; never full P6 or production acceptance",
            "Operator must exclude independent CLI writers; Helm has no atomic revision compare-and-swap",
            {"rollout": "Standard rolling replacement and post-Ready business probe; no proof of in-flight drain, node drain, SIGKILL or availability SLO",
             "migration-failure": "Real Flyway/JDBC invalid-port rejection; no SQL failure, schema mutation or database outage",
             "rollback": "Pinned application revisions on the existing schema; no database schema downgrade"}[args.scenario]]}
    try:
        report["maintenanceLockId"] = mutation_guard()
        aws, control, report["provenance"] = eks_guard()
        if args.scenario == "rollout":
            rollout_scenario(control, report, args.output, args.timeout)
        elif args.scenario == "migration-failure":
            migration_scenario(control, report, args.output, args.timeout, args.migration_job_name, args.migration_job_uid)
        else:
            rollback_scenario(aws, control, report, args.output, args.timeout, args.rollback_revision, args.previous_image_manifest)
    except Exception as error:
        report["fatalErrorType"] = type(error).__name__
    finally:
        report["status"] = "PASS" if report["scenarioStatus"] == "PASS" and report["restoreStatus"] == "PASS" and "fatalErrorType" not in report else "FAIL"
        write_report(args.output, report)
    print(f"EKS {args.scenario}: {report['status']}; full P6 matrix: INCOMPLETE; report: {args.output}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
